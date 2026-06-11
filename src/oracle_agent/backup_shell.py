"""backup_shell.py -- ``oracle backup`` / ``oracle restore`` (P1-T6).

    oracle backup [NAME] [--out DIR] [--tier TIER]
    oracle backup --profile
    oracle restore NAME --from PATH [--allow-cross-origin] [--trust-archive]

Backup wraps the kernel's ``_tools/backup.py run`` per instance under the
per-root lock (NB, refuses if busy).  After the kernel run the shell:
  * chmods every produced file 0600, every produced directory 0700
    (P1S-9 / P1F-8 -- kernel writes at umask, invariant binds the shell)
  * records {instance, root, ts, dest, manifest_sha256} in
    ~/.oracle/backups/index.json (0600, atomic write)

``--profile`` backs up config.json only.  A deny-exact-names list ensures
``.env`` (exact name) and ``.env.nosync`` can never land in any archive the
shell produces (G5: secrets are NEVER archived, no opt-in).

Restore is fully shell-owned (the kernel has only ``run`` and ``verify-restore``).
Five steps in order, all fail-closed (INV-I4):
  1. Read backup-manifest.json.  If recorded in index.json the manifest sha256
     must match; if NOT in the index, require --trust-archive (P1S-5).
  2. Origin binding (P1S-3): manifest ``root`` must resolve to same instance root
     as NAME; refuse unless --allow-cross-origin, which prints both roots and
     requires interactive TTY confirmation.
  3. Containment (P1S-4): every rel path is rejected if absolute or containing a
     ``..`` component and must resolve strictly under the target root; first
     violation aborts with NOTHING written.
  4. Copy file-by-file, verifying each sha256 BEFORE writing; mismatch aborts and
     the error reports which files were already restored.
  5. Optional kernel ``verify-restore`` as post-restore self-check.

The whole restore runs under ``root_lock(NAME, nb=True)`` (refuse if busy).

SECRET exclusion: ``DENY_EXACT_NAMES`` and ``DENY_CONTAINS`` implement a
shell-side deny list that is independent of the kernel's SECRET_NAME_TOKENS.
The kernel's filter does NOT match the exact name ``.env``; the shell adds
that explicitly (P1S-9, P1F-7).

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config
from .service.scheduler import root_lock

# ---------------------------------------------------------------------------
# secret deny lists (shell-side, independent of kernel SECRET_NAME_TOKENS)
# ---------------------------------------------------------------------------

# Files whose EXACT name is forbidden from any archive the shell produces.
DENY_EXACT_NAMES: frozenset[str] = frozenset({
    ".env",
    ".env.nosync",
})

# Sub-strings that, if found in a filename, also deny it.
DENY_CONTAINS: tuple[str, ...] = (".env.", "KILL-SWITCH")

# File suffixes to deny.
DENY_SUFFIXES: tuple[str, ...] = (".pem", ".key")


def _is_shell_secret(path: Path) -> bool:
    """Return True if ``path`` should be excluded from any shell-produced archive."""
    name = path.name
    if name in DENY_EXACT_NAMES:
        return True
    for tok in DENY_CONTAINS:
        if tok in name:
            return True
    if path.suffix.lower() in DENY_SUFFIXES:
        return True
    return False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_MANIFEST_NAME = "backup-manifest.json"
_INDEX_NAME = "index.json"
_COPY_CHUNK = 1 << 20  # 1 MiB


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_COPY_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _backups_dir() -> Path:
    """~/.oracle/backups/  (created 0700 on demand)."""
    d = config.profile_dir() / "backups"
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    return d


def _index_path() -> Path:
    return _backups_dir() / _INDEX_NAME


def _load_index() -> dict:
    p = _index_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(idx: dict) -> None:
    """Write index.json atomically at 0600."""
    p = _index_path()
    text = json.dumps(idx, indent=2, sort_keys=True) + "\n"
    config._atomic_write(p, text, mode=0o600)


def _chmod_archive(dest: Path) -> None:
    """Recursively chmod dest: files 0600, dirs 0700."""
    for item in dest.rglob("*"):
        try:
            if item.is_symlink():
                continue
            if item.is_dir():
                os.chmod(item, 0o700)
            else:
                os.chmod(item, 0o600)
        except OSError:
            pass
    # Also set the root dest itself.
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass


def _kernel_backup_run(root: Path, dest: Path, tier: str) -> int:
    """Run the kernel's backup.py run --dest DEST --tier TIER under the root."""
    backup_py = root / "_tools" / "backup.py"
    if not backup_py.exists():
        raise FileNotFoundError(f"backup.py not found at {backup_py}")

    from .agentloop.verbtools import _scrubbed_env
    proc = subprocess.run(
        [
            sys.executable, str(backup_py),
            "--root", str(root),
            "run",
            "--dest", str(dest),
            "--tier", tier,
        ],
        cwd=str(root),
        env=_scrubbed_env(),
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


def _kernel_verify_restore(root: Path, tier: str = "all") -> int:
    """Run the kernel's backup.py verify-restore as a post-restore self-check."""
    backup_py = root / "_tools" / "backup.py"
    if not backup_py.exists():
        return 1  # best effort

    from .agentloop.verbtools import _scrubbed_env
    proc = subprocess.run(
        [
            sys.executable, str(backup_py),
            "--root", str(root),
            "verify-restore",
            "--tier", tier,
        ],
        cwd=str(root),
        env=_scrubbed_env(),
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

def cmd_backup(argv: list[str]) -> int:
    """oracle backup [NAME] [--out DIR] [--tier TIER] | --profile"""
    import argparse
    ap = argparse.ArgumentParser(prog="oracle backup")
    ap.add_argument("name", nargs="?", help="instance name (default: auto-resolve)")
    ap.add_argument("--out", help="backup destination directory (default: ~/.oracle/backups/<name>/<ts>/)")
    ap.add_argument(
        "--tier", default="0",
        help="backup tier: 0 (default, excludes _data.nosync), 1, 2, all (includes _data.nosync)",
    )
    ap.add_argument("--profile", action="store_true", help="backup config.json only")
    ns = ap.parse_args(argv)

    if ns.profile:
        return _cmd_backup_profile(ns)

    cfg = config.load_config()
    from . import cli as cli_mod
    name, root = cli_mod.resolve_instance(cfg, ns.name)
    root = Path(root).resolve()

    ts = _now_iso()
    if ns.out:
        dest = Path(ns.out).expanduser().resolve()
    else:
        dest = _backups_dir() / name / ts
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)

    # Acquire NB root lock (refuse if busy / serve running).
    try:
        with root_lock(name, nb=True):
            rc = _kernel_backup_run(root, dest, ns.tier)
    except BlockingIOError:
        print(
            f"oracle backup: root busy — stop `oracle serve` or retry",
            file=sys.stderr,
        )
        return 1

    if rc != 0:
        print(f"oracle backup: kernel backup.py run failed (rc={rc})", file=sys.stderr)
        return rc

    # Post-run: chmod pass.
    _chmod_archive(dest)

    # Read the manifest and record in index.
    manifest_path = dest / _MANIFEST_NAME
    if not manifest_path.exists():
        print(
            f"oracle backup: backup-manifest.json not found at {manifest_path}",
            file=sys.stderr,
        )
        return 1

    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = _sha256_bytes(manifest_bytes)

    idx = _load_index()
    entry_key = str(dest)
    idx[entry_key] = {
        "instance": name,
        "root": str(root),
        "ts": ts,
        "dest": str(dest),
        "manifest_sha256": manifest_sha,
    }
    _save_index(idx)

    print(f"oracle backup: {name} -> {dest}  (manifest sha256: {manifest_sha[:12]}...)")
    return 0


def _cmd_backup_profile(ns) -> int:
    """Backup config.json only (--profile)."""
    cfg_path = config.config_path()
    if not cfg_path.exists():
        print("oracle backup --profile: no config.json found", file=sys.stderr)
        return 1

    ts = _now_iso()
    if ns.out:
        dest = Path(ns.out).expanduser().resolve()
    else:
        dest = _backups_dir() / "profile" / ts
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)

    # Copy config.json only — NEVER .env.
    src = cfg_path
    if _is_shell_secret(src):
        print(
            f"oracle backup --profile: refusing to archive secret file {src.name!r}",
            file=sys.stderr,
        )
        return 2

    dst = dest / src.name
    dst_tmp = None
    try:
        src_hash = _sha256_file(src)
        old_umask = os.umask(0o077)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(dest), prefix=".tmp-", suffix="~")
            dst_tmp = Path(tmp)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    with open(src, "rb") as fsrc:
                        for chunk in iter(lambda: fsrc.read(_COPY_CHUNK), b""):
                            fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, str(dst))
                dst_tmp = None
            except BaseException:
                try:
                    Path(tmp).unlink()
                except OSError:
                    pass
                raise
        finally:
            os.umask(old_umask)
    except Exception as exc:
        print(f"oracle backup --profile: copy failed: {exc}", file=sys.stderr)
        return 1

    # Verify the copy.
    dst_hash = _sha256_file(dst)
    if dst_hash != src_hash:
        print(
            f"oracle backup --profile: hash mismatch after copy (src={src_hash[:12]}, dst={dst_hash[:12]})",
            file=sys.stderr,
        )
        try:
            dst.unlink()
        except OSError:
            pass
        return 1

    # Write a minimal backup-manifest.json.
    manifest: dict = {
        "root": str(cfg_path.parent),
        "dest": str(dest),
        "tier": "profile",
        "tiers": [],
        "created_at": ts,
        "count": 1,
        "total_bytes": dst.stat().st_size,
        "secrets_excluded": 0,
        "files": [{"rel": src.name, "sha256": dst_hash, "bytes": dst.stat().st_size}],
    }
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    manifest_path = dest / _MANIFEST_NAME
    config._atomic_write(manifest_path, manifest_text, mode=0o600)

    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = _sha256_bytes(manifest_bytes)

    idx = _load_index()
    entry_key = str(dest)
    idx[entry_key] = {
        "instance": "__profile__",
        "root": str(cfg_path.parent),
        "ts": ts,
        "dest": str(dest),
        "manifest_sha256": manifest_sha,
    }
    _save_index(idx)

    _chmod_archive(dest)

    print(f"oracle backup --profile: config.json -> {dest}  (sha256: {dst_hash[:12]}...)")
    return 0


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def cmd_restore(argv: list[str]) -> int:
    """oracle restore NAME --from PATH [--allow-cross-origin] [--trust-archive]"""
    import argparse
    ap = argparse.ArgumentParser(prog="oracle restore")
    ap.add_argument("name", help="instance name to restore into")
    ap.add_argument("--from", dest="from_path", required=True,
                    help="path to the backup archive directory")
    ap.add_argument("--allow-cross-origin", action="store_true",
                    help="allow restoring an archive from a different instance root")
    ap.add_argument("--trust-archive", action="store_true",
                    help="trust an archive not recorded in the profile index")
    ns = ap.parse_args(argv)

    cfg = config.load_config()
    from . import cli as cli_mod
    name, target_root = cli_mod.resolve_instance(cfg, ns.name)
    target_root = Path(target_root).resolve()

    archive = Path(ns.from_path).expanduser().resolve()

    # --- Step 1: Read backup-manifest.json; check index anchor ---
    manifest_path = archive / _MANIFEST_NAME
    if not manifest_path.exists():
        print(
            f"oracle restore: no backup-manifest.json found at {archive}",
            file=sys.stderr,
        )
        return 2

    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"oracle restore: cannot read backup-manifest.json: {exc}", file=sys.stderr)
        return 2

    if not isinstance(manifest, dict) or "files" not in manifest:
        print("oracle restore: backup-manifest.json is malformed", file=sys.stderr)
        return 2

    live_manifest_sha = _sha256_bytes(manifest_bytes)
    idx = _load_index()
    archive_key = str(archive)

    if archive_key in idx:
        recorded = idx[archive_key]
        if live_manifest_sha != recorded.get("manifest_sha256", ""):
            print(
                f"oracle restore: REFUSED — backup-manifest.json has been tampered with.\n"
                f"  Recorded sha256:  {recorded.get('manifest_sha256', '?')[:16]}...\n"
                f"  Live sha256:      {live_manifest_sha[:16]}...",
                file=sys.stderr,
            )
            return 2
    else:
        # Not in index — require --trust-archive.
        if not ns.trust_archive:
            print(
                f"oracle restore: REFUSED — archive {archive} is not recorded in the "
                f"profile index (~/.oracle/backups/index.json).\n"
                f"  Pass --trust-archive to restore an unregistered archive.",
                file=sys.stderr,
            )
            return 2

    # --- Step 2: Origin binding ---
    manifest_root = manifest.get("root", "")
    try:
        manifest_root_resolved = Path(manifest_root).resolve()
    except (TypeError, ValueError):
        manifest_root_resolved = Path("/nonexistent/placeholder")

    if manifest_root_resolved != target_root:
        if not ns.allow_cross_origin:
            print(
                f"oracle restore: REFUSED — archive was made from a different root.\n"
                f"  Archive root:  {manifest_root_resolved}\n"
                f"  Target root:   {target_root}\n"
                f"  Pass --allow-cross-origin to override (interactive confirmation required).",
                file=sys.stderr,
            )
            return 2
        # Cross-origin override: print both roots and require interactive confirmation.
        print(
            f"oracle restore: CROSS-ORIGIN restore requested.\n"
            f"  Archive root:  {manifest_root_resolved}\n"
            f"  Target root:   {target_root}"
        )
        if not sys.stdin.isatty():
            print(
                "oracle restore: REFUSED — cross-origin restore requires interactive TTY confirmation.",
                file=sys.stderr,
            )
            return 2
        try:
            answer = input("Confirm cross-origin restore [yes/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\noracle restore: aborted", file=sys.stderr)
            return 1
        if answer != "yes":
            print("oracle restore: aborted by user")
            return 1

    # --- Step 3: Containment check (nothing written yet) ---
    files_meta: list[dict] = manifest.get("files", [])
    for entry in files_meta:
        rel_str = entry.get("rel", "")
        # Reject absolute paths.
        if Path(rel_str).is_absolute():
            print(
                f"oracle restore: REFUSED — manifest entry has absolute path: {rel_str!r}",
                file=sys.stderr,
            )
            return 2
        # Reject paths with '..' components.
        parts = Path(rel_str).parts
        if ".." in parts:
            print(
                f"oracle restore: REFUSED — manifest entry contains '..': {rel_str!r}",
                file=sys.stderr,
            )
            return 2
        # Must resolve strictly under target root.
        resolved = (target_root / rel_str).resolve()
        try:
            resolved.relative_to(target_root)
        except ValueError:
            print(
                f"oracle restore: REFUSED — manifest entry escapes target root: {rel_str!r}",
                file=sys.stderr,
            )
            return 2

    # All containment checks passed — acquire NB lock and proceed.
    try:
        with root_lock(name, nb=True):
            return _do_restore(archive, target_root, manifest, files_meta, name)
    except BlockingIOError:
        print(
            f"oracle restore: root busy — stop `oracle serve` or retry",
            file=sys.stderr,
        )
        return 1


