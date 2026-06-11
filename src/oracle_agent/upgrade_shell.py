"""upgrade_shell.py -- ``oracle upgrade`` family (P1-T4, P1-T5).

    oracle upgrade [--check]                     # per-instance status report
    oracle upgrade kernel NAME [--approve ADMIN] [--force-downgrade]
    oracle upgrade self --from-dir DIR           # maintainer re-vendor (git checkout only)

Direction-aware ``--check`` compares the manifest aggregate_sha256 AND the
kernel check() changed/added/removed sets -- NOT just the tools_version string
-- so diverged (same version, different hash) is detected.

``upgrade kernel`` behaviors (per PHASE-1-foundation-hardening.md):
  1. Self-verify vendored tree vs shipped .kernel-manifest.json (refuses on
     mismatch -- detects locally tampered/corrupted vendored tree).
  2. Kernel check() first -- zero changes -> "already current", no apply.
  3. Approval ONLY from --approve or interactive TTY; non-TTY without flag
     refuses (never become the headless bypass of upgrade.py's guarantee).
  4. Acquires per-root lock non-blocking; refuses if busy.
  5. On apply ok:false or mid-swap exception: hash-verified copy-back from
     Meta.nosync/tool-backups/<ts>/, then re-run check to prove recovery.
  6. Downgrade refusal (--force-downgrade overrides with printed warning).
  7. Scrubbed env throughout.

``upgrade self`` behaviors:
  1. Refuses outside a git checkout (no .git / git rev-parse fails).
  2. Diffs incoming oracle_lint.py + tests/ vs current vendored copies;
     requires explicit confirmation if changed.
  3. Copies tree, re-renders manifest, runs ``make check``.
  4. Refuses to leave a failing tree (restores previous state on failure).
  5. No-op detection: aggregate_sha256 + files equality (excluding
     'generated' timestamp) -- byte-identical re-vendor is truly a no-op.

KERNEL_VERSION sourcing: manifest.render() reads tools_version from
_tools/KERNEL_VERSION in the kernel tree (manifest.py P1-T5).

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from . import config, manifest as manifest_mod
from .agentloop.verbtools import _scrubbed_env
from .service.scheduler import root_lock

_VENDORED_KERNEL = Path(__file__).resolve().parent / "assets" / "oracle-kernel"
_MANIFEST_FILE = ".kernel-manifest.json"
_TOOLS_DIR = "_tools"

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _read_manifest(kernel_dir: Path) -> dict:
    """Load a .kernel-manifest.json; returns {} on missing/bad."""
    mp = kernel_dir / _MANIFEST_FILE
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_files(kernel_dir: Path) -> dict[str, str]:
    """Hash every _tools file -> {posix relpath: sha256}.  Mirrors manifest.compute_files."""
    tools = kernel_dir / _TOOLS_DIR
    files: dict[str, str] = {}
    if tools.exists():
        for p in sorted(tools.rglob("*")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            rel = p.relative_to(kernel_dir).as_posix()
            files[rel] = _sha256_file(p)
    return files


def _aggregate_sha(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode())
        h.update(b"\n")
        h.update(files[rel].encode())
        h.update(b"\n")
    return h.hexdigest()


def _verify_vendored_tree(vendored: Path) -> tuple[bool, str]:
    """Self-verify vendored kernel tree against its shipped manifest.

    Returns (ok, message).  Computes live hashes and compares against
    the committed .kernel-manifest.json so we detect local tampering/corruption.
    """
    shipped = _read_manifest(vendored)
    if not shipped or not isinstance(shipped.get("files"), dict):
        return False, "vendored .kernel-manifest.json missing or malformed"
    expected_files = shipped["files"]
    live = _compute_files(vendored)
    missing = sorted(set(expected_files) - set(live))
    extra = sorted(set(live) - set(expected_files))
    changed = sorted(k for k in expected_files if k in live and live[k] != expected_files[k])
    if missing or extra or changed:
        parts = []
        if missing:
            parts.append(f"missing: {missing}")
        if extra:
            parts.append(f"extra: {extra}")
        if changed:
            parts.append(f"hash-mismatch: {changed}")
        return False, "vendored tree differs from .kernel-manifest.json -- " + "; ".join(parts)
    return True, "ok"


def _run_kernel_check(root: Path, vendored: Path) -> dict:
    """Run upgrade.py check via the root's own python; return the JSON report."""
    upgrade_script = root / _TOOLS_DIR / "upgrade.py"
    if not upgrade_script.exists():
        # Fall back to vendored upgrade.py if the root lacks one (fresh spawn).
        upgrade_script = vendored / _TOOLS_DIR / "upgrade.py"
    proc = subprocess.run(
        [sys.executable, str(upgrade_script), "--root", str(root),
         "check", "--from-kernel", str(vendored)],
        capture_output=True, text=True, env=_scrubbed_env(),
        cwd=str(root),
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "refusal": proc.stderr.strip() or proc.stdout.strip()}


