#!/usr/bin/env python3
"""improvements.py -- the improvement lifecycle: proposed -> applied -> verified.

``Meta.nosync/Improvements/`` is where the oracle records concrete changes to
itself. Before this module, improvements could stall at ``proposed`` forever:
feedback -> improvement creation was automated, but improvement -> action ->
verification was not. This module closes that loop the same way
``recommendation.py`` closes the advice loop: a verdict is computed from
OBSERVED ledger evidence, never from someone asserting "done".

An improvement note's load-bearing frontmatter:

    status: proposed | applied | verified | regressed | rejected | needs_review
    applied: "YYYY-MM-DD"          # stamped when the change lands
    verify: auto | manual          # how the expected signal is checked
    expected_signal:               # machine-checkable predicate (verify: auto)
      event: value_event | feedback_event | failure_event
      target: <id or business object the events reference>
      polarity: positive | negative      # for value/feedback events
      min_count: 1                       # 0 = absence predicate
      within_days: 30                    # window after `applied`

Predicate semantics (deterministic, evaluated against the event ledgers):

  * presence (min_count >= 1): VERIFIED once >= min_count matching events land
    within the window; PENDING before the deadline; EXPIRED after the deadline
    without enough evidence (surfaced for a manual call -- absence of good news
    is not automatically bad news).
  * absence (min_count == 0): VERIFIED only after the window fully elapses
    with zero matches; a match inside the window REGRESSES immediately (e.g.
    "failure mode X does not recur for 30 days").
  * negative evidence: opposite-polarity events or failure_events targeting the
    same target REGRESS a presence predicate at any time.

The adjudication is written into an ``adjudication:`` block (verdict, as_of,
evidence drop_ids); the original trigger/expected_signal fields are never
rewritten. ``oracle_lint`` FAILS an ``applied`` improvement that carries
neither a parseable ``expected_signal`` nor an explicit ``verify: manual``
stamp -- an applied change with no way to ever know whether it worked is the
exact rot this module exists to prevent.

``run_improvement_lifecycle_loop`` is the builtin runner for the
``improvement-lifecycle`` loop (ACTIVE at spawn): it adjudicates every
auto-verifiable applied improvement and hands back a worklist for what needs
judgment (expired predicates, manual verifications, aging proposals).

Stdlib only. Note writes via ``safe_paths.contain`` + mkstemp/os.replace.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - exercised both ways across environments
    import safe_paths
    import ledger
    from oracle_yaml import safe_load, UnsupportedYAML
except Exception:  # pragma: no cover
    from . import safe_paths  # type: ignore
    from . import ledger  # type: ignore
    from .oracle_yaml import safe_load, UnsupportedYAML  # type: ignore


META_BASE = "Meta.nosync"
IMPROVEMENTS_DIR = "Meta.nosync/Improvements"

EVENT_LEDGERS = {
    "feedback_event": "Meta.nosync/ledgers/feedback_event.jsonl",
    "value_event": "Meta.nosync/ledgers/value_event.jsonl",
    "failure_event": "Meta.nosync/ledgers/failure_event.jsonl",
}

STATUSES = ("proposed", "applied", "verified", "regressed", "rejected", "needs_review")
# proposed/needs_review improvements older than this surface for a decision.
PROPOSED_AGE_DAYS = 14
# manual applied improvements older than this surface for verification.
MANUAL_VERIFY_AGE_DAYS = 14
DEFAULT_WITHIN_DAYS = 30


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now_default() -> datetime:
    return datetime.now()


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Ledger timestamps are naive local ISO; normalize aware callers (e.g.
    review_queue passes UTC-aware datetimes) so age math never raises."""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _today_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip().replace("Z", "").replace("z", "")
    if not s or s.lower() in ("null", "none", "~"):
        return None
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _sign(value: Any) -> int:
    try:
        f = float(value)
    except (TypeError, ValueError):
        s = str(value).strip().lower()
        return {"positive": 1, "+": 1, "negative": -1, "-": -1}.get(s, 0)
    return 1 if f > 0 else (-1 if f < 0 else 0)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        fm = safe_load(fm_text) if fm_text.strip() else {}
    except UnsupportedYAML as exc:
        raise ValueError(f"improvement note frontmatter not in safe subset: {exc}")
    if not isinstance(fm, dict):
        raise ValueError("improvement note frontmatter is not a mapping")
    return fm, body.lstrip("\n")


