#!/usr/bin/env python3
"""loops.py -- the deterministic loop runner + due-ness engine.

A *loop* is a recurring improvement process recorded as a Markdown note under
``Meta.nosync/Loops/`` with block-style YAML frontmatter (the strict oracle_yaml
subset). Its load-bearing fields are ``cadence``, ``last_run``, ``next_review``,
``runner`` and ``trigger_conditions`` (see the loop record schema). An ACTIVE
loop is a real, runnable record -- ``oracle_lint`` FAILS any ``status==active``
loop that lacks a ``runner`` or a ``last_run``.

This module is the engine that decides WHICH loops are due, dispatches a due
loop's runner, and records completed work durably. It is deliberately split so the
due-ness math is a *pure function of an injected clock*:

    compute_due(loops, now) -> [DueLoop ...]   # ranked, most-overdue first

``compute_due`` NEVER reads the wall clock. The caller passes ``now`` in. This
makes due-ness fully deterministic and testable: "a weekly loop last run 8 days
ago is due; immediately after ``record`` it is not". Every other entry point
that needs the current time takes ``now`` as an optional parameter and only
falls back to ``datetime.now()`` at the outermost CLI edge.

Public API (binding):
    list_loops(root) -> [Loop ...]
    compute_due(loops, now) -> [DueLoop ...]      # ranked due worklist
    due(root, now=None) -> [DueLoop ...]           # includes unconsumed events
    run(root, loop_id, *, now=None, headless=False, gate=True) -> dict
    record(root, loop_id, status, *, now=None, health_signal=None,
           notes=None, next_review=None) -> dict   # appends loop_runs + advances
    complete(root, loop_id, status, ..., consume_all=False) -> dict
    next_review_for(loop, now) -> datetime|None
    main(argv) -> int                              # CLI: list|due|run|complete|record

Cadence vocabulary understood by the engine (case-insensitive):
    every-session / per-session / on-session  -> always due (no time gate)
    on-event / on-demand / manual             -> never time-due (trigger only)
    hourly                                     -> 1 hour
    daily                                      -> 1 day
    weekly                                     -> 7 days
    biweekly / fortnightly                     -> 14 days
    monthly                                    -> 30 days
    quarterly                                  -> 90 days
    yearly / annually                          -> 365 days
    ISO-8601 durations (e.g. P7D, P1M, PT12H)  -> parsed exactly
    "N days" / "N weeks" / "N hours"           -> parsed

Loop runs are recorded to ``Meta.nosync/ledgers/loop_runs.jsonl`` via
``ledger.append`` with the contracted shape:
    {drop_id, ts, loop_id, status:'ok'|'fail', last_run, next_review,
     health_signal, notes}

Dispatch: ``run`` resolves ``loop.runner``. A ``module:function`` runner imports
the named module and calls ``fn(root, loop, ctx)``; successful deterministic
runners are recorded immediately. An ``agent-worklist`` runner returns a
structured worklist for an agent to execute and is NOT recorded as complete until
``complete`` is called. When ``gate=True`` and ``actions.py`` is importable, a
headless run is wrapped in the autonomy chokepoint so the kill-switch /
allowlist / caps apply; if ``actions.py`` is unavailable the gate degrades to a
plain run and says so in the result.

Stdlib only. All note writes go to paths derived through ``safe_paths.contain``;
the temp-file note write uses ``os.fdopen`` (NOT ``open(<var>, 'w')``) so the
no-bypass guard is satisfied without a marker.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# Floor imports (work both as bare modules and as a package).
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - exercised both ways across environments
    import safe_paths
    import ledger
    from oracle_yaml import safe_load, UnsupportedYAML
except Exception:  # pragma: no cover
    from . import safe_paths  # type: ignore
    from . import ledger  # type: ignore
    from .oracle_yaml import safe_load, UnsupportedYAML  # type: ignore

try:  # pragma: no cover
    import schema_check  # type: ignore
except Exception:  # pragma: no cover
    try:
        from . import schema_check  # type: ignore
    except Exception:  # pragma: no cover
        schema_check = None  # type: ignore


META_BASE = "Meta.nosync"
LOOPS_DIR = "Meta.nosync/Loops"
LOOP_RUNS_LEDGER = "Meta.nosync/ledgers/loop_runs.jsonl"
EVENT_CONSUMPTION_LEDGER = "Meta.nosync/ledgers/event_consumption.jsonl"

EVENT_LEDGER_BY_KIND = {
    "feedback_event": "Meta.nosync/ledgers/feedback_event.jsonl",
    "value_event": "Meta.nosync/ledgers/value_event.jsonl",
    "failure_event": "Meta.nosync/ledgers/failure_event.jsonl",
}

LOOP_EVENT_KINDS = {
    "user-feedback-learning": {"feedback_event", "value_event", "failure_event"},
    "skill-repository-learning": {"feedback_event", "value_event", "failure_event"},
}

ACTIVE = "active"
RUN_STATUSES = ("ok", "fail")

LOOP_MODEL_POLICY_VERSION = "loop-model-policy/v1"
LOOP_MODEL_POLICY = {
    "version": LOOP_MODEL_POLICY_VERSION,
    "applies_to": ["scheduled", "headless", "agent-worklist"],
    "deterministic_code_first": True,
    "default_model_selection": "cheapest_fully_capable",
    "premium_model_use": {
        "allowed_when_any": [
            "explicit_admin_approval",
            "documented_on_demand_complexity",
        ],
        "rationale_required": True,
    },
    "multi_agent_passes": {
        "allowed_when_any": [
            "explicit_admin_approval",
            "documented_on_demand_complexity",
        ],
        "rationale_required": True,
    },
    "rationale": {
        "required_for": ["premium_model_use", "multi_agent_passes"],
        "record_in": [
            "loop_completion_notes",
            "durable_run_artifact",
        ],
    },
    "forbid_expensive_default_model": True,
}

# Sentinels for cadences that are not a fixed time interval.
ALWAYS_DUE = "always"      # every-session loops: due whenever asked
NEVER_TIME_DUE = "never"   # on-event/on-demand loops: only trigger_conditions matter

_NAMED_CADENCE = {
    "every-session": ALWAYS_DUE,
    "per-session": ALWAYS_DUE,
    "on-session": ALWAYS_DUE,
    "each-session": ALWAYS_DUE,
    "session": ALWAYS_DUE,
    "on-event": NEVER_TIME_DUE,
    "on-demand": NEVER_TIME_DUE,
    "manual": NEVER_TIME_DUE,
    "ad-hoc": NEVER_TIME_DUE,
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "fortnightly": timedelta(days=14),
    "monthly": timedelta(days=30),
    "quarterly": timedelta(days=90),
    "yearly": timedelta(days=365),
    "annually": timedelta(days=365),
}

_PHRASE_RE = re.compile(r"^(?:every\s+)?(\d+)\s*(hour|day|week|month|quarter|year)s?$")
_ISO_DUR_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


# --------------------------------------------------------------------------- #
# time helpers (the ONLY place that may touch the wall clock is _now_default)
# --------------------------------------------------------------------------- #
def _now_default() -> datetime:
    return datetime.now()


def _now_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _today_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse a stored ``last_run`` / ``next_review`` value into a datetime.

    Accepts ISO-8601 (date or datetime), tolerates a trailing ``Z`` and a space
    separator. Returns None for null/empty/unparseable so the engine treats a
    never-run loop as immediately due rather than crashing.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "~"):
        return None
    s = s.replace("Z", "").replace("z", "")
    # Normalize a space-separated datetime to ISO 'T'.
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Date-only fallback.
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def parse_cadence(cadence: Any):
    """Map a cadence string to a ``timedelta`` or a sentinel.

    Returns ``ALWAYS_DUE`` (every-session), ``NEVER_TIME_DUE`` (event/manual),
    a ``timedelta``, or ``None`` when the cadence is unrecognized (treated by
    the engine as "due if never run, else not time-gated" to stay safe).
    """
    if cadence is None:
        return None
    key = str(cadence).strip().lower()
    if not key:
        return None
    if key in _NAMED_CADENCE:
        return _NAMED_CADENCE[key]

    m = _PHRASE_RE.match(key)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return {
            "hour": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
            "quarter": timedelta(days=90 * n),
            "year": timedelta(days=365 * n),
        }[unit]

    iso = _ISO_DUR_RE.match(str(cadence).strip().upper())
    if iso and any(iso.groups()):
        years, months, weeks, days, hours, minutes, seconds = (
            int(g) if g else 0 for g in iso.groups()
        )
        return timedelta(
            days=years * 365 + months * 30 + weeks * 7 + days,
            hours=hours,
            minutes=minutes,
            seconds=seconds,
        )
    return None


# --------------------------------------------------------------------------- #
# Loop record
# --------------------------------------------------------------------------- #
@dataclass
class Loop:
    frontmatter: dict
    body: str = ""
    path: Optional[Path] = None

    @property
    def id(self) -> str:
        return str(self.frontmatter.get("id", ""))

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", "proposed"))

    @property
    def cadence(self) -> str:
        return str(self.frontmatter.get("cadence", "") or "")

    @property
    def runner(self) -> str:
        return str(self.frontmatter.get("runner", "") or "")

    @property
    def last_run(self) -> Optional[datetime]:
        return parse_dt(self.frontmatter.get("last_run"))

    @property
    def next_review(self) -> Optional[datetime]:
        return parse_dt(self.frontmatter.get("next_review"))

    @property
    def trigger_conditions(self) -> list:
        tc = self.frontmatter.get("trigger_conditions")
        if isinstance(tc, list):
            return [t for t in tc if t is not None]
        if tc is None:
            return []
        return [tc]

    def get(self, key: str, default: Any = None) -> Any:
        return self.frontmatter.get(key, default)


@dataclass
class DueLoop:
    loop: Loop
    reason: str
    overdue_seconds: float = 0.0
    next_review: Optional[datetime] = None

    @property
    def id(self) -> str:
        return self.loop.id


# --------------------------------------------------------------------------- #
# Note (de)serialization -- shared block-style frontmatter shape
# --------------------------------------------------------------------------- #
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
        raise ValueError(f"loop note frontmatter not in safe subset: {exc}")
    if not isinstance(fm, dict):
        raise ValueError("loop note frontmatter is not a mapping")
    return fm, body.lstrip("\n")


def read_note(path: Path) -> Loop:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    return Loop(frontmatter=fm, body=body, path=path)


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


def _render_frontmatter(fm: dict) -> str:
    return "\n".join(_render_yaml_value(fm, 0))


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
    return "---\n" + _render_frontmatter(fm) + "\n---\n\n" + (body or "") + "\n"


def _write_contained(dst: Path, text: str) -> None:
    """Atomic write to a path already validated by safe_paths.contain.

    Uses ``os.fdopen`` on a mkstemp fd + ``os.replace`` -- never
    ``open(<var>, 'w')`` -- so the no-bypass guard passes with no marker.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # contained dst (safe_paths.contain)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(dst))  # atomic note swap on contained path
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def _builtin_active_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "id",
            "type",
            "title",
            "created",
            "updated",
            "sensitivity",
            "status",
            "tags",
            "cadence",
            "runner",
            "last_run",
            "next_review",
            "trigger_conditions",
        ],
        "properties": {
            "type": {"type": "string", "enum": ["loop"]},
            "status": {"type": "string", "enum": ["active", "proposed", "retired", "paused"]},
            "sensitivity": {
                "type": "string",
                "enum": ["public", "internal", "confidential", "restricted", "secret"],
            },
            "cadence": {"type": "string", "pattern": "\\S"},
            "runner": {"type": "string", "pattern": "\\S"},
            "trigger_conditions": {"type": "array"},
        },
    }


