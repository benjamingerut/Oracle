#!/usr/bin/env python3
"""safe_paths.py -- THE path-containment chokepoint.

Every kernel tool that writes a user-/config-influenced path MUST route it
through this module. No other kernel file may call ``shutil.move/copy/copy2``
or ``open(..., 'w'/'a')`` on a target path -- that invariant is enforced
structurally by ``tests/test_no_bypass_guard.py``.

Public API (binding interface contract):
    contain(root, candidate, *, base='Workproduct.nosync') -> Path
    safe_slug(s) -> str
    assert_lane(root, lane) -> str
    safe_copy_verify_delete(src, dst) -> str   # returns sha256_12
    is_within(root, p) -> bool

Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

__all__ = [
    "contain",
    "safe_slug",
    "assert_lane",
    "safe_copy_verify_delete",
    "is_within",
    "today",
    "sha256_12",
]

# Characters / sequences that are never permitted inside a user-supplied
# path segment. These are checked BEFORE any filesystem resolution so a
# malicious value can never reach realpath().
_DISALLOWED_SEGMENT = re.compile(r"(\.\.)|[\x00]")
_SLUG_KEEP = re.compile(r"[^a-z0-9]+")
_DRIVE_COLON = re.compile(r"^[A-Za-z]:")


def today() -> str:
    """Local date as YYYY-MM-DD (used for the mandatory filename prefix)."""
    return datetime.now().strftime("%Y-%m-%d")


def sha256_12(path: Path) -> str:
    """First 12 hex chars of the sha256 of a file's bytes (streamed)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:  # read-only: not a write target, guard-exempt
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def safe_slug(s: str) -> str:
    """Lowercase, map any non ``[a-z0-9]`` run to a single ``-``, strip.

    Raises ValueError if the result is empty (e.g. input was only separators
    or non-alphanumerics).
    """
    if s is None:
        raise ValueError("safe_slug: empty slug")
    out = _SLUG_KEEP.sub("-", str(s).strip().lower()).strip("-")
    if not out:
        raise ValueError(f"safe_slug: empty slug from {s!r}")
    return out


def _reject_segment(seg: str) -> None:
    """Raise ValueError if a single user-supplied path segment is unsafe."""
    if seg in ("", ".", ".."):
        raise ValueError(f"unsafe path segment: {seg!r}")
    if _DISALLOWED_SEGMENT.search(seg):
        raise ValueError(f"unsafe path segment (traversal/null): {seg!r}")
    if seg.startswith("/"):
        raise ValueError(f"unsafe path segment (absolute): {seg!r}")
    if "\\" in seg:
        raise ValueError(f"unsafe path segment (backslash): {seg!r}")
    if os.sep in seg or (os.altsep and os.altsep in seg):
        raise ValueError(f"unsafe path segment (separator): {seg!r}")
    if _DRIVE_COLON.match(seg):
        raise ValueError(f"unsafe path segment (drive): {seg!r}")


def _split_candidate(candidate) -> list[str]:
    """Turn a candidate (str | Path | iterable of parts) into vetted segments.

    A single string is split on '/' so callers may pass 'a/b/c'; each resulting
    segment is independently validated. Absolute candidates are rejected.
    """
    if isinstance(candidate, Path):
        parts = candidate.parts
        # An absolute Path has its anchor as the first part.
        if candidate.is_absolute():
            raise ValueError(f"absolute candidate not allowed: {candidate!r}")
        segs: list[str] = list(parts)
    else:
        text = str(candidate)
        if text.startswith("/"):
            raise ValueError(f"absolute candidate not allowed: {text!r}")
        # Normalize backslashes into something _reject_segment will catch
        # rather than silently treating them as separators.
        segs = [p for p in text.split("/")]
        # Drop a single trailing empty (from a trailing slash) but keep
        # interior empties so '//' is rejected as unsafe.
        if segs and segs[-1] == "" and text.endswith("/"):
            segs = segs[:-1]
    if not segs:
        raise ValueError("empty candidate")
    for seg in segs:
        _reject_segment(seg)
    return segs


def is_within(root: Path, p: Path) -> bool:
    """True iff resolved ``p`` lies inside resolved ``root`` (inclusive)."""
    try:
        rp = Path(os.path.realpath(p))
        rr = Path(os.path.realpath(root))
    except OSError:
        return False
    try:
        if rp == rr or rp.is_relative_to(rr):
            common = os.path.commonpath([str(rp), str(rr)])
            return common == str(rr)
    except ValueError:
        return False
    return False


def _has_symlinked_component(base: Path, segs: list[str]) -> None:
    """Walk segments from ``base`` and reject if any intermediate or final
    component is a symlink (TOCTOU hardening). ``base`` itself is resolved by
    the caller, so only the user-supplied components are inspected here.
    """
    cur = base
    for seg in segs:
        cur = cur / seg
        # lexists: catches dangling symlinks too.
        if cur.is_symlink():
            raise ValueError(f"symlinked path component refused: {cur}")


