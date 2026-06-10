#!/usr/bin/env python3
"""Tests for safe_paths.py -- THE containment chokepoint.

These tests encode traversal as a MUST-REJECT case and prove the containment +
non-destructive-move guarantees. They depend only on safe_paths itself plus the
conftest helpers.
"""
from __future__ import annotations

import os

import pytest

import safe_paths


# ---------------------------------------------------------------------------
# safe_slug
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Q3 Revenue Report", "q3-revenue-report"),
        ("  Already-Clean  ", "already-clean"),
        ("MixED___CASE!!!name", "mixed-case-name"),
        ("héllo wörld", "h-llo-w-rld"),
        ("a/b\\c", "a-b-c"),
        ("2026 plan", "2026-plan"),
    ],
)
def test_safe_slug_maps(raw, expected):
    assert safe_paths.safe_slug(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "///", "!!!", "----", None])
def test_safe_slug_rejects_empty(bad):
    with pytest.raises(ValueError):
        safe_paths.safe_slug(bad)


# ---------------------------------------------------------------------------
# contain -- traversal and escape vectors
# ---------------------------------------------------------------------------

def test_contain_rejects_escape_lane(tmp_path, minimal_oracle):
    """A lane of '../../ESCAPE' must be REFUSED."""
    root = minimal_oracle(tmp_path)
    with pytest.raises(ValueError):
        safe_paths.contain(root, "../../ESCAPE")
    with pytest.raises(ValueError):
        safe_paths.contain(root, "../../ESCAPE_ZONE/received/pwned.txt")


def test_contain_rejects_absolute_paths(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(ValueError):
        safe_paths.contain(root, "/etc/passwd")
    with pytest.raises(ValueError):
        safe_paths.contain(root, "/tmp/anything")


def test_contain_rejects_dotdot_anywhere(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    for bad in ["..", "a/../../b", "01_Finance/../../../etc", "foo/..bar/.."]:
        with pytest.raises(ValueError):
            safe_paths.contain(root, bad)


def test_contain_rejects_os_sep_and_backslash_segments(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # A backslash inside a segment is rejected (Windows-style traversal vector).
    with pytest.raises(ValueError):
        safe_paths.contain(root, "lane\\..\\..\\escape")
    # A NUL byte is always rejected.
    with pytest.raises(ValueError):
        safe_paths.contain(root, "good\x00name")


def test_contain_rejects_drive_letter(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(ValueError):
        safe_paths.contain(root, "C:foo")


def test_contain_rejects_symlinked_component(tmp_path, minimal_oracle):
    """TOCTOU hardening: a symlinked component on the path is refused even when
    the link points back inside the base."""
    root = minimal_oracle(tmp_path)
    base = root / "Workproduct.nosync"
    real_target = base / "01_Finance"
    link = base / "sneaky"
    try:
        link.symlink_to(real_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    with pytest.raises(ValueError):
        safe_paths.contain(root, "sneaky/created/x.txt")


def test_contain_rejects_symlink_escaping_base(tmp_path, minimal_oracle):
    """A symlink that points OUTSIDE the base must be refused."""
    root = minimal_oracle(tmp_path)
    base = root / "Workproduct.nosync"
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    link = base / "exfil"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    with pytest.raises(ValueError):
        safe_paths.contain(root, "exfil/secret.txt")


def test_contain_accepts_valid_lane_slug_inside_base(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    lane = safe_paths.assert_lane(root, "01_Finance")
    slug = safe_paths.safe_slug("Q3 Report")
    result = safe_paths.contain(root, f"{lane}/received/{safe_paths.today()}_{slug}.txt")
    base = (root / "Workproduct.nosync").resolve()
    assert result.is_relative_to(base)
    assert os.path.commonpath([str(result), str(base)]) == str(base)
    assert "01_Finance" in str(result)


def test_contain_custom_base(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    result = safe_paths.contain(root, "ledgers/export_event.jsonl", base="Meta.nosync")
    base = (root / "Meta.nosync").resolve()
    assert result.is_relative_to(base)


# ---------------------------------------------------------------------------
# assert_lane
# ---------------------------------------------------------------------------

def test_assert_lane_accepts_known_lane(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    assert safe_paths.assert_lane(root, "04_Operations") == "04_Operations"


def test_assert_lane_rejects_unknown_lane(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(ValueError):
        safe_paths.assert_lane(root, "99_NotALane")


def test_assert_lane_rejects_traversal_lane(tmp_path, minimal_oracle):
    """A traversal lane value routed through assert_lane is refused."""
    root = minimal_oracle(tmp_path)
    with pytest.raises(ValueError):
        safe_paths.assert_lane(root, "../../ESCAPE_ZONE")


def test_assert_lane_rejects_empty(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(ValueError):
        safe_paths.assert_lane(root, "")


# ---------------------------------------------------------------------------
# safe_copy_verify_delete
# ---------------------------------------------------------------------------

def test_copy_verify_delete_happy_path(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello oracle", encoding="utf-8")
    dst = tmp_path / "dest" / "out.txt"
    digest = safe_paths.safe_copy_verify_delete(src, dst)
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "hello oracle"
    assert not src.exists()  # source deleted only after verified durable copy
    assert len(digest) == 12


def test_copy_verify_delete_missing_src_raises(tmp_path):
    with pytest.raises(ValueError):
        safe_paths.safe_copy_verify_delete(tmp_path / "nope.txt", tmp_path / "out.txt")


def test_copy_verify_delete_keeps_source_on_hash_mismatch(tmp_path, monkeypatch):
    """If verification fails, the destination is removed and the SOURCE STAYS.

    We force a hash mismatch by making the post-copy sha256 differ from the
    pre-copy one. The source must be left intact.
    """
    src = tmp_path / "src.bin"
    src.write_bytes(b"important original bytes")

    calls = {"n": 0}
    real = safe_paths.sha256_12

    def fake_sha(path):
        calls["n"] += 1
        # First call = source hash (real); second call = dest hash (corrupted).
        if calls["n"] == 1:
            return real(path)
        return "deadbeef0000"

    monkeypatch.setattr(safe_paths, "sha256_12", fake_sha)
    dst = tmp_path / "dest" / "out.bin"
    with pytest.raises(ValueError):
        safe_paths.safe_copy_verify_delete(src, dst)

    assert src.exists(), "source must survive a verification failure"
    assert src.read_bytes() == b"important original bytes"
    assert not dst.exists(), "failed copy must be cleaned up"


def test_copy_verify_delete_keeps_source_on_copy_failure(tmp_path, monkeypatch):
    """If the copy itself raises, the source is untouched."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"do not lose me")

    import shutil as _shutil

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(_shutil, "copy2", boom)
    dst = tmp_path / "dest" / "out.bin"
    with pytest.raises(ValueError):
        safe_paths.safe_copy_verify_delete(src, dst)
    assert src.exists()
    assert src.read_bytes() == b"do not lose me"


# ---------------------------------------------------------------------------
# is_within
# ---------------------------------------------------------------------------

def test_is_within(tmp_path):
    root = tmp_path / "root"
    inside = root / "a" / "b"
    inside.mkdir(parents=True)
    assert safe_paths.is_within(root, inside) is True
    assert safe_paths.is_within(root, root) is True
    assert safe_paths.is_within(root, tmp_path / "elsewhere") is False
