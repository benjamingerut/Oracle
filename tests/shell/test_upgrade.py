"""Tests for oracle upgrade --check / upgrade kernel (P1-T4).

Acceptance criteria:
  - --check reports 'equal' against a just-spawned root (vendored == installed).
  - simulated older root (stale manifest) reports 'behind' and apply upgrades it.
  - a NEWER root refuses without --force-downgrade.
  - apply failure (planted failing kernel test) triggers verified copy-back and
    post-recovery check is green (shows remaining delta, not error).
  - non-TTY without --approve refuses.
  - busy lock refuses.
  - direction = 'diverged' when hashes differ but version strings are equal.

Tests that run `upgrade kernel apply` (which spawns subprocesses and runs lint)
are guarded with pytest.mark.slow (the spawned_root fixture itself is session-
scoped so spawn overhead is paid once).  Non-subprocess tests run unconditionally.

Stdlib only (match repo style).
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_manifest(kernel_dir: Path) -> dict:
    mp = kernel_dir / ".kernel-manifest.json"
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(kernel_dir: Path, data: dict) -> None:
    (kernel_dir / ".kernel-manifest.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# upgrade --check: direction unit tests (no subprocess, no real root needed)
# ---------------------------------------------------------------------------

class TestCheckDirection:
    """Direction computation logic in upgrade_shell.cmd_check."""

    def test_check_help(self, profile, capsys):
        from oracle_agent import cli
        rc = cli.main(["upgrade", "--help"])
        assert rc == 0

    def test_no_instances_returns_nonzero(self, profile):
        from oracle_agent import cli
        rc = cli.main(["upgrade", "--check"])
        assert rc != 0  # 1 or 2

    def _with_manifest(self, spawned_root, manifest_data):
        """Context manager that writes a manifest and restores the original."""
        import contextlib

        @contextlib.contextmanager
        def _cm():
            original = _read_manifest(spawned_root)
            _write_manifest(spawned_root, manifest_data)
            try:
                yield
            finally:
                if original:
                    _write_manifest(spawned_root, original)

        return _cm()

    def test_check_equal(self, profile, spawned_root, tmp_path, monkeypatch):
        """A just-spawned root's manifest matches the vendored tree -> equal."""
        from oracle_agent import cli, config
        from oracle_agent.upgrade_shell import _VENDORED_KERNEL

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco", spawned_root)
        config.save_config(cfg)

        vendored_m = _read_manifest(_VENDORED_KERNEL)
        with self._with_manifest(spawned_root, vendored_m):
            rc = cli.main(["upgrade", "--check"])
        assert rc == 0

    def test_check_behind(self, profile, spawned_root, tmp_path, monkeypatch):
        """Root with older aggregate hash and lower version -> behind."""
        from oracle_agent import cli, config

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco2", spawned_root)
        config.save_config(cfg)

        old_manifest = {
            "tools_version": "1.0.0",
            "aggregate_sha256": "0" * 64,
            "files": {},
        }

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": ["_tools/foo.py"], "added": [], "removed": [],
        })

        with self._with_manifest(spawned_root, old_manifest):
            rc = cli.main(["upgrade", "--check"])
        assert rc == 1

    def test_check_ahead(self, profile, spawned_root, monkeypatch):
        """Root with a newer version string -> ahead."""
        from oracle_agent import cli, config

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco3", spawned_root)
        config.save_config(cfg)

        new_manifest = {
            "tools_version": "999.0.0",
            "aggregate_sha256": "a" * 64,
            "files": {},
        }

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": ["_tools/foo.py"], "added": [], "removed": [],
        })

        with self._with_manifest(spawned_root, new_manifest):
            rc = cli.main(["upgrade", "--check"])
        assert rc == 1

    def test_check_diverged(self, profile, spawned_root, monkeypatch):
        """Same version strings but different hashes -> diverged."""
        from oracle_agent import cli, config
        from oracle_agent.upgrade_shell import _VENDORED_KERNEL

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco4", spawned_root)
        config.save_config(cfg)

        vendored_ver = _read_manifest(_VENDORED_KERNEL).get("tools_version", "3.0.0")
        diverged_manifest = {
            "tools_version": vendored_ver,  # same version
            "aggregate_sha256": "b" * 64,   # different hash
            "files": {},
        }

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": ["_tools/foo.py"], "added": [], "removed": [],
        })

        with self._with_manifest(spawned_root, diverged_manifest):
            rc = cli.main(["upgrade", "--check"])
        assert rc == 1