def contain(root, candidate, *, base: str = "Workproduct.nosync") -> Path:
    """Resolve ``candidate`` to an absolute path guaranteed to live under
    ``root/base``.

    Defenses, in order:
      1. Reject any user segment containing '..', a leading '/', a backslash,
         a drive-colon, an os.sep, or a NUL (BEFORE touching the filesystem).
      2. Resolve the base directory's realpath.
      3. Refuse if any built path component is a symlink (TOCTOU).
      4. Realpath-resolve the final target.
      5. Assert the result is_relative_to(base) AND
         os.path.commonpath([realpath(result), realpath(base)]) == realpath(base).

    Raises ValueError on any violation. The result is an absolute Path.
    """
    root = Path(root)
    base_dir = (root / base)
    # Realpath the base so symlinked roots are normalized consistently.
    base_real = Path(os.path.realpath(base_dir))

    segs = _split_candidate(candidate)

    # Build the lexical target under the (real) base and check for symlinked
    # components along the user-supplied path before final resolution.
    _has_symlinked_component(base_real, segs)

    target = base_real
    for seg in segs:
        target = target / seg

    target_real = Path(os.path.realpath(target))

    # is_relative_to gate (lexical containment after resolution).
    if not (target_real == base_real or target_real.is_relative_to(base_real)):
        raise ValueError(
            f"containment violation: {target_real} escapes {base_real}"
        )
    # commonpath gate (defense-in-depth against is_relative_to edge cases).
    try:
        common = os.path.commonpath([str(target_real), str(base_real)])
    except ValueError as exc:  # different drives, etc.
        raise ValueError(
            f"containment violation: {target_real} vs {base_real}: {exc}"
        )
    if common != str(base_real):
        raise ValueError(
            f"containment violation: commonpath {common!r} != base {str(base_real)!r}"
        )
    return target_real


def assert_lane(root, lane: str) -> str:
    """Validate ``lane`` against ``oracle.yml`` ``workproduct.routing_lanes``.

    Returns the lane unchanged on success; raises ValueError on a miss or if
    the lane string itself is structurally unsafe.

    The routing_lanes list in oracle.yml is the SINGLE source of truth for
    which Workproduct lanes a writer may target.
    """
    if lane is None or str(lane).strip() == "":
        raise ValueError("assert_lane: empty lane")
    # A lane is itself a single safe segment -- never a traversal vector.
    _reject_segment(str(lane))

    lanes = _load_routing_lanes(Path(root))
    if lane not in lanes:
        raise ValueError(
            f"assert_lane: {lane!r} not in routing_lanes {lanes!r}"
        )
    return lane


def _load_routing_lanes(root: Path) -> list[str]:
    """Read workproduct.routing_lanes from oracle.yml via the safe-subset loader."""
    cfg_path = root / "oracle.yml"
    if not cfg_path.exists():
        raise ValueError(f"assert_lane: no oracle.yml at {cfg_path}")
    # Lazy import: oracle_yaml is a sibling floor module. Importing lazily keeps
    # contain() usable without a config file and avoids import-time coupling.
    try:
        from oracle_yaml import safe_load
    except Exception:  # pragma: no cover - fallback when run as a package
        from . import oracle_yaml  # type: ignore
        safe_load = oracle_yaml.safe_load
    data = safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("assert_lane: oracle.yml is not a mapping")
    wp = data.get("workproduct") or {}
    lanes = wp.get("routing_lanes") if isinstance(wp, dict) else None
    if not isinstance(lanes, list) or not lanes:
        raise ValueError("assert_lane: workproduct.routing_lanes missing/empty")
    return [str(x) for x in lanes]


def safe_copy_verify_delete(src, dst) -> str:
    """Non-destructive move: copy ``src`` to ``dst``, fsync, verify by sha256,
    then delete the source. Returns the 12-char sha256 of the content.

    Never a bare move. If the post-copy hash does not match the source hash
    (or any step fails), the destination is removed and the SOURCE IS LEFT
    INTACT, then ValueError is raised. A failed or escaping write can never
    destroy the original.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise ValueError(f"safe_copy_verify_delete: missing src {src}")
    if not src.is_file():
        raise ValueError(f"safe_copy_verify_delete: src not a regular file {src}")

    src_hash = sha256_12(src)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Copy contents + metadata. shutil/open here are inside safe_paths.py, the
    # ONE module permitted to use raw I/O (guard-exempt by design).
    try:
        shutil.copy2(str(src), str(dst))
        # fsync the destination file's bytes to stable storage.
        with open(dst, "rb") as fdst:
            os.fsync(fdst.fileno())
        # fsync the containing directory so the rename/create is durable.
        dir_fd = os.open(str(dst.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
        dst_hash = sha256_12(dst)
    except Exception as exc:
        # Clean up a partial/failed destination; never touch the source.
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise ValueError(f"safe_copy_verify_delete: copy failed: {exc}") from exc

    if dst_hash != src_hash:
        # Verification failed -- remove the bad copy, keep the source intact.
        try:
            dst.unlink()
        except OSError:
            pass
        raise ValueError(
            f"safe_copy_verify_delete: hash mismatch src={src_hash} dst={dst_hash}"
        )

    # Only now, with a verified durable copy, delete the source.
    src.unlink()
    return dst_hash


if __name__ == "__main__":  # pragma: no cover - tiny manual harness
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="safe_paths containment chokepoint")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("slug")
    s.add_argument("text")
    c = sub.add_parser("contain")
    c.add_argument("--root", required=True)
    c.add_argument("--candidate", required=True)
    c.add_argument("--base", default="Workproduct.nosync")
    args = ap.parse_args()
    try:
        if args.cmd == "slug":
            print(safe_slug(args.text))
        elif args.cmd == "contain":
            print(contain(args.root, args.candidate, base=args.base))
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)
