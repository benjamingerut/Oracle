#!/usr/bin/env python3
"""session_interface.py -- stateless Admin/User session-interface helper.

This module enforces the UX/control-plane portion of the session-interface
contract when callers route requests through it:

    oracle session default
    oracle session contract
    oracle session gate --interface user --capability change_architecture

It is intentionally NOT authentication. It reads ``oracle.yml`` →
``session_interfaces`` and answers only:

* what interface a fresh session starts in;
* whether a text prefix is a configured legacy switch command, if present;
* whether a capability is allowed/blocked by the selected interface.

Privileged writes still require ``policy.require_role`` at the actual write
site. This tool is a session UX gate, not an identity or role gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "SessionInterfaceError",
    "DEFAULT_ADMIN_PROMPT",
    "DEFAULT_REDIRECT",
    "load_config",
    "default_interface",
    "contract",
    "goal_clarity_policy",
    "resolve_text",
    "gate",
    "main",
]

DEFAULT_ADMIN_PROMPT = (
    "This requires the Admin interface. Do you approve entering Admin mode "
    "for this request?"
)
# Backward-compatible name for older callers that expected a redirect string.
DEFAULT_REDIRECT = DEFAULT_ADMIN_PROMPT


def _import_yaml():
    try:
        import oracle_yaml  # type: ignore
        return oracle_yaml
    except Exception:  # pragma: no cover - package fallback
        from . import oracle_yaml  # type: ignore
        return oracle_yaml


class SessionInterfaceError(ValueError):
    """Raised when the session-interface config or request is invalid."""


def _load_oracle_yml(root: Path) -> dict:
    cfg = Path(root) / "oracle.yml"
    if not cfg.exists():
        raise SessionInterfaceError(f"no oracle.yml at {cfg}")
    yaml_mod = _import_yaml()
    data = yaml_mod.safe_load(cfg.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SessionInterfaceError("oracle.yml is not a mapping")
    return data


def load_config(root: Path) -> dict:
    """Return ``oracle.yml`` → ``session_interfaces`` as a mapping."""
    data = _load_oracle_yml(root)
    cfg = data.get("session_interfaces")
    if not isinstance(cfg, dict):
        raise SessionInterfaceError("oracle.yml missing session_interfaces mapping")
    return cfg


def _modes(cfg: dict) -> dict:
    modes = cfg.get("modes")
    if not isinstance(modes, dict):
        raise SessionInterfaceError("session_interfaces.modes is missing or invalid")
    return modes


def _mode_spec(cfg: dict, interface: str) -> dict:
    name = str(interface or "").strip()
    modes = _modes(cfg)
    spec = modes.get(name)
    if not isinstance(spec, dict):
        raise SessionInterfaceError(f"unknown session interface {interface!r}")
    return spec


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [str(x) for x in value]


def default_interface(root: Path) -> str:
    """Return the interface name for a new session."""
    cfg = load_config(root)
    default = str(cfg.get("default", "user")).strip() or "user"
    _mode_spec(cfg, default)
    return default


def contract(root: Path) -> dict:
    """Return the machine-readable session-interface contract."""
    cfg = load_config(root)
    default = default_interface(root)
    switch = cfg.get("switch_commands") if isinstance(cfg.get("switch_commands"), dict) else {}
    return {
        "default": default,
        "startup_prompt": bool(cfg.get("startup_prompt", False)),
        "reset_policy": str(cfg.get("reset_policy", "")),
        "switch_commands": dict(switch or {}),
        "goal_clarity_policy": goal_clarity_policy(root),
        "modes": _modes(cfg),
        "honesty_boundary": (
            "Session interfaces are UX/control-plane state, not authentication; "
            "privileged writes still require policy.require_role."
        ),
    }


def goal_clarity_policy(root: Path) -> dict:
    """Return the machine-readable pre-execution clarity policy."""
    cfg = load_config(root)
    policy = cfg.get("goal_clarity_policy")
    if not isinstance(policy, dict):
        return {}
    return json.loads(json.dumps(policy))


def _matches_switch(text: str, command: str) -> tuple[bool, str]:
    command = str(command or "").strip()
    if not command:
        return False, text
    if text == command:
        return True, ""
    if text.startswith(command) and text[len(command):len(command) + 1].isspace():
        return True, text[len(command):].strip()
    return False, text


def resolve_text(root: Path, text: str, *, current: Optional[str] = None) -> dict:
    """Resolve configured legacy switch commands from ``text``.

    This is stateless. Callers own the session variable; the returned
    ``interface`` is the value they should apply after processing this text.
    """
    cfg = load_config(root)
    active = str(current or default_interface(root)).strip() or default_interface(root)
    _mode_spec(cfg, active)
    switch = cfg.get("switch_commands") if isinstance(cfg.get("switch_commands"), dict) else {}
    raw = str(text or "").strip()
    for interface, command in switch.items():
        matched, remainder = _matches_switch(raw, str(command))
        if matched:
            _mode_spec(cfg, str(interface))
            return {
                "interface": str(interface),
                "switched": True,
                "command": str(command),
                "text": remainder,
            }
    return {
        "interface": active,
        "switched": False,
        "command": "",
        "text": str(text or ""),
    }


def gate(root: Path, interface: str, capability: str) -> dict:
    """Check whether ``interface`` allows ``capability``.

    Returns a verdict dict. This gate is code-enforced only for callers that
    route through it; it does not replace ``policy.require_role``.
    """
    cfg = load_config(root)
    spec = _mode_spec(cfg, interface)
    cap = str(capability or "").strip()
    if not cap:
        raise SessionInterfaceError("capability is required")
    allow = _string_list(spec.get("allow_capabilities"))
    block = _string_list(spec.get("block_capabilities"))
    admin_prompt = str(
        spec.get("admin_prompt") or spec.get("redirect") or DEFAULT_ADMIN_PROMPT
    )

    if cap in block:
        return {
            "allowed": False,
            "interface": str(interface),
            "capability": cap,
            "reason": "blocked_by_session_interface",
            "admin_prompt": admin_prompt,
            "redirect": admin_prompt,
            "requires_admin_approval": True,
            "next_interface_on_approval": "admin",
            "code_enforced": "oracle session gate",
            "role_gate_still_required": "policy.require_role",
        }
    if "*" in allow or cap in allow:
        return {
            "allowed": True,
            "interface": str(interface),
            "capability": cap,
            "reason": "allowed_by_session_interface",
            "admin_prompt": "",
            "redirect": "",
            "requires_admin_approval": False,
            "next_interface_on_approval": "",
            "code_enforced": "oracle session gate",
            "role_gate_still_required": "policy.require_role",
        }
    return {
        "allowed": False,
        "interface": str(interface),
        "capability": cap,
        "reason": "not_allowed_by_session_interface",
        "admin_prompt": admin_prompt,
        "redirect": admin_prompt,
        "requires_admin_approval": True,
        "next_interface_on_approval": "admin",
        "code_enforced": "oracle session gate",
        "role_gate_still_required": "policy.require_role",
    }


def _print_contract_text(data: dict) -> None:
    print(f"default: {data['default']}")
    print(f"startup_prompt: {str(data['startup_prompt']).lower()}")
    print(f"reset_policy: {data['reset_policy']}")
    switch = data.get("switch_commands") or {}
    if switch:
        print("legacy_switch_commands:")
        for name, command in sorted(switch.items()):
            print(f"  {name}: {command}")
    policy = data.get("goal_clarity_policy") or {}
    if policy:
        print("goal_clarity_policy:")
        print(f"  version: {policy.get('version', '')}")
        print(f"  default_behavior: {policy.get('default_behavior', '')}")
    print("honesty_boundary:")
    print(f"  {data['honesty_boundary']}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Oracle session-interface helper")
    parser.add_argument("--root", default=".", help="oracle root")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_default = sub.add_parser("default", help="print new-session default interface")
    p_default.add_argument("--json", action="store_true")

    p_contract = sub.add_parser("contract", help="print session-interface contract")
    p_contract.add_argument("--json", action="store_true")

    p_resolve = sub.add_parser("resolve", help="resolve configured interface switch text")
    p_resolve.add_argument("--text", required=True)
    p_resolve.add_argument("--current", default=None, help="current interface")
    p_resolve.add_argument("--json", action="store_true")

    p_gate = sub.add_parser("gate", help="check interface/capability allowance")
    p_gate.add_argument("--interface", required=True)
    p_gate.add_argument("--capability", required=True)
    p_gate.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        if args.cmd == "default":
            value = default_interface(root)
            if args.json:
                print(json.dumps({"default": value}, indent=2, ensure_ascii=False))
            else:
                print(value)
            return 0

        if args.cmd == "contract":
            data = contract(root)
            if args.json:
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                _print_contract_text(data)
            return 0

        if args.cmd == "resolve":
            result = resolve_text(root, args.text, current=args.current)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(result["interface"])
                if result["text"]:
                    print(result["text"])
            return 0

        if args.cmd == "gate":
            result = gate(root, args.interface, args.capability)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            elif result["allowed"]:
                print("ALLOWED")
            else:
                print(f"DENIED: {result['admin_prompt']}", file=sys.stderr)
            return 0 if result["allowed"] else 2
    except SessionInterfaceError as exc:
        print(f"session interface: {exc}", file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