# ---------------------------------------------------------------------------
# upgrade kernel: approval + lock unit tests (no real apply)
# ---------------------------------------------------------------------------

class TestUpgradeKernelGuards:

    def _with_manifest(self, spawned_root, manifest_data):
        """Context manager that writes a manifest and restores the original."""
        import contextlib

        @contextlib.contextmanager
        def _cm():
            original = _read_manifest(spawned_root)
            _write_manifest(spawned_root, manifest_data)
            try:
                yield
            finally:
                if original:
                    _write_manifest(spawned_root, original)

        return _cm()

    def test_non_tty_without_approve_refuses(self, profile, spawned_root, monkeypatch):
        """Non-TTY stdin without --approve flag must refuse."""
        from oracle_agent import cli, config

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "inst", spawned_root)
        config.save_config(cfg)

        old = {"tools_version": "1.0.0", "aggregate_sha256": "0" * 64, "files": {}}

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": ["_tools/x.py"], "added": [], "removed": [],
        })
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

        with self._with_manifest(spawned_root, old):
            rc = cli.main(["upgrade", "kernel", "inst"])
        assert rc == 2

    def test_approve_flag_accepted(self, profile, spawned_root, monkeypatch):
        """--approve flag is accepted; apply is called (mocked to succeed)."""
        from oracle_agent import cli, config

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "inst2", spawned_root)
        config.save_config(cfg)

        old = {"tools_version": "1.0.0", "aggregate_sha256": "0" * 64, "files": {}}

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": ["_tools/x.py"], "added": [], "removed": [],
        })
        monkeypatch.setattr(ush, "_run_kernel_apply", lambda root, vendored, admin: {
            "ok": True, "swapped": ["_tools/x.py"], "backup_dir": "Meta.nosync/tool-backups/ts",
        })

        with self._with_manifest(spawned_root, old):
            rc = cli.main(["upgrade", "kernel", "inst2", "--approve", "testadmin"])
        assert rc == 0

    def test_already_current_short_circuits(self, profile, spawned_root, monkeypatch):
        """When check() returns no changes, apply is never called."""
        from oracle_agent import cli, config
        from oracle_agent.upgrade_shell import _VENDORED_KERNEL

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "inst3", spawned_root)
        config.save_config(cfg)

        vendored_m = _read_manifest(_VENDORED_KERNEL)

        import oracle_agent.upgrade_shell as ush
        apply_called = []
        # Mock _run_kernel_check to return no changes (already current).
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": [], "added": [], "removed": [],
        })
        monkeypatch.setattr(ush, "_run_kernel_apply",
                            lambda *a, **k: apply_called.append(1) or {})

        with self._with_manifest(spawned_root, vendored_m):
            rc = cli.main(["upgrade", "kernel", "inst3", "--approve", "admin"])
        assert rc == 0
        assert apply_called == []  # apply was NOT called

    def test_downgrade_refused(self, profile, spawned_root, monkeypatch):
        """Root at newer version than vendored -> refused without --force-downgrade."""
        from oracle_agent import cli, config

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "inst4", spawned_root)
        config.save_config(cfg)

        new = {"tools_version": "999.0.0", "aggregate_sha256": "a" * 64, "files": {}}

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_verify_vendored_tree", lambda v: (True, "ok"))

        with self._with_manifest(spawned_root, new):
            rc = cli.main(["upgrade", "kernel", "inst4", "--approve", "admin"])
        assert rc == 2

    def test_force_downgrade_proceeds(self, profile, spawned_root, monkeypatch):
        """--force-downgrade lets a downgrade proceed."""
        from oracle_agent import cli, config

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "inst5", spawned_root)
        config.save_config(cfg)

        new = {"tools_version": "999.0.0", "aggregate_sha256": "a" * 64, "files": {}}

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_verify_vendored_tree", lambda v: (True, "ok"))
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": ["_tools/x.py"], "added": [], "removed": [],
        })
        monkeypatch.setattr(ush, "_run_kernel_apply", lambda root, vendored, admin: {
            "ok": True, "swapped": [],
        })

        with self._with_manifest(spawned_root, new):
            rc = cli.main(["upgrade", "kernel", "inst5", "--approve", "admin",
                            "--force-downgrade"])
        assert rc == 0

    def test_busy_lock_refuses(self, profile, spawned_root, monkeypatch):
        """If the root lock is held (busy), upgrade refuses."""
        from oracle_agent import cli, config

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "inst6", spawned_root)
        config.save_config(cfg)

        old = {"tools_version": "1.0.0", "aggregate_sha256": "0" * 64, "files": {}}

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_verify_vendored_tree", lambda v: (True, "ok"))
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": ["_tools/x.py"], "added": [], "removed": [],
        })

        import contextlib

        @contextlib.contextmanager
        def _busy_lock(*a, **kw):
            raise BlockingIOError("lock busy")
            yield  # noqa: unreachable

        monkeypatch.setattr(ush, "root_lock", _busy_lock)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

        with self._with_manifest(spawned_root, old):
            rc = cli.main(["upgrade", "kernel", "inst6", "--approve", "admin"])
        assert rc == 1

    def test_tampered_vendored_tree_refused(self, profile, spawned_root, monkeypatch):
        """If vendored tree self-verification fails, upgrade refuses immediately."""
        from oracle_agent import cli, config

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "inst7", spawned_root)
        config.save_config(cfg)

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_verify_vendored_tree",
                            lambda v: (False, "hash mismatch: _tools/foo.py"))

        rc = cli.main(["upgrade", "kernel", "inst7", "--approve", "admin"])
        assert rc == 2