def _scalar_yaml(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    needs_quote = (
        s == ""
        or s.strip() != s
        or any(c in s for c in (":", "#", "'", '"', "[", "]", "{", "}", "&", "*", "!", "|", ">"))
        or s.lower() in ("true", "false", "null", "yes", "no")
    )
    if needs_quote:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _render_yaml_value(value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        out: list[str] = []
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                out.append(f"{pad}{key}:")
                out.extend(_render_yaml_value(child, indent + 2))
            else:
                out.append(f"{pad}{key}: {_scalar_yaml(child)}")
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (dict, list)):
                out.append(f"{pad}-")
                out.extend(_render_yaml_value(item, indent + 2))
            else:
                out.append(f"{pad}- {_scalar_yaml(item)}")
        return out
    return [f"{pad}{_scalar_yaml(value)}"]


def _render_note(fm: dict, body: str) -> str:
    return "---\n" + "\n".join(_render_yaml_value(fm, 0)) + "\n---\n\n" + (body or "") + "\n"


def _write_contained(dst: Path, text: str) -> None:
    """Atomic write to a contained path (mkstemp fd + os.replace)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # contained dst (safe_paths.contain)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(dst))  # safe_paths-internal: atomic swap, dst from safe_paths.contain()
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #
def load_all(root: Path) -> list[dict]:
    """Every improvement note as {fm, body, path}, name-sorted."""
    folder = Path(root) / IMPROVEMENTS_DIR
    out: list[dict] = []
    if not folder.is_dir():
        return out
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            fm, body = _split_frontmatter(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if str(fm.get("type", "")) != "improvement":
            continue
        out.append({"fm": fm, "body": body, "path": p})
    return out


def expected_signal_of(fm: dict) -> Optional[dict]:
    """The parsed expected_signal predicate, or None when absent/malformed."""
    sig = fm.get("expected_signal")
    if not isinstance(sig, dict):
        return None
    event = str(sig.get("event", "")).strip()
    if event not in EVENT_LEDGERS:
        return None
    target = str(sig.get("target", "")).strip()
    if not target:
        return None
    try:
        min_count = int(sig.get("min_count", 1))
    except (TypeError, ValueError):
        min_count = 1
    try:
        within_days = int(sig.get("within_days", DEFAULT_WITHIN_DAYS))
    except (TypeError, ValueError):
        within_days = DEFAULT_WITHIN_DAYS
    return {
        "event": event,
        "target": target,
        "polarity": _sign(sig.get("polarity", "positive")),
        "min_count": max(0, min_count),
        "within_days": max(1, within_days),
    }


def is_auto_verifiable(fm: dict) -> bool:
    return str(fm.get("verify", "")).strip().lower() != "manual" and expected_signal_of(fm) is not None


# --------------------------------------------------------------------------- #
# evaluation (pure read-side)
# --------------------------------------------------------------------------- #
def _matching_rows(root: Path, sig: dict, start: datetime, end: datetime) -> list[dict]:
    rows, _w = ledger.load(Path(root) / EVENT_LEDGERS[sig["event"]])
    out = []
    target = sig["target"].lower()
    for r in rows:
        ts = parse_dt(r.get("ts"))
        if ts is None or not (start < ts <= end):
            continue
        if target not in str(r.get("target", "")).lower():
            continue
        if sig["event"] != "failure_event" and sig["polarity"] != 0:
            if _sign(r.get("polarity", 0)) != sig["polarity"]:
                continue
        out.append(r)
    return out


def _negative_rows(root: Path, sig: dict, start: datetime, end: datetime) -> list[dict]:
    """Counter-evidence for a presence predicate: opposite-polarity events on
    the same target, plus failure_events targeting it."""
    out: list[dict] = []
    target = sig["target"].lower()
    for event, rel in EVENT_LEDGERS.items():
        rows, _w = ledger.load(Path(root) / rel)
        for r in rows:
            ts = parse_dt(r.get("ts"))
            if ts is None or not (start < ts <= end):
                continue
            if target not in str(r.get("target", "")).lower():
                continue
            if event == "failure_event":
                out.append(r)
            elif _sign(r.get("polarity", 0)) == -sig["polarity"] and sig["polarity"] != 0:
                out.append(r)
    return out


def evaluate(root: Path, fm: dict, *, now: Optional[datetime] = None) -> dict:
    """Evaluate one improvement's expected signal against observed ledgers.

    Returns {verdict, matched, negative, deadline}. Verdicts: 'verified' /
    'regressed' / 'pending' / 'expired' (deadline passed, presence unmet) /
    'manual' (no auto predicate).
    """
    now = _naive(now) or _now_default()
    sig = expected_signal_of(fm)
    if sig is None or str(fm.get("verify", "")).strip().lower() == "manual":
        return {"verdict": "manual", "matched": [], "negative": [], "deadline": None}
    applied = parse_dt(fm.get("applied")) or parse_dt(fm.get("updated"))
    if applied is None:
        return {"verdict": "pending", "matched": [], "negative": [], "deadline": None}
    deadline = applied + timedelta(days=sig["within_days"])
    horizon = min(now, deadline)
    matched = _matching_rows(root, sig, applied, horizon)
    matched_ids = [str(r.get("drop_id", "")) for r in matched]
    result = {
        "matched": matched_ids,
        "negative": [],
        "deadline": _today_str(deadline),
    }

    if sig["min_count"] == 0:
        # absence predicate: clean only once the whole window has elapsed.
        if matched:
            result["verdict"] = "regressed"
        elif now >= deadline:
            result["verdict"] = "verified"
        else:
            result["verdict"] = "pending"
        return result

    negative = _negative_rows(root, sig, applied, now)
    result["negative"] = [str(r.get("drop_id", "")) for r in negative]
    if negative and len(negative) >= len(matched):
        result["verdict"] = "regressed"
    elif len(matched) >= sig["min_count"]:
        result["verdict"] = "verified"
    elif now >= deadline:
        result["verdict"] = "expired"
    else:
        result["verdict"] = "pending"
    return result


# --------------------------------------------------------------------------- #
# adjudication (write side: adjudication block + status, originals untouched)
# --------------------------------------------------------------------------- #
def adjudicate_all(root: Path, *, now: Optional[datetime] = None) -> list[dict]:
    """Adjudicate every auto-verifiable APPLIED improvement. Returns a list of
    {id, path, verdict, changed} entries (one per applied improvement seen)."""
    root = Path(root)
    now = _naive(now) or _now_default()
    results: list[dict] = []
    for note in load_all(root):
        fm = note["fm"]
        status = str(fm.get("status", "")).strip().lower()
        if status != "applied":
            continue
        verdict_info = evaluate(root, fm, now=now)
        verdict = verdict_info["verdict"]
        entry = {
            "id": str(fm.get("id", note["path"].stem)),
            "path": str(note["path"]),
            "verdict": verdict,
            "changed": False,
        }
        if verdict in ("verified", "regressed"):
            new_fm = dict(fm)
            new_fm["adjudication"] = {
                "verdict": verdict,
                "as_of": _today_str(now),
                "matched": verdict_info["matched"],
                "negative": verdict_info["negative"],
                "deadline": verdict_info["deadline"] or "",
                "evidence_basis": "observed_event_ledgers",
            }
            new_fm["status"] = verdict
            new_fm["updated"] = _today_str(now)
            dst = safe_paths.contain(
                root, f"Improvements/{note['path'].name}", base=META_BASE
            )
            _write_contained(dst, _render_note(new_fm, note["body"]))
            entry["changed"] = True
        results.append(entry)
    return results


# --------------------------------------------------------------------------- #
# aging + KPIs (consumed by review_queue and scorecard)
# --------------------------------------------------------------------------- #
def aging(root: Path, *, now: Optional[datetime] = None) -> list[dict]:
    """Improvements waiting on a human/agent decision, oldest first.

    Three kinds: 'stale-proposed' (proposed/needs_review older than
    PROPOSED_AGE_DAYS), 'verify-manual' (manual applied older than
    MANUAL_VERIFY_AGE_DAYS), 'expired-signal' (auto predicate ran out of
    window without evidence)."""
    root = Path(root)
    now = _naive(now) or _now_default()
    items: list[dict] = []
    for note in load_all(root):
        fm = note["fm"]
        status = str(fm.get("status", "")).strip().lower()
        created = parse_dt(fm.get("created")) or parse_dt(fm.get("updated"))
        age = (now - created).total_seconds() / 86400.0 if created else None
        ident = str(fm.get("id", note["path"].stem))
        title = str(fm.get("title", ident))
        rel = str(note["path"])
        if status in ("proposed", "needs_review"):
            if age is not None and age > PROPOSED_AGE_DAYS:
                items.append(
                    {
                        "kind": "stale-proposed",
                        "id": ident,
                        "title": title,
                        "path": rel,
                        "age_days": round(age, 1),
                        "action": (
                            "decide this improvement: apply it (stamp status: applied + "
                            "applied date + expected_signal) or reject it"
                        ),
                    }
                )
            continue
        if status != "applied":
            continue
        verdict_info = evaluate(root, fm, now=now)
        applied = parse_dt(fm.get("applied")) or parse_dt(fm.get("updated"))
        applied_age = (now - applied).total_seconds() / 86400.0 if applied else None
        if verdict_info["verdict"] == "manual":
            if applied_age is not None and applied_age > MANUAL_VERIFY_AGE_DAYS:
                items.append(
                    {
                        "kind": "verify-manual",
                        "id": ident,
                        "title": title,
                        "path": rel,
                        "age_days": round(applied_age, 1),
                        "action": (
                            "verify by observed reality and set status verified/regressed "
                            "(an applied improvement with no observed signal is unverified)"
                        ),
                    }
                )
        elif verdict_info["verdict"] == "expired":
            items.append(
                {
                    "kind": "expired-signal",
                    "id": ident,
                    "title": title,
                    "path": rel,
                    "age_days": round(applied_age, 1) if applied_age is not None else None,
                    "action": (
                        "expected_signal window elapsed without evidence: judge manually, "
                        "extend within_days, or mark regressed"
                    ),
                }
            )
    items.sort(key=lambda i: -(i["age_days"] or 0))
    return items


def kpis(root: Path, *, start: datetime, end: datetime) -> dict:
    """The scorecard's improvement section: status counts + window throughput."""
    root = Path(root)
    by_status: dict[str, int] = {}
    verified_in_window = 0
    regressed_in_window = 0
    open_proposed_ages: list[float] = []
    for note in load_all(root):
        fm = note["fm"]
        status = str(fm.get("status", "")).strip().lower() or "proposed"
        by_status[status] = by_status.get(status, 0) + 1
        adj = fm.get("adjudication")
        if isinstance(adj, dict):
            as_of = parse_dt(adj.get("as_of"))
            if as_of is not None and start < as_of <= end:
                if str(adj.get("verdict")) == "verified":
                    verified_in_window += 1
                elif str(adj.get("verdict")) == "regressed":
                    regressed_in_window += 1
        if status in ("proposed", "needs_review"):
            created = parse_dt(fm.get("created"))
            if created is not None:
                open_proposed_ages.append((end - created).total_seconds() / 86400.0)
    return {
        "available": True,
        "by_status": by_status,
        "proposed_open": by_status.get("proposed", 0) + by_status.get("needs_review", 0),
        "verified_in_window": verified_in_window,
        "regressed_in_window": regressed_in_window,
        "proposed_median_age_days": (
            round(statistics.median(open_proposed_ages), 1) if open_proposed_ages else None
        ),
    }


# --------------------------------------------------------------------------- #
# builtin loop runner
# --------------------------------------------------------------------------- #
def run_improvement_lifecycle_loop(root, loop=None, *, now: Optional[datetime] = None) -> dict:
    """Builtin runner for the ``improvement-lifecycle`` loop.

    Deterministic half: adjudicate every auto-verifiable applied improvement
    against the ledgers. Judgment half: hand back a worklist of stale
    proposals, manual verifications, and expired predicates.
    """
    now = _naive(now) or _now_default()
    try:
        adjudicated = adjudicate_all(root, now=now)
        items = aging(root, now=now)
    except Exception as exc:
        return {
            "status": "fail",
            "performed": False,
            "kind": "builtin:improvement-lifecycle",
            "error": f"{type(exc).__name__}: {exc}",
        }
    changed = [a for a in adjudicated if a["changed"]]
    regressed = [a for a in changed if a["verdict"] == "regressed"]
    result = {
        "status": "worklist" if items else "ok",
        "performed": not items,
        "kind": "builtin:improvement-lifecycle",
        "health_signal": "degraded" if regressed else "healthy",
        "adjudicated": adjudicated,
        "summary": (
            f"{len(changed)} improvement(s) adjudicated "
            f"({len(regressed)} regressed); {len(items)} item(s) need judgment"
        ),
    }
    if items:
        result["worklist"] = {
            "loop_id": getattr(loop, "id", "improvement-lifecycle") if loop else "improvement-lifecycle",
            "items": items,
            "instructions": (
                "Work each item, update the note, then call "
                "`loops complete improvement-lifecycle --status ok`."
            ),
        }
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="improvements", description="improvement lifecycle: adjudicate + age"
    )
    parser.add_argument("--root", default=".", help="oracle root (default: .)")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_adj = sub.add_parser("adjudicate", help="adjudicate applied improvements (default)")
    p_adj.add_argument("--now", help="ISO datetime override")
    p_adj.add_argument("--json", action="store_true")

    p_age = sub.add_parser("aging", help="list improvements waiting on a decision")
    p_age.add_argument("--now", help="ISO datetime override")
    p_age.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    root = Path(args.root)
    cmd = args.cmd or "adjudicate"
    now = parse_dt(getattr(args, "now", None)) if getattr(args, "now", None) else None

    try:
        if cmd == "adjudicate":
            res = adjudicate_all(root, now=now)
            if getattr(args, "json", False):
                print(json.dumps(res, indent=2, default=str))
            else:
                for r in res:
                    print(f"  {r['id']:<36} {r['verdict']:<10} changed={r['changed']}")
                print(f"adjudicated: {len(res)} applied improvement(s)")
            return 0
        if cmd == "aging":
            items = aging(root, now=now)
            if getattr(args, "json", False):
                print(json.dumps(items, indent=2, default=str))
            else:
                print(f"improvements waiting on a decision: {len(items)}")
                for i in items:
                    print(f"  [{i['kind']}] {i['title']} ({i['age_days']}d) -> {i['action']}")
            return 0
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"improvements: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
