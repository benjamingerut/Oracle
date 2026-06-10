#!/usr/bin/env python3
"""upgrade.py -- tool-layer-ONLY kernel migration, hash-verified.

This reconciles local sovereignty with patchability: kernel tool updates can
reach an already-spawned oracle WITHOUT a destructive re-spawn, but ONLY the
executable tool layer (``_tools/``) is ever replaced. ``oracle.yml``,
``Memory.nosync/``, ``Meta.nosync/``, ``Connectors/``, all root doctrine
``*.md`` files, and any business config are NEVER touched.

Guarantees (each enforced here, in order):
  1. NEVER headless. ``apply`` requires an explicit ``--approve`` admin flag; a
     run without it (or with ``ORACLE_HEADLESS`` set) refuses. (Admin identity
     comes from a flag -- advisory-plus-logged, per GOVERNANCE.md.)
  2. Tool-layer only. The set of files the upgrade will write is computed and
     asserted to live ENTIRELY under ``_tools/``. If the incoming bundle's
     manifest references any path outside ``_tools/`` the upgrade REFUSES.
  3. Hash-verified. Every incoming ``_tools`` file is sha256-checked against the
     incoming ``.kernel-manifest.json``; a mismatch (tampered/partial bundle)
     REFUSES before any swap.
  4. Backed up. The CURRENT ``_tools`` is copied (verified) into a timestamped
     backup under ``Meta.nosync/tool-backups/`` before replacement, so a bad
     upgrade is reversible.
  5. Ordered migrations. After the swap, ``migrations.apply_all`` runs every
     migration in ``NNNN`` order (idempotent).
  6. Re-verified. ``oracle_lint`` (and, when available, the kernel ``pytest``
     suite) is re-run post-swap; a failure is reported (and the backup remains
     for rollback).

``.kernel-manifest.json`` format consumed/produced here::

    {
      "tools_version": "3.0.0",
      "files": { "<relpath-under-_tools>": "<sha256-hex>", ... }
    }

CLI:
    python3 _tools/upgrade.py --root R check  --from-kernel SRC
    python3 _tools/upgrade.py --root R apply  --from-kernel SRC --approve ADMIN
                                              [--skip-tests] [--skip-lint]

``SRC`` is a kernel directory that contains a new ``_tools/`` and a
``.kernel-manifest.json`` describing it.

Stdlib only. The only write sinks are (a) the timestamped tool-backup copy and
(b) the new ``_tools`` files -- both into locations this module controls under
the oracle root (never user-derived from outside) -- via a verified-copy sink
carrying the ``# safe_paths-internal`` marker the no-bypass guard allowlists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_TOOLS = "_tools"
_MANIFEST = ".kernel-manifest.json"
_TOOL_BACKUPS = "Meta.nosync/tool-backups"

# Paths that the upgrade is FORBIDDEN to ever write (sovereign data/doctrine).
# Any incoming manifest entry resolving outside _tools/ triggers a refusal; this
# list is the human-readable statement of intent surfaced in errors.
SOVEREIGN_ROOTS = (
    "oracle.yml",
    "Memory.nosync",
    "Meta.nosync",
    "Connectors",
)


class UpgradeRefused(Exception):
    """Raised when an upgrade is refused for a safety/integrity reason."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now_compact() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:  # read-only source: not a write target
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_tools_manifest(kernel_dir: Path) -> dict:
    """Hash every ``_tools`` file (recursively) -> manifest dict.

    Skips ``__pycache__`` and ``.pyc`` (rebuildable). The relpaths are POSIX-style
    and rooted at ``_tools/`` so they are portable and comparable across hosts.
    """
    kernel_dir = Path(kernel_dir)
    tools = kernel_dir / _TOOLS
    files: dict[str, str] = {}
    if tools.exists():
        for p in sorted(tools.rglob("*")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            rel = p.relative_to(kernel_dir).as_posix()
            files[rel] = sha256_file(p)
    return {"files": files}


def load_manifest(kernel_dir: Path) -> dict:
    """Read ``.kernel-manifest.json`` from a kernel dir; compute it if absent.

    A bundle SHOULD ship a manifest; if it does not, we derive one from the
    bundle's own ``_tools`` so an integrity baseline still exists (the derived
    manifest trivially matches the bundle, but the tool-layer-only and backup
    guarantees still hold).
    """
    kernel_dir = Path(kernel_dir)
    mpath = kernel_dir / _MANIFEST
    if mpath.exists():
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            raise UpgradeRefused(f"incoming manifest unreadable: {exc}")
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            raise UpgradeRefused("incoming manifest missing 'files' map")
        return data
    return compute_tools_manifest(kernel_dir)


def _assert_tool_layer_only(manifest: dict) -> list[str]:
    """Return the manifest's file list, REFUSING if any entry escapes _tools/.

    This is the structural enforcement of "tool-layer only": a malicious or
    mistaken bundle that lists ``oracle.yml`` or ``Memory.nosync/...`` is rejected
    before a single byte is swapped.
    """
    files = manifest.get("files") or {}
    out: list[str] = []
    for rel in files:
        rel_posix = Path(rel).as_posix()
        parts = Path(rel_posix).parts
        if Path(rel_posix).is_absolute() or ".." in parts:
            raise UpgradeRefused(f"refused: unsafe manifest path {rel!r}")
        if not parts or parts[0] != _TOOLS:
            raise UpgradeRefused(
                f"refused: upgrade would touch non-tool path {rel!r}; "
                f"only files under {_TOOLS}/ may be replaced "
                f"(sovereign data/doctrine: {', '.join(SOVEREIGN_ROOTS)})"
            )
        out.append(rel_posix)
    return out


def _verify_incoming_hashes(src_kernel: Path, manifest: dict, rels: list[str]) -> None:
    """Every incoming _tools file must hash-match the incoming manifest."""
    files = manifest["files"]
    for rel in rels:
        f = src_kernel / rel
        if not f.is_file():
            raise UpgradeRefused(f"refused: bundle missing file {rel!r}")
        actual = sha256_file(f)
        expected = files[rel]
        if actual != expected:
            raise UpgradeRefused(
                f"refused: hash mismatch for {rel!r} "
                f"(manifest {expected[:12]} != bundle {actual[:12]})"
            )


def _verified_copy(src: Path, dst: Path) -> str:
    """copy -> fsync -> sha256-verify; source-preserving. Returns full sha256."""
    src = Path(src)
    dst = Path(dst)
    if not src.is_file():
        raise UpgradeRefused(f"upgrade: missing src {src}")
    src_hash = sha256_file(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(src, "rb") as fsrc:  # read-only source
            data = fsrc.read()
        # safe_paths-internal: verified-copy upgrade sink (upgrade-controlled dst)
        with open(dst, "wb") as fdst:  # safe_paths-internal
            fdst.write(data)
            fdst.flush()
            os.fsync(fdst.fileno())
        dst_hash = sha256_file(dst)
    except Exception as exc:
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise UpgradeRefused(f"upgrade: copy failed {src} -> {dst}: {exc}") from exc
    if dst_hash != src_hash:
        try:
            dst.unlink()
        except OSError:
            pass
        raise UpgradeRefused(
            f"upgrade: post-copy hash mismatch for {src.name}"
        )
    return dst_hash


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def check(root: Path, src_kernel: Path) -> dict:
    """Dry comparison: what WOULD change, with integrity + scope verified.

    Returns a report dict (no writes). ``ok`` True means the bundle is a valid,
    tool-layer-only, hash-consistent upgrade candidate. ``changed``/``added``/
    ``removed`` list the relpaths whose content differs from the installed kernel.
    """
    root = Path(root).resolve()
    src_kernel = Path(src_kernel).resolve()
    report: dict = {
        "root": str(root),
        "from_kernel": str(src_kernel),
        "ok": False,
        "tool_layer_only": False,
        "hash_verified": False,
        "incoming_version": "",
        "installed_version": _installed_version(root),
        "changed": [],
        "added": [],
        "removed": [],
        "refusal": None,
    }
    try:
        manifest = load_manifest(src_kernel)
        report["incoming_version"] = str(manifest.get("tools_version", ""))
        rels = _assert_tool_layer_only(manifest)
        report["tool_layer_only"] = True
        _verify_incoming_hashes(src_kernel, manifest, rels)
        report["hash_verified"] = True

        installed = compute_tools_manifest(root)["files"]
        incoming = manifest["files"]
        for rel, sha in incoming.items():
            if rel not in installed:
                report["added"].append(rel)
            elif installed[rel] != sha:
                report["changed"].append(rel)
        for rel in installed:
            if rel not in incoming:
                report["removed"].append(rel)
        report["ok"] = True
    except UpgradeRefused as exc:
        report["refusal"] = str(exc)
    return report


def _installed_version(root: Path) -> str:
    cfg = root / "oracle.yml"
    if not cfg.exists():
        return ""
    try:
        try:
            import oracle_yaml  # type: ignore
        except Exception:  # pragma: no cover
            from . import oracle_yaml  # type: ignore
        data = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict):
        kernel = data.get("kernel") or {}
        if isinstance(kernel, dict):
            return str(kernel.get("tools_version", "") or "")
    return ""


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def apply(
    root: Path,
    src_kernel: Path,
    *,
    approve: Optional[str] = None,
    skip_tests: bool = False,
    skip_lint: bool = False,
    run_migrations: bool = True,
) -> dict:
    """Perform the tool-layer-only upgrade. NEVER headless; admin approval required.

    Order: refuse-unless-approved -> verify scope+hashes -> back up current
    _tools -> swap in new _tools (verified) -> run ordered migrations -> re-run
    lint (+ pytest). Returns a report dict; raises UpgradeRefused on any safety
    or integrity failure BEFORE the swap (so the oracle is never left half-swapped
    for a refusal reason).
    """
    root = Path(root).resolve()
    src_kernel = Path(src_kernel).resolve()

    # GUARANTEE 1: never headless / admin approval required.
    if os.environ.get("ORACLE_HEADLESS"):
        raise UpgradeRefused(
            "refused: upgrade must never run headless (ORACLE_HEADLESS set)"
        )
    if not (approve and str(approve).strip()):
        raise UpgradeRefused(
            "refused: upgrade requires explicit admin approval (--approve NAME)"
        )

    manifest = load_manifest(src_kernel)
    # GUARANTEE 2: tool-layer only.
    rels = _assert_tool_layer_only(manifest)
    # GUARANTEE 3: hash-verified bundle.
    _verify_incoming_hashes(src_kernel, manifest, rels)

    report: dict = {
        "root": str(root),
        "from_kernel": str(src_kernel),
        "approved_by": str(approve).strip(),
        "incoming_version": str(manifest.get("tools_version", "")),
        "backup_dir": "",
        "swapped": [],
        "migrations": [],
        "lint_ok": None,
        "tests_ok": None,
        "ok": False,
    }

    # GUARANTEE 4: back up current _tools before replacing.
    backup_dir = root / _TOOL_BACKUPS / _now_compact()
    cur_tools = root / _TOOLS
    if cur_tools.exists():
        for p in sorted(cur_tools.rglob("*")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            rel = p.relative_to(root)
            _verified_copy(p, backup_dir / rel)
    report["backup_dir"] = str(backup_dir.relative_to(root))

    # Swap in new _tools (verified copy). We write ONLY the manifest's files,
    # each of which has already been asserted to live under _tools/.
    for rel in rels:
        src = src_kernel / rel
        dst = root / rel
        _verified_copy(src, dst)
        report["swapped"].append(rel)

    # GUARANTEE 5: ordered, idempotent migrations.
    if run_migrations:
        report["migrations"] = _run_migrations(root)

    # GUARANTEE 6: re-verify post-swap (lint, then pytest).
    if not skip_lint:
        report["lint_ok"] = _run_lint(root)
    if not skip_tests:
        report["tests_ok"] = _run_pytest(root)

    report["ok"] = (
        (skip_lint or report["lint_ok"] is True)
        and (skip_tests or report["tests_ok"] in (True, None))
    )
    return report


def _run_migrations(root: Path) -> list[dict]:
    try:
        import migrations  # type: ignore
    except Exception:  # pragma: no cover - package import fallback
        from . import migrations  # type: ignore
    return migrations.apply_all(root)


def _run_lint(root: Path) -> bool:
    """Re-run the schema-validating linter against the upgraded oracle."""
    try:
        import oracle_lint  # type: ignore
    except Exception:  # pragma: no cover - import fallback
        from . import oracle_lint  # type: ignore
    baseline = root / "known-failures.txt"
    schemas = root / _TOOLS / "schemas"
    report = oracle_lint.run(
        root,
        baseline_path=baseline if baseline.exists() else None,
        schemas_dir=schemas if schemas.exists() else None,
    )
    return bool(report.get("ok"))


def _run_pytest(root: Path) -> Optional[bool]:
    """Re-run the kernel's shipped pytest suite post-swap, if pytest is present.

    Returns True/False on a real run, or None when pytest is unavailable (the
    kernel is stdlib-only at runtime; pytest is a dev dependency, so its absence
    is not a failure -- lint remains the binding gate). The tests live INSIDE the
    kernel precisely so upgrade can re-verify here.
    """
    tests_dir = root / "tests"
    if not tests_dir.exists():
        return None
    try:
        import pytest  # type: ignore  # noqa: F401
    except Exception:
        return None
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(tests_dir)],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Tool-layer-only kernel upgrade (hash-verified, never headless)"
    )
    ap.add_argument("--root", default=".", help="oracle root to upgrade")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="dry comparison (no writes)")
    p_check.add_argument("--from-kernel", required=True, help="incoming kernel dir")

    p_apply = sub.add_parser("apply", help="perform the upgrade (admin-approved)")
    p_apply.add_argument("--from-kernel", required=True, help="incoming kernel dir")
    p_apply.add_argument(
        "--approve", required=True, help="admin name approving the upgrade"
    )
    p_apply.add_argument("--skip-tests", action="store_true")
    p_apply.add_argument("--skip-lint", action="store_true")

    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        if args.cmd == "check":
            report = check(root, Path(args.from_kernel))
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        if args.cmd == "apply":
            report = apply(
                root,
                Path(args.from_kernel),
                approve=args.approve,
                skip_tests=args.skip_tests,
                skip_lint=args.skip_lint,
            )
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
    except UpgradeRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
