#!/usr/bin/env python3
"""Tests for immutable Source-record creation and lint verification."""
from __future__ import annotations

from pathlib import Path

import pytest

import ledger
import oracle_lint
import oracle_yaml
import source_record


def _payload(**overrides) -> dict:
    payload = {
        "title": "Board package",
        "provenance": "Uploaded by the administrator from the board portal.",
        "raw_location": "board portal export",
        "locality": "snapshot_local",
        "sensitivity": "confidential",
        "as_of": "2026-06-08",
        "grain": "One board package for one meeting date.",
        "notes": "Initial capture.",
    }
    payload.update(overrides)
    return payload


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    end = lines.index("---", 1)
    return oracle_yaml.safe_load("\n".join(lines[1:end]))


def test_create_reserves_unique_source_id_and_renders_dates_as_strings(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    first = source_record.create(root, _payload(title="Board package"))
    second = source_record.create(root, _payload(title="Board package follow-up"))

    assert first["source_id"].startswith("SRC-")
    assert second["source_id"].startswith("SRC-")
    assert first["source_id"] != second["source_id"]

    note = root / first["path"]
    fm = _frontmatter(note)
    assert fm["created"] == str(fm["created"])
    assert fm["updated"] == str(fm["updated"])
    assert fm["as_of"] == "2026-06-08"

    rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "source_record.jsonl")
    assert any(r.get("event") == "reserve_source_id" for r in rows)
    assert len(source_record.list_records(root)) == 2


def test_create_persists_authority_metadata_and_captured_hash(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    row = source_record.create(
        root,
        _payload(
            sha256="a" * 64,
            business_object="Revenue",
            authoritative_for=["Revenue", "Invoices"],
            source_system="accounting/ERP",
            authority_id="erp-prod",
            connector="accounting",
            origin_filename="revenue.csv",
            input_drop_id="INP-1",
        ),
    )

    note = root / row["path"]
    fm = _frontmatter(note)

    assert fm["captured_sha256"] == "a" * 64
    assert fm["business_object"] == "Revenue"
    assert fm["authoritative_for"] == ["Revenue", "Invoices"]
    assert fm["source_system"] == "accounting/ERP"
    assert row["captured_sha256"] == "a" * 64
    assert row["business_object"] == "Revenue"
    assert row["authoritative_for"] == ["Revenue", "Invoices"]


def test_user_can_create_non_authority_source_record(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    row = source_record.create(root, _payload(), actor="user1", role="user")

    assert row["source_id"].startswith("SRC-")
    assert row["role"] == "user"


def test_user_cannot_create_authority_bearing_source_record(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    with pytest.raises(PermissionError):
        source_record.create(
            root,
            _payload(business_object="Revenue", source_system="accounting/ERP"),
            actor="user1",
            role="user",
        )


def test_lint_detects_mutated_source_record(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    row = source_record.create(root, _payload())
    note = root / row["path"]
    note.write_text(note.read_text(encoding="utf-8") + "\nTampered after registration.\n", encoding="utf-8")

    violations = []
    oracle_lint.check_source_record_immutability(root, violations)

    assert any(v.code == "source-record-mutated" for v in violations)
