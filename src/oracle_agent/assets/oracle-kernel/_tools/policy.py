#!/usr/bin/env python3
"""policy.py -- the processing / export / role decision gate.

This is the FLOOR policy chokepoint (stdlib-only). Every place that wants to
process material in a given environment, export an artifact, or perform a
role-gated capability routes the decision through here so the verdict is one
machine-checked decision table rather than scattered prose. It is the enforcer
named by SECURITY.md / PROCESSING-MATRIX.md / GOVERNANCE.md.

Public API (binding interface contract -- "policy API + CLI"):

    check_processing(sensitivity, environment) -> 'allow' | 'allow-minimized' | 'deny'
        environment in {local_deterministic, local_agent, external}.
        Mirrors PROCESSING-MATRIX.md exactly; stricter-row-wins on any
        uncertainty (unknown sensitivity collapses to the strictest row).

    gate_export(sensitivity, approval, actor, role, *, root=..., destination=...,
                classification=..., purpose=...) -> dict
        Raises PermissionError WITHOUT admin approval for confidential /
        restricted / secret. On success appends an export_event row and returns
        it. public / internal need no approval.

    require_role(actor, role, capability, *, root=...) -> None
        Reads oracle.yml governance.roles. Raises PermissionError if the role
        cannot perform the capability (explicit `cannot` list, or absence from
        the role's `can` list). Admin is permitted any capability.

    record_redaction(actor, reason, action, *, root=..., approved_by=...,
                     stub_location=...) -> dict
        Appends a redaction_event row and returns it.

Ledgers are written through ledger.append to
``Meta.nosync/ledgers/export_event.jsonl`` and
``Meta.nosync/ledgers/redaction_event.jsonl`` -- metadata only, NEVER payloads.

Stdlib only. Imports floor siblings ledger + oracle_yaml lazily/bare so it works
both as a flat module (tests inject _tools on sys.path) and as a package.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

__all__ = [
    "check_processing",
    "gate_export",
    "require_role",
    "record_redaction",
    "PolicyError",
    "PROCESSING_MATRIX",
    "SENSITIVITY_ORDER",
    "ENVIRONMENTS",
]


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


class PolicyError(PermissionError):
    """A policy refusal. Subclass of PermissionError so callers can catch either."""


# --------------------------------------------------------------------------- #
# the decision table -- this IS PROCESSING-MATRIX.md, in code.
# --------------------------------------------------------------------------- #
# Sensitivity ordered least -> most sensitive. Index is the strictness rank;
# "if uncertain, choose the stricter row" => take the MAX rank.
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted", "secret"]

ENVIRONMENTS = ("local_deterministic", "local_agent", "external")

# Verdicts allowed by contract.
_ALLOW = "allow"
_ALLOW_MIN = "allow-minimized"
_DENY = "deny"

# Row per sensitivity -> column per environment.
# Mirrors PROCESSING-MATRIX.md:
#   | public       | allowed                  | allowed                 | allowed (unless admin restricts) |
#   | internal     | allowed                  | allowed                 | denied (admin approval required) |
#   | confidential | allowed                  | allowed with minimization | denied unless approved         |
#   | restricted   | allowed with minimization| allowed with minimization | denied by default              |
#   | secret       | minimized only           | minimized only          | denied                          |
#
# The "external" column collapses "approval required / denied unless approved /
# denied by default" to deny at the *automatic* decision layer; gate_export is
# where an explicit admin approval can override for an export. check_processing
# answers "may I do this automatically?", and the safe default for any external
# environment above public is deny.
PROCESSING_MATRIX = {
    "public": {
        "local_deterministic": _ALLOW,
        "local_agent": _ALLOW,
        "external": _ALLOW,
    },
    "internal": {
        "local_deterministic": _ALLOW,
        "local_agent": _ALLOW,
        "external": _DENY,
    },
    "confidential": {
        "local_deterministic": _ALLOW,
        "local_agent": _ALLOW_MIN,
        "external": _DENY,
    },
    "restricted": {
        "local_deterministic": _ALLOW_MIN,
        "local_agent": _ALLOW_MIN,
        "external": _DENY,
    },
    "secret": {
        "local_deterministic": _ALLOW_MIN,
        "local_agent": _ALLOW_MIN,
        "external": _DENY,
    },
}

# Sensitivities that require admin approval to leave the building (export).
_EXPORT_APPROVAL_REQUIRED = {"confidential", "restricted", "secret"}


def _normalize_sensitivity(sensitivity: Optional[str]) -> str:
    """Map an input sensitivity to a known label, collapsing unknown/blank to
    the STRICTEST row ('secret') so uncertainty is always handled conservatively.
    """
    if sensitivity is None:
        return "secret"
    s = str(sensitivity).strip().lower()
    if s in PROCESSING_MATRIX:
        return s
    # Unknown label => stricter-row-wins => treat as the most sensitive.
    return "secret"


def _normalize_environment(environment: Optional[str]) -> str:
    if environment is None:
        raise ValueError("check_processing: environment is required")
    e = str(environment).strip().lower()
    if e not in ENVIRONMENTS:
        raise ValueError(
            f"check_processing: unknown environment {environment!r}; "
            f"expected one of {ENVIRONMENTS}"
        )
    return e


def check_processing(sensitivity: str, environment: str) -> str:
    """Return the processing verdict for (sensitivity, environment).

    Verdict is one of 'allow' | 'allow-minimized' | 'deny'. Mirrors
    PROCESSING-MATRIX.md exactly. Stricter-row-wins: an unknown / blank
    sensitivity is treated as 'secret' (the strictest row), never as 'public'.
    """
    sens = _normalize_sensitivity(sensitivity)
    env = _normalize_environment(environment)
    return PROCESSING_MATRIX[sens][env]


# --------------------------------------------------------------------------- #
# helpers shared by the gates
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ledgers_dir(root: Path) -> Path:
    return Path(root) / "Meta.nosync" / "ledgers"


def _is_admin_approval(approval) -> bool:
    """An approval is meaningful only if it is a non-empty, non-placeholder ref.

    Accepts any truthy reference string (e.g. a directive id, a ticket, an
    admin-name+date). Rejects None, '', whitespace, and obvious placeholders so
    a blank or templated value cannot wave material out the door.
    """
    if approval is None:
        return False
    a = str(approval).strip()
    if not a:
        return False
    placeholders = {
        "none", "no", "false", "n/a", "na", "tbd", "todo",
        "changeme", "<approval>", "pending",
    }
    if a.lower() in placeholders:
        return False
    return True


def _load_oracle_yml(root: Path) -> dict:
    cfg = Path(root) / "oracle.yml"
    if not cfg.exists():
        raise PolicyError(f"policy: no oracle.yml at {cfg}")
    yaml_mod = _import_yaml()
    data = yaml_mod.safe_load(cfg.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PolicyError("policy: oracle.yml is not a mapping")
    return data


# --------------------------------------------------------------------------- #
# export gate
# --------------------------------------------------------------------------- #
def gate_export(
    sensitivity: str,
    approval,
    actor: Optional[str] = None,
    role: Optional[str] = None,
    *,
    root: Optional[Path] = None,
    destination: str = "_OUTPUT",
    classification: Optional[str] = None,
    purpose: str = "",
    record: bool = True,
) -> dict:
    """Authorize an export of material at ``sensitivity``.

    Confidential / restricted / secret REQUIRE a meaningful admin approval ref;
    without one this raises PermissionError (PolicyError) and writes nothing.
    public / internal export freely.

    On success, when ``record`` is True and ``root`` is given, appends an
    export_event row (metadata only -- NO payload) and returns it. The returned
    dict always carries the event fields even when not recorded.

    The ``role`` is informational/logged here; the binding admin gate is the
    presence of an approval (an admin acts by attaching one). See GOVERNANCE.md:
    --actor/--role come from flags (advisory-plus-logged) until session-context
    identity exists, so we never *trust* role alone to bypass approval.
    """
    sens = _normalize_sensitivity(sensitivity)
    classification = classification or sens

    if sens in _EXPORT_APPROVAL_REQUIRED and not _is_admin_approval(approval):
        raise PolicyError(
            f"export of {sens!r} material to {destination!r} requires admin "
            f"approval; none supplied (got approval={approval!r})"
        )

    event = {
        "actor": actor or "unknown",
        "role": role or "unknown",
        "classification": classification,
        "destination": str(destination),
        "approval": (str(approval).strip() if _is_admin_approval(approval) else ""),
        "purpose": str(purpose or ""),
    }

    if record and root is not None:
        ledger = _import_ledger()
        path = _ledgers_dir(root) / "export_event.jsonl"
        drop_id = ledger.append(path, dict(event), id_prefix="EXP")
        event["drop_id"] = drop_id
        # ts is stamped by ledger.append; reflect it back for the caller.
        rows, _ = ledger.load(path)
        for r in reversed(rows):
            if r.get("drop_id") == drop_id:
                event["ts"] = r.get("ts")
                break
    return event


# --------------------------------------------------------------------------- #
# role gate
# --------------------------------------------------------------------------- #
def _role_caps(data: dict, role: str) -> tuple[list[str], list[str]]:
    """Return (can, cannot) capability lists for ``role`` from oracle.yml."""
    gov = data.get("governance") or {}
    roles = gov.get("roles") or {} if isinstance(gov, dict) else {}
    spec = roles.get(role) if isinstance(roles, dict) else None
    if not isinstance(spec, dict):
        return [], []
    can = spec.get("can") or []
    cannot = spec.get("cannot") or []
    can = [str(x) for x in can] if isinstance(can, list) else []
    cannot = [str(x) for x in cannot] if isinstance(cannot, list) else []
    return can, cannot


def require_role(
    actor: str,
    role: str,
    capability: str,
    *,
    root: Optional[Path] = None,
) -> None:
    """Enforce that ``role`` may perform ``capability`` per oracle.yml roles.

    Rules:
      * An explicit ``cannot`` listing always denies (even for admin, though
        admin is not normally given a cannot list).
      * 'admin' is permitted any capability not explicitly in its cannot list
        (admin is the authority root).
      * Any other role must list the capability in its ``can`` list; absence is
        a denial (default-deny).

    Raises PermissionError (PolicyError) on denial; returns None on grant.
    """
    role = (role or "").strip()
    capability = (capability or "").strip()
    if not role:
        raise PolicyError(f"require_role: empty role for actor {actor!r}")
    if not capability:
        raise PolicyError(f"require_role: empty capability for actor {actor!r}")

    if root is None:
        raise PolicyError("require_role: root is required to read governance roles")

    data = _load_oracle_yml(root)
    can, cannot = _role_caps(data, role)

    if capability in cannot:
        raise PolicyError(
            f"role {role!r} (actor {actor!r}) is explicitly denied capability "
            f"{capability!r}"
        )
    if role == "admin":
        return None
    if capability in can:
        return None
    raise PolicyError(
        f"role {role!r} (actor {actor!r}) is not permitted capability "
        f"{capability!r}; allowed: {can!r}"
    )


# --------------------------------------------------------------------------- #
# redaction record
# --------------------------------------------------------------------------- #
def record_redaction(
    actor: str,
    reason: str,
    action: str,
    *,
    root: Path,
    approved_by: str = "",
    stub_location: str = "",
) -> dict:
    """Append a redaction_event row and return it.

    Redaction (per SECURITY.md) preserves epistemology, not prohibited content:
    legal/privacy/owner/security requirements can redact a record while leaving
    a safe stub and this meta event.
    """
    event = {
        "actor": str(actor or "unknown"),
        "reason": str(reason or ""),
        "approved_by": str(approved_by or ""),
        "action": str(action or ""),
        "stub_location": str(stub_location or ""),
    }
    ledger = _import_ledger()
    path = _ledgers_dir(root) / "redaction_event.jsonl"
    drop_id = ledger.append(path, dict(event), id_prefix="RED")
    event["drop_id"] = drop_id
    rows, _ = ledger.load(path)
    for r in reversed(rows):
        if r.get("drop_id") == drop_id:
            event["ts"] = r.get("ts")
            break
    return event


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Oracle policy gate")
    parser.add_argument("--root", default=".", help="oracle root (for export/redact/role)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="processing-matrix verdict")
    p_check.add_argument("--sensitivity", required=True)
    p_check.add_argument(
        "--env", required=True, choices=list(ENVIRONMENTS),
        help="processing environment",
    )

    p_exp = sub.add_parser("export", help="authorize + log an export")
    p_exp.add_argument("--sensitivity", required=True)
    p_exp.add_argument("--classification", default=None)
    p_exp.add_argument("--destination", default="_OUTPUT")
    p_exp.add_argument("--actor", default="cli")
    p_exp.add_argument("--role", default="unknown")
    p_exp.add_argument("--approval", default="")
    p_exp.add_argument("--purpose", default="")

    p_role = sub.add_parser("role", help="check a role capability")
    p_role.add_argument("--actor", required=True)
    p_role.add_argument("--role", required=True)
    p_role.add_argument("--capability", required=True)

    p_red = sub.add_parser("redact", help="log a redaction event")
    p_red.add_argument("--actor", required=True)
    p_red.add_argument("--reason", required=True)
    p_red.add_argument("--action", required=True)
    p_red.add_argument("--approved-by", default="")
    p_red.add_argument("--stub-location", default="")

    args = parser.parse_args(argv)
    root = Path(args.root)

    if args.cmd == "check":
        verdict = check_processing(args.sensitivity, args.env)
        print(verdict)
        return 0 if verdict != _DENY else 1

    if args.cmd == "export":
        try:
            event = gate_export(
                args.sensitivity,
                args.approval,
                actor=args.actor,
                role=args.role,
                root=root,
                destination=args.destination,
                classification=args.classification,
                purpose=args.purpose,
            )
        except PermissionError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(event, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "role":
        try:
            require_role(args.actor, args.role, args.capability, root=root)
        except PermissionError as exc:
            print(f"DENIED: {exc}", file=sys.stderr)
            return 2
        print("GRANTED")
        return 0

    if args.cmd == "redact":
        event = record_redaction(
            args.actor,
            args.reason,
            args.action,
            root=root,
            approved_by=args.approved_by,
            stub_location=args.stub_location,
        )
        print(json.dumps(event, indent=2, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
