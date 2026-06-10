#!/usr/bin/env python3
"""meta_health.py -- the consumer for the oracle's own telemetry.

Before this module, the kernel produced operational telemetry nothing read:
``loop_runs`` health signals accumulated, ``skill_event`` usage counts grew,
``action_event`` grant/deny patterns piled up -- and a broken loop could fail
weekly forever while a critical failure_event aged unconsumed. meta-health is
the deterministic loop (ACTIVE at spawn) that closes those leaks:

  1. **Loop health.** A loop whose last ``CONSECUTIVE_FAILS`` recorded runs all
     failed is PAUSED (``status: paused`` + ``paused_reason``) and a
     failure_event is captured so the learning loops and the Review Inbox see
     it. Auto-pause is fail-safe; auto-resume is not -- resuming means working
     the inbox item and flipping the status back to active.

  2. **Signal aging (backpressure).** The doctrine guarantee "no captured
     signal ages silently": an unconsumed critical/high failure_event older
     than ``CRITICAL_AGE_DAYS``, or ANY unconsumed event older than
     ``ANY_AGE_DAYS``, is surfaced -- here in the worklist and permanently in
     the Review Inbox (``review_queue`` calls :func:`aged_signals`).

  3. **Skill hygiene.** Active skills with zero recorded uses for
     ``SKILL_UNUSED_DAYS`` become archive candidates; skills patched more
     often than used become stability-review candidates. Candidates only --
     archiving stays an agent decision through ``./oracle skills archive``.

  4. **Autonomy fit.** Repeated allowlist denials for the same loop draft an
     allowlist-expansion proposal; granted actions that then failed draft a
     tightening/investigation item. When the promotion criteria hold
     (``actions.promotion_readiness``), meta-health DRAFTS the level-promotion
     proposal -- the admin approves it with one command
     (``./oracle admin autonomy promote``). meta-health never changes the
     autonomy posture itself.

Everything here is deterministic and read-mostly; the only writes are the
loop-note pause (via ``loops.set_status``), the pause failure_event (via
``capture``), and the promotion-proposal ledger row (via ``actions``). All
sibling imports are lazy so the module degrades cleanly in partial harnesses.

Stdlib only.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - exercised both ways across environments
    import ledger
except Exception:  # pragma: no cover
    from . import ledger  # type: ignore


LOOP_RUNS_LEDGER = "Meta.nosync/ledgers/loop_runs.jsonl"
ACTION_LEDGER = "Meta.nosync/ledgers/action_event.jsonl"
CONSUMPTION_LEDGER = "Meta.nosync/ledgers/event_consumption.jsonl"
EVENT_LEDGERS = {
    "feedback_event": "Meta.nosync/ledgers/feedback_event.jsonl",
    "value_event": "Meta.nosync/ledgers/value_event.jsonl",
    "failure_event": "Meta.nosync/ledgers/failure_event.jsonl",
}

CONSECUTIVE_FAILS = 3       # runs that all failed -> pause the loop
CRITICAL_AGE_DAYS = 7       # unconsumed critical/high failure_event budget
ANY_AGE_DAYS = 30           # unconsumed anything budget
SKILL_UNUSED_DAYS = 90      # active skill with no use -> archive candidate
DENIAL_PATTERN_MIN = 3      # same-loop allowlist denials -> expansion proposal
GRANT_FAIL_MIN = 2          # same-loop granted-then-failed -> investigate


def _now_default() -> datetime:
    return datetime.now()


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Ledger timestamps are naive local ISO; normalize aware callers (e.g.
    review_queue passes UTC-aware datetimes) so age math never raises."""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _import(name: str):
    for candidate in (name, f".{name}"):
        try:
            if candidate.startswith("."):
                return importlib.import_module(candidate, package="_tools")
            return importlib.import_module(candidate)
        except Exception:
            continue
    return None


def _parse_dt(value: Any) -> Optional[datetime]:
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


def _age_days(ts: Any, now: datetime) -> Optional[float]:
    dt = _parse_dt(ts)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _rows(root: Path, rel: str) -> list[dict]:
    rows, _w = ledger.load(Path(root) / rel)
    return rows