def _do_restore(
    archive: Path,
    target_root: Path,
    manifest: dict,
    files_meta: list[dict],
    name: str,
) -> int:
    """Steps 4–5: per-file verified copy + optional verify-restore."""
    already_restored: list[str] = []

    # --- Step 4: Copy file-by-file, verify sha256 BEFORE writing ---
    for entry in files_meta:
        rel_str = entry.get("rel", "")
        expected_sha = entry.get("sha256", "")
        src = archive / rel_str

        if not src.exists():
            print(
                f"oracle restore: ABORTED — archive file missing: {rel_str!r}\n"
                f"  Files already restored ({len(already_restored)}): "
                + (", ".join(already_restored) if already_restored else "(none)"),
                file=sys.stderr,
            )
            return 2

        # Verify the archive copy sha256 BEFORE writing to the target.
        actual_sha = _sha256_file(src)
        if actual_sha != expected_sha:
            print(
                f"oracle restore: ABORTED — sha256 mismatch for {rel_str!r}\n"
                f"  Expected: {expected_sha[:16]}...\n"
                f"  Actual:   {actual_sha[:16]}...\n"
                f"  Files already restored ({len(already_restored)}): "
                + (", ".join(already_restored) if already_restored else "(none)"),
                file=sys.stderr,
            )
            return 2

        # Hash verified — now write to target root (matching source modes).
        dst = target_root / rel_str
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            _copy_verified(src, dst, expected_sha)
        except ValueError as exc:
            print(
                f"oracle restore: ABORTED — write failed for {rel_str!r}: {exc}\n"
                f"  Files already restored ({len(already_restored)}): "
                + (", ".join(already_restored) if already_restored else "(none)"),
                file=sys.stderr,
            )
            return 2

        already_restored.append(rel_str)

    print(f"oracle restore: restored {len(already_restored)} file(s) into {target_root}")

    # --- Step 5: Optional kernel verify-restore self-check ---
    tier = manifest.get("tier", "all")
    vr_rc = _kernel_verify_restore(target_root, tier)
    if vr_rc != 0:
        print(
            f"oracle restore: verify-restore self-check returned rc={vr_rc} "
            f"(this is a live-root round-trip test; it cannot validate the archive itself)",
            file=sys.stderr,
        )
    else:
        print("oracle restore: verify-restore self-check passed")

    return 0