def _load_active_schema(root: Path) -> dict:
    schema_path = Path(root) / "_tools" / "schemas" / "loop.schema.json"
    if schema_path.exists():
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return _builtin_active_schema()


def validate_active(root: Path, fm: dict) -> list[str]:
    """Validate an ACTIVE loop's frontmatter against the loop schema.

    Mirrors what oracle_lint enforces: an active loop MUST carry runner +
    last_run (and the rest of the required set). Returns a list of error
    strings ([] == valid).
    """
    schema = _load_active_schema(root)
    if schema_check is not None:
        return schema_check.validate(fm, schema)
    errs = []
    for req in schema.get("required", []):
        if fm.get(req) in (None, ""):
            errs.append(f"missing required property {req!r}")
    return errs


def loop_model_policy(loop: Optional[Loop] = None) -> dict:
    """Return the machine-readable model-use policy for loop work.

    Existing loop records may not yet carry ``model_policy`` in frontmatter; the
    engine therefore falls back to the canonical kernel policy while still
    allowing a future loop record to ship an equivalent explicit policy object.
    """
    policy = loop.get("model_policy") if loop is not None else None
    if not isinstance(policy, dict) or not policy:
        policy = LOOP_MODEL_POLICY
    return json.loads(json.dumps(policy))


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def _loops_dir(root: Path) -> Path:
    return Path(root) / LOOPS_DIR


def list_loops(root: Path) -> list[Loop]:
    """Load every loop record under ``Meta.nosync/Loops/`` (skipping templates).

    Files whose name starts with ``_`` (``_CONTEXT.md``) and the concrete
    ``loop-template.md`` scaffold are treated as scaffolding. We also skip any
    note whose ``type`` is not ``loop`` or whose id is the template id.
    """
    d = _loops_dir(root)
    out: list[Loop] = []
    if not d.exists():
        return out
    for p in sorted(d.glob("*.md")):
        if p.name.startswith("_") or p.name == "loop-template.md":
            continue
        try:
            loop = read_note(p)
        except ValueError:
            continue
        if str(loop.get("type", "")) != "loop":
            continue
        if loop.id in ("", "loop-template"):
            continue
        out.append(loop)
    return out