# --------------------------------------------------------------------------- #
# 1. loop health -- pause repeat offenders
# --------------------------------------------------------------------------- #
def degraded_loops(root: Path) -> list[str]:
    """Loop ids whose last CONSECUTIVE_FAILS recorded runs ALL failed."""
    runs_by_loop: dict[str, list[dict]] = {}
    for row in _rows(root, LOOP_RUNS_LEDGER):
        lid = str(row.get("loop_id", "")).strip()
        if lid:
            runs_by_loop.setdefault(lid, []).append(row)
    out: list[str] = []
    for lid, rows in sorted(runs_by_loop.items()):
        rows.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("drop_id", ""))))
        tail = rows[-CONSECUTIVE_FAILS:]
        if len(tail) < CONSECUTIVE_FAILS:
            continue
        if all(str(r.get("status", "")).strip().lower() == "fail" for r in tail):
            out.append(lid)
    return out


def pause_degraded_loops(root: Path, *, now: Optional[datetime] = None) -> list[dict]:
    """Pause every active loop that qualifies as degraded. Returns the pauses."""
    now = _naive(now) or _now_default()
    loops_mod = _import("loops")
    if loops_mod is None:
        return []
    paused: list[dict] = []
    active_ids = {l.id for l in loops_mod.list_loops(root) if l.status == "active"}
    for lid in degraded_loops(root):
        if lid not in active_ids:
            continue  # already paused/retired -- nothing to do
        reason = (
            f"{CONSECUTIVE_FAILS} consecutive failed runs (loop_runs ledger); "
            "paused by meta-health -- fix the runner, then set status: active"
        )
        try:
            loops_mod.set_status(root, lid, "paused", reason=reason, now=now)
        except Exception as exc:
            paused.append({"loop_id": lid, "paused": False, "error": str(exc)})
            continue
        capture_mod = _import("capture")
        event_id = ""
        if capture_mod is not None:
            try:
                event_id = capture_mod.failure_event(
                    root,
                    target=lid,
                    severity="high",
                    failure_mode="loop-degraded",
                    excerpt=reason,
                    actor="meta-health",
                    now=now,
                )["drop_id"]
            except Exception:
                event_id = ""
        paused.append({"loop_id": lid, "paused": True, "reason": reason, "failure_event": event_id})
    return paused


def paused_loops(root: Path) -> list[dict]:
    """Currently paused loop records (for the Review Inbox)."""
    loops_mod = _import("loops")
    if loops_mod is None:
        return []
    out = []
    for loop in loops_mod.list_loops(root):
        if loop.status == "paused":
            out.append(
                {
                    "loop_id": loop.id,
                    "reason": str(loop.get("paused_reason", "")),
                    "path": str(loop.path) if loop.path else "",
                }
            )
    return out


# --------------------------------------------------------------------------- #
# 2. signal aging -- "no captured signal ages silently"
# --------------------------------------------------------------------------- #
def aged_signals(root: Path, *, now: Optional[datetime] = None) -> list[dict]:
    """Unconsumed events past their age budget, most urgent first.

    An event is unconsumed when NO loop has recorded consumption for it.
    Critical/high failure_events get the tight CRITICAL_AGE_DAYS budget; every
    event gets the ANY_AGE_DAYS budget.
    """
    root = Path(root)
    now = _naive(now) or _now_default()
    consumed = {
        str(r.get("event_drop_id", "")).strip()
        for r in _rows(root, CONSUMPTION_LEDGER)
    }
    out: list[dict] = []
    for kind, rel in EVENT_LEDGERS.items():
        for row in _rows(root, rel):
            eid = str(row.get("drop_id", "")).strip()
            if not eid or eid in consumed:
                continue
            age = _age_days(row.get("ts"), now)
            if age is None:
                continue
            severity = str(row.get("severity", "")).strip().lower()
            is_critical = kind == "failure_event" and severity in ("critical", "high")
            if is_critical and age > CRITICAL_AGE_DAYS:
                budget = CRITICAL_AGE_DAYS
            elif age > ANY_AGE_DAYS:
                budget = ANY_AGE_DAYS
            else:
                continue
            out.append(
                {
                    "drop_id": eid,
                    "event_kind": kind,
                    "target": str(row.get("target", "")),
                    "severity": severity,
                    "age_days": round(age, 1),
                    "budget_days": budget,
                    "critical": is_critical,
                    "action": "run the consuming loop: ./oracle loops due / loops run <id>",
                }
            )
    out.sort(key=lambda i: (not i["critical"], -i["age_days"]))
    return out


