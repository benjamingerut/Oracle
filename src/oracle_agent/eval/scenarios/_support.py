"""Shared scenario support: template-root provider, markers, fault patching.

A scenario's ``setup(Harness)`` calls :func:`scenario_root` to obtain a FRESH
copy of a once-per-process spawned template root (P6-T1 isolation). Markers
are secret-scan-safe ``EVALMARK-<8 hex>`` tokens (never key/bearer shaped --
``make secret`` scans docs, SH-055); each scenario derives its own deterministic
marker from its id so two runs are byte-identical yet no two scenarios collide.

The fault-injection seam (:func:`patched_noop`) lets the planted-fault
meta-tests no-op a dotted shell callable and prove the scenario flips to fail,
and a call-recording variant (:func:`recording_patch`) proves the patched seam
is actually ON the scenario's code path (a dead-seam patch fails the meta-test,
P6S-7).

Stdlib only. testkit-importing is sanctioned for this package (P6S-12).
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Once-per-process spawned template root
# ---------------------------------------------------------------------------
_TEMPLATE_ROOT: Path | None = None
_TEMPLATE_DIR: tempfile.TemporaryDirectory | None = None
_COPY_DIRS: list[tempfile.TemporaryDirectory] = []


def template_root() -> Path:
    """Spawn (once) and return a reusable template oracle root.

    Cheap: the spawn subprocess runs at most once per process; every scenario
    copytree's this template rather than re-spawning (P6-T1).
    """
    global _TEMPLATE_ROOT, _TEMPLATE_DIR
    if _TEMPLATE_ROOT is not None:
        return _TEMPLATE_ROOT
    from oracle_agent.testkit import spawn_test_root

    _TEMPLATE_DIR = tempfile.TemporaryDirectory(prefix="oracle-eval-template-")
    dest = Path(_TEMPLATE_DIR.name) / "root"
    spawn_test_root(dest, name="Eval Template Co")
    _TEMPLATE_ROOT = dest
    return _TEMPLATE_ROOT


def scenario_root() -> Path:
    """A FRESH copy of the template root for one scenario (never shared)."""
    from oracle_agent.eval.harness import fresh_root

    template = template_root()
    holder = tempfile.TemporaryDirectory(prefix="oracle-eval-scn-")
    _COPY_DIRS.append(holder)  # keep alive for the catalog run
    dest = Path(holder.name) / "root"
    return fresh_root(template, dest)


def reset_template_cache() -> None:
    """Drop the cached template + copies (used by tests that need a clean slate)."""
    global _TEMPLATE_ROOT, _TEMPLATE_DIR, _COPY_DIRS
    _TEMPLATE_ROOT = None
    if _TEMPLATE_DIR is not None:
        _TEMPLATE_DIR.cleanup()
        _TEMPLATE_DIR = None
    for h in _COPY_DIRS:
        with contextlib.suppress(Exception):
            h.cleanup()
    _COPY_DIRS = []


# ---------------------------------------------------------------------------
# Secret-scan-safe markers (EVALMARK-<8 hex>, never key/bearer shaped)
# ---------------------------------------------------------------------------

def marker_for(scenario_id: str, slot: str = "0") -> str:
    """A deterministic, unique, secret-scan-safe marker for a scenario slot.

    Deterministic so two runs are byte-identical; unique per (scenario, slot)
    so a sink scan can prove WHICH scenario's content reached the sink.
    """
    digest = hashlib.sha256(f"{scenario_id}:{slot}".encode("utf-8")).hexdigest()
    return f"EVALMARK-{digest[:8]}"


# ---------------------------------------------------------------------------
# Fault injection: no-op / record a dotted shell callable (P6S-7)
# ---------------------------------------------------------------------------

def _resolve_dotted(dotted: str):
    """Resolve ``pkg.mod.attr`` (or ``pkg.mod.Class.method``) to (owner, name).

    Returns ``(owner_object, attr_name, original_value)`` so a caller can
    setattr a replacement and restore it. Supports a single class hop.
    """
    parts = dotted.split(".")
    # Find the longest importable module prefix.
    for split in range(len(parts) - 1, 0, -1):
        mod_name = ".".join(parts[:split])
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
        owner = mod
        attrs = parts[split:]
        for name in attrs[:-1]:
            owner = getattr(owner, name)
        leaf = attrs[-1]
        return owner, leaf, getattr(owner, leaf)
    raise ImportError(f"cannot resolve dotted path {dotted!r}")


# Some enforcer seams are RANK/comparison functions whose "did nothing" fault is
# not "return None" but "return a value that makes the ceiling check never fire"
# (e.g. _rank always 0 -> nothing ever ranks above the ceiling). The canonical
# fault replacement for such a seam is registered here so the meta-test stays
# generic: patched_noop consults this map before falling back to a return-None
# no-op. Every entry is a "the enforcer is defeated" replacement.
FAULT_REPLACEMENTS: dict[str, "object"] = {
    # A defeated rank: every label ranks 0, so no ceiling comparison ever trips.
    "oracle_agent.agentloop.verbtools.Dispatcher._rank":
        (lambda self, label: 0),
    # A defeated label normalizer: every chunk reads as 'public' and survives
    # the embed drop.
    "oracle_agent.agentloop.embedder._norm_label":
        (lambda label: "public"),
    # A defeated allow-gate: every verb is "allowed", so a dropped verb is no
    # longer denied by the dispatch chokepoint (the gate is bypassed).
    "oracle_agent.agentloop.verbtools.Dispatcher._allowed":
        (lambda self, name: True),
    # A defeated auth-verifier: every message verifies, so a DMARC-spoof unlocks
    # private (the public cap is bypassed).
    "oracle_agent.gateway.email.EmailAdapter._auth_verified":
        (lambda self, msg: True),
    # A defeated role clamp: the claimed role is returned verbatim, so a
    # role:admin entry threads admin into a gateway write.
    "oracle_agent.gateway.core.GatewayCore._resolve_role":
        (lambda self, user_id, raw_entry: (raw_entry.get("role") or "user")),
    # A defeated non-private cap: the configured ceiling is returned even on a
    # non-private channel, so above-public content could egress to a group.
    "oracle_agent.gateway.core.GatewayCore._ceiling_for":
        (lambda self, is_private: self.surface_cfg.get("max_sensitivity",
                                                       "internal")),
    # A defeated env scrub: the raw process environment crosses into the kernel
    # subprocess, so a connector credential is no longer contained.
    "oracle_agent.agentloop.verbtools._scrubbed_env":
        (lambda extra_drop=None: dict(__import__("os").environ)),
}


@contextlib.contextmanager
def patched_noop(dotted: str, replacement=None):
    """Patch *dotted* to a no-op (or *replacement*) for the duration.

    The no-op default is a callable that returns ``None`` and ignores all
    arguments -- the canonical "the enforcer did nothing" fault -- UNLESS the
    dotted path is registered in :data:`FAULT_REPLACEMENTS`, in which case the
    registered "enforcer defeated" replacement is used (some seams are
    rank/comparison functions whose defeat is a value, not None). The
    planted-fault meta-test asserts that, under this patch, the scenario's
    assert_outcome flips to fail.
    """
    owner, name, original = _resolve_dotted(dotted)

    if replacement is None:
        replacement = FAULT_REPLACEMENTS.get(dotted)
    if replacement is None:
        def replacement(*_a, **_k):  # noqa: ANN001
            return None

    setattr(owner, name, replacement)
    try:
        yield original
    finally:
        setattr(owner, name, original)


@contextlib.contextmanager
def recording_patch(dotted: str):
    """Patch *dotted* with a recording wrapper that still calls the original.

    Yields a list that the wrapper appends to on every call. If the list is
    empty after the scenario runs, the seam is DEAD (not on the code path) and
    the meta-test must fail -- patching a dead seam would otherwise pass for the
    wrong reason (P6S-7).
    """
    owner, name, original = _resolve_dotted(dotted)
    calls: list[tuple] = []

    def wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    setattr(owner, name, wrapper)
    try:
        yield calls
    finally:
        setattr(owner, name, original)
