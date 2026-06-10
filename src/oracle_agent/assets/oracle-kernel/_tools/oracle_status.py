#!/usr/bin/env python3
"""oracle_status.py -- the session protocol: ``status`` opens, ``checkpoint`` closes.

The v2 session contract is three beats (pinned in AGENTS.md / CLAUDE.md):

    ./oracle status      -> where things stand + what to do next (read-only)
    ...work...              (playbook per the AGENTS.md decision tree)
    ./oracle checkpoint  -> matriculate the session: run due builtin loops,
                            re-surface the inbox, leave nothing unrecorded

``status`` is the one screen an operating agent needs at session start:
maturity counts, authority coverage, the Review Inbox summary, due loops, and
concrete suggested next actions. ``checkpoint`` executes the due builtin loops
(memory matriculation, insight synthesis, leadership briefing when due) and
reports what remains for the agent.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:  # bare import (conftest puts _tools on sys.path); package fallback
    import review_queue as _rq
    import truth_map as _truth_map
except Exception:  # pragma: no cover - package import path
    from . import review_queue as _rq  # type: ignore
    from . import truth_map as _truth_map  # type: ignore

__all__ = ["status", "checkpoint"]

# Builtin-runnable loops checkpoint may execute directly (deterministic, no
# agent judgment needed to RUN them; their worklists may still hand work back).
_CHECKPOINT_LOOPS = (
    "memory-matriculation",
    "insight-synthesis",
    "leadership-briefing",
    "improvement-lifecycle",
    "meta-health",
    "stale-finding-refresh",
    "value-scorecard",
    "architecture-retrospective",
)


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _count_notes(root: Path, kind: str) -> int:
    folder = root / "Memory.nosync" / kind
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.glob("*.md") if not p.name.startswith("_"))


def _loops_mod():
    try:
        import loops as _loops  # type: ignore
        return _loops
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import loops as _loops  # type: ignore
            return _loops
        except Exception:
            return None


def _ledger_has_rows(root: Path, rel: str) -> bool:
    p = root / rel
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _maturity_rung(root: Path, counts: dict, confirmed_rows: int, total_rows: int) -> dict:
    """The BOOTSTRAP-STATUS.md ladder, computed instead of asserted.

    Rung 1 (configured) is what `./oracle audit` verifies; this computation
    reports the evidence-driven rungs (0, 2-5).
    """
    if counts["sources"] == 0:
        return {
            "rung": 0,
            "label": "inert-but-safe: no evidence ingested yet "
            "(rung 1 'configured' is verified by ./oracle audit)",
        }
    if confirmed_rows == 0:
        return {
            "rung": 2,
            "label": "seeded: evidence exists; answers ship supported (exit 2), none grounded yet",
        }
    standing = _ledger_has_rows(
        root, "Workproduct.nosync/_STANDING/.registry.jsonl"
    )
    consuming = _ledger_has_rows(root, "Meta.nosync/ledgers/event_consumption.jsonl")
    if standing and consuming:
        return {
            "rung": 5,
            "label": "self-improving: grounded answers, standing deliverables, "
            "feedback events consumed by learning loops",
        }
    if standing:
        return {
            "rung": 4,
            "label": "productive: grounded answers and standing deliverables ship; "
            "capture feedback to reach self-improving",
        }
    return {
        "rung": 3,
        "label": f"grounded: {confirmed_rows}/{total_rows} truth-map rows confirmed; "
        "publish a brief to reach productive",
    }


def status(root, now: Optional[datetime] = None) -> dict:
    """Build the session-start status report. Read-only."""
    root = Path(root)
    t = _now(now)

    counts = {
        "sources": _count_notes(root, "Sources"),
        "findings": _count_notes(root, "Findings"),
        "models": _count_notes(root, "Models"),
        "questions": _count_notes(root, "Questions"),
        "contradictions": _count_notes(root, "Contradictions"),
    }
    try:
        diags = _truth_map.validate_rows(root)
    except Exception:
        diags = []
    confirmed = sum(1 for d in diags if d["status"] == "confirmed")
    inbox = _rq.summary(root, t)

    due_loops = []
    loops_mod = _loops_mod()
    if loops_mod is not None:
        try:
            for d in loops_mod.due(root, t):
                due_loops.append(
                    {
                        "loop_id": getattr(d, "loop_id", None) or getattr(d, "id", ""),
                        "reason": getattr(d, "reason", ""),
                    }
                )
        except Exception:
            pass

    suggestions: list[str] = []
    if counts["sources"] == 0:
        suggestions.append(
            "No evidence yet: start with `./oracle ingest <files or folders>` -- "
            "outside paths are staged in automatically."
        )
    most = inbox.get("most_urgent")
    if most:
        suggestions.append(f"Review inbox top item [{most['kind']}]: {most['action']}")
    for d in due_loops[:3]:
        suggestions.append(f"Loop due: `./oracle loops run {d['loop_id']}`")
    promotable = [d for d in diags if d.get("promotable")]
    for d in promotable[:2]:
        suggestions.append(
            f"Authority ready to confirm: `./oracle admin truth promote --object \"{d['business_object']}\" --actor <admin>`"
        )
    if not suggestions:
        suggestions.append("Nothing pending. Ask, ingest, or run `./oracle brief`.")

    return {
        "generated": t.isoformat(),
        "maturity": _maturity_rung(root, counts, confirmed, len(diags)),
        "memory": counts,
        "authority": {
            "rows": len(diags),
            "confirmed": confirmed,
            "promotable": len(promotable),
        },
        "review_inbox": inbox,
        "due_loops": due_loops,
        "suggested_next": suggestions,
    }


def render_status_md(s: dict) -> str:
    m = s["maturity"]
    lines = [
        f"# Oracle status -- rung {m['rung']}: {m['label']}",
        "",
        f"- memory: {s['memory']['sources']} sources, {s['memory']['findings']} findings, "
        f"{s['memory']['models']} models, {s['memory']['questions']} questions, "
        f"{s['memory']['contradictions']} contradictions",
        f"- authority: {s['authority']['confirmed']}/{s['authority']['rows']} rows confirmed"
        + (f", {s['authority']['promotable']} promotable" if s["authority"]["promotable"] else ""),
        f"- review inbox: {s['review_inbox']['total']} item(s)",
        f"- due loops: {', '.join(d['loop_id'] for d in s['due_loops']) or '(none)'}",
        "",
        "## Do next",
        "",
    ]
    for sug in s["suggested_next"]:
        lines.append(f"- {sug}")
    lines += [
        "",
        "_Session protocol: work per AGENTS.md, then close with `./oracle checkpoint`._",
    ]
    return "\n".join(lines)


def checkpoint(root, now: Optional[datetime] = None) -> dict:
    """Session close: run due builtin loops, then report what remains."""
    root = Path(root)
    t = _now(now)
    ran: list[dict] = []
    loops_mod = _loops_mod()
    if loops_mod is not None:
        try:
            due_ids = set()
            for d in loops_mod.due(root, t):
                due_ids.add(getattr(d, "loop_id", None) or getattr(d, "id", ""))
            for loop_id in _CHECKPOINT_LOOPS:
                if loop_id not in due_ids:
                    continue
                result = loops_mod.run(root, loop_id, now=t, headless=False)
                worklist = (
                    (result.get("dispatch") or {}).get("worklist")
                    or (result.get("outcome") or {}).get("worklist")
                    or result.get("worklist")
                )
                ran.append(
                    {
                        "loop_id": loop_id,
                        "status": result.get("status"),
                        "has_worklist": bool(worklist),
                    }
                )
        except Exception as exc:
            ran.append({"loop_id": "(engine)", "status": "fail", "error": str(exc)})

    after = status(root, t)
    return {
        "generated": t.isoformat(),
        "loops_ran": ran,
        "review_inbox": after["review_inbox"],
        "remaining": after["suggested_next"],
        "reminder": (
            "If this session produced feedback, praise, a missed call, or "
            "measurable value, record it: ./oracle capture feedback|value|failure. "
            "If material facts were learned, capture them: ./oracle remember ..."
        ),
    }


def render_checkpoint_md(c: dict) -> str:
    lines = ["# Session checkpoint", ""]
    if c["loops_ran"]:
        for r in c["loops_ran"]:
            extra = " (worklist returned -- finish it before closing)" if r.get("has_worklist") else ""
            lines.append(f"- ran loop {r['loop_id']}: {r['status']}{extra}")
    else:
        lines.append("- no builtin loops were due")
    lines += ["", f"- review inbox now: {c['review_inbox']['total']} item(s)", "", "## Remaining", ""]
    for s in c["remaining"]:
        lines.append(f"- {s}")
    lines += ["", f"_{c['reminder']}_"]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="oracle_status",
        description="Session protocol: status (open) and checkpoint (close).",
    )
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=False)
    st = sub.add_parser("status", help="session-start report (default)")
    st.add_argument("--json", action="store_true")
    ck = sub.add_parser("checkpoint", help="session-close: run due builtin loops + report")
    ck.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    root = Path(args.root)
    cmd = args.cmd or "status"

    if cmd == "status":
        s = status(root)
        print(json.dumps(s, indent=2, default=str) if getattr(args, "json", False) else render_status_md(s))
        return 0
    if cmd == "checkpoint":
        c = checkpoint(root)
        print(json.dumps(c, indent=2, default=str) if getattr(args, "json", False) else render_checkpoint_md(c))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