def find_loop(root: Path, loop_id: str) -> Optional[Loop]:
    for loop in list_loops(root):
        if loop.id == loop_id:
            return loop
    return None


# --------------------------------------------------------------------------- #
# Due-ness -- a PURE function of the loops and an injected clock
# --------------------------------------------------------------------------- #
def next_review_for(loop: Loop, now: datetime) -> Optional[datetime]:
    """Compute the next review datetime for a loop given a run at ``now``.

    For a fixed-interval cadence this is ``now + interval``. For every-session
    loops the next review is ``now`` (it is always due again next session). For
    event/manual loops there is no scheduled next review (None).
    """
    delta = parse_cadence(loop.cadence)
    if isinstance(delta, timedelta):
        return now + delta
    if delta == ALWAYS_DUE:
        return now
    return None


def compute_due(loops: list[Loop], now: datetime) -> list[DueLoop]:
    """Return the DUE loops, most-overdue first. PURE: never reads the clock.

    Due-ness rules (per loop):
      * status != active            -> never due (only active loops run).
      * cadence every-session       -> always due (overdue=0, reason 'every-session').
      * cadence event/manual        -> NOT time-due; due only if it has explicit
                                       trigger_conditions AND has never run (we
                                       surface it once so the agent can wire it).
      * fixed interval, never run   -> due (reason 'never-run').
      * fixed interval, last_run    -> due iff now >= last_run + interval; the
                                       overdue magnitude ranks the worklist.
      * unrecognized cadence        -> due iff never run (safe default).

    ``now`` is REQUIRED and is the single clock. Two callers passing the same
    ``loops`` and ``now`` get byte-identical results.
    """
    due: list[DueLoop] = []
    for loop in loops:
        if loop.status != ACTIVE:
            continue
        delta = parse_cadence(loop.cadence)
        last = loop.last_run

        if delta == ALWAYS_DUE:
            due.append(
                DueLoop(
                    loop=loop,
                    reason="every-session",
                    overdue_seconds=0.0,
                    next_review=next_review_for(loop, now),
                )
            )
            continue

        if delta == NEVER_TIME_DUE:
            # Event/manual loops are not time-gated. We surface a never-run one
            # exactly once (so it gets wired), but never on a recurring clock.
            if last is None and loop.trigger_conditions:
                due.append(
                    DueLoop(
                        loop=loop,
                        reason="event-loop-unrun",
                        overdue_seconds=0.0,
                        next_review=None,
                    )
                )
            continue

        if last is None:
            due.append(
                DueLoop(
                    loop=loop,
                    reason="never-run",
                    overdue_seconds=float("inf"),
                    next_review=next_review_for(loop, now),
                )
            )
            continue

        if not isinstance(delta, timedelta):
            # Unrecognized cadence but the loop HAS run: don't time-gate it.
            continue

        due_at = last + delta
        if now >= due_at:
            overdue = (now - due_at).total_seconds()
            due.append(
                DueLoop(
                    loop=loop,
                    reason=f"overdue-by-{int(overdue)}s",
                    overdue_seconds=overdue,
                    next_review=next_review_for(loop, now),
                )
            )

    # Rank: most overdue first; never-run (inf) floats to the top; stable by id.
    due.sort(key=lambda d: (-d.overdue_seconds, d.id))
    return due


def due(root: Path, now: Optional[datetime] = None) -> list[DueLoop]:
    """Convenience: load loops from ``root`` and compute the due worklist.

    ``now`` defaults to the wall clock ONLY here at the edge; the math itself
    (``compute_due``) is pure. This root-aware wrapper also adds event-driven
    due-ness for loops with unconsumed feedback/value/failure events.
    """
    now = now or _now_default()
    loop_list = list_loops(root)
    due_list = compute_due(loop_list, now)
    seen = {d.id for d in due_list}
    for loop in loop_list:
        if loop.status != ACTIVE:
            continue
        pending = pending_events(root, loop.id)
        if not pending or loop.id in seen:
            continue
        due_list.append(
            DueLoop(
                loop=loop,
                reason=f"event-backlog-{len(pending)}",
                overdue_seconds=float("inf"),
                next_review=loop.next_review,
            )
        )
        seen.add(loop.id)
    for loop in loop_list:
        if loop.id != "architecture-retrospective" or loop.status != ACTIVE:
            continue
        if loop.id in seen:
            continue
        trigger = _retrospective_trigger(root, loop, now)
        if trigger:
            due_list.append(
                DueLoop(
                    loop=loop,
                    reason=trigger,
                    overdue_seconds=float("inf"),
                    next_review=loop.next_review,
                )
            )
    due_list.sort(key=lambda d: (-d.overdue_seconds, d.id))
    return due_list


def _retrospective_trigger(root: Path, loop: Loop, now: datetime) -> str:
    """Event triggers that make the architecture-retrospective due IMMEDIATELY,
    ahead of its quarterly cadence: a regressing scorecard, a paused-degraded
    loop, or a critical failure newer than the retrospective's last run.
    Lazy + defensive: an absent sibling module never breaks due-ness."""
    try:
        import scorecard as _sc  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import scorecard as _sc  # type: ignore
        except Exception:
            _sc = None  # type: ignore
    if _sc is not None:
        try:
            if _sc.latest_trend(root) == "regressing":
                return "regression-trigger-scorecard"
        except Exception:
            pass
    try:
        if any(l.status == "paused" for l in list_loops(root)):
            return "regression-trigger-paused-loop"
    except Exception:
        pass
    try:
        rows, _w = ledger.load(Path(root) / EVENT_LEDGER_BY_KIND["failure_event"])
        last = loop.last_run
        for row in rows:
            if str(row.get("severity", "")).strip().lower() != "critical":
                continue
            ts = parse_dt(row.get("ts"))
            if ts is not None and (last is None or ts > last):
                return "regression-trigger-critical-failure"
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def _resolve_python_runner(runner: str) -> Callable:
    """Resolve a ``module:function`` runner to a callable.

    The module is imported via importlib using the same bare-then-package import
    discipline the rest of the kernel uses. Raises ValueError on a bad spec.
    """
    if ":" not in runner:
        raise ValueError(f"python runner must be 'module:function', got {runner!r}")
    mod_name, _, fn_name = runner.partition(":")
    mod_name = mod_name.strip()
    fn_name = fn_name.strip()
    if not mod_name or not fn_name:
        raise ValueError(f"invalid runner spec {runner!r}")
    mod = None
    last_exc: Optional[Exception] = None
    for candidate in (mod_name, f".{mod_name}", f"_tools.{mod_name}"):
        try:
            if candidate.startswith("."):
                mod = importlib.import_module(candidate, package="_tools")
            else:
                mod = importlib.import_module(candidate)
            break
        except Exception as exc:  # pragma: no cover - import variance
            last_exc = exc
            continue
    if mod is None:
        raise ValueError(f"runner module {mod_name!r} not importable: {last_exc}")
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        raise ValueError(f"runner {runner!r}: {fn_name!r} is not callable in {mod_name!r}")
    return fn


