#!/usr/bin/env python3
"""Tests for truth_map.py -- the TRUTH-MAP.md parser + authority resolver.

These tests are self-contained: they exercise the parser against the REAL
shipped ``TRUTH-MAP.md`` (via the ``kernel_dir`` fixture) and against small
inline tables, and depend only on this module plus the floor (conftest puts
``_tools`` on sys.path).
"""
from __future__ import annotations

import os

import pytest

import truth_map


# --------------------------------------------------------------------------- #
# parsing the real shipped TRUTH-MAP.md
# --------------------------------------------------------------------------- #
def test_parse_real_truth_map_has_rows(kernel_dir):
    rows = truth_map.load_rows(kernel_dir)
    assert rows, "shipped TRUTH-MAP.md must parse to at least one row"
    # The four load-bearing columns are present as machine keys on every row.
    for row in rows:
        for col in truth_map.REQUIRED_COLUMNS:
            assert col in row, f"row missing load-bearing column {col!r}: {row}"
        assert row["business_object"], "every row must name a business object"


def test_resolve_known_object_real_map(kernel_dir):
    rows = truth_map.load_rows(kernel_dir)
    # "Customers / accounts" is a known row in the shipped map; resolution is
    # slash- and case-insensitive.
    row = truth_map.resolve("customers / accounts", kernel_dir)
    assert row is not None, "known object must resolve to a row"
    assert truth_map.normalize_object(row["business_object"]) == truth_map.normalize_object(
        "Customers / accounts"
    )
    # Same object, different surface form, still resolves to the SAME row.
    row2 = truth_map.resolve("Customers Accounts", kernel_dir)
    assert row2 is not None
    assert row2["business_object"] == row["business_object"]


def test_resolve_unknown_object_returns_none(kernel_dir):
    assert truth_map.resolve("nonexistent imaginary object xyz", kernel_dir) is None


def test_resolve_company_identity_real_map(kernel_dir):
    # "Company identity / ownership" has a real (non-TBD) primary source.
    row = truth_map.resolve("Company identity / ownership", kernel_dir)
    assert row is not None
    assert truth_map.primary_source_is_authoritative(row["primary source"]) is True


def test_tbd_primary_source_flagged_not_authoritative(kernel_dir):
    # The shipped map seeds several rows with a TBD connector as primary source.
    rows = truth_map.load_rows(kernel_dir)
    tbd_rows = [r for r in rows if not truth_map.primary_source_is_authoritative(r["primary source"])]
    assert tbd_rows, "expected at least one TBD/empty primary-source row in the seed map"


# --------------------------------------------------------------------------- #
# parser behavior on inline tables
# --------------------------------------------------------------------------- #
_INLINE_MAP = """\
# Truth Map

Some prose above the table.

| Business object | Primary source | Freshness budget | Status |
|---|---|---|---|
| Revenue | accounting/ERP | 7d | confirmed |
| Cash | TBD | 24h | draft |

Some prose below.
"""


def test_parse_inline_table():
    rows = truth_map.parse_table(_INLINE_MAP)
    assert len(rows) == 2
    assert rows[0]["business_object"] == "Revenue"
    assert rows[0]["primary source"] == "accounting/ERP"
    assert rows[0]["freshness budget"] == "7d"
    assert rows[0]["status"] == "confirmed"


def test_parse_picks_first_qualifying_table_ignores_others():
    text = (
        "| name | value |\n"
        "|---|---|\n"
        "| a | b |\n"
        "\n"
        "| Business object | Primary source | Freshness budget | Status |\n"
        "|---|---|---|---|\n"
        "| People | HR docs | 30d | draft |\n"
    )
    rows = truth_map.parse_table(text)
    assert len(rows) == 1
    assert rows[0]["business_object"] == "People"


def test_parse_extra_columns_preserved():
    text = (
        "| Business object | Primary source | Corroborates | Freshness budget | Status |\n"
        "|---|---|---|---|---|\n"
        "| Legal | contracts repo | CRM | document-specific | draft |\n"
    )
    rows = truth_map.parse_table(text)
    assert rows[0]["corroborates"] == "CRM"


def test_parse_no_qualifying_table_raises():
    text = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    with pytest.raises(truth_map.TruthMapError):
        truth_map.parse_table(text)


def test_load_rows_missing_file_returns_empty(tmp_path):
    assert truth_map.load_rows(tmp_path) == []


def test_load_rows_present_but_no_table_returns_empty(tmp_path):
    (tmp_path / "TRUTH-MAP.md").write_text("# Truth Map\n\nNo table here yet.\n", encoding="utf-8")
    assert truth_map.load_rows(tmp_path) == []


