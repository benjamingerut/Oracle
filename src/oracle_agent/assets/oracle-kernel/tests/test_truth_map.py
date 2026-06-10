#!/usr/bin/env python3
"""Tests for truth_map.py -- the TRUTH-MAP.md parser + authority resolver.

These tests are self-contained: they exercise the parser against the REAL
shipped ``TRUTH-MAP.md`` (via the ``kernel_dir`` fixture) and against small
inline tables, and depend only on this module plus the floor (conftest puts
``_tools`` on sys.path).
"""
from __future__ import annotations

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
