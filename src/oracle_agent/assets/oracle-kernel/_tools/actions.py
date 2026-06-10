#!/usr/bin/env python3
"""actions.py -- the autonomous-action chokepoint (TIER 3, autonomy OFF by default).

This is the single gate every autonomous side effect MUST pass through. It is
the highest-blast-radius capability in the kernel, so it is defended in a strict,
fail-closed order. An action is GRANTED only if ALL of these hold, checked in
this exact sequence:

  1. KILL-SWITCH FIRST. If ``Meta.nosync/Autonomy/KILL-SWITCH`` (path read from
     autonomy.yml ``kill_switch_file``) exists, EVERYTHING is denied -- even when
     autonomy is otherwise enabled. The kill switch is a sovereign hard stop that
     a human can engage by simply creating a file; its presence short-circuits
     before any allowlist is consulted.

  2. AUTONOMY ENABLED. ``autonomy.yml`` must exist and carry ``enabled: true``.
     A missing/empty/false config means autonomy is OFF and every action is
     denied. This is the default a fresh spawn ships with.

  3. ADMIN ALLOWLIST. The action's ``loop`` (when the scope names one) must be in
     ``allowed_loops``; every writable lane the scope touches must be in
     ``writable_lanes``; every connector the scope touches must be in
     ``readonly_connectors``. Anything not explicitly allowlisted is denied
     (default-deny).

  4. BLAST-RADIUS CAPS. The scope's declared ``files`` / ``bytes`` must not
     exceed ``blast_radius_caps.max_files_per_run`` / ``max_bytes``. Over-cap is
     denied.

Every decision is logged to the ``action_event`` ledger. A granted action that
is actually carried out logs TWICE: once with ``phase: 'intended'`` (before the
side effect) and once with ``phase: 'actual'`` (after), so the ledger records
both what was authorized and what happened. A denial logs a single ``intended``
row with ``result: 'deny'`` and the reason.

action_event ledger shape (interface contract):
    {drop_id, ts, action, scope, phase:'intended'|'actual', caps, result}
written via ledger.append to ``Meta.nosync/ledgers/action_event.jsonl``.

autonomy.yml shape (consumed here; written block-style by spawn):
    enabled: false
    allowed_loops:            # block list (empty => bare key => None)
    writable_lanes:
    readonly_connectors:
    blast_radius_caps:
      max_files_per_run: 0
      max_bytes: 0
    kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"

Honesty note (mirrors GOVERNANCE.md): the ``--actor`` identity is a flag an
agent can set, so role enforcement here is advisory-plus-logged until session
context provides a trusted identity. It still routes through policy.require_role
so a configured ``cannot`` is honored and every decision is on the record.

Stdlib only. Imports floor siblings (ledger, oracle_yaml, optionally policy)
bare-or-as-package so it works both when tests inject ``_tools`` on sys.path and
when the kernel is imported as a package. NO raw filesystem-write primitives are
used here -- the kill switch is read with ``Path.exists()`` and every ledger row
goes through ``ledger.append`` (the durability chokepoint).
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional


# --------------------------------------------------------------------------- #
# sibling-import shim (works flat OR as a package)
# --------------------------------------------------------------------------- #
def _import_ledger():
    try:
        import ledger  # type: ignore
        return ledger
    except Exception:  # pragma: no cover - package fallback
        from . import ledger  # type: ignore
        return ledger


def _import_yaml():
    try:
        import oracle_yaml  # type: ignore
        return oracle_yaml
    except Exception:  # pragma: no cover - package fallback
        from . import oracle_yaml  # type: ignore
        return oracle_yaml


def _import_policy():
    """policy is optional here: a configured ``cannot`` is honored when present,
    but actions must still gate (fail-closed) even if policy cannot be imported
    in isolation. Returns the module or None."""
    try:
        import policy  # type: ignore
        return policy
    except Exception:
        try:  # pragma: no cover - package fallback
            from . import policy  # type: ignore
            return policy
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# constants / defaults
# --------------------------------------------------------------------------- #
DEFAULT_KILL_SWITCH = "Meta.nosync/Autonomy/KILL-SWITCH"
DEFAULT_CONFIG_REL = "Meta.nosync/Autonomy/autonomy.yml"
ACTION_LEDGER_REL = "Meta.nosync/ledgers/action_event.jsonl"
AUTONOMY_LEDGER_REL = "Meta.nosync/ledgers/autonomy_event.jsonl"
FAILURE_LEDGER_REL = "Meta.nosync/ledgers/failure_event.jsonl"

PHASE_INTENDED = "intended"
PHASE_ACTUAL = "actual"

RESULT_GRANT = "grant"
RESULT_DENY = "deny"
RESULT_OK = "ok"
RESULT_FAIL = "fail"

# ---- the graduated autonomy ladder (trust earned by ledger) ----------------
#
# level 0  nothing runs headless (the spawn default; explicit hand-written
#          allowlists with enabled:true keep working as "manual mode").
# level 1  the deterministic builtin loops below may run headless on schedule.
# level 2  + dream sessions (headless agent work on worklists; outputs land
#          status: needs_review and flow through the Review Inbox).
# level 3  + auto-apply for the enumerated low-risk improvement classes only.
#
# Promotion REQUIRES an evidence-cited proposal (drafted by meta-health when
# the criteria hold) plus admin approval (`enable_autonomy` capability).
# Demotion is automatic and fail-closed (enforce_demotion_policy). Truth
# authority, schema, doctrine, security policy, exports, connectors stay
# admin-only at EVERY level (the roles `cannot` list is level-invariant).
MAX_LEVEL = 3
DETERMINISTIC_LOOPS = (
    "memory-matriculation",
    "user-feedback-learning",
    "skill-repository-learning",
    "insight-synthesis",
    "leadership-briefing",
    "value-scorecard",
    "improvement-lifecycle",
    "meta-health",
    "stale-finding-refresh",
    "architecture-retrospective",
)
ACTION_DREAM = "dream.session"
ACTION_IMPROVEMENT_APPLY = "improvement.apply"
# Caps written by `promote` when the admin has not configured any: a promoted
# level with zero caps would deny every loop run (files=1 > 0), which is the
# right default for hand-edits but a footgun for the one-command ladder.
PROMOTED_DEFAULT_CAPS = {"max_files_per_run": 50, "max_bytes": 10_000_000}
# Promotion readiness windows.
READINESS_WINDOW_DAYS = 30
READINESS_SCORECARDS = 2


class ActionDenied(PermissionError):
    """Raised when an autonomous action is refused by the chokepoint.

    Subclass of PermissionError so callers may catch either. Carries the
    machine-readable ``reason`` and the (possibly partial) ``decision`` dict.
    """

    def __init__(self, reason: str, decision: Optional[dict] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision = decision or {}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_list(value: Any) -> list[str]:
    """Normalize a YAML block list that may be None (empty 'key:') or a scalar.

    The oracle_yaml loader parses an empty ``key:`` to None and a populated
    block list to a Python list; we coerce both, and a stray scalar, into a
    clean list of strings.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if x is not None and str(x) != ""]
    return [str(value)]