def _copy_verified(src: Path, dst: Path, expected_sha: str) -> None:
    """Copy src to dst and verify the copy has the expected sha256.

    On a hash mismatch the bad dst is removed and ValueError is raised.
    """
    try:
        old_umask = os.umask(0o077)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".tmp-restore-", suffix="~")
            tmp_path = Path(tmp)
            try:
                with os.fdopen(fd, "wb") as fh:
                    with open(src, "rb") as fsrc:
                        for chunk in iter(lambda: fsrc.read(_COPY_CHUNK), b""):
                            fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, str(dst))
                tmp_path = None
            except BaseException:
                try:
                    Path(tmp).unlink()
                except OSError:
                    pass
                raise
        finally:
            os.umask(old_umask)
    except Exception as exc:
        raise ValueError(f"copy failed {src} -> {dst}: {exc}") from exc

    # Verify post-write hash.
    actual = _sha256_file(dst)
    if actual != expected_sha:
        try:
            dst.unlink()
        except OSError:
            pass
        raise ValueError(
            f"post-write hash mismatch: expected {expected_sha[:12]}, got {actual[:12]}"
        )


# ---------------------------------------------------------------------------
# CLI entry points (called from cli.py)
# ---------------------------------------------------------------------------

def cmd_backup_dispatch(argv: list[str]) -> int:
    """Entry point for ``oracle backup ...``"""
    return cmd_backup(argv)


def cmd_restore_dispatch(argv: list[str]) -> int:
    """Entry point for ``oracle restore ...``"""
    return cmd_restore(argv)
