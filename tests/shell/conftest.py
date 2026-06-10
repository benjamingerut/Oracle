"""Shared fixtures for the shell test suite."""
from __future__ import annotations

import os
import subprocess
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
    """Spawn one real oracle root for the shell suite (session-scoped)."""
    root = tmp_path_factory.mktemp("shell_oracle") / "root"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "oracle_agent.spawn", "--root", str(root),
         "--company-name", "Shell Test Co", "--codename", "shelltest",
         "--admin-name", "Shell Admin"],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0 or not (root / "oracle.yml").exists():
        pytest.skip(f"spawn failed: {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return root