def _as_bool(value: Any) -> bool:
    """Interpret a YAML scalar as a strict boolean. Only a real ``true`` (or the
    Python bool ``True``) enables autonomy; anything else -- None, '', 'false',
    'no', 0, a typo -- is OFF. Fail-closed by construction."""
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in ("true", "yes", "1", "on", "enabled")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# autonomy config
# --------------------------------------------------------------------------- #
@dataclass
class Autonomy:
    """Parsed, normalized view of ``Meta.nosync/Autonomy/autonomy.yml``.

    A missing or unparseable config yields ``enabled=False`` with empty
    allowlists and zero caps -- i.e. autonomy OFF, every action denied. This is
    the safe default a fresh spawn ships and the behavior if the file is
    corrupted.
    """

    enabled: bool = False
    level: int = 0
    allowed_loops: list[str] = field(default_factory=list)
    writable_lanes: list[str] = field(default_factory=list)
    readonly_connectors: list[str] = field(default_factory=list)
    max_files_per_run: int = 0
    max_bytes: int = 0
    kill_switch_file: str = DEFAULT_KILL_SWITCH
    dream: dict = field(default_factory=dict)
    source: str = "default-off"

    @classmethod
    def load(cls, root: Path, *, config_rel: str = DEFAULT_CONFIG_REL) -> "Autonomy":
        root = Path(root)
        cfg_path = root / config_rel
        if not cfg_path.exists():
            return cls(source="missing-config")
        try:
            yaml_mod = _import_yaml()
            data = yaml_mod.safe_load(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            # A malformed autonomy.yml must never *enable* autonomy. Fail closed.
            return cls(source="unparseable-config")
        if not isinstance(data, dict):
            return cls(source="non-mapping-config")
        caps = data.get("blast_radius_caps") or {}
        if not isinstance(caps, dict):
            caps = {}
        dream = data.get("dream") or {}
        if not isinstance(dream, dict):
            dream = {}
        level = _as_int(data.get("level"), 0)
        return cls(
            enabled=_as_bool(data.get("enabled")),
            level=max(0, min(MAX_LEVEL, level)),
            allowed_loops=_as_list(data.get("allowed_loops")),
            writable_lanes=_as_list(data.get("writable_lanes")),
            readonly_connectors=_as_list(data.get("readonly_connectors")),
            max_files_per_run=_as_int(caps.get("max_files_per_run"), 0),
            max_bytes=_as_int(caps.get("max_bytes"), 0),
            kill_switch_file=str(data.get("kill_switch_file") or DEFAULT_KILL_SWITCH),
            dream=dream,
            source=str(cfg_path),
        )

    def caps_dict(self) -> dict:
        return {
            "max_files_per_run": self.max_files_per_run,
            "max_bytes": self.max_bytes,
        }

    def effective_allowed_loops(self) -> list[str]:
        """Explicit allowlist UNION the level preset (level >= 1 admits the
        deterministic builtin loops; explicit admin entries always count)."""
        out = list(self.allowed_loops)
        if self.level >= 1:
            for lid in DETERMINISTIC_LOOPS:
                if lid not in out:
                    out.append(lid)
        return out


# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #
@dataclass
class Scope:
    """What an autonomous action intends to touch.

    All fields are optional; an empty scope (no loop/lanes/connectors and zero
    files/bytes) describes a no-op read-style action. The gate checks every
    populated field against the allowlist + caps.
    """

    loop: Optional[str] = None
    lanes: list[str] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)
    files: int = 0
    bytes: int = 0
    actor: str = "unknown"
    role: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "loop": self.loop,
            "lanes": list(self.lanes),
            "connectors": list(self.connectors),
            "files": int(self.files),
            "bytes": int(self.bytes),
            "actor": self.actor,
            "role": self.role,
        }


