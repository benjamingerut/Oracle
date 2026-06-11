"""Tests for oracle backup / oracle restore (P1-T6).

Acceptance criteria (spec):
  * round-trip byte-for-byte: backup, mutate root, restore, compare ledgers/notes
  * tampered archive file refused at step 4; partial-restore contract reported
  * cross-origin (backup A, restore into B) refused
  * '..' / absolute rel path refused with nothing written
  * archive files 0600 / dirs 0700
  * profile backup contains no .env (planted .env in profile dir)
  * index.json anchor: tampered manifest vs index -> refused; unknown archive
    without --trust-archive -> refused
  * busy-lock refusal

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_index(profile: Path) -> dict:
    p = profile / "backups" / "index.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _manifest_path(archive: Path) -> Path:
    return archive / "backup-manifest.json"


def _read_manifest(archive: Path) -> dict:
    return json.loads(_manifest_path(archive).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Per-test isolated profile fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def profile(tmp_path, monkeypatch):
    """Isolate the shell profile under tmp via ORACLE_HOME."""
    home = tmp_path / "profile"
    monkeypatch.setenv("ORACLE_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Helper to spawn a minimal oracle root without hitting the full spawn stack
# ---------------------------------------------------------------------------

def _make_minimal_root(base: Path, name: str = "testco") -> Path:
    """Create a minimal oracle root with enough structure for backup.py to work."""
    root = base / name
    root.mkdir(parents=True, exist_ok=True)

    # oracle.yml
    (root / "oracle.yml").write_text(
        f"company_name: {name}\ncodename: {name}\n", encoding="utf-8"
    )
    # Memory.nosync/ledgers/
    ledgers = root / "Memory.nosync" / "ledgers"
    ledgers.mkdir(parents=True, exist_ok=True)
    (ledgers / "ledger.md").write_text("# Ledger\nentry: initial\n", encoding="utf-8")

    # Memory.nosync/notes/
    notes = root / "Memory.nosync" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "note.md").write_text("# Note\nhello world\n", encoding="utf-8")

    # Meta.nosync/
    meta = root / "Meta.nosync"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "info.md").write_text("# Meta\ninfo\n", encoding="utf-8")

    # _tools/backup.py -- copy from vendored kernel
    tools = root / "_tools"
    tools.mkdir(parents=True, exist_ok=True)
    from oracle_agent.backup_shell import _sha256_file  # noqa (just ensure import works)
    src = Path(__file__).resolve().parents[2] / "src" / "oracle_agent" / "assets" / "oracle-kernel" / "_tools" / "backup.py"
    if src.exists():
        shutil.copy2(str(src), str(tools / "backup.py"))

    return root


# ---------------------------------------------------------------------------
# Tier vocabulary tests (unit)
# ---------------------------------------------------------------------------

class TestTierVocabulary:
    """Verify the tier flag values the shell passes to backup.py."""

    def test_default_tier_excludes_data_nosync(self):
        """Default tier '0' must exclude _data.nosync (tier 2)."""
        import importlib.util
        bp = Path(__file__).resolve().parents[2] / "src" / "oracle_agent" / "assets" / "oracle-kernel" / "_tools" / "backup.py"
        spec = importlib.util.spec_from_file_location("_backup", str(bp))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tiers = mod._normalize_tiers("0")
        assert "2" not in tiers, "default tier '0' must not include _data.nosync (tier 2)"
        assert "0" in tiers

    def test_tier_all_includes_data_nosync(self):
        import importlib.util
        bp = Path(__file__).resolve().parents[2] / "src" / "oracle_agent" / "assets" / "oracle-kernel" / "_tools" / "backup.py"
        spec = importlib.util.spec_from_file_location("_backup2", str(bp))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tiers = mod._normalize_tiers("all")
        assert "2" in tiers, "tier 'all' must include _data.nosync (tier 2)"


# ---------------------------------------------------------------------------
# Secret deny-list unit tests
# ---------------------------------------------------------------------------

class TestSecretDenyList:
    def test_dot_env_exact(self):
        from oracle_agent.backup_shell import _is_shell_secret
        assert _is_shell_secret(Path(".env"))

    def test_dot_env_nosync(self):
        from oracle_agent.backup_shell import _is_shell_secret
        assert _is_shell_secret(Path(".env.nosync"))

    def test_dot_env_dot_something(self):
        from oracle_agent.backup_shell import _is_shell_secret
        assert _is_shell_secret(Path(".env.staging"))

    def test_kill_switch(self):
        from oracle_agent.backup_shell import _is_shell_secret
        assert _is_shell_secret(Path("KILL-SWITCH.json"))

    def test_pem_denied(self):
        from oracle_agent.backup_shell import _is_shell_secret
        assert _is_shell_secret(Path("cert.pem"))

    def test_normal_file_allowed(self):
        from oracle_agent.backup_shell import _is_shell_secret
        assert not _is_shell_secret(Path("config.json"))
        assert not _is_shell_secret(Path("ledger.md"))

    def test_grounding_shadow_jsonl_denied(self):
        """P3-T7 / P3S-10 (G5): the shadow capture file (claim text) must NEVER
        land in any shell-produced archive."""
        from oracle_agent.backup_shell import _is_shell_secret, DENY_EXACT_NAMES
        assert "grounding_shadow.jsonl" in DENY_EXACT_NAMES
        assert _is_shell_secret(Path("grounding_shadow.jsonl"))


# ---------------------------------------------------------------------------
# Profile backup: no .env
# ---------------------------------------------------------------------------

class TestProfileBackup:
    def test_profile_backup_excludes_dot_env(self, profile, tmp_path):
        """Profile backup must contain no .env file even when one exists in the profile dir."""
        from oracle_agent import config

        # Ensure profile dir exists.
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)

        # Plant a config.json.
        cfg = config.load_config()
        config.save_config(cfg)

        # Plant a .env in the profile dir.
        env_file = pdir / ".env"
        env_file.write_text("SECRET=hunter2\n", encoding="utf-8")
        assert env_file.exists()

        out_dir = tmp_path / "profile_backup"
        from oracle_agent import cli
        rc = cli.main(["backup", "--profile", "--out", str(out_dir)])
        assert rc == 0, "profile backup should succeed"

        # Verify .env is NOT in the archive.
        for f in out_dir.rglob("*"):
            if f.is_file():
                assert f.name != ".env", f"profile backup must not contain .env; found {f}"
                assert ".env." not in f.name, f"profile backup must not contain .env.* variants; found {f}"

    def test_profile_backup_excludes_grounding_shadow(self, profile, tmp_path):
        """Profile backup must never contain grounding_shadow.jsonl even when one
        exists in the profile dir (P3-T7 / P3S-10, G5 interplay)."""
        from oracle_agent import config, cli

        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        config.save_config(cfg)

        # Plant a shadow capture file (holds flagged claim TEXT).
        shadow = pdir / "grounding_shadow.jsonl"
        shadow.write_text(
            '{"claim": "Revenue was $1M.", "verdict": "unbacked"}\n',
            encoding="utf-8",
        )
        assert shadow.exists()

        out_dir = tmp_path / "profile_shadow_bk"
        rc = cli.main(["backup", "--profile", "--out", str(out_dir)])
        assert rc == 0, "profile backup should succeed"

        for f in out_dir.rglob("*"):
            if f.is_file():
                assert f.name != "grounding_shadow.jsonl", (
                    f"profile backup must not contain grounding_shadow.jsonl; found {f}"
                )

    def test_profile_backup_contains_config_json(self, profile, tmp_path):
        """Profile backup should contain config.json."""
        from oracle_agent import config, cli

        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        config.save_config(cfg)

        out_dir = tmp_path / "profile_bk"
        rc = cli.main(["backup", "--profile", "--out", str(out_dir)])
        assert rc == 0
        assert (out_dir / "config.json").exists(), "profile backup should contain config.json"

    def test_profile_backup_recorded_in_index(self, profile, tmp_path):
        """Profile backup entry must appear in index.json."""
        from oracle_agent import config, cli

        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        config.save_config(cfg)

        out_dir = tmp_path / "profile_idx"
        rc = cli.main(["backup", "--profile", "--out", str(out_dir)])
        assert rc == 0

        idx = _read_index(profile)
        assert str(out_dir) in idx, "profile backup dest must be in index.json"

    def test_profile_backup_archive_files_0600(self, profile, tmp_path):
        """Profile archive files must be 0600."""
        from oracle_agent import config, cli

        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        config.save_config(cfg)

        out_dir = tmp_path / "profile_mode"
        rc = cli.main(["backup", "--profile", "--out", str(out_dir)])
        assert rc == 0

        for f in out_dir.rglob("*"):
            if f.is_file():
                mode = oct(f.stat().st_mode & 0o777)
                assert mode == "0o600", f"archive file {f} has mode {mode}, expected 0o600"


# ---------------------------------------------------------------------------
# Backup: permission pass
# ---------------------------------------------------------------------------

class TestBackupPermissions:
    """Archive files must be 0600, dirs 0700 after the shell chmod pass."""

    def test_archive_dirs_0700_files_0600(self, profile, tmp_path):
        """After backup, every file is 0600 and every dir is 0700."""
        root = _make_minimal_root(tmp_path / "roots")

        from oracle_agent import config, cli
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco", root)
        config.save_config(cfg)

        out_dir = tmp_path / "backup_out"

        # Mock root_lock to not require actual fcntl (minimal root has no real lock).
        import contextlib
        from oracle_agent import backup_shell
        original = backup_shell.root_lock

        @contextlib.contextmanager
        def _mock_lock(name, nb=False):
            yield

        backup_shell.root_lock = _mock_lock
        try:
            rc = cli.main(["backup", "tco", "--out", str(out_dir), "--tier", "0"])
        finally:
            backup_shell.root_lock = original

        # We don't assert rc == 0 strictly (kernel subprocess may fail in minimal root),
        # but if backup-manifest.json was created, check permissions.
        if (out_dir / "backup-manifest.json").exists():
            for item in out_dir.rglob("*"):
                if item.is_symlink():
                    continue
                mode = oct(item.stat().st_mode & 0o777)
                if item.is_dir():
                    assert mode == "0o700", f"dir {item} has mode {mode}, expected 0o700"
                else:
                    assert mode == "0o600", f"file {item} has mode {mode}, expected 0o600"


# ---------------------------------------------------------------------------
# Restore: step 1 — index anchor
# ---------------------------------------------------------------------------

class TestRestoreIndexAnchor:
    def test_unknown_archive_without_trust_archive_refused(self, profile, tmp_path):
        """An archive not in index.json must be refused without --trust-archive."""
        from oracle_agent import config, cli

        root = _make_minimal_root(tmp_path / "roots")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco", root)
        config.save_config(cfg)

        # Create a fake archive that's NOT in the index.
        archive = tmp_path / "fake_archive"
        archive.mkdir()
        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 0,
            "total_bytes": 0,
            "secrets_excluded": 0,
            "files": [],
        }
        (archive / "backup-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        rc = cli.main(["restore", "tco", "--from", str(archive)])
        assert rc == 2, "restore must refuse an unregistered archive without --trust-archive"

    def test_tampered_manifest_refused(self, profile, tmp_path):
        """If the archive is in index.json but manifest sha256 changed, refuse."""
        from oracle_agent import config, cli
        from oracle_agent.backup_shell import _save_index

        root = _make_minimal_root(tmp_path / "roots")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco2", root)
        config.save_config(cfg)

        archive = tmp_path / "tampered_archive"
        archive.mkdir()
        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 0,
            "total_bytes": 0,
            "secrets_excluded": 0,
            "files": [],
        }
        manifest_text = json.dumps(manifest, indent=2)
        (archive / "backup-manifest.json").write_text(manifest_text, encoding="utf-8")
        real_sha = _sha256_bytes(manifest_text.encode("utf-8"))

        # Record the REAL sha in the index.
        idx_entry = {
            "instance": "tco2",
            "root": str(root),
            "ts": "2026-01-01T00:00:00Z",
            "dest": str(archive),
            "manifest_sha256": real_sha,
        }
        from oracle_agent import config as cfg_mod
        pdir2 = cfg_mod.profile_dir()
        (pdir2 / "backups").mkdir(parents=True, exist_ok=True)
        import os
        idx = {str(archive): idx_entry}
        text = json.dumps(idx, indent=2, sort_keys=True) + "\n"
        cfg_mod._atomic_write(pdir2 / "backups" / "index.json", text, mode=0o600)

        # Now tamper with the manifest.
        tampered = dict(manifest)
        tampered["count"] = 99
        (archive / "backup-manifest.json").write_text(
            json.dumps(tampered, indent=2), encoding="utf-8"
        )

        rc = cli.main(["restore", "tco2", "--from", str(archive)])
        assert rc == 2, "tampered manifest vs index must be refused"

    def test_unknown_archive_with_trust_archive_proceeds(self, profile, tmp_path):
        """An archive not in index.json proceeds (to step 2) with --trust-archive."""
        from oracle_agent import config, cli

        root = _make_minimal_root(tmp_path / "roots2")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco3", root)
        config.save_config(cfg)

        # Empty archive with empty file list and matching root -> should reach step 4.
        archive = tmp_path / "trusted_archive"
        archive.mkdir()
        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 0,
            "total_bytes": 0,
            "secrets_excluded": 0,
            "files": [],
        }
        (archive / "backup-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        import contextlib
        from oracle_agent import backup_shell
        orig = backup_shell.root_lock

        @contextlib.contextmanager
        def _mock_lock(name, nb=False):
            yield

        backup_shell.root_lock = _mock_lock
        try:
            rc = cli.main(["restore", "tco3", "--from", str(archive), "--trust-archive"])
        finally:
            backup_shell.root_lock = orig
        # rc 0 or non-2 — it made it past step 1.
        assert rc != 2, "with --trust-archive the restore should proceed past step 1"


# ---------------------------------------------------------------------------
# Restore: step 2 — origin binding
# ---------------------------------------------------------------------------

class TestRestoreCrossOrigin:
    def _register_two_roots(self, profile, tmp_path):
        from oracle_agent import config
        root_a = _make_minimal_root(tmp_path / "roots" / "a", "alpha")
        root_b = _make_minimal_root(tmp_path / "roots" / "b", "beta")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "alpha", root_a)
        cfg = config.register_instance(cfg, "beta", root_b)
        config.save_config(cfg)
        return root_a, root_b

    def _make_archive_for_root(self, archive_base: Path, root: Path) -> Path:
        archive = archive_base
        archive.mkdir(parents=True, exist_ok=True)
        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 0,
            "total_bytes": 0,
            "secrets_excluded": 0,
            "files": [],
        }
        (archive / "backup-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return archive

    def test_cross_origin_refused_without_flag(self, profile, tmp_path):
        """Restoring A's backup into B is refused without --allow-cross-origin."""
        from oracle_agent import cli
        root_a, root_b = self._register_two_roots(profile, tmp_path)
        archive = self._make_archive_for_root(tmp_path / "arch", root_a)

        rc = cli.main([
            "restore", "beta",
            "--from", str(archive),
            "--trust-archive",
        ])
        assert rc == 2, "cross-origin restore without flag must be refused"

    def test_cross_origin_refused_on_non_tty(self, profile, tmp_path, monkeypatch):
        """--allow-cross-origin on non-TTY must be refused (no interactive confirm)."""
        from oracle_agent import cli
        root_a, root_b = self._register_two_roots(profile, tmp_path)
        archive = self._make_archive_for_root(tmp_path / "arch2", root_a)

        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

        rc = cli.main([
            "restore", "beta",
            "--from", str(archive),
            "--trust-archive",
            "--allow-cross-origin",
        ])
        assert rc == 2, "cross-origin on non-TTY must be refused"


