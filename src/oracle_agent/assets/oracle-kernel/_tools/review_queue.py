#!/usr/bin/env python3
"""review_queue.py -- the Review Inbox: one ranked queue of everything pending.

Nothing in the oracle is allowed to rot silently. Every state that needs a
human-or-agent decision flows into this single queue, surfaced by
``./oracle status``, ``./oracle review``, and every leadership brief:

  * open contradictions (must_resolve first)
  * promotable truth-map rows (draft + real source + resolving evidence)
  * authority-candidate Sources awaiting admin review
  * ``needs_review`` Findings (with age)
  * ``needs_review`` Queries (session-derived retrieval strategies awaiting
    promotion to a stable reusable query, or retirement)
  * Sources tagged ``needs-ocr`` (scanned/image material the operating agent
    should transcribe with its own multimodal ability, then re-ingest)
  * open Questions past their escalation budget
  * Models past their staleness budget
  * unconsumed feedback/value/failure events backing the learning loops

Each item carries an ``action``: the exact command or playbook step that
resolves it. The queue is self-cleaning -- performing the action changes the
underlying state, which removes the item on the next build. There is no
separate "mark resolved" bookkeeping to forget.

Read-only: this module never writes. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:  # bare import (conftest puts _tools on sys.path); package fallback
    import answer_protocol as _ap
    import truth_map as _truth_map
except Exception:  # pragma: no cover - package import path
    from . import answer_protocol as _ap  # type: ignore
    from . import truth_map as _truth_map  # type: ignore

__all__ = ["build_queue", "summary", "DEFAULT_BUDGETS"]

# Priority rank by kind (lower = more urgent). must_resolve contradictions are
# promoted to rank 0 regardless of this table.
_KIND_RANK = {
    "contradiction": 1,
    "paused-loop": 1,
    "aged-signal": 2,
    "promotable-row": 2,
    "authority-candidate": 3,
    "autonomy": 3,
    "needs-ocr": 4,
    "needs-review-finding": 5,
    "stale-improvement": 5,
    "needs-review-query": 6,
    "stale-question": 6,
    "stale-model": 7,
    "unconsumed-events": 8,
}

# Staleness budgets (days), overridable via oracle.yml ``review:`` section.
DEFAULT_BUDGETS = {
    "question_stale_days": 14,
    "model_stale_days": 60,
    "finding_warn_days": 7,
}

_OPEN_STATUSES = {"open", "investigating"}


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _budgets(root: Path) -> dict:
    out = dict(DEFAULT_BUDGETS)
    try:
        import oracle_yaml  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import oracle_yaml  # type: ignore
        except Exception:
            return out
    try:
        data = oracle_yaml.safe_load((Path(root) / "oracle.yml").read_text(encoding="utf-8")) or {}
        review = data.get("review") or {}
        if isinstance(review, dict):
            for key in out:
                if key in review:
                    out[key] = int(review[key])
    except Exception:
        pass
    return out


def _age_days(fm: dict, now: datetime, *keys: str) -> Optional[float]:
    """Age in days from the newest parsable timestamp among ``keys``."""
    best = None
    for key in keys:
        dt = _ap._parse_as_of(str(fm.get(key, "")))
        if dt is not None and (best is None or dt > best):
            best = dt
    if best is None:
        return None
    return max(0.0, (now - best).total_seconds() / 86400.0)


def _tags(fm: dict) -> list[str]:
    v = fm.get("tags")
    if isinstance(v, list):
        return [str(t).strip().lower() for t in v]
    if isinstance(v, str):
        s = v.strip().strip("[]")
        return [t.strip().strip("'\"").lower() for t in s.split(",") if t.strip()]
    return []


def _iter_notes(folder: Path):
    if not folder.is_dir():
        return
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        yield p


def _source_entries(root: Path) -> list[dict]:
    """Source notes as ``{"path", "stem", "fm"}`` dicts.

    Served by the self-healing source catalog when available (no per-build
    re-parse of every note); degrades to the direct folder walk otherwise.
    """
    try:
        import source_catalog  # type: ignore
    except Exception:  # pragma: no cover - package import path
        try:
            from . import source_catalog  # type: ignore
        except Exception:
            source_catalog = None  # type: ignore
    if source_catalog is not None:
        try:
            return [
                {"path": e["path"], "stem": Path(e["name"]).stem, "fm": e["fm"]}
                for e in source_catalog.entries(root)
            ]
        except Exception:
            pass
    out = []
    for p in _iter_notes(root / "Memory.nosync" / "Sources"):
        out.append(
            {"path": str(p.relative_to(root)), "stem": p.stem, "fm": _ap.read_frontmatter(p)}
        )
    return out


def _item(kind: str, title: str, action: str, *, path: str = "", age_days=None, detail: str = "", urgent: bool = False) -> dict:
    rank = 0 if urgent else _KIND_RANK.get(kind, 9)
    return {
        "kind": kind,
        "rank": rank,
        "title": title,
        "path": path,
        "age_days": round(age_days, 1) if isinstance(age_days, float) else age_days,
        "detail": detail,
        "action": action,
    }


# --------------------------------------------------------------------------- #
# collectors
# --------------------------------------------------------------------------- #
def _collect_contradictions(root: Path, now: datetime) -> list[dict]:
    out = []
    folder = root / "Memory.nosync" / "Contradictions"
    for p in _iter_notes(folder):
        fm = _ap.read_frontmatter(p)
        status = str(fm.get("status", "")).strip().lower()
        if status not in _OPEN_STATUSES:
            continue
        must = _ap._is_must_resolve(fm)
        out.append(
            _item(
                "contradiction",
                str(fm.get("title", p.stem)),
                "adjudicate per PLAYBOOKS/review.md; update the note's status "
                "and record the resolution",
                path=str(p.relative_to(root)),
                age_days=_age_days(fm, now, "updated", "created"),
                detail="must_resolve" if must else str(fm.get("severity", "")),
                urgent=must,
            )
        )
    return out


def _collect_promotable_rows(root: Path, now: datetime) -> list[dict]:
    out = []
    try:
        diags = _truth_map.validate_rows(root)
    except Exception:
        return out
    for d in diags:
        if d.get("promotable"):
            bo = d["business_object"]
            out.append(
                _item(
                    "promotable-row",
                    f"truth-map row ready to confirm: {bo}",
                    f'./oracle admin truth promote --object "{bo}" --actor "<admin>"',
                    detail=f"evidence={d.get('evidence_count', 0)} freshness={d.get('freshness')}",
                )
            )
        elif not d.get("authority") and d.get("candidate_evidence_count"):
            bo = d["business_object"]
            out.append(
                _item(
                    "promotable-row",
                    f"evidence exists but no primary source set: {bo}",
                    f'./oracle admin truth propose --object "{bo}" --source "<authority of record>"',
                    detail=f"candidate_evidence={d.get('candidate_evidence_count')}",
                )
            )
    return out


def _collect_sources(root: Path, now: datetime) -> list[dict]:
    out = []
    for entry in _source_entries(root):
        fm = entry["fm"]
        tags = _tags(fm)
        if "authority-candidate" in tags:
            out.append(
                _item(
                    "authority-candidate",
                    f"authority proposal awaiting admin review: {fm.get('title', entry['stem'])}",
                    "review the note's Authority candidate section, then "
                    "./oracle admin truth propose/promote to wire it",
                    path=entry["path"],
                    age_days=_age_days(fm, now, "created"),
                )
            )
        if "needs-ocr" in tags or str(fm.get("needs_ocr", "")).strip().lower() == "true":
            out.append(
                _item(
                    "needs-ocr",
                    f"scanned/image source needs transcription: {fm.get('title', entry['stem'])}",
                    "transcribe with your multimodal reading (see PLAYBOOKS/ingest.md), "
                    "then ./oracle ingest the transcript with --derivation agent-ocr",
                    path=entry["path"],
                    age_days=_age_days(fm, now, "created"),
                )
            )
    return out


def _collect_findings(root: Path, now: datetime, budgets: dict) -> list[dict]:
    out = []
    folder = root / "Memory.nosync" / "Findings"
    for p in _iter_notes(folder):
        fm = _ap.read_frontmatter(p)
        if str(fm.get("status", "")).strip().lower() != "needs_review":
            continue
        age = _age_days(fm, now, "updated", "created")
        overdue = age is not None and age > budgets["finding_warn_days"]
        out.append(
            _item(
                "needs-review-finding",
                f"finding awaiting review: {fm.get('title', p.stem)}",
                "review the claim against its source; set status to confirmed "
                "or retire it (PLAYBOOKS/review.md)",
                path=str(p.relative_to(root)),
                age_days=age,
                detail="overdue" if overdue else "",
            )
        )
    return out


def _collect_queries(root: Path, now: datetime, budgets: dict) -> list[dict]:
    """Session-derived retrieval strategies parked ``needs_review`` by dreaming."""
    out = []
    folder = root / "Memory.nosync" / "Queries"
    for p in _iter_notes(folder):
        fm = _ap.read_frontmatter(p)
        if str(fm.get("status", "")).strip().lower() != "needs_review":
            continue
        age = _age_days(fm, now, "updated", "created")
        overdue = age is not None and age > budgets["finding_warn_days"]
        out.append(
            _item(
                "needs-review-query",
                f"query awaiting review: {fm.get('title', p.stem)}",
                "promote it to a stable reusable query (fill in the query text, "
                "set status: active) or retire it (PLAYBOOKS/review.md)",
                path=str(p.relative_to(root)),
                age_days=age,
                detail="overdue" if overdue else "",
            )
        )
    return out


def _collect_questions(root: Path, now: datetime, budgets: dict) -> list[dict]:
    out = []
    folder = root / "Memory.nosync" / "Questions"
    for p in _iter_notes(folder):
        fm = _ap.read_frontmatter(p)
        if str(fm.get("status", "")).strip().lower() not in _OPEN_STATUSES | {"", "unresolved"}:
            continue
        age = _age_days(fm, now, "created")
        if age is None or age <= budgets["question_stale_days"]:
            continue
        out.append(
            _item(
                "stale-question",
                f"open question going stale: {fm.get('title', p.stem)}",
                "check whether new evidence answers it; answer it, plan research, "
                "or escalate to the admin",
                path=str(p.relative_to(root)),
                age_days=age,
            )
        )
    return out


def _collect_models(root: Path, now: datetime, budgets: dict) -> list[dict]:
    out = []
    folder = root / "Memory.nosync" / "Models"
    for p in _iter_notes(folder):
        fm = _ap.read_frontmatter(p)
        age = _age_days(fm, now, "last_validated", "updated", "created")
        if age is None or age <= budgets["model_stale_days"]:
            continue
        out.append(
            _item(
                "stale-model",
                f"model past staleness budget: {fm.get('title', p.stem)}",
                "re-validate against current findings (run the insight-synthesis "
                "loop), update the model, and stamp last_validated",
                path=str(p.relative_to(root)),
                age_days=age,
            )
        )
    return out


def _collect_competing_authority(root: Path) -> list[dict]:
    """Contradiction candidates: one business object, multiple authority claims.

    Deterministic heuristic -- when ingested Sources for the same business
    object name MORE THAN ONE distinct source system/authority, an agent should
    adjudicate which is the authority of record (or record a Contradiction).
    """
    out = []
    claims: dict[str, set] = {}
    titles: dict[str, str] = {}
    for entry in _source_entries(root):
        fm = entry["fm"]
        objects = []
        for key in ("business_object", "authoritative_for"):
            v = fm.get(key)
            objects.extend(v if isinstance(v, list) else ([v] if v else []))
        label = str(fm.get("authority_id") or fm.get("source_system") or "").strip().lower()
        if not label or label == "manual":
            continue
        for obj in objects:
            norm = _truth_map.normalize_object(str(obj))
            if not norm:
                continue
            claims.setdefault(norm, set()).add(label)
            titles.setdefault(norm, str(obj))
    for norm, systems in sorted(claims.items()):
        if len(systems) > 1:
            out.append(
                _item(
                    "contradiction",
                    f"competing authority claims for {titles[norm]!r}: "
                    + ", ".join(sorted(systems)),
                    "decide the authority of record (./oracle admin truth propose/promote); "
                    "if the sources genuinely disagree, record a Contradiction note",
                    detail="authority-conflict-candidate",
                )
            )
    return out


def _collect_aged_signals(root: Path, now: datetime) -> list[dict]:
    """The 'no captured signal ages silently' guarantee, surfaced permanently.

    Canonical thresholds live in meta_health.aged_signals; a critical aged
    failure_event tops the inbox (urgent)."""
    try:
        import meta_health as _mh  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import meta_health as _mh  # type: ignore
        except Exception:
            return []
    out = []
    try:
        for sig in _mh.aged_signals(root, now=now):
            out.append(
                _item(
                    "aged-signal",
                    f"unconsumed {sig['event_kind']} {sig['drop_id']} aged "
                    f"{sig['age_days']}d (budget {sig['budget_days']}d)",
                    sig.get("action", "run the consuming loop (./oracle loops due)"),
                    age_days=float(sig["age_days"]),
                    detail=sig.get("severity") or sig.get("target", ""),
                    urgent=bool(sig.get("critical")),
                )
            )
    except Exception:
        return []
    return out


def _collect_paused_loops(root: Path) -> list[dict]:
    try:
        import meta_health as _mh  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import meta_health as _mh  # type: ignore
        except Exception:
            return []
    out = []
    try:
        for p in _mh.paused_loops(root):
            out.append(
                _item(
                    "paused-loop",
                    f"loop {p['loop_id']} is paused (auto-paused on repeated failures)",
                    "fix the runner/root cause, then set the loop note's status back "
                    "to active (loops.set_status or edit the note)",
                    path=p.get("path", ""),
                    detail=p.get("reason", ""),
                )
            )
    except Exception:
        return []
    return out


def _collect_stale_improvements(root: Path, now: datetime) -> list[dict]:
    try:
        import improvements as _imp  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import improvements as _imp  # type: ignore
        except Exception:
            return []
    out = []
    try:
        for it in _imp.aging(root, now=now):
            out.append(
                _item(
                    "stale-improvement",
                    f"improvement waiting on a decision [{it['kind']}]: {it['title']}",
                    it["action"],
                    path=it.get("path", ""),
                    age_days=it.get("age_days"),
                    detail=it["kind"],
                )
            )
    except Exception:
        return []
    return out


def _collect_autonomy(root: Path) -> list[dict]:
    """Pending promotion proposals + a most-recent demotion needing eyes."""
    try:
        import actions as _actions  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import actions as _actions  # type: ignore
        except Exception:
            return []
    out = []
    try:
        proposal = _actions.pending_proposal(root)
        if proposal is not None:
            out.append(
                _item(
                    "autonomy",
                    f"autonomy promotion to level {proposal.get('to_level')} proposed "
                    f"({proposal.get('drop_id')})",
                    "review the cited evidence, then approve: ./oracle admin autonomy "
                    "promote --actor <admin> --role admin (or ignore to decline)",
                    detail=str(proposal.get("reason", "")),
                )
            )
        events = _actions._autonomy_events(root)
        if events and str(events[-1].get("action")) == "demote":
            last = events[-1]
            out.append(
                _item(
                    "autonomy",
                    f"autonomy was DEMOTED to level {last.get('to_level')} "
                    f"({last.get('drop_id')})",
                    "investigate the cited evidence before re-earning the level "
                    "(./oracle actions log; readiness re-evaluates from ledgers)",
                    detail=str(last.get("reason", "")),
                    urgent=True,
                )
            )
    except Exception:
        return []
    return out


def _collect_unconsumed_events(root: Path) -> list[dict]:
    out = []
    try:
        import loops as _loops  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import loops as _loops  # type: ignore
        except Exception:
            return out
    try:
        for loop_id in sorted(_loops.LOOP_EVENT_KINDS):
            pending = _loops.pending_events(root, loop_id)
            if pending:
                out.append(
                    _item(
                        "unconsumed-events",
                        f"{len(pending)} unconsumed event(s) for loop {loop_id}",
                        f"./oracle loops run {loop_id}",
                        detail=", ".join(sorted({e.get('event_kind', '') for e in pending})),
                    )
                )
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def build_queue(root, now: Optional[datetime] = None) -> list[dict]:
    """Build the ranked Review Inbox for ``root``. Read-only."""
    root = Path(root)
    t = _now(now)
    budgets = _budgets(root)
    items: list[dict] = []
    items += _collect_contradictions(root, t)
    items += _collect_competing_authority(root)
    items += _collect_promotable_rows(root, t)
    items += _collect_sources(root, t)
    items += _collect_findings(root, t, budgets)
    items += _collect_queries(root, t, budgets)
    items += _collect_questions(root, t, budgets)
    items += _collect_models(root, t, budgets)
    items += _collect_unconsumed_events(root)
    items += _collect_aged_signals(root, t)
    items += _collect_paused_loops(root)
    items += _collect_stale_improvements(root, t)
    items += _collect_autonomy(root)
    items.sort(key=lambda i: (i["rank"], -(i["age_days"] or 0)))
    return items


def summary(root, now: Optional[datetime] = None) -> dict:
    """Counts by kind plus the single most urgent item."""
    items = build_queue(root, now)
    by_kind: dict[str, int] = {}
    for i in items:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
    return {
        "total": len(items),
        "by_kind": by_kind,
        "most_urgent": items[0] if items else None,
    }


def render_md(items: list[dict], *, limit: int = 0) -> str:
    if not items:
        return "Review inbox: empty. Nothing is waiting on a decision."
    shown = items[: limit or len(items)]
    lines = [f"# Review inbox -- {len(items)} item(s)", ""]
    for i, it in enumerate(shown, 1):
        age = f" ({it['age_days']}d old)" if it.get("age_days") else ""
        detail = f" [{it['detail']}]" if it.get("detail") else ""
        lines.append(f"{i}. **{it['kind']}**{detail}: {it['title']}{age}")
        if it.get("path"):
            lines.append(f"   - note: `{it['path']}`")
        lines.append(f"   - do: {it['action']}")
    if limit and len(items) > limit:
        lines.append(f"\n…and {len(items) - limit} more (run `./oracle review --all`).")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="review_queue",
        description="The Review Inbox: one ranked queue of everything pending.",
    )
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=False)

    ls = sub.add_parser("list", help="show the ranked inbox (default)")
    ls.add_argument("--json", action="store_true", help="emit JSON")
    ls.add_argument("--limit", type=int, default=15, help="max items shown (0 = all)")
    ls.add_argument("--all", action="store_true", help="show every item")

    sm = sub.add_parser("summary", help="counts by kind")
    sm.add_argument("--json", action="store_true", help="emit JSON")

    args = ap.parse_args(argv)
    root = Path(args.root)
    cmd = args.cmd or "list"

    if cmd == "list":
        items = build_queue(root)
        limit = 0 if getattr(args, "all", False) else getattr(args, "limit", 15)
        if getattr(args, "json", False):
            print(json.dumps(items[: limit or len(items)], indent=2, default=str))
        else:
            print(render_md(items, limit=limit))
        return 0

    if cmd == "summary":
        s = summary(root)
        if getattr(args, "json", False):
            print(json.dumps(s, indent=2, default=str))
        else:
            print(f"review inbox: {s['total']} item(s)")
            for kind, n in sorted(s["by_kind"].items()):
                print(f"  {kind}: {n}")
        return 0

    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
