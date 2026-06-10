#!/usr/bin/env python3
"""backup.py -- tiered backup + REAL restore-verify for the oracle kernel.

This module performs a tiered backup and a genuine restore round-trip (back up
to a temp tree, restore into a SECOND temp tree, hash-diff every file) and only
then stamps
``last_verified_restore`` in ``BACKUP-RECOVERY.md`` -- so the claim "we can
recover" is proven, not asserted.

Tiers (mirroring BACKUP-RECOVERY.md):
    tier_0  control plane  -- oracle.yml, root *.md docs, Memory.nosync/,
                              Meta.nosync/ (incl. ledgers), AgentResources.nosync/,
                              Connectors/, _tools/
    tier_1  artifacts      -- Workproduct.nosync/, Analysis.nosync/,
                              dashboards.nosync/
    tier_2  raw data       -- _data.nosync/ (admin-decision-required)
    tier_3  secrets        -- NEVER in plaintext. .env.nosync and any KILL-SWITCH
                              payloads are EXCLUDED from every tier; this module
                              refuses to write secret-tier bytes in the clear.

Non-destructive by construction: every copy is copy -> fsync -> sha256-verify
(NEVER a bare move; the source is always preserved). The verified-copy sink is
the documented chokepoint-internal exception (it is structurally the same
durable-copy primitive that lives in safe_paths.safe_copy_verify_delete minus
the delete step) and carries the ``# safe_paths-internal`` marker the no-bypass
guard allowlists. Source reads are read-only and guard-exempt.

CLI:
    python3 _tools/backup.py --root R run [--tier all|0|1|2] --dest DIR
    python3 _tools/backup.py --root R verify-restore [--tier all|0|1|2] [--keep]

``run`` produces a backup tree under ``--dest`` (or a temp dir).
``verify-restore`` does the full round-trip and, on success, populates
``last_verified_restore`` in BACKUP-RECOVERY.md.

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# tier definitions -- relative paths under the oracle root
# ---------------------------------------------------------------------------
# Per-tier *directory* roots and *glob* selectors. Everything is relative to the
# oracle root and is a CONSTANT internal location (never user-derived).
TIER_DIRS: dict[str, list[str]] = {
    "0": [
        "Memory.nosync",
        "Meta.nosync",
        "AgentResources.nosync",
        "Connectors",
        "_tools",
    ],
    "1": [
        "Workproduct.nosync",
        "Analysis.nosync",
        "dashboards.nosync",
    ],
    "2": [
        "_data.nosync",
    ],
}
# Top-level files always included in tier 0 (the control plane).
TIER0_FILES_GLOB = ["oracle.yml", "*.md", ".gitignore", ".env.example", "load-env.sh"]

# Tier 3 = secrets. These are NEVER written to a backup in plaintext. Any path
# whose name matches one of these is skipped in every tier.
SECRET_NAME_TOKENS = (".env.nosync", ".env.", "KILL-SWITCH")
SECRET_SUFFIXES = (".pem", ".key")

_BACKUP_DOC = "BACKUP-RECOVERY.md"
_MANIFEST_NAME = "backup-manifest.json"
_COPY_CHUNK_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    """Full sha256 hex of a file's bytes (streamed). Read-only, guard-exempt."""
    h = hashlib.sha256()
    with open(path, "rb") as f:  # read-only source: not a write target
        for chunk in iter(lambda: f.read(_COPY_CHUNK_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_secret(path: Path) -> bool:
    name = path.name
    for tok in SECRET_NAME_TOKENS:
        if tok in name:
            return True
    if path.suffix.lower() in SECRET_SUFFIXES:
        return True
    return False


def _verified_copy(src: Path, dst: Path) -> str:
    """Copy ``src`` to ``dst``: copy -> fsync -> sha256-verify. Source-preserving.

    Returns the full sha256 of the copied content. On a post-copy hash mismatch
    the bad destination is removed and ValueError is raised, so a corrupt backup
    file is never recorded. ``dst`` is always a path the caller built under a
    backup root we control (never user-derived from outside); the single
    ``open(dst, 'wb')`` is the documented chokepoint-internal verified-copy sink.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.is_file():
        raise ValueError(f"backup: missing/non-file src {src}")
    src_hash = sha256_file(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(src, "rb") as fsrc:  # read-only source
            # safe_paths-internal: verified-copy backup sink (backup-controlled dst)
            with open(dst, "wb") as fdst:  # safe_paths-internal
                for chunk in iter(lambda: fsrc.read(_COPY_CHUNK_BYTES), b""):
                    fdst.write(chunk)
                fdst.flush()
                os.fsync(fdst.fileno())
        dst_hash = sha256_file(dst)
    except Exception as exc:
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise ValueError(f"backup: copy failed {src} -> {dst}: {exc}") from exc
    if dst_hash != src_hash:
        try:
            dst.unlink()
        except OSError:
            pass
        raise ValueError(
            f"backup: hash mismatch for {src.name} src={src_hash[:12]} dst={dst_hash[:12]}"
        )
    return dst_hash


def _normalize_tiers(tier: str) -> list[str]:
    """Map the --tier flag to the ordered list of tier ids to include."""
    tier = (tier or "all").strip().lower()
    if tier in ("all", "*"):
        return ["0", "1", "2"]
    if tier in TIER_DIRS:
        return [tier]
    # accept "tier_0"/"tier0" forms
    for k in TIER_DIRS:
        if tier in (f"tier_{k}", f"tier{k}"):
            return [k]
    raise ValueError(f"unknown tier {tier!r}; expected one of all|0|1|2")


def _iter_tier_sources(root: Path, tiers: list[str]) -> Iterable[Path]:
    """Yield every regular file (recursively) belonging to the requested tiers.

    Secret-tier files are excluded everywhere. Quarantine/temp artifacts and
    Python bytecode caches are skipped (they are rebuildable noise)."""
    seen: set[Path] = set()
    if "0" in tiers:
        for pattern in TIER0_FILES_GLOB:
            for p in sorted(root.glob(pattern)):
                if p.is_file():
                    yield from _emit_file(p, seen)
    for t in tiers:
        for rel in TIER_DIRS.get(t, []):
            base = root / rel
            if not base.exists():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    yield from _emit_file(p, seen)


def _emit_file(p: Path, seen: set[Path]):
    if p in seen:
        return
    if _is_secret(p):
        return
    if "__pycache__" in p.parts:
        return
    if p.suffix == ".pyc":
        return
    seen.add(p)
    yield p


# ---------------------------------------------------------------------------
# run -- produce a backup tree + manifest
# ---------------------------------------------------------------------------
def run(root: Path, dest: Path, *, tier: str = "all") -> dict:
    """Back up the requested tiers from ``root`` into ``dest``.

    Returns a manifest dict: ``{root, dest, tier, created_at, files:[{rel,sha256,
    bytes}], count, total_bytes, secrets_excluded}`` and also writes that
    manifest to ``dest/backup-manifest.json``. Mirrors the on-disk relative
    layout so a restore is a straight copy-back.
    """
    root = Path(root).resolve()
    dest = Path(dest)
    tiers = _normalize_tiers(tier)
    dest.mkdir(parents=True, exist_ok=True)

    files_meta: list[dict] = []
    total_bytes = 0
    secrets_excluded = 0

    # Count excluded secrets for transparency (so the report proves we saw and
    # skipped them rather than silently missing them). We sweep every file in the
    # tier scope -- including the top-level control plane -- and tally any that
    # the secret filter would refuse, so a plaintext secret can never slip in
    # un-noticed.
    for t in tiers:
        for rel in TIER_DIRS.get(t, []):
            base = root / rel
            if base.exists():
                for p in base.rglob("*"):
                    if p.is_file() and _is_secret(p):
                        secrets_excluded += 1
    if "0" in tiers:
        # Tier 0 owns the control plane: sweep ALL top-level files for secrets
        # (not just the backup whitelist) so e.g. ``.env.nosync`` is tallied.
        for p in root.iterdir():
            if p.is_file() and _is_secret(p):
                secrets_excluded += 1

    for src in _iter_tier_sources(root, tiers):
        rel = src.relative_to(root)
        dst = dest / rel
        sha = _verified_copy(src, dst)
        size = src.stat().st_size
        total_bytes += size
        files_meta.append({"rel": str(rel), "sha256": sha, "bytes": size})

    manifest = {
        "root": str(root),
        "dest": str(dest),
        "tier": tier,
        "tiers": tiers,
        "created_at": _now_iso(),
        "count": len(files_meta),
        "total_bytes": total_bytes,
        "secrets_excluded": secrets_excluded,
        "files": files_meta,
    }
    # Manifest is a constant-named internal artifact under the backup root we
    # control -- written like other constant-internal renders.
    (dest / _MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


# ---------------------------------------------------------------------------
# verify-restore -- REAL round-trip hash-diff
# ---------------------------------------------------------------------------
def verify_restore(root: Path, *, tier: str = "all", keep: bool = False) -> dict:
    """Prove recoverability via a real round-trip.

    Steps:
      1. ``run`` the backup into a fresh temp dir (BACKUP).
      2. "Restore" by copying BACKUP into a SECOND temp dir (RESTORE), verifying
         each copy by hash.
      3. Hash-diff every file across the original sources, BACKUP, and RESTORE.
      4. On a clean diff, stamp ``last_verified_restore`` in BACKUP-RECOVERY.md.

    Returns a report dict with ``ok`` plus per-stage counts and any mismatches.
    The temp trees are removed unless ``keep`` is set.
    """
    root = Path(root).resolve()
    tiers = _normalize_tiers(tier)

    work = Path(tempfile.mkdtemp(prefix="oracle-backup-verify-"))
    backup_dir = work / "backup"
    restore_dir = work / "restore"
    report: dict = {
        "root": str(root),
        "tier": tier,
        "tiers": tiers,
        "checked_at": _now_iso(),
        "ok": False,
        "backed_up": 0,
        "restored": 0,
        "mismatches": [],
        "missing_after_restore": [],
        "secrets_excluded": 0,
        "work_dir": str(work),
    }
    try:
        # 1. backup the live oracle into BACKUP.
        manifest = run(root, backup_dir, tier=tier)
        report["backed_up"] = manifest["count"]
        report["secrets_excluded"] = manifest["secrets_excluded"]
        source_hashes = {f["rel"]: f["sha256"] for f in manifest["files"]}

        # 2. restore: copy BACKUP -> RESTORE (verified), excluding the manifest.
        restored = 0
        restore_hashes: dict[str, str] = {}
        for rel in source_hashes:
            src = backup_dir / rel
            dst = restore_dir / rel
            h = _verified_copy(src, dst)
            restore_hashes[rel] = h
            restored += 1
        report["restored"] = restored

        # 3. three-way hash-diff: original source == backup == restore.
        for rel, src_sha in source_hashes.items():
            # original source still on disk?
            live = root / rel
            if not live.is_file():
                report["missing_after_restore"].append(rel)
                continue
            live_sha = sha256_file(live)
            r_sha = restore_hashes.get(rel)
            if not (live_sha == src_sha == r_sha):
                report["mismatches"].append(
                    {
                        "rel": rel,
                        "source": live_sha[:12],
                        "backup": src_sha[:12],
                        "restore": (r_sha[:12] if r_sha else None),
                    }
                )

        report["ok"] = (
            restored == len(source_hashes)
            and not report["mismatches"]
            and not report["missing_after_restore"]
        )

        # 4. on success, stamp last_verified_restore in BACKUP-RECOVERY.md.
        if report["ok"]:
            stamp = _stamp_last_verified_restore(
                root, report["checked_at"], report["backed_up"]
            )
            report["stamped"] = stamp
    finally:
        if not keep:
            _rmtree(work)
            report["work_dir"] = "(removed)"
    return report


def _stamp_last_verified_restore(root: Path, when: str, file_count: int) -> bool:
    """Record a proven restore in BACKUP-RECOVERY.md.

    Updates an existing ``last_verified_restore:`` line if present (inside the
    doc's policy block) or appends a small machine-readable stamp section. The
    doc is a CONSTANT internal file (<root>/BACKUP-RECOVERY.md); written via
    write_text like other constant-internal renders. Returns True if the file
    was changed.
    """
    doc = root / _BACKUP_DOC
    line_val = f"{when} (round-trip hash-verified, {file_count} files)"
    if not doc.exists():
        body = (
            "# Backup and Recovery\n\n"
            "Restore verification is proven by `_tools/backup.py verify-restore`.\n\n"
            f"last_verified_restore: {line_val}\n"
        )
        doc.write_text(body, encoding="utf-8")
        return True

    text = doc.read_text(encoding="utf-8")
    lines = text.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("last_verified_restore:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}last_verified_restore: {line_val}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("## Verification stamp")
        lines.append("")
        lines.append(f"last_verified_restore: {line_val}")
    new_text = "\n".join(lines) + "\n"
    if new_text == text:
        return False
    doc.write_text(new_text, encoding="utf-8")
    return True


def _rmtree(path: Path) -> None:
    """Recursively remove a temp tree we created (best-effort)."""
    path = Path(path)
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        try:
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Tiered backup + real restore-verify")
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="produce a backup tree")
    p_run.add_argument("--tier", default="all", help="all|0|1|2")
    p_run.add_argument("--dest", required=True, help="backup destination directory")

    p_ver = sub.add_parser(
        "verify-restore", help="prove recoverability via a real round-trip"
    )
    p_ver.add_argument("--tier", default="all", help="all|0|1|2")
    p_ver.add_argument(
        "--keep", action="store_true", help="keep the temp backup/restore trees"
    )

    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        if args.cmd == "run":
            manifest = run(root, Path(args.dest), tier=args.tier)
            print(
                json.dumps(
                    {
                        k: manifest[k]
                        for k in (
                            "dest",
                            "tier",
                            "count",
                            "total_bytes",
                            "secrets_excluded",
                            "created_at",
                        )
                    },
                    indent=2,
                )
            )
            return 0
        if args.cmd == "verify-restore":
            report = verify_restore(root, tier=args.tier, keep=args.keep)
            slim = {
                k: report[k]
                for k in (
                    "ok",
                    "backed_up",
                    "restored",
                    "mismatches",
                    "missing_after_restore",
                    "secrets_excluded",
                    "checked_at",
                )
            }
            print(json.dumps(slim, indent=2))
            return 0 if report["ok"] else 1
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