def _coerce_scope(scope: Any) -> Scope:
    if isinstance(scope, Scope):
        return scope
    if scope is None:
        return Scope()
    if isinstance(scope, dict):
        # Accept both this module's ``loop`` key and the sibling loops.py
        # convention ``loop_id`` for naming the loop a scope belongs to.
        loop = scope.get("loop")
        if loop is None:
            loop = scope.get("loop_id")
        lanes = scope.get("lanes")
        if lanes is None:
            lanes = scope.get("writable_lanes")
        connectors = _as_list(scope.get("connectors"))
        readonly_connectors = _as_list(scope.get("readonly_connectors"))
        readonly_connector = _as_list(scope.get("readonly_connector"))
        for conn in readonly_connectors + readonly_connector:
            if conn not in connectors:
                connectors.append(conn)
        return Scope(
            loop=loop,
            lanes=_as_list(lanes),
            connectors=connectors,
            files=_as_int(scope.get("files"), 0),
            bytes=_as_int(scope.get("bytes"), 0),
            actor=str(scope.get("actor") or "unknown"),
            role=str(scope.get("role") or "unknown"),
        )
    raise TypeError(f"actions: cannot coerce scope of type {type(scope)!r}")


def _looks_like_root(value: Any) -> bool:
    """Heuristic: does ``value`` look like an oracle root rather than an action
    name? A Path, or a string that is an existing directory / contains a path
    separator, is treated as a root. Used to accept the alternate positional
    call convention ``authorize(root, action, scope)`` that the sibling
    loops.py uses, without breaking ``authorize(action, scope, root=...)``.
    """
    if isinstance(value, Path):
        return True
    if isinstance(value, str):
        if os_sep_in(value):
            return True
        try:
            return Path(value).is_dir()
        except OSError:
            return False
    return False


def os_sep_in(value: str) -> bool:
    import os as _os
    return (_os.sep in value) or (_os.altsep is not None and _os.altsep in value)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def kill_switch_engaged(root: Path, autonomy: Optional[Autonomy] = None) -> bool:
    """True iff the kill-switch sentinel file exists.

    Read-only check (``Path.exists``); engaging the kill switch is as simple as
    creating the file, and its presence is the FIRST thing every gate consults.
    """
    root = Path(root)
    autonomy = autonomy or Autonomy.load(root)
    ks = root / autonomy.kill_switch_file
    return ks.exists()