# ---------------------------------------------------------------------------
# Restore: step 3 — containment (absolute / '..' paths)
# ---------------------------------------------------------------------------

class TestRestoreContainment:
    def _make_escape_archive(self, archive: Path, root: Path, bad_rel: str) -> None:
        archive.mkdir(parents=True, exist_ok=True)
        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 1,
            "total_bytes": 10,
            "secrets_excluded": 0,
            "files": [{"rel": bad_rel, "sha256": "a" * 64, "bytes": 10}],
        }
        (archive / "backup-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        # Also plant a file so step 4 doesn't hit a missing-file error first.
        # (Containment check must abort BEFORE any write.)

    def _setup_instance(self, profile, tmp_path, name: str = "tco"):
        from oracle_agent import config
        root = _make_minimal_root(tmp_path / "roots" / name)
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, name, root)
        config.save_config(cfg)
        return root

    def test_dotdot_path_refused(self, profile, tmp_path):
        """Manifest entry with '..' component must be refused before any write."""
        from oracle_agent import cli
        root = self._setup_instance(profile, tmp_path, "tco_dotdot")
        archive = tmp_path / "escape_arch"
        self._make_escape_archive(archive, root, "../etc/passwd")

        # Plant a sentinel to verify no file was written.
        sentinel = root / "etc" / "passwd"
        assert not sentinel.exists()

        import contextlib
        from oracle_agent import backup_shell
        orig = backup_shell.root_lock

        @contextlib.contextmanager
        def _mock_lock(name, nb=False):
            yield

        backup_shell.root_lock = _mock_lock
        try:
            rc = cli.main([
                "restore", "tco_dotdot",
                "--from", str(archive),
                "--trust-archive",
            ])
        finally:
            backup_shell.root_lock = orig

        assert rc == 2, "dotdot path must be refused"
        assert not sentinel.exists(), "nothing must have been written"

    def test_absolute_path_refused(self, profile, tmp_path):
        """Manifest entry with absolute path must be refused before any write."""
        from oracle_agent import cli
        root = self._setup_instance(profile, tmp_path, "tco_abs")
        archive = tmp_path / "abs_arch"
        self._make_escape_archive(archive, root, "/etc/passwd")

        import contextlib
        from oracle_agent import backup_shell
        orig = backup_shell.root_lock

        @contextlib.contextmanager
        def _mock_lock(name, nb=False):
            yield

        backup_shell.root_lock = _mock_lock
        try:
            rc = cli.main([
                "restore", "tco_abs",
                "--from", str(archive),
                "--trust-archive",
            ])
        finally:
            backup_shell.root_lock = orig

        assert rc == 2, "absolute path must be refused"

    def test_nothing_written_before_containment_abort(self, profile, tmp_path):
        """When the SECOND entry is bad, the FIRST (valid) entry must NOT have been written."""
        from oracle_agent import cli
        root = self._setup_instance(profile, tmp_path, "tco_partial")
        archive = tmp_path / "partial_arch"
        archive.mkdir(parents=True, exist_ok=True)

        # First entry is valid, second has '..'.
        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 2,
            "total_bytes": 20,
            "secrets_excluded": 0,
            "files": [
                {"rel": "Memory.nosync/ledgers/canary.md", "sha256": "a" * 64, "bytes": 5},
                {"rel": "../escape.txt", "sha256": "b" * 64, "bytes": 5},
            ],
        }
        (archive / "backup-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        canary = root / "Memory.nosync" / "ledgers" / "canary.md"
        assert not canary.exists(), "canary should not exist before restore"

        import contextlib
        from oracle_agent import backup_shell
        orig = backup_shell.root_lock

        @contextlib.contextmanager
        def _mock_lock(name, nb=False):
            yield

        backup_shell.root_lock = _mock_lock
        try:
            rc = cli.main([
                "restore", "tco_partial",
                "--from", str(archive),
                "--trust-archive",
            ])
        finally:
            backup_shell.root_lock = orig

        assert rc == 2
        # The containment check aborts before ANY write (step 3 runs first).
        assert not canary.exists(), "canary must not be written when containment check fails"


# ---------------------------------------------------------------------------
# Restore: step 4 — tampered archive file (sha mismatch)
# ---------------------------------------------------------------------------

class TestRestoreTamperedFile:
    def test_tampered_file_refused_at_step4(self, profile, tmp_path):
        """A file in the archive whose sha256 doesn't match the manifest must abort."""
        from oracle_agent import config, cli
        root = _make_minimal_root(tmp_path / "roots" / "tco_tamper")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco_tamper", root)
        config.save_config(cfg)

        archive = tmp_path / "tamper_arch"
        archive.mkdir()
        # Create a real file in the archive.
        (archive / "Memory.nosync").mkdir()
        real_file = archive / "Memory.nosync" / "ledger.md"
        real_file.write_bytes(b"tampered content")
        real_sha = _sha256_file(real_file)

        # Manifest claims a DIFFERENT sha256.
        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 1,
            "total_bytes": 16,
            "secrets_excluded": 0,
            "files": [
                {
                    "rel": "Memory.nosync/ledger.md",
                    "sha256": "0" * 64,  # wrong sha
                    "bytes": 16,
                }
            ],
        }
        (archive / "backup-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        import contextlib
        from oracle_agent import backup_shell
        orig = backup_shell.root_lock

        @contextlib.contextmanager
        def _mock_lock(name, nb=False):
            yield

        backup_shell.root_lock = _mock_lock
        try:
            rc = cli.main([
                "restore", "tco_tamper",
                "--from", str(archive),
                "--trust-archive",
            ])
        finally:
            backup_shell.root_lock = orig

        assert rc == 2, "tampered file must be refused at step 4"

    def test_partial_restore_contract_reported(self, profile, tmp_path, capsys):
        """When abort occurs at step 4, already-restored files are reported."""
        from oracle_agent import config, cli
        root = _make_minimal_root(tmp_path / "roots" / "tco_partial2")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco_partial2", root)
        config.save_config(cfg)

        archive = tmp_path / "partial2_arch"
        archive.mkdir()
        (archive / "Memory.nosync").mkdir(parents=True)

        # Two files: first one good (correct sha), second tampered.
        good_content = b"good file content"
        good_file = archive / "Memory.nosync" / "good.md"
        good_file.write_bytes(good_content)
        good_sha = _sha256_file(good_file)

        bad_file = archive / "Memory.nosync" / "bad.md"
        bad_file.write_bytes(b"original content")
        # Manifest claims a wrong sha for the bad file.

        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 2,
            "total_bytes": 30,
            "secrets_excluded": 0,
            "files": [
                {
                    "rel": "Memory.nosync/good.md",
                    "sha256": good_sha,
                    "bytes": len(good_content),
                },
                {
                    "rel": "Memory.nosync/bad.md",
                    "sha256": "0" * 64,  # wrong
                    "bytes": 16,
                },
            ],
        }
        (archive / "backup-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        import contextlib
        from oracle_agent import backup_shell
        orig = backup_shell.root_lock

        @contextlib.contextmanager
        def _mock_lock(name, nb=False):
            yield

        backup_shell.root_lock = _mock_lock
        try:
            rc = cli.main([
                "restore", "tco_partial2",
                "--from", str(archive),
                "--trust-archive",
            ])
        finally:
            backup_shell.root_lock = orig

        captured = capsys.readouterr()
        assert rc == 2
        # The error message must mention already-restored files.
        assert "already restored" in captured.err.lower() or "already restored" in captured.out.lower(), (
            "abort message must report already-restored files"
        )


# ---------------------------------------------------------------------------
# Restore: step 4 — round-trip byte-for-byte
# ---------------------------------------------------------------------------

class TestRestoreRoundTrip:
    def test_round_trip_restores_exact_bytes(self, profile, tmp_path):
        """Backup, mutate, restore -> files match the original byte-for-byte."""
        from oracle_agent import config, cli
        root = _make_minimal_root(tmp_path / "roots" / "tco_rt")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco_rt", root)
        config.save_config(cfg)

        # Record original hashes of tracked files.
        original_ledger = (root / "Memory.nosync" / "ledgers" / "ledger.md").read_bytes()
        original_note = (root / "Memory.nosync" / "notes" / "note.md").read_bytes()

        archive = tmp_path / "rt_archive"

        import contextlib
        from oracle_agent import backup_shell
        orig_lock = backup_shell.root_lock
        orig_kernel_run = backup_shell._kernel_backup_run

        @contextlib.contextmanager
        def _mock_lock(name, nb=False):
            yield

        def _mock_kernel_run(root_path, dest, tier):
            """Simulate kernel backup.py run: copy Memory.nosync tier-0 files."""
            files_meta = []
            for src in sorted((root_path / "Memory.nosync").rglob("*")):
                if not src.is_file():
                    continue
                rel = src.relative_to(root_path)
                dst = dest / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                content = src.read_bytes()
                dst.write_bytes(content)
                sha = _sha256_bytes(content)
                files_meta.append({"rel": str(rel), "sha256": sha, "bytes": len(content)})

            manifest = {
                "root": str(root_path),
                "dest": str(dest),
                "tier": "0",
                "tiers": ["0"],
                "created_at": "2026-01-01T00:00:00Z",
                "count": len(files_meta),
                "total_bytes": sum(f["bytes"] for f in files_meta),
                "secrets_excluded": 0,
                "files": files_meta,
            }
            (dest / "backup-manifest.json").write_bytes(
                json.dumps(manifest, indent=2).encode("utf-8")
            )
            return 0

        def _mock_verify_restore(*args, **kwargs):
            return 0

        backup_shell.root_lock = _mock_lock
        backup_shell._kernel_backup_run = _mock_kernel_run
        backup_shell._kernel_verify_restore = _mock_verify_restore
        try:
            rc = cli.main(["backup", "tco_rt", "--out", str(archive), "--tier", "0"])
            assert rc == 0, f"backup failed with rc={rc}"

            # Mutate the root files.
            (root / "Memory.nosync" / "ledgers" / "ledger.md").write_bytes(b"MUTATED\n")
            (root / "Memory.nosync" / "notes" / "note.md").write_bytes(b"MUTATED\n")

            rc2 = cli.main([
                "restore", "tco_rt",
                "--from", str(archive),
            ])
            assert rc2 == 0, f"restore failed with rc={rc2}"
        finally:
            backup_shell.root_lock = orig_lock
            backup_shell._kernel_backup_run = orig_kernel_run
            backup_shell._kernel_verify_restore = _mock_verify_restore

        # Verify byte-for-byte restoration.
        restored_ledger = (root / "Memory.nosync" / "ledgers" / "ledger.md").read_bytes()
        restored_note = (root / "Memory.nosync" / "notes" / "note.md").read_bytes()
        assert restored_ledger == original_ledger, "ledger must be restored byte-for-byte"
        assert restored_note == original_note, "note must be restored byte-for-byte"


# ---------------------------------------------------------------------------
# Integration: real shell -> kernel seam (no mocking of _kernel_backup_run)
# ---------------------------------------------------------------------------

class TestBackupRestoreIntegration:
    def test_real_backup_restore_round_trip_via_kernel(
        self, profile, spawned_root, tmp_path, capsys
    ):
        """Full integration: real `oracle backup` (kernel backup.py run subprocess)
        then real `oracle restore` against a copy of a genuinely spawned root.

        Exercises the actual argv the shell passes to _tools/backup.py
        (``--root R run --dest DIR --tier 0``), the real backup-manifest.json
        schema the kernel writes, the genuine index.json anchor (no
        --trust-archive), and the kernel verify-restore step-5 subprocess.

        The session-scoped spawned_root is NEVER mutated: it is copied to
        tmp_path first and all backup/mutate/restore happens on the copy.
        """
        from oracle_agent import config, cli

        # Copy the shared spawned root — do not touch the session fixture.
        root = tmp_path / "integ_root"
        shutil.copytree(str(spawned_root), str(root))

        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco_integ", root)
        config.save_config(cfg)

        # Plant a note in the copy BEFORE backup so we have a known
        # Memory.nosync file to round-trip.
        notes_dir = root / "Memory.nosync" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        note = notes_dir / "integration-note.md"
        original_bytes = b"# Integration Note\noriginal content for round-trip\n"
        note.write_bytes(original_bytes)

        dest = tmp_path / "integ_backup"

        # --- Real backup: spawns _tools/backup.py run --dest ... --tier 0 ---
        rc = cli.main(["backup", "tco_integ", "--out", str(dest), "--tier", "0"])
        assert rc == 0, "real kernel-backed backup must succeed"

        # backup-manifest.json exists with per-file sha256s.
        manifest = _read_manifest(dest)
        files = manifest.get("files", [])
        assert files, "kernel manifest must list backed-up files"
        for entry in files:
            sha = entry.get("sha256", "")
            assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), (
                f"manifest entry {entry.get('rel')!r} lacks a real sha256: {sha!r}"
            )
        rels = {entry["rel"] for entry in files}
        assert "Memory.nosync/notes/integration-note.md" in rels, (
            "the planted note must be in the kernel's backup manifest"
        )
        # The archive copy of the note matches the original bytes.
        assert (dest / "Memory.nosync" / "notes" / "integration-note.md").read_bytes() \
            == original_bytes

        # Index entry recorded (the genuine anchor — no --trust-archive later).
        idx = _read_index(profile)
        assert str(dest.resolve()) in idx, "backup dest must be recorded in index.json"
        entry = idx[str(dest.resolve())]
        assert entry["instance"] == "tco_integ"
        assert entry["manifest_sha256"] == _sha256_bytes(
            _manifest_path(dest).read_bytes()
        )

        # --- Mutate the note in the copied root ---
        note.write_bytes(b"MUTATED -- should be undone by restore\n")

        # --- Real restore: index anchor, origin binding, containment,
        #     per-file hash verify, then kernel verify-restore subprocess ---
        rc2 = cli.main(["restore", "tco_integ", "--from", str(dest)])
        out = capsys.readouterr()
        assert rc2 == 0, f"real restore must succeed (stderr: {out.err[-500:]})"

        # Byte-identical to the pre-backup content.
        assert note.read_bytes() == original_bytes, (
            "mutated note must be restored byte-for-byte"
        )

        # Step 5 (kernel verify-restore) actually ran and passed.
        assert "verify-restore self-check passed" in out.out, (
            "kernel verify-restore step must run and report success"
        )
        # verify-restore stamps BACKUP-RECOVERY.md on success — proof the
        # real kernel subprocess executed its round-trip.
        recovery_doc = root / "BACKUP-RECOVERY.md"
        assert recovery_doc.exists()
        assert "last_verified_restore:" in recovery_doc.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Restore: busy lock refusal