# ---------------------------------------------------------------------------
# upgrade kernel: copy-back recovery (mocked apply failure)
# ---------------------------------------------------------------------------

class TestCopyBackRecovery:

    def _with_manifest(self, spawned_root, manifest_data):
        """Context manager that writes a manifest and restores the original."""
        import contextlib

        @contextlib.contextmanager
        def _cm():
            original = _read_manifest(spawned_root)
            _write_manifest(spawned_root, manifest_data)
            try:
                yield
            finally:
                if original:
                    _write_manifest(spawned_root, original)

        return _cm()

    def test_apply_failure_triggers_copy_back(self, profile, tmp_path, monkeypatch, capsys):
        """When apply returns ok:false, copy-back is attempted from tool-backup.

        Uses a tmp_path-scoped (not session-scoped) root so we can freely plant
        and remove files in _tools without polluting the shared spawned_root.
        """
        from oracle_agent import cli, config
        from oracle_agent.testkit import spawn_test_root

        # Spawn a fresh root just for this test.
        test_root = tmp_path / "recov_root"
        try:
            spawn_test_root(test_root, name="Recovery Test")
        except RuntimeError as exc:
            pytest.skip(f"spawn failed: {exc}")

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "recov", test_root)
        config.save_config(cfg)

        old = {"tools_version": "1.0.0", "aggregate_sha256": "0" * 64, "files": {}}

        # Plant a fake backup directory.
        backup_ts = "20260101-000000"
        backup_dir = test_root / "Meta.nosync" / "tool-backups" / backup_ts
        # Use a non-critical file name so if copy-back succeeds it doesn't break anything.
        fake_tool = backup_dir / "_tools" / "dummy_backup.py"
        fake_tool.parent.mkdir(parents=True, exist_ok=True)
        fake_tool.write_text("# restored from backup\n", encoding="utf-8")

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_verify_vendored_tree", lambda v: (True, "ok"))

        check_count = [0]
        post_check_calls = []

        def _check_controlled(root, vendored):
            check_count[0] += 1
            if check_count[0] == 1:
                return {"ok": True, "changed": ["_tools/dummy_backup.py"], "added": [], "removed": []}
            post_check_calls.append(1)
            return {"ok": True, "changed": ["_tools/dummy_backup.py"], "added": [], "removed": []}

        monkeypatch.setattr(ush, "_run_kernel_check", _check_controlled)
        monkeypatch.setattr(ush, "_run_kernel_apply", lambda root, vendored, admin: {
            "ok": False, "refusal": "lint failed post-swap",
            "backup_dir": f"Meta.nosync/tool-backups/{backup_ts}",
        })

        with self._with_manifest(test_root, old):
            rc = cli.main(["upgrade", "kernel", "recov", "--approve", "admin"])
        # Should fail (apply failed) but copy-back was attempted.
        assert rc == 1
        # Post-recovery check was called.
        assert len(post_check_calls) >= 1
        # The restored file should exist in _tools.
        assert (test_root / "_tools" / "dummy_backup.py").exists()

    def test_apply_failure_no_backup_reports_path(self, profile, tmp_path,
                                                   monkeypatch, capsys):
        """When apply fails and no backup exists, print manual recovery hint.

        Uses a tmp_path-scoped root to safely remove the backup dir.
        """
        from oracle_agent import cli, config
        from oracle_agent.testkit import spawn_test_root

        test_root = tmp_path / "nobackup_root"
        try:
            spawn_test_root(test_root, name="No Backup Test")
        except RuntimeError as exc:
            pytest.skip(f"spawn failed: {exc}")

        cfg = config.load_config()
        cfg = config.register_instance(cfg, "nobackup", test_root)
        config.save_config(cfg)

        old = {"tools_version": "1.0.0", "aggregate_sha256": "0" * 64, "files": {}}

        import oracle_agent.upgrade_shell as ush
        monkeypatch.setattr(ush, "_verify_vendored_tree", lambda v: (True, "ok"))
        monkeypatch.setattr(ush, "_run_kernel_check", lambda root, vendored: {
            "ok": True, "changed": ["_tools/x.py"], "added": [], "removed": [],
        })
        monkeypatch.setattr(ush, "_run_kernel_apply", lambda root, vendored, admin: {
            "ok": False, "refusal": "lint failed",
        })
        # Ensure there's no backup directory.
        import shutil
        backup_root = test_root / "Meta.nosync" / "tool-backups"
        if backup_root.exists():
            shutil.rmtree(str(backup_root))

        with self._with_manifest(test_root, old):
            rc = cli.main(["upgrade", "kernel", "nobackup", "--approve", "admin"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "NO BACKUP" in err or "copy-back" in err.lower() or "recovery" in err.lower()


# ---------------------------------------------------------------------------
# vendored tree self-verification unit tests
# ---------------------------------------------------------------------------

class TestVendoredTreeVerification:

    def test_self_verify_passes_on_real_tree(self):
        """The real vendored tree must self-verify cleanly."""
        from oracle_agent.upgrade_shell import _VENDORED_KERNEL, _verify_vendored_tree
        ok, msg = _verify_vendored_tree(_VENDORED_KERNEL)
        assert ok, f"vendored tree self-verify failed: {msg}"

    def test_self_verify_detects_extra_file(self, tmp_path):
        """A file present in tree but absent from manifest triggers failure."""
        from oracle_agent.upgrade_shell import _verify_vendored_tree
        import shutil
        # Build a minimal tree.
        kernel = tmp_path / "kernel"
        (kernel / "_tools").mkdir(parents=True)
        tool = kernel / "_tools" / "foo.py"
        tool.write_text("# tool\n", encoding="utf-8")
        h = _sha256(tool)
        manifest = {"tools_version": "1.0", "aggregate_sha256": "x",
                    "files": {"_tools/foo.py": h}}
        _write_manifest(kernel, manifest)
        # Add an extra file not in manifest.
        (kernel / "_tools" / "extra.py").write_text("# extra\n", encoding="utf-8")
        ok, msg = _verify_vendored_tree(kernel)
        assert not ok
        assert "extra" in msg

    def test_self_verify_detects_hash_mismatch(self, tmp_path):
        """A file whose content differs from the manifest is detected."""
        from oracle_agent.upgrade_shell import _verify_vendored_tree
        kernel = tmp_path / "kernel"
        (kernel / "_tools").mkdir(parents=True)
        tool = kernel / "_tools" / "foo.py"
        tool.write_text("# original\n", encoding="utf-8")
        manifest = {"tools_version": "1.0", "aggregate_sha256": "x",
                    "files": {"_tools/foo.py": "0" * 64}}  # wrong hash
        _write_manifest(kernel, manifest)
        ok, msg = _verify_vendored_tree(kernel)
        assert not ok

    def test_self_verify_detects_missing_file(self, tmp_path):
        """A file listed in manifest but absent from tree is detected."""
        from oracle_agent.upgrade_shell import _verify_vendored_tree
        kernel = tmp_path / "kernel"
        (kernel / "_tools").mkdir(parents=True)
        manifest = {"tools_version": "1.0", "aggregate_sha256": "x",
                    "files": {"_tools/missing.py": "0" * 64}}
        _write_manifest(kernel, manifest)
        ok, msg = _verify_vendored_tree(kernel)
        assert not ok


# ---------------------------------------------------------------------------
# KERNEL_VERSION sourcing (manifest.py P1-T5)
# ---------------------------------------------------------------------------

class TestKernelVersionSourcing:

    def test_manifest_reads_kernel_version_file(self, tmp_path):
        """manifest.build_manifest() reads tools_version from _tools/KERNEL_VERSION."""
        from oracle_agent import manifest as m
        kernel = tmp_path / "kernel"
        (kernel / "_tools").mkdir(parents=True)
        vfile = kernel / "_tools" / "KERNEL_VERSION"
        vfile.write_text("# comment\n\n5.1.2\n", encoding="utf-8")
        (kernel / "_tools" / "foo.py").write_text("# tool\n", encoding="utf-8")
        result = m.build_manifest(kernel)
        assert result["tools_version"] == "5.1.2"

    def test_manifest_falls_back_to_default(self, tmp_path):
        """Without KERNEL_VERSION file, falls back to _DEFAULT_TOOLS_VERSION."""
        from oracle_agent import manifest as m
        kernel = tmp_path / "kernel"
        (kernel / "_tools").mkdir(parents=True)
        (kernel / "_tools" / "foo.py").write_text("# tool\n", encoding="utf-8")
        result = m.build_manifest(kernel)
        assert result["tools_version"] == m._DEFAULT_TOOLS_VERSION

    def test_manifest_explicit_override_wins(self, tmp_path):
        """Explicit tools_version= argument overrides the KERNEL_VERSION file."""
        from oracle_agent import manifest as m
        kernel = tmp_path / "kernel"
        (kernel / "_tools").mkdir(parents=True)
        (kernel / "_tools" / "KERNEL_VERSION").write_text("5.0.0\n", encoding="utf-8")
        result = m.build_manifest(kernel, tools_version="9.9.9")
        assert result["tools_version"] == "9.9.9"

    def test_real_vendored_kernel_has_version_file(self):
        """The vendored _tools/KERNEL_VERSION file exists and has a semver string."""
        from oracle_agent.upgrade_shell import _VENDORED_KERNEL
        vfile = _VENDORED_KERNEL / "_tools" / "KERNEL_VERSION"
        assert vfile.exists(), "KERNEL_VERSION file missing from vendored kernel"
        lines = vfile.read_text(encoding="utf-8").splitlines()
        non_comment = [l.strip() for l in lines
                       if l.strip() and not l.strip().startswith("#")]
        assert non_comment, "KERNEL_VERSION file has no version line"
        ver = non_comment[-1]
        parts = ver.split(".")
        assert len(parts) >= 2, f"version {ver!r} is not semver-ish"

    def test_manifest_version_matches_kernel_version_file(self):
        """The shipped .kernel-manifest.json tools_version matches KERNEL_VERSION."""
        from oracle_agent.upgrade_shell import _VENDORED_KERNEL
        from oracle_agent import manifest as m
        shipped = _read_manifest(_VENDORED_KERNEL)
        file_ver = m._read_kernel_version(_VENDORED_KERNEL)
        assert shipped.get("tools_version") == file_ver, (
            f"Manifest tools_version {shipped.get('tools_version')!r} "
            f"!= KERNEL_VERSION {file_ver!r} -- run `make manifest` to sync"
        )