def authorize(*args: Any, root: Optional[Path] = None,
              autonomy: Optional[Autonomy] = None) -> dict:
    """Decide whether an ``action`` over a ``scope`` is permitted. PURE: no
    logging, no side effects -- just the verdict, in the fail-closed order
    documented at module top. Returns a decision dict::

        {action, scope, result:'grant'|'deny', allowed:bool, granted:bool,
         reason, caps, phase:'intended'}

    Call conventions accepted:
      * ``authorize(action, scope, root=ROOT)``  -- the binding contract.
      * ``authorize(root, action, scope)``       -- root-first positional, used
        by some sibling callers (e.g. loops.py). Detected when the first
        positional looks like an oracle root and no keyword ``root`` was given.

    The ``allowed`` / ``granted`` booleans mirror ``result == 'grant'`` so a
    caller that inspects either key (the sibling gate looks for ``allowed`` /
    ``granted``) gets a consistent verdict.

    ``guard`` / ``with_action`` wrap this with ledger logging; call ``authorize``
    directly when you only need the verdict (e.g. a dry-run report).
    """
    action: Any = None
    scope: Any = None
    if root is None and len(args) == 3 and _looks_like_root(args[0]):
        # authorize(root, action, scope)
        root, action, scope = args[0], args[1], args[2]
    elif len(args) >= 2:
        # authorize(action, scope[, ...])
        action, scope = args[0], args[1]
    elif len(args) == 1:
        action, scope = args[0], None
    else:
        raise TypeError("authorize: expected (action, scope) or (root, action, scope)")

    if root is None:
        raise TypeError("authorize: 'root' is required (pass root=ORACLE_ROOT)")

    root = Path(root)
    autonomy = autonomy or Autonomy.load(root)
    sc = _coerce_scope(scope)

    decision: dict[str, Any] = {
        "action": str(action),
        "scope": sc.to_dict(),
        "phase": PHASE_INTENDED,
        "caps": autonomy.caps_dict(),
        "result": RESULT_DENY,
        "allowed": False,
        "granted": False,
        "reason": "",
    }

    # 1) KILL-SWITCH FIRST -- sovereign hard stop, before anything else.
    if kill_switch_engaged(root, autonomy):
        decision["reason"] = "kill-switch-engaged"
        return decision

    # 2) AUTONOMY ENABLED -- default OFF denies everything.
    if not autonomy.enabled:
        decision["reason"] = f"autonomy-disabled ({autonomy.source})"
        return decision

    # 2b) LEVEL-GATED ACTION KINDS -- the graduated ladder's new capabilities
    # exist only above their level, no matter what the allowlists say.
    if str(action) == ACTION_DREAM and autonomy.level < 2:
        decision["reason"] = f"dream sessions require autonomy level 2 (level={autonomy.level})"
        return decision
    if str(action) == ACTION_IMPROVEMENT_APPLY and autonomy.level < 3:
        decision["reason"] = (
            f"autonomous improvement application requires autonomy level 3 (level={autonomy.level})"
        )
        return decision

    # 3) ADMIN ALLOWLIST -- default-deny on anything not explicitly listed.
    # (level >= 1 admits the deterministic builtin loops as a preset; explicit
    # admin entries always count -- see Autonomy.effective_allowed_loops.)
    if sc.loop is not None and sc.loop not in autonomy.effective_allowed_loops():
        decision["reason"] = f"loop {sc.loop!r} not in allowed_loops"
        return decision
    for lane in sc.lanes:
        if lane not in autonomy.writable_lanes:
            decision["reason"] = f"lane {lane!r} not in writable_lanes"
            return decision
    for conn in sc.connectors:
        if conn not in autonomy.readonly_connectors:
            decision["reason"] = f"connector {conn!r} not in readonly_connectors"
            return decision

    # 4) BLAST-RADIUS CAPS -- over-cap is denied.
    if sc.files > autonomy.max_files_per_run:
        decision["reason"] = (
            f"files {sc.files} exceeds max_files_per_run "
            f"{autonomy.max_files_per_run}"
        )
        return decision
    if sc.bytes > autonomy.max_bytes:
        decision["reason"] = (
            f"bytes {sc.bytes} exceeds max_bytes {autonomy.max_bytes}"
        )
        return decision

    # 5) ROLE GATE (advisory-plus-logged) -- honor a configured ``cannot``.
    policy = _import_policy()
    if policy is not None and sc.actor and sc.role and sc.role != "unknown":
        try:
            policy.require_role(sc.actor, sc.role, "act_autonomously", root=root)
        except PermissionError as exc:
            decision["reason"] = f"role-denied: {exc}"
            return decision
        except Exception:
            # Missing capability wiring must not silently grant; but a benign
            # role-config absence should not block when the role is plausible.
            # We only HARD-deny on an explicit PermissionError above.
            pass

    decision["result"] = RESULT_GRANT
    decision["allowed"] = True
    decision["granted"] = True
    decision["reason"] = "allowlisted-within-caps"
    return decision


def _ledger_path(root: Path) -> Path:
    return Path(root) / ACTION_LEDGER_REL


def log_action_event(root: Path, *, action: str, scope: dict, phase: str,
                     caps: dict, result: str, reason: str = "") -> str:
    """Append one ``action_event`` row and return its drop_id.

    Row shape (interface contract): {drop_id, ts, action, scope, phase, caps,
    result}. ``reason`` is carried as an extra field for forensics. Written via
    ledger.append -- the durability chokepoint -- never raw file I/O.
    """
    ledger = _import_ledger()
    row = {
        "action": str(action),
        "scope": scope,
        "phase": str(phase),
        "caps": caps,
        "result": str(result),
        "reason": str(reason or ""),
    }
    return ledger.append(_ledger_path(root), row, id_prefix="ACT")


def guard(action: str, scope: Any, *, root: Path,
          autonomy: Optional[Autonomy] = None) -> dict:
    """Authorize ``action`` AND log the ``intended`` decision.

    Returns the decision dict (with ``drop_id`` of the logged intended row). On a
    DENY this logs a single intended row with ``result: 'deny'`` and RAISES
    ``ActionDenied`` so a caller that forgets to check the verdict still cannot
    proceed. On a GRANT it logs the intended row and returns the decision; the
    caller is then expected to perform the side effect and log the ``actual``
    phase (``with_action`` does this automatically).
    """
    root = Path(root)
    decision = authorize(action, scope, root=root, autonomy=autonomy)
    drop_id = log_action_event(
        root,
        action=decision["action"],
        scope=decision["scope"],
        phase=PHASE_INTENDED,
        caps=decision["caps"],
        result=decision["result"],
        reason=decision["reason"],
    )
    decision["drop_id"] = drop_id
    if decision["result"] != RESULT_GRANT:
        raise ActionDenied(decision["reason"], decision)
    return decision