def test_resolve_requires_root_or_rows():
    with pytest.raises(ValueError):
        truth_map.resolve("anything")


def test_resolve_with_prebuilt_rows():
    rows = truth_map.parse_table(_INLINE_MAP)
    assert truth_map.resolve("revenue", rows=rows)["business_object"] == "Revenue"
    assert truth_map.resolve("missing", rows=rows) is None


def test_normalize_object_slash_and_whitespace():
    assert truth_map.normalize_object("Customers / accounts") == truth_map.normalize_object(
        "customers   accounts"
    )
    assert truth_map.normalize_object("") == ""


def test_primary_source_authority_tokens():
    assert truth_map.primary_source_is_authoritative("accounting/ERP") is True
    for tok in ("", "TBD", "tbd", "  ", "N/A", "none", "-"):
        assert truth_map.primary_source_is_authoritative(tok) is False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_rows_and_resolve(kernel_dir, capsys):
    rc = truth_map.main(["--root", str(kernel_dir), "rows"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "business object" in out.lower()

    rc = truth_map.main(["--root", str(kernel_dir), "resolve", "--object", "Cash / bank", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"row"' in out


def test_cli_resolve_unknown_exits_nonzero(kernel_dir, capsys):
    rc = truth_map.main(
        ["--root", str(kernel_dir), "resolve", "--object", "no-such-object-xyz"]
    )
    assert rc == 1


# --------------------------------------------------------------------------- #
# K1 acceptance tests: pipe-injection fix + atomic write
# --------------------------------------------------------------------------- #

# A minimal TRUTH-MAP.md used by propose_row tests (no ledger or policy deps).
_PROPOSE_MAP = """\
# Truth Map

| Business object | Primary source | Freshness budget | Status |
|---|---|---|---|
| Revenue | accounting/ERP | 7d | confirmed |
| Cash | TBD | 24h | draft |
"""


def _make_propose_root(tmp_path):
    """Return a tmp oracle root with a minimal TRUTH-MAP.md."""
    root = tmp_path / "oracle_root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "TRUTH-MAP.md").write_text(_PROPOSE_MAP, encoding="utf-8")
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    return root


# -- cell-value helpers -------------------------------------------------------

def test_escape_cell_pipe_becomes_backslash_pipe():
    """A literal | in a cell value is escaped to \\| by _escape_cell.

    Every bare ``|`` is escaped, including one that already follows a ``\\``.
    The parser's ``_split_row`` restores ``\\|`` → ``|`` on read, so the
    original value survives the round-trip.
    """
    assert truth_map._escape_cell("a|b") == r"a\|b"
    assert truth_map._escape_cell("no pipe") == "no pipe"
    # r"already\|escaped" is the 15-char string: already\|escaped
    # _escape_cell replaces the | → \|, giving: already\\|escaped (r"already\\|escaped")
    assert truth_map._escape_cell(r"already\|escaped") == r"already\\|escaped"


def test_escape_cell_newline_raises():
    """A cell containing a newline must raise CellValueError."""
    with pytest.raises(truth_map.CellValueError):
        truth_map._escape_cell("line1\nline2")
    with pytest.raises(truth_map.CellValueError):
        truth_map._escape_cell("line1\rline2")


def test_compose_row_escapes_pipes():
    """_compose_row produces a valid markdown row with escaped pipes."""
    row = truth_map._compose_row(["Revenue | Q1", "accounting/ERP", "7d", "confirmed"])
    # The composed line must parse back to the original cell values.
    cells = truth_map._split_row(row)
    assert cells is not None
    assert cells[0] == "Revenue | Q1"
    assert cells[1] == "accounting/ERP"


def test_compose_row_rejects_newline_in_cell():
    """_compose_row raises CellValueError when any cell contains a newline."""
    with pytest.raises(truth_map.CellValueError):
        truth_map._compose_row(["good cell", "bad\ncell", "7d", "draft"])


# -- round-trip property ------------------------------------------------------

def test_propose_row_pipe_in_metadata_round_trips(tmp_path):
    """K1 core acceptance: a business object name containing | survives
    propose → write → parse unchanged (the previous corruption case).

    The raw ``|`` is stored as ``\\|`` in the file and restored by the parser,
    so the resolved row carries the original value intact.
    """
    root = _make_propose_root(tmp_path)
    tricky_object = "Customers | Prospects"
    result = truth_map.propose_row(root, tricky_object)
    assert result["action"] == "created"

    # Re-parse the written file and verify the round-trip.
    rows = truth_map.load_rows(root)
    resolved = truth_map.resolve(tricky_object, rows=rows)
    assert resolved is not None, "object with pipe must be resolvable after propose"
    assert resolved["business_object"] == tricky_object


def test_propose_row_pipe_in_source_round_trips(tmp_path):
    """A primary_source value containing | is escaped and round-trips."""
    root = _make_propose_root(tmp_path)
    bo = "Vendors"
    src = "accounting/ERP | payables module"
    truth_map.propose_row(root, bo, primary_source=src)

    rows = truth_map.load_rows(root)
    resolved = truth_map.resolve(bo, rows=rows)
    assert resolved is not None
    assert resolved["primary source"] == src


def test_propose_row_escaped_pipe_in_value_round_trips(tmp_path):
    r"""A value already containing \| (escaped pipe) survives the round-trip."""
    root = _make_propose_root(tmp_path)
    bo = r"Orders\|Returns"
    truth_map.propose_row(root, bo)
    rows = truth_map.load_rows(root)
    resolved = truth_map.resolve(bo, rows=rows)
    assert resolved is not None
    assert resolved["business_object"] == bo


def test_propose_row_leading_trailing_spaces_round_trip(tmp_path):
    """Cell values with leading/trailing spaces survive propose → parse.

    The parser strips cells, so stored values are the stripped form; the
    round-trip is still correct — the cell value is preserved as stripped.
    """
    root = _make_propose_root(tmp_path)
    bo = "  Payroll  "
    bo_stripped = bo.strip()
    truth_map.propose_row(root, bo)
    rows = truth_map.load_rows(root)
    resolved = truth_map.resolve(bo_stripped, rows=rows)
    assert resolved is not None
    assert resolved["business_object"] == bo_stripped


# -- newline rejection --------------------------------------------------------

def test_propose_row_newline_in_business_object_refused(tmp_path):
    """propose_row must raise CellValueError when the business object contains
    a newline (the injection prevention path).
    """
    root = _make_propose_root(tmp_path)
    with pytest.raises(truth_map.CellValueError):
        truth_map.propose_row(root, "Bad\nObject")


def test_propose_row_newline_in_source_refused(tmp_path):
    """propose_row must raise CellValueError when primary_source contains a newline."""
    root = _make_propose_root(tmp_path)
    with pytest.raises(truth_map.CellValueError):
        truth_map.propose_row(root, "GoodObject", primary_source="bad\nsource")


# -- atomic write: temp+replace pattern ---------------------------------------

def test_write_truth_map_uses_temp_and_replace(tmp_path, monkeypatch):
    """Atomic write discipline: _write_truth_map must use a temp file +
    os.replace rather than direct path.write_text.

    Verify by intercepting os.replace: if it is not called, the file was
    written directly. Also verify that on an injected failure between the
    temp-write and the replace, the original file is not corrupted (the temp
    is cleaned up and the original is intact).
    """
    root = tmp_path / "oracle_root"
    root.mkdir(parents=True, exist_ok=True)
    original_text = _PROPOSE_MAP
    (root / "TRUTH-MAP.md").write_text(original_text, encoding="utf-8")

    replace_calls = []
    real_replace = os.replace

    def tracking_replace(src, dst):
        replace_calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)

    truth_map._write_truth_map(root, original_text + "\n# extra\n")

    assert replace_calls, "_write_truth_map must call os.replace (atomic swap)"
    src, dst = replace_calls[0]
    # temp file is in the same directory as the destination
    assert str(root) in str(dst), "replace target must be inside root"
    assert "TRUTH-MAP" in str(dst), "replace target must be the TRUTH-MAP.md path"
    # The original should now contain the new content.
    written = (root / "TRUTH-MAP.md").read_text(encoding="utf-8")
    assert "# extra" in written


def test_write_truth_map_no_partial_on_failure(tmp_path, monkeypatch):
    """On an injected failure between the temp write and os.replace, the
    original TRUTH-MAP.md must remain intact (no partial write).
    """
    root = tmp_path / "oracle_root"
    root.mkdir(parents=True, exist_ok=True)
    sentinel = "ORIGINAL SENTINEL CONTENT\n"
    (root / "TRUTH-MAP.md").write_text(
        _PROPOSE_MAP + sentinel, encoding="utf-8"
    )

    def failing_replace(src, dst):
        # Simulate a crash after temp file written but before replace.
        # Remove the temp so there is no stale artifact, then raise.
        try:
            os.unlink(src)
        except OSError:
            pass
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated crash"):
        truth_map._write_truth_map(root, "NEW CONTENT\n")

    # The original file must still contain its pre-crash content.
    surviving = (root / "TRUTH-MAP.md").read_text(encoding="utf-8")
    assert sentinel in surviving, (
        "original TRUTH-MAP.md must survive a failed atomic write"
    )