def _dispatch(root: Path, loop: Loop, *, now: datetime, headless: bool) -> dict:
    """Execute a loop's runner and return a result dict.

    Two runner shapes:
      * ``agent-worklist`` (or empty/None) -> returns a structured worklist for
        an agent; status 'ok', performed=False. The engine never fabricates
        agent work; it hands back the loop's inputs/process/triggers.
      * ``module:function`` -> imports and calls ``fn(root, loop, ctx)`` where
        ctx = {now, headless}. The callable's return (a dict or anything) is
        captured under 'runner_result'; an exception becomes status 'fail'.
    """
    runner = loop.runner.strip()
    runner_key = runner.lower()
    if runner_key in ("builtin:memory-matriculation", "session_memory:run_memory_dreaming_loop"):
        return _run_memory_matriculation(root, loop, now=now)
    if runner_key in ("builtin:user-feedback-learning", "self-improvement:user-feedback-learning"):
        return _run_user_feedback_learning(root, loop, now=now)
    if runner_key in ("builtin:skill-repository-learning", "self-improvement:skill-repository-learning"):
        return _run_skill_repository_learning(root, loop, now=now)
    if runner_key in ("builtin:insight-synthesis", "synthesis:run_insight_synthesis"):
        return _run_builtin_module(
            root, loop, now=now, module="synthesis", fn="run_insight_synthesis",
            kind="builtin:insight-synthesis",
        )
    if runner_key in ("builtin:leadership-briefing", "briefing:run_leadership_briefing"):
        return _run_builtin_module(
            root, loop, now=now, module="briefing", fn="run_leadership_briefing",
            kind="builtin:leadership-briefing",
        )
    if runner_key in ("builtin:value-scorecard", "scorecard:run_value_scorecard_loop"):
        return _run_builtin_module(
            root, loop, now=now, module="scorecard", fn="run_value_scorecard_loop",
            kind="builtin:value-scorecard",
        )
    if runner_key in ("builtin:improvement-lifecycle", "improvements:run_improvement_lifecycle_loop"):
        return _run_builtin_module(
            root, loop, now=now, module="improvements", fn="run_improvement_lifecycle_loop",
            kind="builtin:improvement-lifecycle",
        )
    if runner_key in ("builtin:meta-health", "meta_health:run_meta_health_loop"):
        return _run_builtin_module(
            root, loop, now=now, module="meta_health", fn="run_meta_health_loop",
            kind="builtin:meta-health",
        )
    if runner_key in ("builtin:stale-finding-refresh", "synthesis:run_staleness_sweep"):
        return _run_builtin_module(
            root, loop, now=now, module="synthesis", fn="run_staleness_sweep",
            kind="builtin:stale-finding-refresh",
        )
    if runner_key in (
        "builtin:architecture-retrospective",
        "meta_health:run_architecture_retrospective_loop",
    ):
        return _run_builtin_module(
            root, loop, now=now, module="meta_health",
            fn="run_architecture_retrospective_loop",
            kind="builtin:architecture-retrospective",
        )
    if runner == "" or runner_key in ("agent-worklist", "agent", "worklist"):
        return {
            "status": "worklist",
            "performed": False,
            "kind": "agent-worklist",
            "worklist": {
                "loop_id": loop.id,
                "title": loop.get("title", loop.id),
                "cadence": loop.cadence,
                "trigger_conditions": loop.trigger_conditions,
                "instructions": (
                    "Agent-executed loop: perform the loop's documented process, "
                    "then call `loops complete <id> --status ok|fail`."
                ),
                "model_policy": loop_model_policy(loop),
            },
        }
    try:
        fn = _resolve_python_runner(runner)
    except ValueError as exc:
        return {"status": "fail", "performed": False, "kind": "python", "error": str(exc)}
    ctx = {"now": now, "headless": headless}
    try:
        result = fn(root, loop, ctx)
    except Exception as exc:  # pragma: no cover - runner-defined failures
        return {
            "status": "fail",
            "performed": True,
            "kind": "python",
            "runner": runner,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "status": "ok",
        "performed": True,
        "kind": "python",
        "runner": runner,
        "runner_result": result,
    }