@contextmanager
def with_action(action: str, scope: Any, *, root: Path,
                autonomy: Optional[Autonomy] = None):
    """Context manager wrapping an autonomous side effect in the full gate.

    On enter: runs ``guard`` (kill-switch -> enabled -> allowlist -> caps ->
    role), logging the ``intended`` row. If denied, ``ActionDenied`` is raised
    BEFORE the body runs, so no side effect occurs. On exit: logs an ``actual``
    row -- ``result: 'ok'`` if the body completed, ``result: 'fail'`` (with the
    exception text) if it raised -- so the ledger records both what was
    authorized and what actually happened.

    Usage::

        with actions.with_action("connector-pull",
                                 {"loop": "connector-health",
                                  "connectors": ["localfolder"], "files": 3},
                                 root=root):
            do_the_pull()
    """
    root = Path(root)
    decision = guard(action, scope, root=root, autonomy=autonomy)
    try:
        yield decision
    except BaseException as exc:  # log the failure, then re-raise
        log_action_event(
            root,
            action=decision["action"],
            scope=decision["scope"],
            phase=PHASE_ACTUAL,
            caps=decision["caps"],
            result=RESULT_FAIL,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        log_action_event(
            root,
            action=decision["action"],
            scope=decision["scope"],
            phase=PHASE_ACTUAL,
            caps=decision["caps"],
            result=RESULT_OK,
            reason="completed",
        )


# --------------------------------------------------------------------------- #
# the graduated ladder: propose -> promote (admin) / demote (automatic)
# --------------------------------------------------------------------------- #
def _autonomy_ledger_path(root: Path) -> Path:
    return Path(root) / AUTONOMY_LEDGER_REL


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip().replace("Z", "").replace("z", "")
    if not s:
        return None
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _autonomy_events(root: Path) -> list[dict]:
    ledger = _import_ledger()
    rows, _w = ledger.load(_autonomy_ledger_path(root))
    rows.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("drop_id", ""))))
    return rows


def _last_transition_ts(root: Path) -> Optional[datetime]:
    """ts of the most recent promote/demote (the demotion-watermark)."""
    out = None
    for row in _autonomy_events(root):
        if str(row.get("action")) in ("promote", "demote"):
            ts = _parse_ts(row.get("ts"))
            if ts is not None and (out is None or ts > out):
                out = ts
    return out


def pending_proposal(root: Path, *, to_level: Optional[int] = None) -> Optional[dict]:
    """The open promotion proposal (newer than the last promote/demote), or None."""
    autonomy = Autonomy.load(root)
    want = to_level if to_level is not None else autonomy.level + 1
    watermark = _last_transition_ts(root)
    best = None
    for row in _autonomy_events(root):
        if str(row.get("action")) != "propose":
            continue
        if _as_int(row.get("to_level"), -1) != want:
            continue
        ts = _parse_ts(row.get("ts"))
        if watermark is not None and (ts is None or ts <= watermark):
            continue
        best = row
    return best


def propose_promotion(root: Path, *, to_level: int, evidence: Optional[list] = None,
                      reason: str = "", actor: str = "", now: Optional[datetime] = None) -> dict:
    """Record an evidence-cited promotion proposal (drafted, never self-applied).

    Deduplicates: one open proposal per target level. The proposal is a ledger
    row (metadata only); the admin approves it with ``promote``.
    """
    root = Path(root)
    autonomy = Autonomy.load(root)
    to_level = max(0, min(MAX_LEVEL, int(to_level)))
    if to_level <= autonomy.level:
        return {"proposed": False, "reason": f"already at level {autonomy.level}"}
    existing = pending_proposal(root, to_level=to_level)
    if existing is not None:
        return {"proposed": False, "reason": "already-proposed", "drop_id": existing.get("drop_id")}
    ledger = _import_ledger()
    row = {
        "kind": "autonomy_event",
        "action": "propose",
        "from_level": autonomy.level,
        "to_level": to_level,
        "actor": str(actor or ""),
        "reason": str(reason or ""),
        "evidence": [str(e) for e in (evidence or [])],
    }
    if now is not None:
        row["ts"] = now.isoformat(timespec="seconds")
    drop_id = ledger.append(_autonomy_ledger_path(root), row, id_prefix="AUTO")
    return {"proposed": True, "drop_id": drop_id, "to_level": to_level}