def _run_kernel_apply(root: Path, vendored: Path, admin: str) -> dict:
    """Run upgrade.py apply; return the JSON report."""
    upgrade_script = root / _TOOLS_DIR / "upgrade.py"
    if not upgrade_script.exists():
        upgrade_script = vendored / _TOOLS_DIR / "upgrade.py"
    proc = subprocess.run(
        [sys.executable, str(upgrade_script), "--root", str(root),
         "apply", "--from-kernel", str(vendored), "--approve", admin],
        capture_output=True, text=True, env=_scrubbed_env(),
        cwd=str(root),
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "refusal": proc.stderr.strip() or proc.stdout.strip()}


def _latest_tool_backup(root: Path) -> Optional[Path]:
    """Return the most recent timestamped tool-backup dir under Meta.nosync/tool-backups/."""
    backups = root / "Meta.nosync" / "tool-backups"
    if not backups.is_dir():
        return None
    candidates = sorted(
        (d for d in backups.iterdir() if d.is_dir()),
        key=lambda d: d.name, reverse=True,
    )
    return candidates[0] if candidates else None


def _copy_back_from_backup(root: Path, backup_dir: Path) -> tuple[bool, str]:
    """Hash-verified copy-back of _tools from backup_dir into root.

    backup_dir is a timestamped dir that mirrors root layout (i.e. the
    _tools files live at backup_dir/_tools/...).  Each file is copied and
    sha256-verified.  Returns (ok, message).
    """
    src_tools = backup_dir / _TOOLS_DIR
    if not src_tools.exists():
        return False, f"backup dir has no _tools: {backup_dir}"
    dst_tools = root / _TOOLS_DIR
    errors = []
    for src in sorted(src_tools.rglob("*")):
        if not src.is_file():
            continue
        if "__pycache__" in src.parts or src.suffix == ".pyc":
            continue
        rel = src.relative_to(backup_dir)
        dst = root / rel
        try:
            src_hash = _sha256_file(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(src, "rb") as fsrc:
                data = fsrc.read()
            with open(dst, "wb") as fdst:
                fdst.write(data)
                fdst.flush()
                os.fsync(fdst.fileno())
            if _sha256_file(dst) != src_hash:
                errors.append(f"hash-mismatch after copy: {rel}")
        except OSError as exc:
            errors.append(f"copy failed {rel}: {exc}")
    if errors:
        return False, "copy-back errors: " + "; ".join(errors)
    return True, "ok"


def _pkg_tuple(version: str) -> tuple:
    """Parse a semver-ish version string into a comparable tuple."""
    try:
        return tuple(int(x) for x in version.split("."))
    except (ValueError, AttributeError):
        return (0,)


# --------------------------------------------------------------------------- #
# upgrade --check
# --------------------------------------------------------------------------- #

def cmd_check(argv: list[str]) -> int:
    """Per-instance direction report: equal | ahead | behind | diverged."""
    import argparse
    ap = argparse.ArgumentParser(prog="oracle upgrade --check")
    ap.add_argument("name", nargs="?", help="instance name (default: auto-resolve)")
    ns = ap.parse_args(argv)

    vendored = _VENDORED_KERNEL
    vendored_manifest = _read_manifest(vendored)
    vendored_agg = vendored_manifest.get("aggregate_sha256", "")
    vendored_ver = vendored_manifest.get("tools_version", "")

    cfg = config.load_config()
    roots = config.instance_roots(cfg)
    if ns.name:
        if ns.name not in roots:
            print(f"oracle upgrade: no instance {ns.name!r}", file=sys.stderr)
            return 2
        roots = {ns.name: roots[ns.name]}
    if not roots:
        print("oracle upgrade: no instances registered", file=sys.stderr)
        return 1

    any_not_equal = False
    for iname, iroot in sorted(roots.items()):
        iroot = Path(iroot)
        root_manifest = _read_manifest(iroot)
        root_agg = root_manifest.get("aggregate_sha256", "")
        root_ver = root_manifest.get("tools_version", "")

        if root_agg == vendored_agg:
            direction = "equal"
        else:
            # Use kernel check() for precise changed/added/removed sets.
            check_report = _run_kernel_check(iroot, vendored)
            changed = check_report.get("changed", [])
            added = check_report.get("added", [])
            removed = check_report.get("removed", [])

            pkg_v = _pkg_tuple(vendored_ver)
            root_v = _pkg_tuple(root_ver)
            if root_ver == vendored_ver and (changed or added or removed):
                direction = "diverged"
            elif pkg_v > root_v:
                direction = "behind"   # root is older than vendored
            elif pkg_v < root_v:
                direction = "ahead"    # root is newer than vendored
            else:
                # Version equal but hashes differ.
                direction = "diverged"

        print(f"  {iname}: {direction} "
              f"(root={root_ver or '?'}, packaged={vendored_ver or '?'})")
        if direction != "equal":
            any_not_equal = True

    return 1 if any_not_equal else 0


# --------------------------------------------------------------------------- #
# upgrade kernel NAME
# --------------------------------------------------------------------------- #

def cmd_upgrade_kernel(argv: list[str]) -> int:
    """Apply the vendored kernel to a named instance root."""
    import argparse
    ap = argparse.ArgumentParser(prog="oracle upgrade kernel")
    ap.add_argument("name", help="instance name")
    ap.add_argument("--approve", help="admin name approving the upgrade")
    ap.add_argument("--force-downgrade", action="store_true",
                    help="allow upgrading to an older kernel version")
    ns = ap.parse_args(argv)

    vendored = _VENDORED_KERNEL

    # 1. Self-verify vendored tree vs shipped manifest (P1S-2).
    ok, msg = _verify_vendored_tree(vendored)
    if not ok:
        print(f"oracle upgrade: REFUSED — vendored kernel integrity check failed: {msg}",
              file=sys.stderr)
        return 2

    cfg = config.load_config()
    roots = config.instance_roots(cfg)
    if ns.name not in roots:
        print(f"oracle upgrade: no instance {ns.name!r}", file=sys.stderr)
        return 2
    root = Path(roots[ns.name])

    vendored_manifest = _read_manifest(vendored)
    vendored_ver = vendored_manifest.get("tools_version", "")
    root_manifest = _read_manifest(root)
    root_ver = root_manifest.get("tools_version", "")

    # 2. Downgrade check (P1S-12).
    if (not ns.force_downgrade and vendored_ver and root_ver and
            _pkg_tuple(vendored_ver) < _pkg_tuple(root_ver)):
        print(
            f"oracle upgrade: REFUSED — instance '{ns.name}' is at {root_ver}, "
            f"packaged kernel is {vendored_ver} (older). "
            "Use --force-downgrade to override.",
            file=sys.stderr,
        )
        return 2
    if ns.force_downgrade:
        print(f"WARNING: force-downgrade: going from root {root_ver} to packaged {vendored_ver}",
              file=sys.stderr)

    # 3. Kernel check() -- short-circuit if already current (P1F-4).
    check_report = _run_kernel_check(root, vendored)
    if not check_report.get("ok"):
        print(f"oracle upgrade: kernel check refused: {check_report.get('refusal', '')}",
              file=sys.stderr)
        return 2

    changed = check_report.get("changed", [])
    added = check_report.get("added", [])
    removed = check_report.get("removed", [])
    if not changed and not added and not removed:
        print(f"oracle upgrade: instance '{ns.name}' is already current ({root_ver})")
        return 0

    # 4. Approval: --approve flag or interactive TTY prompt (P1S-7).
    admin = ns.approve
    if not admin:
        if not sys.stdin.isatty():
            print(
                "oracle upgrade: REFUSED — non-TTY without --approve flag. "
                "Pass --approve ADMIN to approve in non-interactive mode.",
                file=sys.stderr,
            )
            return 2
        try:
            admin = input("Approving admin name: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\noracle upgrade: aborted", file=sys.stderr)
            return 1
    if not admin:
        print("oracle upgrade: REFUSED — admin name must not be empty", file=sys.stderr)
        return 2

    # 5. Acquire NB root lock (P1S-13).
    try:
        _lock_ctx = root_lock(ns.name, nb=True)
    except Exception:
        _lock_ctx = None

    def _do_apply() -> int:
        nonlocal check_report
        # Run apply.
        try:
            apply_report = _run_kernel_apply(root, vendored, admin)
        except Exception as exc:
            apply_report = {"ok": False, "refusal": str(exc)}

        print(json.dumps(apply_report, indent=2))

        # 6. Failure contract: copy-back on ok:false or mid-swap exception (P1S-8/P1F-2).
        if not apply_report.get("ok"):
            print("oracle upgrade: apply failed; attempting recovery from tool-backup...",
                  file=sys.stderr)
            backup_dir = _latest_tool_backup(root)
            if backup_dir is None:
                print(
                    f"oracle upgrade: NO BACKUP FOUND under {root}/Meta.nosync/tool-backups/. "
                    "Manual recovery required.",
                    file=sys.stderr,
                )
                return 1
            cb_ok, cb_msg = _copy_back_from_backup(root, backup_dir)
            if not cb_ok:
                print(
                    f"oracle upgrade: copy-back FAILED: {cb_msg}\n"
                    f"  Backup path: {backup_dir}\n"
                    f"  Manual recovery: cp -r {backup_dir}/_tools {root}/_tools",
                    file=sys.stderr,
                )
                return 1
            # Re-run check to prove recovery.
            post_check = _run_kernel_check(root, vendored)
            if post_check.get("ok"):
                post_changed = post_check.get("changed", [])
                post_added = post_check.get("added", [])
                post_removed = post_check.get("removed", [])
                print(
                    f"oracle upgrade: copy-back OK; post-recovery check shows "
                    f"{len(post_changed)} changed, {len(post_added)} added, "
                    f"{len(post_removed)} removed (root needs upgrade).",
                    file=sys.stderr,
                )
            else:
                print(
                    f"oracle upgrade: copy-back done but post-recovery check errored: "
                    f"{post_check.get('refusal', '')}",
                    file=sys.stderr,
                )
            return 1
        return 0

    try:
        with root_lock(ns.name, nb=True):
            return _do_apply()
    except BlockingIOError:
        print(
            f"oracle upgrade: root busy — stop `oracle serve` or retry",
            file=sys.stderr,
        )
        return 1


# --------------------------------------------------------------------------- #
# upgrade self --from-dir DIR
# --------------------------------------------------------------------------- #

def _in_git_checkout(path: Path) -> bool:
    """Return True iff *path* is inside a git checkout (git rev-parse --git-dir succeeds)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(path), capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _diff_file(path_a: Path, path_b: Path) -> bool:
    """Return True iff the two files differ (by content)."""
    try:
        return path_a.read_bytes() != path_b.read_bytes()
    except OSError:
        return True


def _manifest_equal_ignoring_timestamp(m1: dict, m2: dict) -> bool:
    """True iff aggregate_sha256 and files are equal (generated timestamp excluded)."""
    return (m1.get("aggregate_sha256") == m2.get("aggregate_sha256") and
            m1.get("files") == m2.get("files"))


def cmd_upgrade_self(argv: list[str]) -> int:
    """Maintainer re-vendor: copies kernel tree from --from-dir, re-renders manifest,
    runs make check.  Refuses outside a git checkout."""
    import argparse
    ap = argparse.ArgumentParser(prog="oracle upgrade self")
    ap.add_argument("--from-dir", required=True, dest="from_dir",
                    help="source kernel directory (the dir that contains _tools/)")
    ns = ap.parse_args(argv)

    src = Path(ns.from_dir).expanduser().resolve()
    if not src.is_dir():
        print(f"oracle upgrade self: source not a directory: {src}", file=sys.stderr)
        return 2
    if not (src / _TOOLS_DIR).is_dir():
        print(f"oracle upgrade self: source has no _tools/ dir: {src}", file=sys.stderr)
        return 2

    # 1. Refuse outside a git checkout (P1S-1/2).
    repo_root = Path(__file__).resolve().parent.parent.parent
    if not _in_git_checkout(repo_root):
        print(
            "oracle upgrade self: REFUSED — not in a git checkout. "
            "upgrade self is maintainer-only and requires a git checkout "
            "so the re-rendered manifest lands as a reviewable diff.",
            file=sys.stderr,
        )
        return 2

    dst = _VENDORED_KERNEL

    # 2. No-op check: compare incoming tree hashes vs current vendored (excluding timestamp).
    incoming_files = _compute_files(src)
    current_files = _compute_files(dst)
    incoming_agg = _aggregate_sha(incoming_files)
    current_agg = _aggregate_sha(current_files)
    if incoming_agg == current_agg and incoming_files == current_files:
        print("oracle upgrade self: incoming tree is identical to current vendored — no-op.")
        return 0

    # 3. Gate-code diff confirmation: oracle_lint.py + tests/ (P1S-1).
    gate_diffs = []
    oracle_lint_src = src / _TOOLS_DIR / "oracle_lint.py"
    oracle_lint_dst = dst / _TOOLS_DIR / "oracle_lint.py"
    if oracle_lint_src.exists() and _diff_file(oracle_lint_src, oracle_lint_dst):
        gate_diffs.append("oracle_lint.py")
    # tests/ directory
    tests_src = src / "tests"
    tests_dst = dst / "tests"
    if tests_src.exists():
        for ts in sorted(tests_src.rglob("*")):
            if not ts.is_file():
                continue
            rel = ts.relative_to(tests_src)
            td = tests_dst / rel
            if _diff_file(ts, td):
                gate_diffs.append(f"tests/{rel}")

    if gate_diffs:
        print("oracle upgrade self: gate-code files changed in incoming tree:")
        for f in gate_diffs:
            print(f"    {f}")
        print(
            "  These are the lint/test files that gate this repo. "
            "Vetting kernel intent is YOUR manual code-review responsibility. "
            "Lint/tests prove conformance, not trustworthiness."
        )
        if not sys.stdin.isatty():
            print(
                "oracle upgrade self: REFUSED — gate-code diff detected and stdin is "
                "not a TTY; cannot prompt for confirmation.",
                file=sys.stderr,
            )
            return 2
        try:
            answer = input("Proceed with changed gate-code? [yes/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\noracle upgrade self: aborted", file=sys.stderr)
            return 1
        if answer != "yes":
            print("oracle upgrade self: aborted by user")
            return 1

    # 4. Save current state for rollback.
    backup_tmp = tempfile.mkdtemp(prefix="oracle_self_upgrade_backup_")
    try:
        _backup_dir = Path(backup_tmp)
        for p in sorted(dst.rglob("*")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            rel = p.relative_to(dst.parent)
            bk = _backup_dir / rel
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(p), str(bk))

        # 5. Copy tree from src to dst.
        # Remove old tree contents (except .kernel-manifest.json which we regenerate).
        if dst.exists():
            for item in sorted(dst.iterdir()):
                if item.name == _MANIFEST_FILE:
                    continue
                if item.is_dir() and item.name != "__pycache__":
                    shutil.rmtree(str(item))
                elif item.is_file():
                    item.unlink()

        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            rel = p.relative_to(src)
            d = dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(p), str(d))

        # 6. Re-render manifest (reads KERNEL_VERSION from _tools/KERNEL_VERSION).
        new_manifest = manifest_mod.render(dst)
        print(f"oracle upgrade self: manifest re-rendered "
              f"(tools_version={new_manifest.get('tools_version')}, "
              f"aggregate_sha256={new_manifest.get('aggregate_sha256', '')[:12]}...)")

        # 7. Run make check in the repo root.
        make_result = subprocess.run(
            ["make", "check"],
            cwd=str(repo_root),
            env=_scrubbed_env(),
        )
        if make_result.returncode != 0:
            raise RuntimeError(f"make check failed (rc={make_result.returncode})")

        print("oracle upgrade self: done.")
        return 0

    except Exception as exc:
        # Restore previous state.
        print(f"oracle upgrade self: FAILED ({exc}); restoring previous state...",
              file=sys.stderr)
        try:
            dst_in_backup = _backup_dir / dst.relative_to(dst.parent)
            if dst_in_backup.exists():
                for p in sorted(dst.rglob("*")):
                    if not p.is_file():
                        continue
                    if "__pycache__" in p.parts or p.suffix == ".pyc":
                        continue
                    try:
                        p.unlink()
                    except OSError:
                        pass
                for p in sorted(dst_in_backup.rglob("*")):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(dst_in_backup)
                    d = dst / rel
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(p), str(d))
                print("oracle upgrade self: previous state restored.", file=sys.stderr)
        except Exception as re:
            print(f"oracle upgrade self: RESTORE FAILED: {re}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(backup_tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# main dispatcher
# --------------------------------------------------------------------------- #

def cmd_upgrade(argv: list[str]) -> int:
    """Entry point for ``oracle upgrade ...``"""
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: oracle upgrade [--check]\n"
            "       oracle upgrade kernel NAME [--approve ADMIN] [--force-downgrade]\n"
            "       oracle upgrade self --from-dir DIR"
        )
        return 0

    if argv[0] == "--check":
        return cmd_check(argv[1:])

    if argv[0] == "kernel":
        return cmd_upgrade_kernel(argv[1:])

    if argv[0] == "self":
        return cmd_upgrade_self(argv[1:])

    # Bare "oracle upgrade" with no sub-command runs --check on all instances.
    if not argv:
        return cmd_check([])

    print(f"oracle upgrade: unknown sub-command {argv[0]!r}", file=sys.stderr)
    return 2