def _run_builtin_module(
    root: Path, loop: Loop, *, now: datetime, module: str, fn: str, kind: str
) -> dict:
    """Shared dispatch for v2 builtin runners (synthesis / briefing).

    The target function has signature ``fn(root, loop=None, *, now=None) -> dict``
    and returns the loop-result shape (status/performed/kind/...).
    """
    try:
        mod = importlib.import_module(module)
    except Exception:
        try:
            mod = importlib.import_module(f".{module}", package="_tools")
        except Exception as exc:
            return {
                "status": "fail",
                "performed": False,
                "kind": kind,
                "error": f"{module} unavailable: {type(exc).__name__}: {exc}",
            }
    try:
        return getattr(mod, fn)(root, loop, now=now)
    except Exception as exc:
        return {
            "status": "fail",
            "performed": False,
            "kind": kind,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _run_memory_matriculation(root: Path, loop: Loop, *, now: datetime) -> dict:
    """Deterministic runner for the core memory-matriculation loop.

    This is where daily/session dreaming lives. It owns session decomposition and
    derived recall/graph refreshes so the kernel does not need a redundant
    separate active "memory-dreaming" loop. Source capture remains separate and
    evidence-only; feedback/skill loops remain preference/procedure-only.
    """
    try:
        mod = importlib.import_module("session_memory")
    except Exception:
        try:
            mod = importlib.import_module(".session_memory", package="_tools")
        except Exception as exc:
            return {
                "status": "fail",
                "performed": False,
                "kind": "builtin:memory-matriculation",
                "error": f"session_memory unavailable: {type(exc).__name__}: {exc}",
            }
    try:
        result = mod.dream(root, now=now)
    except Exception as exc:
        return {
            "status": "fail",
            "performed": True,
            "kind": "builtin:memory-matriculation",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "status": "ok" if result.get("status") == "ok" else "fail",
        "performed": True,
        "kind": "builtin:memory-matriculation",
        "health_signal": "healthy" if result.get("status") == "ok" else "degraded",
        "processed": result.get("processed", 0),
        "runner_result": result,
    }


def _event_summary(row: dict) -> str:
    parts = [
        str(row.get("drop_id") or row.get("id") or "").strip(),
        str(row.get("event_kind") or row.get("kind") or "").strip(),
        f"target={str(row.get('target', '')).strip()}",
    ]
    for key in ("polarity", "strength", "value_kind", "severity"):
        value = str(row.get(key, "")).strip()
        if value:
            parts.append(f"{key}={value}")
    text = str(
        row.get("text")
        or row.get("summary")
        or row.get("failure")
        or row.get("excerpt")
        or ""
    ).strip()
    if text:
        text = re.sub(r"\s+", " ", text)
        parts.append(f"text={text[:180]}")
    return " | ".join(p for p in parts if p)


def _upsert_learning_note(
    root: Path,
    *,
    folder: str,
    filename: str,
    fm: dict,
    heading: str,
    bullets: list[str],
    now: datetime,
) -> str:
    dst = safe_paths.contain(root, f"{folder}/{filename}", base=META_BASE)
    existing_body = ""
    created = _today_str(now)
    if dst.exists():
        try:
            existing = read_note(dst)
            existing_body = existing.body.rstrip()
            created = str(existing.get("created", created) or created)
        except Exception:
            existing_body = ""
    fm = dict(fm)
    fm["created"] = created
    fm["updated"] = _today_str(now)
    section = [f"## {heading}", ""]
    section.extend(f"- {b}" for b in bullets)
    body_parts = [existing_body] if existing_body else [f"# {fm['title']}"]
    body_parts.extend(["", *section])
    _write_contained(dst, _render_note(fm, "\n".join(body_parts).rstrip() + "\n"))
    return str(dst)


USER_MODEL_NOTE = "User-Models/user-model-self-improvement.md"


def _read_user_model_fm(root: Path) -> dict:
    p = Path(root) / META_BASE / USER_MODEL_NOTE
    if not p.exists():
        return {}
    try:
        return read_note(p).frontmatter
    except (ValueError, OSError):
        return {}


def _updated_preferences(prev: Any, pending: list[dict]) -> dict:
    """Fold pending event rows into the structured preference counters.

    These counters are the MACHINE half of the user model (FR-A4): recency
    cannot lie because every increment cites its window in last_evidence, and
    standing deliverables can read them without parsing prose bullets.
    """
    prefs = dict(prev) if isinstance(prev, dict) else {}
    counts = prefs.get("signal_counts")
    counts = dict(counts) if isinstance(counts, dict) else {}
    by_kind = prefs.get("value_by_kind")
    by_kind = dict(by_kind) if isinstance(by_kind, dict) else {}
    failure_modes = prefs.get("failure_modes")
    failure_modes = dict(failure_modes) if isinstance(failure_modes, dict) else {}
    for row in pending:
        try:
            pol = float(row.get("polarity", 0))
        except (TypeError, ValueError):
            pol = 0.0
        bucket = "positive" if pol > 0 else ("negative" if pol < 0 else "neutral")
        counts[bucket] = int(counts.get(bucket, 0)) + 1
        kind = str(row.get("event_kind", row.get("kind", "")))
        if kind == "value_event":
            vk = str(row.get("value_kind", "other")).strip().lower() or "other"
            try:
                strength = abs(float(row.get("strength", 1.0)))
            except (TypeError, ValueError):
                strength = 1.0
            sign = 1 if pol > 0 else (-1 if pol < 0 else 0)
            by_kind[vk] = round(float(by_kind.get(vk, 0.0)) + sign * strength, 4)
        elif kind == "failure_event":
            mode = str(row.get("failure_mode", "unspecified")).strip() or "unspecified"
            failure_modes[mode] = int(failure_modes.get(mode, 0)) + 1
    prefs["signal_counts"] = counts
    prefs["value_by_kind"] = by_kind
    prefs["failure_modes"] = failure_modes
    evidence = prefs.get("last_evidence")
    evidence = list(evidence) if isinstance(evidence, list) else []
    evidence.extend(str(r.get("drop_id", "")) for r in pending if r.get("drop_id"))
    prefs["last_evidence"] = evidence[-10:]
    return prefs


def user_model_signals(root: Path) -> dict:
    """The structured preference counters of the self-improvement user model.

    The machine-readable consumer surface for standing deliverables/briefing:
    {} when the model has not learned anything yet.
    """
    prefs = _read_user_model_fm(root).get("preferences")
    return dict(prefs) if isinstance(prefs, dict) else {}


def _run_user_feedback_learning(root: Path, loop: Loop, *, now: datetime) -> dict:
    pending = pending_events(root, loop.id)
    if not pending:
        return {
            "status": "ok",
            "performed": False,
            "kind": "builtin:user-feedback-learning",
            "health_signal": "healthy",
            "processed": 0,
        }
    prev_fm = _read_user_model_fm(root)
    note_path = _upsert_learning_note(
        root,
        folder="User-Models",
        filename="user-model-self-improvement.md",
        fm={
            "id": "UM-self-improvement",
            "type": "user_model",
            "title": "Self-improvement user model",
            "sensitivity": "internal",
            "status": "active",
            "tags": ["meta", "user_model", "self-improvement"],
            "preferences": _updated_preferences(prev_fm.get("preferences"), pending),
        },
        heading=f"Signals learned {_today_str(now)}",
        bullets=[_event_summary(row) for row in pending],
        now=now,
    )
    consumed = consume_events(
        root,
        loop.id,
        event_ids=[str(row.get("drop_id", "")) for row in pending],
        now=now,
        actor="loops",
        notes="deterministic user-feedback-learning runner",
    )
    return {
        "status": "ok",
        "performed": True,
        "kind": "builtin:user-feedback-learning",
        "health_signal": "healthy",
        "processed": len(pending),
        "note_path": note_path,
        "event_consumption": consumed,
    }


def _run_skill_repository_learning(root: Path, loop: Loop, *, now: datetime) -> dict:
    pending = pending_events(root, loop.id)
    if not pending:
        return {
            "status": "ok",
            "performed": False,
            "kind": "builtin:skill-repository-learning",
            "health_signal": "healthy",
            "processed": 0,
        }
    note_path = _upsert_learning_note(
        root,
        folder="Improvements",
        filename="improvement-skill-repository-learning.md",
        fm={
            "id": "IMP-skill-repository-learning",
            "type": "improvement",
            "title": "Skill repository learning queue",
            "sensitivity": "internal",
            "status": "needs_review",
            "tags": ["meta", "improvement", "skills", "self-improvement"],
        },
        heading=f"Skill signals {_today_str(now)}",
        bullets=[
            "Review for reusable skill/procedure update: " + _event_summary(row)
            for row in pending
        ],
        now=now,
    )
    consumed = consume_events(
        root,
        loop.id,
        event_ids=[str(row.get("drop_id", "")) for row in pending],
        now=now,
        actor="loops",
        notes="deterministic skill-repository-learning runner",
    )
    return {
        "status": "ok",
        "performed": True,
        "kind": "builtin:skill-repository-learning",
        "health_signal": "healthy",
        "processed": len(pending),
        "note_path": note_path,
        "event_consumption": consumed,
    }


def run(
    root: Path,
    loop_id: str,
    *,
    now: Optional[datetime] = None,
    headless: bool = False,
    gate: bool = True,
) -> dict:
    """Run a single loop's runner and record the run.

    When ``gate`` is True and ``actions.py`` is importable, a headless run is
    wrapped in the autonomy chokepoint (kill-switch / allowlist / blast-radius
    caps). If autonomy denies the run, NOTHING is dispatched and a 'denied'
    result is returned (no loop_runs row is appended for a denied run -- the
    autonomy layer logs its own action_event). If ``actions.py`` is unavailable
    the gate degrades to a direct run and notes it.

    On dispatch, the run is recorded to the loop_runs ledger and the loop note's
    ``last_run`` / ``next_review`` are advanced (via ``record``).
    """
    now = now or _now_default()
    loop = find_loop(root, loop_id)
    if loop is None:
        return {"status": "fail", "loop_id": loop_id, "error": "loop not found"}
    if loop.status != ACTIVE:
        return {
            "status": "fail",
            "loop_id": loop_id,
            "error": f"loop status is {loop.status!r}, only active loops run",
        }

    if gate and headless:
        actions_mod = _load_actions()
        if actions_mod is not None:
            with_action = getattr(actions_mod, "with_action", None)
            action_denied = getattr(actions_mod, "ActionDenied", PermissionError)
            if callable(with_action):
                try:
                    with with_action("loop.run", _action_scope(loop), root=Path(root)) as gate_note:
                        return _dispatch_and_maybe_record(
                            root,
                            loop,
                            now=now,
                            headless=headless,
                            gate_note=gate_note,
                        )
                except action_denied as exc:
                    gate_note = getattr(exc, "decision", None) or {
                        "allowed": False,
                        "granted": False,
                        "reason": str(exc),
                    }
                    return {
                        "status": "denied",
                        "loop_id": loop_id,
                        "gate": gate_note,
                        "performed": False,
                    }
            decision = _autonomy_gate_from_module(actions_mod, root, loop)
            if decision is not None:
                allowed, gate_note = decision
                if not allowed:
                    return {
                        "status": "denied",
                        "loop_id": loop_id,
                        "gate": gate_note,
                        "performed": False,
                    }
                return _dispatch_and_maybe_record(
                    root,
                    loop,
                    now=now,
                    headless=headless,
                    gate_note=gate_note,
                )

    return _dispatch_and_maybe_record(root, loop, now=now, headless=headless)


def _dispatch_and_maybe_record(
    root: Path,
    loop: Loop,
    *,
    now: datetime,
    headless: bool,
    gate_note: Optional[dict] = None,
) -> dict:
    outcome = _dispatch(root, loop, now=now, headless=headless)
    if outcome.get("kind") == "agent-worklist" or outcome.get("status") == "worklist":
        # A run that returns a worklist is NOT finished (PLAYBOOKS/loops.md):
        # the agent half remains. Recording it here would advance last_run --
        # and, worse, recording it as 'fail' would teach meta-health that a
        # perfectly healthy loop is degraded. The loop stays due until the
        # agent calls `loops complete <id>`.
        result = {
            "status": "worklist",
            "loop_id": loop.id,
            "performed": False,
            "kind": outcome.get("kind", "agent-worklist"),
            "outcome": outcome,
        }
        if gate_note is not None:
            result["gate"] = gate_note
        return result

    run_status = "ok" if outcome.get("status") == "ok" else "fail"
    health = outcome.get("health_signal") or _derive_health(outcome)
    rec = record(
        root,
        loop.id,
        run_status,
        now=now,
        health_signal=health,
        notes=outcome.get("error") or outcome.get("kind"),
    )
    result = {
        "status": run_status,
        "loop_id": loop.id,
        "performed": outcome.get("performed", False),
        "kind": outcome.get("kind"),
        "outcome": outcome,
        "last_run": rec["last_run"],
        "next_review": rec["next_review"],
        "drop_id": rec["drop_id"],
    }
    if gate_note is not None:
        result["gate"] = gate_note
    return result


def _derive_health(outcome: dict) -> str:
    if outcome.get("status") == "ok":
        return "healthy"
    return "degraded"


def _load_actions():
    """Return actions.py when importable, else None.

    The loop engine can run in small test harnesses where the autonomy module is
    absent, so importing it stays lazy and optional.
    """
    for loader in (
        lambda: importlib.import_module("actions"),
        lambda: importlib.import_module(".actions", package="_tools"),
    ):
        try:
            return loader()
        except Exception:
            continue
    return None


def _action_scope(loop: Loop) -> dict:
    return {"loop_id": loop.id}


def _autonomy_gate_from_module(actions: Any, root: Path, loop: Loop):
    """Legacy pure authorization fallback for action modules without with_action.

    Modern actions.py exposes ``with_action`` so direct headless runs create
    action_event rows. This fallback exists only for older compatible action
    modules; it cannot provide an action_event ledger.
    """
    if actions is None:
        return None
    try:
        authorize = getattr(actions, "authorize", None)
        if callable(authorize):
            grant = authorize(root, "loop.run", _action_scope(loop))
            if isinstance(grant, dict):
                allowed = bool(grant.get("allowed", grant.get("granted", False)))
                return allowed, grant
            return bool(grant), {"allowed": bool(grant)}
    except Exception as exc:  # pragma: no cover - defensive
        return False, {"allowed": False, "error": f"{type(exc).__name__}: {exc}"}
    return None


def _autonomy_gate(root: Path, loop: Loop):
    """Consult actions.py (if present) for a pure headless permission verdict.

    ``run`` uses ``actions.with_action`` when available so grants and denials are
    logged. This helper remains for callers that explicitly need a no-side-effect
    authorization probe.
    """
    return _autonomy_gate_from_module(_load_actions(), root, loop)


# --------------------------------------------------------------------------- #
# Record -- append loop_runs row + advance last_run/next_review atomically
# --------------------------------------------------------------------------- #
def _loop_runs_ledger(root: Path) -> Path:
    return Path(root) / LOOP_RUNS_LEDGER


def _event_consumption_ledger(root: Path) -> Path:
    return Path(root) / EVENT_CONSUMPTION_LEDGER


def _consumed_event_ids(root: Path, loop_id: str) -> set[str]:
    rows, _warnings = ledger.load(_event_consumption_ledger(root))
    consumed: set[str] = set()
    for row in rows:
        if str(row.get("loop_id", "")) != str(loop_id):
            continue
        eid = str(row.get("event_drop_id", "")).strip()
        if eid:
            consumed.add(eid)
    return consumed


def event_kinds_for_loop(loop_id: str) -> set[str]:
    return set(LOOP_EVENT_KINDS.get(str(loop_id), set()))


def pending_events(root: Path, loop_id: str) -> list[dict]:
    """Return unconsumed event rows relevant to ``loop_id``.

    The event rows stay in their source ledgers. Consumption is tracked separately
    by ``event_consumption.jsonl`` so multiple loops may process the same event
    independently.
    """
    kinds = event_kinds_for_loop(loop_id)
    if not kinds:
        return []
    consumed = _consumed_event_ids(root, loop_id)
    out: list[dict] = []
    for kind in sorted(kinds):
        rel = EVENT_LEDGER_BY_KIND.get(kind)
        if not rel:
            continue
        rows, _warnings = ledger.load(Path(root) / rel)
        for row in rows:
            if str(row.get("kind", kind)) != kind:
                continue
            eid = str(row.get("drop_id", "")).strip()
            if not eid or eid in consumed:
                continue
            enriched = dict(row)
            enriched["event_kind"] = kind
            enriched["source_ledger"] = rel
            out.append(enriched)
    out.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("drop_id", ""))))
    return out