def promotion_readiness(root: Path, *, now: Optional[datetime] = None) -> dict:
    """Decide -- from ledgers alone -- whether the next level is earned.

    Criteria (every one cited): at least READINESS_SCORECARDS scorecards with
    the most recent ones non-regressing; zero critical failure_events and zero
    cap/containment violations in the readiness window. Returns
    {ready, to_level, reason, evidence}.
    """
    root = Path(root)
    now = now or datetime.now()
    autonomy = Autonomy.load(root)
    if autonomy.level >= MAX_LEVEL:
        return {"ready": False, "reason": f"already at max level {MAX_LEVEL}"}
    try:
        import scorecard as _sc  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import scorecard as _sc  # type: ignore
        except Exception:
            return {"ready": False, "reason": "scorecard module unavailable"}
    cards = _sc.load_scorecards(root)
    if len(cards) < READINESS_SCORECARDS:
        return {
            "ready": False,
            "reason": f"need {READINESS_SCORECARDS} scorecards, have {len(cards)}",
        }
    recent = cards[-READINESS_SCORECARDS:]
    if any(str(c.get("trend")) == "regressing" for c in recent):
        return {"ready": False, "reason": "a recent scorecard is regressing"}
    ledger = _import_ledger()
    window_start = now - timedelta(days=READINESS_WINDOW_DAYS)
    fail_rows, _w = ledger.load(Path(root) / FAILURE_LEDGER_REL)
    for row in fail_rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts <= window_start:
            continue
        if str(row.get("severity", "")).strip().lower() == "critical":
            return {
                "ready": False,
                "reason": f"critical failure {row.get('drop_id')} in the window",
            }
    act_rows, _w = ledger.load(_ledger_path(root))
    for row in act_rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts <= window_start:
            continue
        reason = str(row.get("reason", ""))
        if str(row.get("result")) == RESULT_DENY and (
            reason.startswith("files ") or reason.startswith("bytes ") or reason.startswith("lane ")
        ):
            return {"ready": False, "reason": f"cap/lane violation {row.get('drop_id')} in the window"}
        if str(row.get("phase")) == PHASE_ACTUAL and str(row.get("result")) == RESULT_FAIL:
            return {"ready": False, "reason": f"granted action failed ({row.get('drop_id')}) in the window"}
    evidence = [str(c.get("id", "")) for c in recent if c.get("id")]
    return {
        "ready": True,
        "to_level": autonomy.level + 1,
        "reason": (
            f"{READINESS_SCORECARDS} non-regressing scorecard(s), zero critical "
            f"failures and zero cap/containment violations in {READINESS_WINDOW_DAYS}d"
        ),
        "evidence": evidence,
    }


def _render_autonomy_yml(data: dict) -> str:
    """Render the autonomy config back to the strict block-style subset."""
    lines: list[str] = [
        "# Autonomy posture -- managed by `./oracle admin autonomy promote|demote`.",
        "# Level ladder: 0 none / 1 deterministic loops / 2 + dream sessions /",
        "# 3 + enumerated auto-apply. Kill switch overrides everything.",
        f"enabled: {'true' if data.get('enabled') else 'false'}",
        f"level: {int(data.get('level', 0))}",
    ]
    for key in ("allowed_loops", "writable_lanes", "readonly_connectors"):
        vals = data.get(key) or []
        lines.append(f"{key}:")
        for v in vals:
            lines.append(f"  - {v}")
    caps = data.get("blast_radius_caps") or {}
    lines.append("blast_radius_caps:")
    lines.append(f"  max_files_per_run: {int(caps.get('max_files_per_run', 0))}")
    lines.append(f"  max_bytes: {int(caps.get('max_bytes', 0))}")
    lines.append(f"kill_switch_file: \"{data.get('kill_switch_file') or DEFAULT_KILL_SWITCH}\"")
    dream = data.get("dream") or {}
    lines.append("dream:")
    if dream.get("command"):
        lines.append(f"  command: \"{dream.get('command')}\"")
    else:
        lines.append("  command:")
    lines.append(f"  max_minutes: {int(_as_int(dream.get('max_minutes'), 30))}")
    lines.append(f"  max_inbox_items: {int(_as_int(dream.get('max_inbox_items'), 10))}")
    return "\n".join(lines) + "\n"


def _write_autonomy_yml(root: Path, autonomy: Autonomy, *, level: int,
                        enabled: Optional[bool] = None) -> Path:
    """Rewrite autonomy.yml with the new level (contained, atomic)."""
    import os as _os
    import tempfile as _tempfile

    try:
        import safe_paths  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        from . import safe_paths  # type: ignore

    caps = autonomy.caps_dict()
    if level >= 1 and caps.get("max_files_per_run", 0) <= 0 and caps.get("max_bytes", 0) <= 0:
        caps = dict(PROMOTED_DEFAULT_CAPS)
    data = {
        "enabled": (level >= 1) if enabled is None else bool(enabled),
        "level": level,
        "allowed_loops": autonomy.allowed_loops,
        "writable_lanes": autonomy.writable_lanes,
        "readonly_connectors": autonomy.readonly_connectors,
        "blast_radius_caps": caps,
        "kill_switch_file": autonomy.kill_switch_file,
        "dream": autonomy.dream,
    }
    dst = safe_paths.contain(Path(root), "Autonomy/autonomy.yml", base="Meta.nosync")
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:  # contained dst (safe_paths.contain)
            f.write(_render_autonomy_yml(data))
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, str(dst))  # atomic config swap on contained path
    except BaseException:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise
    return dst


