"""service/scheduler.py -- headless tick + per-root serialization (SPEC S6).

The shell NEVER invents background actions. ``tick_instance`` runs the root's
own ``harness.py --once``, so the kernel's kill-switch -> autonomy -> allowlist
-> blast-cap chain decides everything (DESIGN D6).

Two locks (STRESS A4/A5):
  * per-root flock (``locks/<instance>.lock``) held around every harness tick
    AND every agent ``run_verb`` -- serializes all writers to one root across
    processes (chat vs serve), preventing lost loop-note updates / sqlite
    contention.
  * a single ``serve.lock`` prevents two daemons.

Autonomy-off shortcut (A5): if the root's autonomy is disabled we skip the
harness spawn entirely -- otherwise a disabled root would still accrue one
"intended/denied" action_event per due loop on every tick.

Stdlib only.
"""
from __future__ import annotations

import contextlib
import fcntl
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import config


@dataclass
class TickResult:
    instance: str
    rc: int
    skipped: bool
    output: str


def autonomy_enabled(root: Path) -> bool:
    """Cheap check: is ``Meta.nosync/Autonomy/autonomy.yml`` ``enabled: true``?

    Reads the file as text (no import, no YAML lib). Missing/false/garbled all
    read as OFF (fail-closed).
    """
    p = Path(root) / "Meta.nosync" / "Autonomy" / "autonomy.yml"
    if not p.exists():
        return False
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            if line.startswith("enabled:"):
                val = line.split(":", 1)[1].strip().strip('"').strip("'").lower()
                return val == "true"
    except OSError:
        return False
    return False


@contextlib.contextmanager
def root_lock(name: str):
    """Exclusive flock for one instance root (held across processes)."""
    path = config.locks_dir() / f"{_safe(name)}.lock"
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def acquire_serve_lock():
    """Non-blocking flock for the single daemon. Returns the fh or None if held."""
    path = config.profile_dir() / "serve.lock"
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def tick_instance(name: str, root: Path, timeout: float = 600.0) -> TickResult:
    """Run one harness pass for ``root`` under its per-root lock.

    Skips (no-op, rc 0) when autonomy is OFF so disabled roots stay
    side-effect-free (A5).
    """
    root = Path(root)
    if not (root / "oracle.yml").exists():
        return TickResult(name, 2, False, "root missing oracle.yml")
    if not autonomy_enabled(root):
        return TickResult(name, 0, True, "autonomy off; harness not spawned")
    harness = root / "_tools" / "harness.py"
    if not harness.exists():
        return TickResult(name, 2, False, "harness.py missing")
    with root_lock(name):
        proc = subprocess.run(
            [sys.executable, str(harness), "--root", str(root), "--once"],
            cwd=str(root), capture_output=True, text=True, timeout=timeout,
        )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return TickResult(name, proc.returncode, False, out)


def tick_all(instances: dict[str, Path]) -> list[TickResult]:
    results = []
    for name, root in instances.items():
        try:
            results.append(tick_instance(name, root))
        except subprocess.TimeoutExpired:
            results.append(TickResult(name, 124, False, "tick timed out"))
        except Exception as exc:  # never let one root kill the scheduler
            results.append(TickResult(name, 1, False, f"{type(exc).__name__}: {exc}"))
    return results


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name) or "instance"
