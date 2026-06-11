"""Shared fixtures for the shell test suite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """Isolate the shell profile under tmp via ORACLE_HOME."""
    home = tmp_path / "profile"
    monkeypatch.setenv("ORACLE_HOME", str(home))
    return home


@pytest.fixture(scope="session")
def spawned_root(tmp_path_factory):
    """Spawn one real oracle root for the shell suite (session-scoped).

    Delegates to testkit.spawn_test_root so the pure spawn logic lives in
    one place (P1-T2).  The pytest.skip contract is preserved here: if spawn
    fails the whole session is skipped rather than erroring.
    """
    from oracle_agent.testkit import spawn_test_root

    root = tmp_path_factory.mktemp("shell_oracle") / "root"
    try:
        spawn_test_root(root, name="Shell Test Co")
    except RuntimeError as exc:
        pytest.skip(f"spawn failed: {exc}")
    return root