# --------------------------------------------------------------------------- #
# 3. skill hygiene
# --------------------------------------------------------------------------- #
def skill_hygiene(root: Path, *, now: Optional[datetime] = None) -> list[dict]:
    now = _naive(now) or _now_default()
    skills_mod = _import("skills")
    if skills_mod is None:
        return []
    try:
        rep = skills_mod.report(root)
    except Exception:
        return []
    last_event_ts: dict[str, datetime] = {}
    last_use_ts: dict[str, datetime] = {}
    for row in rep.get("events", []):
        name = str(row.get("skill", ""))
        ts = _parse_dt(row.get("ts"))
        if not name or ts is None:
            continue
        if ts > last_event_ts.get(name, datetime.min):
            last_event_ts[name] = ts
        if row.get("action") == "use" and ts > last_use_ts.get(name, datetime.min):
            last_use_ts[name] = ts
    by_skill = {r["skill"]: r for r in rep.get("by_skill", [])}
    items: list[dict] = []
    for skill in rep.get("skills", []):
        name = str(skill.get("name", "")) if isinstance(skill, dict) else str(skill)
        if not name:
            continue
        status = str(skill.get("status", "active")) if isinstance(skill, dict) else "active"
        if status != "active":
            continue
        stats = by_skill.get(name, {})
        uses = int(stats.get("uses", 0))
        patches = int(stats.get("patches", 0))
        last_seen = last_use_ts.get(name) or last_event_ts.get(name)
        idle_days = (now - last_seen).total_seconds() / 86400.0 if last_seen else None
        if uses == 0 and idle_days is not None and idle_days > SKILL_UNUSED_DAYS:
            items.append(
                {
                    "kind": "skill-archive-candidate",
                    "skill": name,
                    "idle_days": round(idle_days, 1),
                    "action": (
                        f"unused for {int(idle_days)}d: archive it "
                        f"(./oracle skills archive {name}) or record why it stays"
                    ),
                }
            )
        elif uses and patches > uses:
            items.append(
                {
                    "kind": "skill-stability-review",
                    "skill": name,
                    "patches": patches,
                    "uses": uses,
                    "action": "patched more than used: review whether the procedure is actually stable",
                }
            )
    return items


# --------------------------------------------------------------------------- #
# 4. autonomy fit
# --------------------------------------------------------------------------- #
def autonomy_fit(root: Path, *, now: Optional[datetime] = None) -> list[dict]:
    now = _naive(now) or _now_default()
    items: list[dict] = []
    denials: dict[str, int] = {}
    grant_fails: dict[str, int] = {}
    for row in _rows(root, ACTION_LEDGER):
        age = _age_days(row.get("ts"), now)
        if age is None or age > ANY_AGE_DAYS:
            continue
        scope = row.get("scope") or {}
        lid = str(scope.get("loop") or "") if isinstance(scope, dict) else ""
        result = str(row.get("result", ""))
        reason = str(row.get("reason", ""))
        if result == "deny" and "allowed_loops" in reason and lid:
            denials[lid] = denials.get(lid, 0) + 1
        if row.get("phase") == "actual" and result == "fail" and lid:
            grant_fails[lid] = grant_fails.get(lid, 0) + 1
    for lid, n in sorted(denials.items()):
        if n >= DENIAL_PATTERN_MIN:
            items.append(
                {
                    "kind": "autonomy-expansion-candidate",
                    "loop_id": lid,
                    "denials": n,
                    "action": (
                        f"loop {lid!r} was denied {n}x in {ANY_AGE_DAYS}d: if it should run "
                        "headless, add it to autonomy.yml allowed_loops (admin)"
                    ),
                }
            )
    for lid, n in sorted(grant_fails.items()):
        if n >= GRANT_FAIL_MIN:
            items.append(
                {
                    "kind": "autonomy-tightening-candidate",
                    "loop_id": lid,
                    "failures": n,
                    "action": (
                        f"granted headless runs of {lid!r} failed {n}x: investigate before "
                        "it runs headless again (consider removing from allowed_loops)"
                    ),
                }
            )
    # Promotion readiness: meta-health DRAFTS, the admin approves.
    actions_mod = _import("actions")
    if actions_mod is not None and hasattr(actions_mod, "promotion_readiness"):
        try:
            readiness = actions_mod.promotion_readiness(root, now=now)
            if readiness.get("ready"):
                proposal = actions_mod.propose_promotion(
                    root,
                    to_level=readiness["to_level"],
                    evidence=readiness.get("evidence", []),
                    reason=readiness.get("reason", ""),
                    actor="meta-health",
                    now=now,
                )
                if proposal.get("proposed"):
                    items.append(
                        {
                            "kind": "autonomy-promotion-proposal",
                            "to_level": readiness["to_level"],
                            "evidence": readiness.get("evidence", []),
                            "action": (
                                f"promotion to level {readiness['to_level']} proposed on cited "
                                "evidence: approve with ./oracle admin autonomy promote "
                                "--actor <admin> --role admin"
                            ),
                        }
                    )
        except Exception:
            pass
    return items