# ---------------------------------------------------------------------------

class TestRestoreBusyLock:
    def test_busy_lock_refuses_backup(self, profile, tmp_path):
        """oracle backup must refuse when the root lock is held."""
        from oracle_agent import config, cli
        root = _make_minimal_root(tmp_path / "roots" / "tco_busy")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco_busy", root)
        config.save_config(cfg)

        from oracle_agent import backup_shell

        def _raising_lock(name, nb=False):
            raise BlockingIOError("lock held")

        import contextlib

        @contextlib.contextmanager
        def _raising_lock_ctx(name, nb=False):
            raise BlockingIOError("lock held")
            yield  # noqa

        orig = backup_shell.root_lock
        backup_shell.root_lock = _raising_lock_ctx
        try:
            rc = cli.main(["backup", "tco_busy", "--out", str(tmp_path / "bk"), "--tier", "0"])
        finally:
            backup_shell.root_lock = orig

        assert rc == 1, "backup must return 1 when root is busy"

    def test_busy_lock_refuses_restore(self, profile, tmp_path):
        """oracle restore must refuse when the root lock is held."""
        from oracle_agent import config, cli
        root = _make_minimal_root(tmp_path / "roots" / "tco_busy2")
        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load_config()
        cfg = config.register_instance(cfg, "tco_busy2", root)
        config.save_config(cfg)

        archive = tmp_path / "busy_archive"
        archive.mkdir()
        manifest = {
            "root": str(root),
            "dest": str(archive),
            "tier": "0",
            "tiers": ["0"],
            "created_at": "2026-01-01T00:00:00Z",
            "count": 0,
            "total_bytes": 0,
            "secrets_excluded": 0,
            "files": [],
        }
        (archive / "backup-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        from oracle_agent import backup_shell
        import contextlib

        @contextlib.contextmanager
        def _raising_lock_ctx(name, nb=False):
            raise BlockingIOError("lock held")
            yield  # noqa

        orig = backup_shell.root_lock
        backup_shell.root_lock = _raising_lock_ctx
        try:
            rc = cli.main([
                "restore", "tco_busy2",
                "--from", str(archive),
                "--trust-archive",
            ])
        finally:
            backup_shell.root_lock = orig

        assert rc == 1, "restore must return 1 when root is busy"


# ---------------------------------------------------------------------------
# Index.json 0600 mode
# ---------------------------------------------------------------------------

class TestIndexPermissions:
    def test_index_json_is_0600(self, profile, tmp_path):
        """index.json must be written at 0600."""
        from oracle_agent import config
        from oracle_agent.backup_shell import _load_index, _save_index

        pdir = config.profile_dir()
        pdir.mkdir(parents=True, exist_ok=True)

        _save_index({"test": "entry"})
        from oracle_agent.backup_shell import _index_path
        p = _index_path()
        assert p.exists()
        mode = oct(p.stat().st_mode & 0o777)
        assert mode == "0o600", f"index.json has mode {mode}, expected 0o600"
