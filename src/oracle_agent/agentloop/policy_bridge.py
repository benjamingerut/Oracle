"""agentloop/policy_bridge.py -- environment classification + sensitivity ceiling.

The bridge answers two questions for a given provider endpoint and oracle root:

  1. environment_for(base_url)  -> "local_agent" | "external"
     Is the LLM endpoint provably loopback? Every resolved address must be a
     loopback address; any parse/resolution failure or non-loopback address is
     fail-closed to "external" (STRESS C2/L1/L2).

  2. max_sensitivity_for(root, environment, local_is_confined) -> label
     The highest sensitivity label the root's OWN policy gate marks exactly
     ``allow`` for that environment. The bridge NEVER imports the root's code
     (STRESS C3); it shells out to ``oracle policy check`` and treats anything
     non-``allow`` (deny OR allow-minimized) as out of reach. Any error -> the
     strictest label, "public".

Stdlib only.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit

CANONICAL_ORDER = ["public", "internal", "confidential", "restricted", "secret"]
_LOOPBACK_NAMES = {"localhost"}


def _is_loopback_addr(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def environment_for(base_url: str) -> str:
    """Classify the provider endpoint as ``local_agent`` (loopback) or ``external``.

    Fail-closed: unparseable URL, unresolved host, or ANY non-loopback resolved
    address yields ``external``.
    """
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        return "external"
    if not host:
        return "external"
    host = host.lower()
    if host in _LOOPBACK_NAMES:
        return "local_agent"
    # Literal IP?
    try:
        if ipaddress.ip_address(host).is_loopback:
            return "local_agent"
        return "external"  # a literal, non-loopback IP
    except ValueError:
        pass
    # A name: resolve and require EVERY address to be loopback.
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return "external"
    if not infos:
        return "external"
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0] if sockaddr else ""
        if not _is_loopback_addr(addr):
            return "external"
    return "local_agent"


def sensitivity_order(root: Path) -> list[str]:
    """Read the root's sensitivity labels (ordered) from oracle.yml, as DATA.

    Never imports root code. Falls back to the canonical order on any problem.
    """
    yml = Path(root) / "oracle.yml"
    if not yml.exists():
        return list(CANONICAL_ORDER)
    labels: list[str] = []
    in_block = False
    try:
        for raw in yml.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped.startswith("sensitivity_labels:"):
                in_block = True
                continue
            if in_block:
                if stripped.startswith("- "):
                    labels.append(stripped[2:].strip().strip('"').strip("'"))
                elif stripped and not raw[:1].isspace():
                    break  # next top-level key
                elif stripped and not stripped.startswith("- "):
                    break  # a deeper non-list key ends the labels list
    except OSError:
        return list(CANONICAL_ORDER)
    # Keep only known labels in canonical rank order; fall back if empty.
    known = [lbl for lbl in CANONICAL_ORDER if lbl in labels]
    return known or list(CANONICAL_ORDER)


def sensitivity_rank(label: str, order: list[str]) -> int:
    try:
        return order.index(label)
    except ValueError:
        return len(order)  # unknown -> strictest beyond the scale


def min_sensitivity(a: str, b: str, order: list[str] | None = None) -> str:
    order = order or CANONICAL_ORDER
    return a if sensitivity_rank(a, order) <= sensitivity_rank(b, order) else b


def max_sensitivity_for(root: Path, environment: str,
                        local_is_confined: bool = False,
                        *, policy_check=None) -> str:
    """Highest label the root's policy marks exactly ``allow`` for ``environment``.

    ``allow-minimized`` is NOT a grant (STRESS H2) -- no minimizer exists in
    v1. ``policy_check`` is injectable for tests; it must return the verdict
    string ("allow"|"allow-minimized"|"deny") for (label, environment), or
    raise on error.
    """
    order = sensitivity_order(root)
    if policy_check is None:
        policy_check = _cli_policy_check(root)

    ceiling = "public"
    for label in order:
        try:
            verdict = policy_check(label, environment)
        except Exception:
            # Fail closed: stop climbing on the first error.
            break
        if verdict == "allow":
            ceiling = label
        else:
            # First non-allow (deny or allow-minimized) caps the ladder.
            break
    return ceiling


def _cli_policy_check(root: Path):
    """Return a ``(label, env) -> verdict`` callable backed by the root CLI."""
    import subprocess
    import sys

    oracle = Path(root) / "oracle"

    from .verbtools import _scrubbed_env  # lazy: avoids an import cycle

    def check(label: str, environment: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(oracle), "policy", "check",
             "--sensitivity", label, "--env", environment],
            cwd=str(root), capture_output=True, text=True, timeout=30,
            env=_scrubbed_env(),
        )
        out = (proc.stdout or "").strip().splitlines()
        verdict = out[-1].strip() if out else ""
        if verdict in ("allow", "allow-minimized", "deny"):
            return verdict
        # Unknown output: treat as deny (fail closed).
        return "deny"

    return check