# --------------------------------------------------------------------------- #
# builtin loop runner
# --------------------------------------------------------------------------- #
def run_meta_health_loop(root, loop=None, *, now: Optional[datetime] = None) -> dict:
    """Builtin runner for the ``meta-health`` loop (deterministic)."""
    root = Path(root)
    now = _naive(now) or _now_default()
    try:
        # Fail-closed demotion sweep first: telemetry consumption must never
        # leave an earned-back level standing on top of a critical failure.
        actions_mod = _import("actions")
        demotion = None
        if actions_mod is not None and hasattr(actions_mod, "enforce_demotion_policy"):
            try:
                demotion = actions_mod.enforce_demotion_policy(root, now=now)
            except Exception:
                demotion = None
        pauses = pause_degraded_loops(root, now=now)
        aged = aged_signals(root, now=now)
        skills_items = skill_hygiene(root, now=now)
        autonomy_items = autonomy_fit(root, now=now)
    except Exception as exc:
        return {
            "status": "fail",
            "performed": False,
            "kind": "builtin:meta-health",
            "error": f"{type(exc).__name__}: {exc}",
        }
    judgment_items = (
        [
            {"kind": "paused-loop", **p, "action": "fix the runner, then set the loop status back to active"}
            for p in pauses
            if p.get("paused")
        ]
        + aged
        + skills_items
        + autonomy_items
    )
    degraded = bool(pauses) or any(i.get("critical") for i in aged)
    result = {
        "status": "worklist" if judgment_items else "ok",
        "performed": bool(pauses) or not judgment_items,
        "kind": "builtin:meta-health",
        "health_signal": "degraded" if degraded else "healthy",
        "paused": pauses,
        "aged_signals": len(aged),
        "demotion": demotion,
        "summary": (
            f"{len(pauses)} loop(s) paused, {len(aged)} aged signal(s), "
            f"{len(skills_items)} skill item(s), {len(autonomy_items)} autonomy item(s)"
        ),
    }
    if judgment_items:
        result["worklist"] = {
            "loop_id": getattr(loop, "id", "meta-health") if loop else "meta-health",
            "items": judgment_items,
            "instructions": (
                "Work each item, then call `loops complete meta-health --status ok`."
            ),
        }
    return result


