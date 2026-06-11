"""Installer staging tests (fresh-machine setup flow).

Regression coverage for the bug where ``install.sh``'s ``tar --exclude
tmp.nosync`` (meant for the repo's top-level scratch dir) stripped the kernel
template's own ``tmp.nosync/`` from the installed app — so every oracle
spawned from an installed Oracle failed its post-spawn ``setup_audit`` with
``missing context: tmp.nosync/_CONTEXT.md``, while spawns from a repo
checkout passed.

Runs ``install.sh --copy-only`` (source staging + kernel integrity check,
no venv/pip) into an isolated ORACLE_HOME.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_INSTALL_SH = _REPO / "installer" / "install.sh"
_KERNEL_REL = "src/oracle_agent/assets/oracle-kernel"


def _run_copy_only(tmp_path: Path, from_dir: Path) -> subprocess.CompletedProcess:
    home = tmp_path / "oracle-home"
    return subprocess.run(
        ["sh", str(_INSTALL_SH), "--from-dir", str(from_dir), "--copy-only"],
        env={"ORACLE_HOME": str(home), "HOME": str(tmp_path),
             "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def staged(tmp_path):
    proc = _run_copy_only(tmp_path, _REPO)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return tmp_path / "oracle-home" / "app"


def test_kernel_template_ships_tmp_nosync_context():
    """The vendored kernel template itself must carry the audited context file."""
    assert (_REPO / _KERNEL_REL / "tmp.nosync" / "_CONTEXT.md").is_file()


def test_copy_preserves_kernel_tmp_nosync(staged):
    """The installer copy must NOT strip the kernel template's tmp.nosync."""
    assert (staged / _KERNEL_REL / "tmp.nosync" / "_CONTEXT.md").is_file()


def test_copy_prunes_repo_scratch_dir(staged):
    """The repo's top-level tmp.nosync scratch dir must not ship."""
    assert not (staged / "tmp.nosync").exists()


def test_copy_prunes_caches(staged):
    """.git / __pycache__ / .pytest_cache never ship at any depth."""
    leftovers = [
        p for p in staged.rglob("*")
        if p.name in {".git", "__pycache__", ".pytest_cache"}
    ]
    assert leftovers == []


def test_truncated_source_fails_integrity_check(tmp_path):
    """A source tree missing a kernel sentinel must FAIL the install loudly."""
    broken = tmp_path / "broken-src"
    broken.mkdir()
    for rel in ("installer", "src/oracle_agent/assets/oracle-kernel"):
        src = _REPO / rel
        shutil.copytree(src, broken / rel)
    shutil.rmtree(broken / _KERNEL_REL / "tmp.nosync")
    proc = _run_copy_only(tmp_path, broken)
    assert proc.returncode != 0
    assert "missing kernel file" in proc.stderr


def test_spawn_preflight_rejects_truncated_kernel(tmp_path, monkeypatch):
    """spawn.main fails BEFORE writing anything when the template is truncated."""
    from oracle_agent import spawn

    truncated = tmp_path / "kernel"
    shutil.copytree(_REPO / _KERNEL_REL, truncated)
    shutil.rmtree(truncated / "tmp.nosync")
    monkeypatch.setattr(spawn, "kernel_asset_dir", lambda: truncated)

    root = tmp_path / "root"
    rc = spawn.main([
        "--root", str(root), "--company-name", "T Co", "--admin-name", "T",
    ])
    assert rc == 2
    assert not root.exists()
