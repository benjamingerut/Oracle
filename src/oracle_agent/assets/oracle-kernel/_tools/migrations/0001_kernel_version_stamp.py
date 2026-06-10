#!/usr/bin/env python3
"""0001_kernel_version_stamp -- baseline migration.

Ensures ``oracle.yml`` carries a ``kernel:`` block with ``tools_version`` and
``tools_sha256`` so the tool-layer upgrade path (``upgrade.py``) can compare an
oracle's installed kernel version against an incoming bundle. If a hand-trimmed
config lacks this block, the migration adds it idempotently.

Idempotency: if a non-empty ``kernel.tools_version`` is already present the
migration makes no change and reports ``changed=False``. If the block is missing
or its version is empty/null, the missing keys are added with a baseline value,
preserving every other line of the file verbatim.

The mutation is a SURGICAL, line-oriented edit of a CONSTANT internal file
(``<root>/oracle.yml``) -- not a user-influenced path -- so it writes via
``Path.write_text`` like the other constant-internal renders in the kernel. It
stays strictly within the block-style YAML subset the floor loader accepts
(``key: value`` / nested ``key:`` -> indented children); it never emits flow
collections, anchors, tags, or multi-doc markers.
"""
from __future__ import annotations

from pathlib import Path

VERSION = "3.0.0"
DESCRIPTION = "Stamp kernel.tools_version + kernel.tools_sha256 into oracle.yml if absent."

_DEFAULT_TOOLS_VERSION = "3.0.0"
_MANIFEST_REL = ".kernel-manifest.json"


def _safe_load(text: str):
    """Load YAML via the floor's safe-subset loader (bare or package import)."""
    try:
        import oracle_yaml  # type: ignore
    except Exception:  # pragma: no cover - package import fallback
        from .. import oracle_yaml  # type: ignore
    return oracle_yaml.safe_load(text)


def _has_version(data) -> bool:
    if not isinstance(data, dict):
        return False
    kernel = data.get("kernel")
    if not isinstance(kernel, dict):
        return False
    v = kernel.get("tools_version")
    return bool(v) and str(v).strip().lower() not in ("", "none", "null")


def _insert_kernel_block(text: str) -> str:
    """Append a complete block-style ``kernel:`` section to ``text``.

    Used when oracle.yml has NO ``kernel:`` key at all. The block is appended at
    end-of-file (top-level), separated by a blank line, in the accepted subset.
    """
    block_lines = [
        "",
        "kernel:",
        f'  tools_version: "{_DEFAULT_TOOLS_VERSION}"',
        '  tools_sha256: ""',
        f'  manifest: "{_MANIFEST_REL}"',
    ]
    sep = "" if text.endswith("\n") else "\n"
    return text + sep + "\n".join(block_lines) + "\n"


# Desired (key, rendered-value) for each kernel child this migration manages.
_DESIRED_CHILDREN = (
    ("tools_version", f'"{_DEFAULT_TOOLS_VERSION}"'),
    ("tools_sha256", '""'),
    ("manifest", f'"{_MANIFEST_REL}"'),
)


def _child_value_is_empty(rendered: str) -> bool:
    """True iff a child's on-disk value is empty/null (so it must be filled)."""
    v = rendered.strip().strip('"').strip("'").strip().lower()
    return v in ("", "none", "null")


def _augment_kernel_block(text: str) -> str:
    """Given oracle.yml that HAS a ``kernel:`` key, ensure every managed child is
    present with a non-empty value.

    Two operations, both kept strictly within the block-style subset:
      * REPLACE an existing managed child whose value is empty/null in place
        (e.g. ``tools_version: ""`` -> ``tools_version: "3.0.0"``), preserving
        indentation.
      * APPEND any genuinely-missing managed child directly under the
        ``kernel:`` header line.
    Every other line is preserved verbatim.
    """
    lines = text.splitlines()

    # First pass: find the kernel: block bounds and which managed children exist
    # (and whether their value is empty).
    kernel_header_idx = -1
    block_end_idx = len(lines)
    present: dict[str, int] = {}  # child name -> line index
    in_kernel = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not in_kernel and stripped.rstrip(":") == "kernel" and stripped.endswith(":") and not line[:1].isspace():
            in_kernel = True
            kernel_header_idx = i
            continue
        if in_kernel:
            if line and not line[0].isspace():  # next top-level key ends the block
                block_end_idx = i
                break
            if ":" in line:
                child = line.strip().split(":", 1)[0].strip()
                if child:
                    present.setdefault(child, i)

    if kernel_header_idx < 0:  # pragma: no cover - caller guarantees kernel: exists
        return _insert_kernel_block(text)

    out = list(lines)

    # 1. Replace empty managed children in place.
    for key, value in _DESIRED_CHILDREN:
        if key in present:
            idx = present[key]
            indent = out[idx][: len(out[idx]) - len(out[idx].lstrip())]
            _, _, cur_val = out[idx].partition(":")
            if _child_value_is_empty(cur_val):
                out[idx] = f"{indent}{key}: {value}"

    # 2. Append genuinely-missing managed children just under the kernel header.
    additions = [
        f"  {key}: {value}" for key, value in _DESIRED_CHILDREN if key not in present
    ]
    if additions:
        out[kernel_header_idx + 1 : kernel_header_idx + 1] = additions

    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def apply(root: Path) -> dict:
    root = Path(root)
    cfg = root / "oracle.yml"
    if not cfg.exists():
        return {"changed": False, "notes": f"no oracle.yml at {cfg}"}

    text = cfg.read_text(encoding="utf-8")
    try:
        data = _safe_load(text)
    except Exception as exc:
        return {"changed": False, "notes": f"oracle.yml unparseable: {exc}"}

    if _has_version(data):
        return {"changed": False, "notes": "kernel.tools_version already present"}

    has_kernel = isinstance(data, dict) and isinstance(data.get("kernel"), dict)
    if has_kernel:
        new_text = _augment_kernel_block(text)
    else:
        new_text = _insert_kernel_block(text)

    if new_text == text:
        return {"changed": False, "notes": "no change required"}

    # Validate the result still parses within the safe subset before writing.
    try:
        _safe_load(new_text)
    except Exception as exc:  # pragma: no cover - defensive
        return {"changed": False, "notes": f"refused: edit broke yaml: {exc}"}

    # Constant internal path (<root>/oracle.yml), not user-derived. Written like
    # the other constant-internal renders in the kernel.
    cfg.write_text(new_text, encoding="utf-8")  # safe_paths-internal: root-confined constant path (oracle.yml)
    return {"changed": True, "notes": f"stamped kernel.tools_version={_DEFAULT_TOOLS_VERSION}"}