def promote(root: Path, *, actor: str, role: str = "admin",
            now: Optional[datetime] = None) -> dict:
    """Apply the pending promotion proposal: one admin command, evidence first.

    Fail-closed in order: role gate (``enable_autonomy`` via
    ``policy.require_role`` -- and a missing policy module DENIES, it does not
    wave through), then proposal gate (no evidence-cited proposal for
    current+1 => refuse; ``actions.py`` never grants a level nobody earned).
    """
    root = Path(root)
    policy = _import_policy()
    if policy is None:
        raise ActionDenied("policy module unavailable; cannot verify enable_autonomy")
    policy.require_role(actor, role, "enable_autonomy", root=root)

    autonomy = Autonomy.load(root)
    if autonomy.level >= MAX_LEVEL:
        raise ActionDenied(f"already at max level {MAX_LEVEL}")
    proposal = pending_proposal(root, to_level=autonomy.level + 1)
    if proposal is None:
        raise ActionDenied(
            f"no evidence-cited proposal for level {autonomy.level + 1}; "
            "promotion is earned (meta-health drafts it when the criteria hold) "
            "or proposed explicitly via `autonomy propose`"
        )
    new_level = autonomy.level + 1
    _write_autonomy_yml(root, autonomy, level=new_level)
    ledger = _import_ledger()
    row = {
        "kind": "autonomy_event",
        "action": "promote",
        "from_level": autonomy.level,
        "to_level": new_level,
        "actor": str(actor),
        "reason": f"approved proposal {proposal.get('drop_id')}",
        "evidence": proposal.get("evidence") or [],
    }
    if now is not None:
        row["ts"] = now.isoformat(timespec="seconds")
    drop_id = ledger.append(_autonomy_ledger_path(root), row, id_prefix="AUTO")
    return {"level": new_level, "drop_id": drop_id, "proposal": proposal.get("drop_id")}


def demote(root: Path, *, reason: str, actor: str = "actions",
           evidence: Optional[list] = None, to_level: Optional[int] = None,
           now: Optional[datetime] = None) -> dict:
    """Drop the autonomy level (fail-closed; level 0 also disables ``enabled``)."""
    root = Path(root)
    autonomy = Autonomy.load(root)
    new_level = max(0, autonomy.level - 1) if to_level is None else max(0, min(MAX_LEVEL, int(to_level)))
    if new_level >= autonomy.level:
        return {"demoted": False, "level": autonomy.level, "reason": "no lower level to drop to"}
    _write_autonomy_yml(root, autonomy, level=new_level,
                        enabled=False if new_level == 0 else None)
    ledger = _import_ledger()
    row = {
        "kind": "autonomy_event",
        "action": "demote",
        "from_level": autonomy.level,
        "to_level": new_level,
        "actor": str(actor),
        "reason": str(reason),
        "evidence": [str(e) for e in (evidence or [])],
    }
    if now is not None:
        row["ts"] = now.isoformat(timespec="seconds")
    drop_id = ledger.append(_autonomy_ledger_path(root), row, id_prefix="AUTO")
    return {"demoted": True, "level": new_level, "drop_id": drop_id}


def enforce_demotion_policy(root: Path, *, now: Optional[datetime] = None) -> Optional[dict]:
    """Automatic, fail-closed demotion: a critical failure_event, a blast-cap
    breach, or a granted-then-failed action newer than the last level
    transition drops the level by one. One step per sweep; the evidence is
    cited in the demotion row and the Review Inbox surfaces it. Called from
    the harness, from meta-health, and from capture on critical failures."""
    root = Path(root)
    autonomy = Autonomy.load(root)
    if autonomy.level <= 0:
        return None
    watermark = _last_transition_ts(root)
    triggers: list[str] = []
    ledger = _import_ledger()
    fail_rows, _w = ledger.load(Path(root) / FAILURE_LEDGER_REL)
    for row in fail_rows:
        ts = _parse_ts(row.get("ts"))
        if watermark is not None and (ts is None or ts <= watermark):
            continue
        if str(row.get("severity", "")).strip().lower() == "critical":
            triggers.append(str(row.get("drop_id", "")))
    act_rows, _w = ledger.load(_ledger_path(root))
    for row in act_rows:
        ts = _parse_ts(row.get("ts"))
        if watermark is not None and (ts is None or ts <= watermark):
            continue
        reason = str(row.get("reason", ""))
        if str(row.get("result")) == RESULT_DENY and (
            reason.startswith("files ") or reason.startswith("bytes ")
        ):
            triggers.append(str(row.get("drop_id", "")))
        elif str(row.get("phase")) == PHASE_ACTUAL and str(row.get("result")) == RESULT_FAIL:
            triggers.append(str(row.get("drop_id", "")))
    if not triggers:
        return None
    return demote(
        root,
        reason="automatic fail-closed demotion (critical failure / cap breach / failed grant)",
        actor="actions:enforce_demotion_policy",
        evidence=triggers[:10],
        now=now,
    )