# --------------------------------------------------------------------------- #
# architecture retrospective (builtin convener; the thinking stays agent work)
# --------------------------------------------------------------------------- #
def run_architecture_retrospective_loop(root, loop=None, *, now: Optional[datetime] = None) -> dict:
    """Builtin runner for the ``architecture-retrospective`` loop.

    Quarterly by cadence, and due IMMEDIATELY on a regressing scorecard, a
    paused loop, or a critical failure (``loops.due`` wires the triggers). The
    runner's deterministic half gathers the full evidence dossier; the
    retrospective itself is judgment and stays an agent worklist. Output
    contract for the agent: a clear verdict (architecture stays / evolves),
    Improvements notes for what should change, and -- for any structural
    change -- a READY-TO-APPROVE proposal (exact loop/schema/doctrine diffs +
    the apply command) so the admin decides rather than authors. Structural
    changes still require the ``change_architecture`` capability.
    """
    root = Path(root)
    now = _naive(now) or _now_default()
    loops_mod = _import("loops")
    inventory = {"active": 0, "proposed": 0, "paused": 0, "retired": 0}
    if loops_mod is not None:
        try:
            for l in loops_mod.list_loops(root):
                inventory[l.status] = inventory.get(l.status, 0) + 1
        except Exception:
            pass
    failure_by_mode: dict[str, int] = {}
    for row in _rows(root, EVENT_LEDGERS["failure_event"]):
        age = _age_days(row.get("ts"), now)
        if age is None or age > 90:
            continue
        mode = str(row.get("failure_mode", "unspecified")).strip() or "unspecified"
        failure_by_mode[mode] = failure_by_mode.get(mode, 0) + 1
    scorecard_mod = _import("scorecard")
    latest = None
    trend = ""
    if scorecard_mod is not None:
        try:
            latest = scorecard_mod.latest_scorecard(root)
            trend = scorecard_mod.latest_trend(root)
        except Exception:
            latest = None
    dossier = {
        "scorecard_trend": trend,
        "scorecard": {
            k: latest.get(k)
            for k in ("window_start", "window_end", "composite", "kpis")
        }
        if isinstance(latest, dict)
        else None,
        "loop_inventory": inventory,
        "paused_loops": paused_loops(root),
        "failures_90d_by_mode": failure_by_mode,
        "aged_signals": aged_signals(root, now=now),
    }
    instructions = (
        "Conduct the architecture retrospective against this dossier. Answer: "
        "are the loops the right loops (create/promote/pause/retire)? Is there "
        "schema/ontology debt? Are the chokepoints holding? What do the "
        "period's failures say systemically? Is the oracle delivering value "
        "(scorecard)? Output: (1) a verdict -- architecture stays or evolves; "
        "(2) Improvements notes (status: proposed, with expected_signal) for "
        "every change; (3) for structural changes, a ready-to-approve proposal "
        "with exact diffs and the apply command (admin: change_architecture); "
        "(4) a Retrospectives/ note + an ADR when the shape changes. Then "
        "`loops complete architecture-retrospective --status ok`."
    )
    return {
        "status": "worklist",
        "performed": False,
        "kind": "builtin:architecture-retrospective",
        "worklist": {
            "loop_id": getattr(loop, "id", "architecture-retrospective") if loop else "architecture-retrospective",
            "dossier": dossier,
            "instructions": instructions,
        },
        "summary": (
            f"retrospective convened: trend={trend or 'n/a'}, "
            f"{len(dossier['paused_loops'])} paused loop(s), "
            f"{sum(failure_by_mode.values())} failure(s) in 90d"
        ),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meta_health", description="telemetry consumer: loop health, signal aging, skill hygiene, autonomy fit"
    )
    parser.add_argument("--root", default=".", help="oracle root (default: .)")
    sub = parser.add_subparsers(dest="cmd", required=False)
    p_run = sub.add_parser("run", help="run the full meta-health pass (default)")
    p_run.add_argument("--now", help="ISO datetime override")
    p_run.add_argument("--json", action="store_true")
    p_aged = sub.add_parser("aged", help="list aged unconsumed signals")
    p_aged.add_argument("--now", help="ISO datetime override")

    args = parser.parse_args(argv)
    root = Path(args.root)
    cmd = args.cmd or "run"
    now = _parse_dt(getattr(args, "now", None)) if getattr(args, "now", None) else None

    if cmd == "run":
        res = run_meta_health_loop(root, now=now)
        print(json.dumps(res, indent=2, default=str))
        return 0 if res.get("status") in ("ok", "worklist") else 1
    if cmd == "aged":
        print(json.dumps(aged_signals(root, now=now), indent=2, default=str))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
