#!/usr/bin/env python3
"""render_kernel_manifest.py -- produce the tool-hash manifest for a kernel.

Walks every file under ``<kernel>/_tools`` (recursively), computes its full
sha256, and writes ``<kernel>/.kernel-manifest.json``. The manifest is the
integrity baseline consumed by:

* ``_tools/upgrade.py`` -- to verify an incoming tool bundle byte-for-byte and to
  prove a tool-layer-only swap (any manifest path escaping ``_tools/`` is
  refused).
* ``_tools/oracle_lint.py`` -- as the kernel-integrity reference (advisory if
  absent).

Manifest format (BINDING -- must match ``upgrade.compute_tools_manifest``)::

    {
      "tools_version": "3.0.0",            # optional; carried for upgrade compare
      "generated": "2026-01-01T00:00:00",  # provenance only
      "aggregate_sha256": "<hex>",         # stable digest over the sorted entries
      "files": {
        "_tools/safe_paths.py": "<sha256-hex>",
        ...
      }
    }

The ``files`` map is the load-bearing field: keys are POSIX relpaths rooted at
the kernel directory (``_tools/...``), values are FULL sha256 hex. ``__pycache__``
directories and ``.pyc`` files are skipped (rebuildable, non-portable).

The ``aggregate_sha256`` is a deterministic hash-of-hashes over the sorted
``"<relpath>\\n<sha256>\\n"`` lines; spawn/upgrade may stamp it into
``oracle.yml`` ``kernel.tools_sha256`` as a single-string fingerprint of the
whole tool layer.

This is a SKILL-side (host) build tool. It never writes inside an oracle's
sovereign data -- only the kernel's own ``.kernel-manifest.json`` next to
``_tools/`` -- so it does not route through ``safe_paths``; the destination is a
fixed, non-user-influenced path derived solely from ``--kernel``.

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

_TOOLS = "_tools"
_MANIFEST = ".kernel-manifest.json"
_DEFAULT_TOOLS_VERSION = "3.0.0"


def sha256_file(path: Path) -> str:
    """Full sha256 hex of a file's bytes (streamed)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:  # read-only source: not a write target
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_files(kernel_dir: Path) -> dict[str, str]:
    """Hash every ``_tools`` file -> {posix-relpath-rooted-at-kernel: sha256-hex}.

    Matches ``upgrade.compute_tools_manifest`` exactly: POSIX relpaths rooted at
    the kernel dir, ``__pycache__`` and ``.pyc`` excluded, sorted deterministically.
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
    return files


def aggregate_sha256(files: dict[str, str]) -> str:
    """Deterministic hash-of-hashes over the sorted (relpath, sha256) entries.

    Stable regardless of dict insertion order; suitable as a single-string
    fingerprint of the entire tool layer for ``kernel.tools_sha256``.
    """
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\n")
        h.update(files[rel].encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def build_manifest(kernel_dir: Path, *, tools_version: str = _DEFAULT_TOOLS_VERSION) -> dict:
    """Build the full manifest dict for ``kernel_dir`` (does not write it)."""
    files = compute_files(kernel_dir)
    return {
        "tools_version": tools_version,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "aggregate_sha256": aggregate_sha256(files),
        "files": files,
    }


def render(kernel_dir: Path, *, tools_version: str = _DEFAULT_TOOLS_VERSION) -> dict:
    """Compute and WRITE ``<kernel>/.kernel-manifest.json``; return the manifest.

    The destination is a fixed internal path under ``kernel_dir`` (not
    user-influenced), so a plain ``write_text`` is correct here.
    """
    kernel_dir = Path(kernel_dir)
    if not kernel_dir.exists():
        raise FileNotFoundError(f"kernel dir not found: {kernel_dir}")
    manifest = build_manifest(kernel_dir, tools_version=tools_version)
    out = kernel_dir / _MANIFEST
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--kernel",
        required=True,
        help="path to the oracle-kernel directory (the dir that contains _tools/)",
    )
    ap.add_argument(
        "--tools-version",
        default=_DEFAULT_TOOLS_VERSION,
        help=f"version string recorded in the manifest (default {_DEFAULT_TOOLS_VERSION})",
    )
    ap.add_argument("--json", action="store_true", help="print the manifest to stdout")
    args = ap.parse_args(argv)

    kernel_dir = Path(args.kernel).expanduser().resolve()
    try:
        manifest = render(kernel_dir, tools_version=args.tools_version)
    except FileNotFoundError as exc:
        print(f"render_kernel_manifest: {exc}", file=sys.stderr)
        return 2

    n = len(manifest["files"])
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print(f"wrote {kernel_dir / _MANIFEST}")
        print(f"  files: {n}")
        print(f"  tools_version: {manifest['tools_version']}")
        print(f"  aggregate_sha256: {manifest['aggregate_sha256']}")
    if n == 0:
        print("render_kernel_manifest: WARNING no _tools files found", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
