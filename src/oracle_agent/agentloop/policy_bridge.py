"""agentloop/policy_bridge.py -- environment classification + sensitivity ceiling.

The bridge answers two questions for a given provider endpoint and oracle root:

  1. environment_for(base_url)  -> "local_agent" | "external"
     Is the LLM endpoint provably loopback? Only literal loopback addresses
     and the exact hostname ``localhost`` classify as ``local_agent``; DNS
     resolution is intentionally NOT performed (STRESS C2/L1/L2 TOCTOU fix).
     Accepted loopback forms:
       - exact hostname "localhost"
       - any literal IPv4 address in 127.0.0.0/8
       - literal IPv6 "::1" or bracketed "[::1]"

  2. max_sensitivity_for(root, environment) -> label
     The highest sensitivity label the root's OWN policy gate marks exactly
     ``allow`` for that environment. The bridge NEVER imports the root's code
     (STRESS C3); it shells out to ``oracle policy check`` and treats anything
     non-``allow`` (deny OR allow-minimized) as out of reach. Any error -> the
     strictest label, "public".

Stdlib only.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

CANONICAL_ORDER = ["public", "internal", "confidential", "restricted", "secret"]
_LOOPBACK_NAMES = {"localhost"}


def _is_loopback_addr(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _is_literal_loopback_host(host: str) -> bool:
    """Return True iff ``host`` is a provably-loopback literal (no DNS needed).

    Accepts:
      - exact name "localhost"
      - any literal IPv4 in 127.0.0.0/8
      - literal "::1" (and bracketed form "[::1]" is already stripped by
        urlsplit, but guard it anyway)
    """
    h = host.lower().strip()
    if h in _LOOPBACK_NAMES:
        return True
    # urlsplit strips brackets from IPv6, but handle the bare form too.
    h_stripped = h.strip("[]")
    return _is_loopback_addr(h_stripped)


def environment_for(base_url: str) -> str:
    """Classify the provider endpoint as ``local_agent`` (loopback) or ``external``.

    Fail-closed: unparseable URL, missing host, or any hostname that is not a
    literal loopback address / the exact string ``localhost`` yields
    ``external``.  DNS is deliberately NOT consulted (TOCTOU fix, STRESS C2).
    """
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        return "external"
    if not host:
        return "external"
    return "local_agent" if _is_literal_loopback_host(host) else "external"


def validate_sensitivity_label(label: str, order: list[str] | None = None) -> str:
    """Validate that ``label`` is in the known sensitivity order.

    Returns the label unchanged if valid.  Raises ``ValueError`` with an
    actionable message if the label is unknown or mis-cased — the caller must
    surface this as a hard error (CLI exits non-zero; gateway refuses to start).
    """
    eff_order = order if order is not None else CANONICAL_ORDER
    if label in eff_order:
        return label
    raise ValueError(
        f"Unknown sensitivity label {label!r}. "
        f"Valid labels (case-sensitive): {eff_order}"
    )


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
                        *, policy_check=None) -> str:
    """Highest label the root's policy marks exactly ``allow`` for ``environment``.

    ``allow-minimized`` is NOT a grant (STRESS H2) -- no minimizer exists in
    v1. ``policy_check`` is injectable for tests; it must return the verdict
    string ("allow"|"allow-minimized"|"deny") for (label, environment), or
    raise on error.

    NOTE: the ``local_is_confined`` parameter was removed (S1 remediation).
    It was a dead security knob that was never read.  A real confidential-tier
    confinement mechanism will be re-introduced in roadmap Phase 2.
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