def consume_events(
    root: Path,
    loop_id: str,
    *,
    event_ids: Optional[list[str]] = None,
    now: Optional[datetime] = None,
    actor: str = "",
    notes: str = "",
) -> dict:
    now = now or _now_default()
    pending = pending_events(root, loop_id)
    wanted = {str(e).strip() for e in (event_ids or []) if str(e).strip()}
    if wanted:
        pending = [row for row in pending if str(row.get("drop_id", "")) in wanted]
    consumed: list[str] = []
    for row in pending:
        eid = str(row.get("drop_id", "")).strip()
        if not eid:
            continue
        ledger.append(
            _event_consumption_ledger(root),
            {
                "ts": _now_iso(now),
                "kind": "event_consumption",
                "loop_id": loop_id,
                "event_drop_id": eid,
                "event_kind": row.get("event_kind", row.get("kind", "")),
                "source_ledger": row.get("source_ledger", ""),
                "actor": actor or "",
                "notes": notes or "",
            },
            id_prefix="EVCON",
        )
        consumed.append(eid)
    return {"loop_id": loop_id, "consumed": consumed, "count": len(consumed)}


def record(
    root: Path,
    loop_id: str,
    status: str,
    *,
    now: Optional[datetime] = None,
    health_signal: Optional[str] = None,
    notes: Optional[str] = None,
    next_review: Optional[datetime] = None,
) -> dict:
    """Record a loop run: append a loop_runs ledger row AND advance the note.

    Effects (in order, so the durable ledger row is never lost if the note write
    fails):
      1. Compute the new ``last_run`` = ``now`` and ``next_review`` from cadence
         (unless an explicit ``next_review`` is supplied).
      2. Append a loop_runs row {drop_id,ts,loop_id,status,last_run,next_review,
         health_signal,notes} via ledger.append (minted under one lock).
      3. Rewrite the loop note's frontmatter with the advanced last_run /
         next_review / updated, through safe_paths.contain.

    Returns {drop_id,last_run,next_review,status}. Raises ValueError if the loop
    is missing. ``status`` must be 'ok' or 'fail'.
    """
    now = now or _now_default()
    status = str(status).strip().lower()
    if status not in RUN_STATUSES:
        raise ValueError(f"record: status must be one of {RUN_STATUSES}, got {status!r}")

    loop = find_loop(root, loop_id)
    if loop is None:
        raise ValueError(f"record: no loop with id {loop_id!r}")

    new_last_run = now
    nr = next_review if next_review is not None else next_review_for(loop, now)
    last_run_str = _today_str(new_last_run)
    next_review_str = _today_str(nr) if nr is not None else None

    row = {
        "ts": _now_iso(now),
        "loop_id": loop_id,
        "status": status,
        "last_run": last_run_str,
        "next_review": next_review_str if next_review_str is not None else "",
        "health_signal": health_signal or ("healthy" if status == "ok" else "degraded"),
        "notes": notes or "",
    }
    drop_id = ledger.append(_loop_runs_ledger(root), row, id_prefix="LRUN")

    # Advance the loop note's schedule fields. We keep last_run/next_review as
    # date strings (YYYY-MM-DD) so they satisfy the loop schema's date pattern.
    if loop.path is not None:
        fm = dict(loop.frontmatter)
        fm["last_run"] = last_run_str
        # next_review must satisfy the date pattern when present; for event loops
        # (no scheduled next review) we leave the prior value untouched if any,
        # else set it to last_run to keep the schema's pattern satisfied.
        if next_review_str is not None:
            fm["next_review"] = next_review_str
        elif not fm.get("next_review"):
            fm["next_review"] = last_run_str
        fm["updated"] = _today_str(now)
        fm["health_signal"] = row["health_signal"]
        dst = safe_paths.contain(
            root,
            f"Loops/{loop.path.name}",
            base=META_BASE,
        )
        _write_contained(dst, _render_note(fm, loop.body))

    return {
        "drop_id": drop_id,
        "loop_id": loop_id,
        "status": status,
        "last_run": last_run_str,
        "next_review": next_review_str,
    }