# --------------------------------------------------------------------------- #
# status / inspection
# --------------------------------------------------------------------------- #
def status(root: Path) -> dict:
    """A human-readable snapshot of the autonomy posture."""
    root = Path(root)
    autonomy = Autonomy.load(root)
    proposal = pending_proposal(root)
    return {
        "enabled": autonomy.enabled,
        "level": autonomy.level,
        "kill_switch_engaged": kill_switch_engaged(root, autonomy),
        "kill_switch_file": autonomy.kill_switch_file,
        "allowed_loops": autonomy.allowed_loops,
        "effective_allowed_loops": autonomy.effective_allowed_loops(),
        "writable_lanes": autonomy.writable_lanes,
        "readonly_connectors": autonomy.readonly_connectors,
        "blast_radius_caps": autonomy.caps_dict(),
        "pending_proposal": proposal,
        "config_source": autonomy.source,
        "action_ledger": str(_ledger_path(root)),
    }


def recent_events(root: Path, limit: int = 20) -> list[dict]:
    ledger = _import_ledger()
    rows, _ = ledger.load(_ledger_path(root))
    return rows[-limit:]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Oracle autonomous-action chokepoint (kill-switch first, "
        "allowlist + blast-radius caps, action_event logged)."
    )
    parser.add_argument("--root", default=".", help="oracle root")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show autonomy posture")

    p_log = sub.add_parser("log", help="render the action_event ledger")
    p_log.add_argument("--limit", type=int, default=20)

    p_auth = sub.add_parser(
        "authorize", help="dry-run a verdict (no logging, no side effect)"
    )
    p_auth.add_argument("--action", required=True)
    p_auth.add_argument("--loop", default=None)
    p_auth.add_argument("--lane", action="append", default=[], dest="lanes")
    p_auth.add_argument(
        "--connector", action="append", default=[], dest="connectors"
    )
    p_auth.add_argument("--files", type=int, default=0)
    p_auth.add_argument("--bytes", type=int, default=0)
    p_auth.add_argument("--actor", default="cli")
    p_auth.add_argument("--role", default="unknown")

    p_kill = sub.add_parser("kill", help="check whether the kill switch is engaged")
    sub.add_parser("resume", help="report how to disengage the kill switch")

    sub.add_parser("readiness", help="evaluate promotion readiness from ledgers")

    p_propose = sub.add_parser("propose", help="record a promotion proposal explicitly")
    p_propose.add_argument("--to-level", type=int, required=True)
    p_propose.add_argument("--reason", default="")
    p_propose.add_argument("--actor", default="cli")

    p_promote = sub.add_parser(
        "promote", help="apply the pending promotion proposal (admin: enable_autonomy)"
    )
    p_promote.add_argument("--actor", required=True)
    p_promote.add_argument("--role", default="admin")

    p_demote = sub.add_parser("demote", help="drop the autonomy level by one")
    p_demote.add_argument("--reason", required=True)
    p_demote.add_argument("--actor", default="cli")

    args = parser.parse_args(argv)
    root = Path(args.root)

    if args.cmd == "status":
        print(json.dumps(status(root), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "log":
        events = recent_events(root, limit=args.limit)
        print(json.dumps(events, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "authorize":
        scope = Scope(
            loop=args.loop,
            lanes=list(args.lanes),
            connectors=list(args.connectors),
            files=args.files,
            bytes=args.bytes,
            actor=args.actor,
            role=args.role,
        )
        decision = authorize(args.action, scope, root=root)
        print(json.dumps(decision, indent=2, ensure_ascii=False))
        return 0 if decision["result"] == RESULT_GRANT else 2

    if args.cmd == "kill":
        engaged = kill_switch_engaged(root)
        print("ENGAGED" if engaged else "clear")
        return 0 if engaged else 1

    if args.cmd == "resume":
        autonomy = Autonomy.load(root)
        ks = root / autonomy.kill_switch_file
        print(
            "To resume autonomy, remove the kill-switch sentinel:\n"
            f"  {ks}\n"
            "Autonomy also requires 'enabled: true' in:\n"
            f"  {root / DEFAULT_CONFIG_REL}",
        )
        return 0

    if args.cmd == "readiness":
        print(json.dumps(promotion_readiness(root), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "propose":
        res = propose_promotion(
            root, to_level=args.to_level, reason=args.reason, actor=args.actor
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("proposed") else 1

    if args.cmd == "promote":
        try:
            res = promote(root, actor=args.actor, role=args.role)
        except (ActionDenied, PermissionError) as exc:
            print(f"promote: DENIED -- {exc}", file=sys.stderr)
            return 2
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "demote":
        res = demote(root, reason=args.reason, actor=args.actor)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