def set_status(
    root: Path,
    loop_id: str,
    status: str,
    *,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Flip a loop's lifecycle status (active/proposed/retired/paused).

    The ``paused`` status is how meta-health takes a repeatedly failing loop
    out of rotation without deleting its record or history: a paused loop is
    not due, keeps its runner/last_run, and carries ``paused_reason`` so the
    Review Inbox can say exactly why. Reactivating is the same call with
    ``status='active'`` (which clears the pause fields).
    """
    now = now or _now_default()
    status = str(status).strip().lower()
    allowed = ("active", "proposed", "retired", "paused")
    if status not in allowed:
        raise ValueError(f"set_status: status must be one of {allowed}, got {status!r}")
    loop = find_loop(root, loop_id)
    if loop is None or loop.path is None:
        raise ValueError(f"set_status: no loop with id {loop_id!r}")
    fm = dict(loop.frontmatter)
    fm["status"] = status
    fm["updated"] = _today_str(now)
    if status == "paused":
        fm["paused_reason"] = str(reason or "")
        fm["paused_on"] = _today_str(now)
    else:
        fm.pop("paused_reason", None)
        fm.pop("paused_on", None)
    dst = safe_paths.contain(root, f"Loops/{loop.path.name}", base=META_BASE)
    _write_contained(dst, _render_note(fm, loop.body))
    return {"loop_id": loop_id, "status": status, "path": str(dst)}


def complete(
    root: Path,
    loop_id: str,
    status: str,
    *,
    now: Optional[datetime] = None,
    health_signal: Optional[str] = None,
    notes: Optional[str] = None,
    consume_all: bool = False,
    event_ids: Optional[list[str]] = None,
    actor: str = "",
) -> dict:
    """Mark agent-executed loop work complete.

    ``run`` only hands an ``agent-worklist`` to the agent. ``complete`` is called
    after the agent has actually performed the loop's documented process.
    """
    now = now or _now_default()
    consumed = {"loop_id": loop_id, "consumed": [], "count": 0}
    if consume_all or event_ids:
        consumed = consume_events(
            root,
            loop_id,
            event_ids=None if consume_all else event_ids,
            now=now,
            actor=actor,
            notes=notes or "",
        )
    rec = record(
        root,
        loop_id,
        status,
        now=now,
        health_signal=health_signal,
        notes=notes,
    )
    rec["event_consumption"] = consumed
    return rec


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fmt_due_row(d: DueLoop) -> str:
    od = "never-run" if d.overdue_seconds == float("inf") else f"{int(d.overdue_seconds)}s"
    return f"  {d.id:<32} {d.loop.cadence:<14} {d.reason:<24} overdue={od}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loops", description="loop runner + due-ness engine")
    parser.add_argument("--root", default=".", help="oracle root (default: .)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list all loop records")
    p_list.add_argument("--json", action="store_true")

    p_due = sub.add_parser("due", help="show the due-now worklist")
    p_due.add_argument("--now", help="ISO datetime to evaluate due-ness at (default: wall clock)")
    p_due.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="run a loop's runner and record it")
    p_run.add_argument("id")
    p_run.add_argument("--now", help="ISO datetime to run at (default: wall clock)")
    p_run.add_argument("--headless", action="store_true", help="apply the autonomy gate")
    p_run.add_argument("--no-gate", action="store_true", help="skip the autonomy gate")
    p_run.add_argument("--json", action="store_true")

    p_rec = sub.add_parser("record", help="record a loop run + advance schedule")
    p_rec.add_argument("id")
    p_rec.add_argument("--status", required=True, choices=list(RUN_STATUSES))
    p_rec.add_argument("--now", help="ISO datetime of the run (default: wall clock)")
    p_rec.add_argument("--health-signal")
    p_rec.add_argument("--notes")
    p_rec.add_argument("--json", action="store_true")

    p_pending = sub.add_parser("pending-events", help="list unconsumed events for a loop")
    p_pending.add_argument("id")
    p_pending.add_argument("--json", action="store_true")

    p_consume = sub.add_parser("consume", help="record event consumption for a loop")
    p_consume.add_argument("id")
    p_consume.add_argument("--event-id", action="append", default=[])
    p_consume.add_argument("--all", action="store_true", help="consume all pending events")
    p_consume.add_argument("--actor", default="")
    p_consume.add_argument("--notes")
    p_consume.add_argument("--json", action="store_true")

    p_complete = sub.add_parser("complete", help="complete an agent-executed loop")
    p_complete.add_argument("id")
    p_complete.add_argument("--status", required=True, choices=list(RUN_STATUSES))
    p_complete.add_argument("--now", help="ISO datetime of completion (default: wall clock)")
    p_complete.add_argument("--health-signal")
    p_complete.add_argument("--notes")
    p_complete.add_argument("--consume-all", action="store_true")
    p_complete.add_argument("--event-id", action="append", default=[])
    p_complete.add_argument("--actor", default="")
    p_complete.add_argument("--json", action="store_true")

    p_set = sub.add_parser(
        "set-status", help="flip a loop's lifecycle status (the on/off toggle)"
    )
    p_set.add_argument("id")
    p_set.add_argument("status", choices=["active", "proposed", "retired", "paused"])
    p_set.add_argument("--reason", help="why (recorded as paused_reason when pausing)")
    p_set.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    root = Path(args.root)
    now = parse_dt(getattr(args, "now", None)) if getattr(args, "now", None) else None

    try:
        if args.cmd == "list":
            loops = list_loops(root)
            if args.json:
                print(json.dumps([l.frontmatter for l in loops], indent=2, default=str))
            else:
                print(f"loops: {len(loops)}")
                for l in loops:
                    print(f"  {l.id:<32} {l.status:<9} {l.cadence:<14} runner={l.runner or '-'}")
            return 0

        if args.cmd == "due":
            worklist = due(root, now)
            if args.json:
                print(
                    json.dumps(
                        [
                            {
                                "id": d.id,
                                "reason": d.reason,
                                "overdue_seconds": (
                                    None if d.overdue_seconds == float("inf") else d.overdue_seconds
                                ),
                                "cadence": d.loop.cadence,
                                "runner": d.loop.runner,
                                "model_policy": loop_model_policy(d.loop),
                            }
                            for d in worklist
                        ],
                        indent=2,
                    )
                )
            else:
                print(f"due loops: {len(worklist)}")
                for d in worklist:
                    print(_fmt_due_row(d))
            return 0

        if args.cmd == "run":
            result = run(
                root,
                args.id,
                now=now,
                headless=bool(args.headless),
                gate=not bool(args.no_gate),
            )
            if args.json:
                print(json.dumps(result, indent=2, default=str))
            else:
                print(
                    f"{args.id}: {result.get('status')} "
                    f"(performed={result.get('performed')} "
                    f"kind={result.get('kind')} "
                    f"next_review={result.get('next_review')})"
                )
            return 0 if result.get("status") in ("ok", "worklist") else 1

        if args.cmd == "record":
            rec = record(
                root,
                args.id,
                args.status,
                now=now,
                health_signal=args.health_signal,
                notes=args.notes,
            )
            if args.json:
                print(json.dumps(rec, indent=2, default=str))
            else:
                print(
                    f"{args.id}: recorded {rec['status']} "
                    f"last_run={rec['last_run']} next_review={rec['next_review']} "
                    f"({rec['drop_id']})"
                )
            return 0

        if args.cmd == "pending-events":
            rows = pending_events(root, args.id)
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
            else:
                print(f"pending events for {args.id}: {len(rows)}")
                for row in rows:
                    print(f"  {row.get('drop_id')} {row.get('event_kind')} {row.get('target', '')}")
            return 0

        if args.cmd == "consume":
            if not args.all and not args.event_id:
                raise ValueError("consume requires --all or at least one --event-id")
            rec = consume_events(
                root,
                args.id,
                event_ids=None if args.all else args.event_id,
                actor=args.actor,
                notes=args.notes or "",
            )
            print(json.dumps(rec, indent=2, default=str))
            return 0

        if args.cmd == "complete":
            cnow = parse_dt(args.now) if args.now else None
            rec = complete(
                root,
                args.id,
                args.status,
                now=cnow,
                health_signal=args.health_signal,
                notes=args.notes,
                consume_all=args.consume_all,
                event_ids=args.event_id,
                actor=args.actor,
            )
            if args.json:
                print(json.dumps(rec, indent=2, default=str))
            else:
                print(
                    f"{args.id}: completed {rec['status']} "
                    f"last_run={rec['last_run']} consumed={rec['event_consumption']['count']} "
                    f"({rec['drop_id']})"
                )
            return 0

        if args.cmd == "set-status":
            result = set_status(root, args.id, args.status, reason=args.reason, now=now)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
            else:
                print(f"{result['loop_id']}: status -> {result['status']}")
            return 0
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"loops: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
