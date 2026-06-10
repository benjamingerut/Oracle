#!/usr/bin/env python3
"""Tests for the knowledge ingestion engine (this unit: S6-ingest-chunk-classify).

Covers the three modules this unit owns -- ``chunker``, ``intake_classify`` and
the orchestrator ``ingest_pipeline`` -- in ISOLATION: they depend only on the
floor (safe_paths, secret_scan, optionally policy) plus each other. Optional
engine companions (extractors, knowledge_index, source_record, derive) may be
unavailable in isolated runs, so the pipeline is exercised against its built-in
stdlib fallback and optional stages may report ``skipped`` without failing.

Assertions per the unit's test brief:
  * feed a small txt + csv into a tmp oracle's _INPUT,
  * assert chunks are produced,
  * assert a source-record-SHAPED dict is emitted,
  * assert the classifier labels an obviously-confidential doc >= confidential.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import chunker
import intake_classify
import ingest_pipeline
import knowledge_index
import oracle_yaml


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write_input(root: Path, name: str, text: str) -> Path:
    """Write a file into the oracle's _INPUT and return its path."""
    inp = root / "Workproduct.nosync" / "_INPUT"
    inp.mkdir(parents=True, exist_ok=True)
    p = inp / name
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# chunker
# --------------------------------------------------------------------------- #
def test_chunker_offsets_are_exact():
    text = "Alpha paragraph.\n\n" + ("word " * 400) + "\n\nOmega paragraph."
    chunks = chunker.chunk(text, size=300, overlap=50)
    assert len(chunks) >= 2, "long text should produce multiple chunks"
    for c in chunks:
        # The offset invariant: a chunk's text is exactly the source span.
        assert text[c.start:c.end] == c.text
        assert c.end > c.start
    # First chunk starts at 0; chunks are ordered and overlap (start advances).
    assert chunks[0].start == 0
    starts = [c.start for c in chunks]
    assert starts == sorted(starts)


def test_chunker_overlap_clamped_no_infinite_loop():
    # overlap >= size must be clamped so the window always advances.
    chunks = chunker.chunk("x" * 1000, size=100, overlap=500)
    assert len(chunks) >= 2
    assert chunks[0].text == "x" * 100 or len(chunks[0].text) <= 100


def test_chunker_empty_and_whitespace_yield_nothing():
    assert chunker.chunk("") == []
    assert chunker.chunk("   \n\t  \n") == []
    assert chunker.chunk(None) == []


def test_chunker_short_text_single_chunk():
    chunks = chunker.chunk("just a little text", size=1200, overlap=200)
    assert len(chunks) == 1
    assert chunks[0].text == "just a little text"
    assert chunks[0].start == 0
    assert chunks[0].end == len("just a little text")


def test_chunker_records_markers_in_span():
    text = "AAAA\n\nBBBB\n\nCCCC"
    offsets = [(0, "p0"), (6, "p1"), (12, "p2")]
    chunks = chunker.chunk(text, size=8, overlap=0, offsets=offsets)
    seen_labels = set()
    for c in chunks:
        for m in c.markers:
            seen_labels.add(m["label"])
            assert c.start <= m["offset"] < c.end
    assert seen_labels  # at least one marker landed in some chunk


# --------------------------------------------------------------------------- #
# intake_classify
# --------------------------------------------------------------------------- #
def test_classifier_confidential_doc_is_at_least_confidential():
    text = (
        "STRICTLY CONFIDENTIAL -- Board of Directors materials.\n"
        "This proprietary deck covers the cap table and the pending acquisition.\n"
        "Do not distribute. Trade secret pricing strategy enclosed."
    )
    out = intake_classify.classify(text=text, filename="board-deck.txt")
    assert intake_classify.rank(out["label"]) >= intake_classify.rank("confidential")


def test_classifier_secret_beats_confidential_stricter_wins():
    # A doc that is both "confidential" AND carries a hard secret must classify
    # at the STRICTER tier (secret), proving stricter-row-wins ordering.
    text = (
        "Confidential internal note.\n"
        "export GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab\n"
    )
    out = intake_classify.classify(text=text, filename="notes.txt")
    assert out["label"] == "secret", out


def test_classifier_ssn_is_restricted():
    out = intake_classify.classify(text="Employee SSN: 123-45-6789 on file.")
    assert intake_classify.rank(out["label"]) >= intake_classify.rank("restricted")


def test_classifier_default_is_internal_not_public():
    # Un-marked benign business text must NOT be assumed publishable.
    out = intake_classify.classify(text="The quarterly sync moved to Thursday.")
    assert out["label"] == "internal"


def test_classifier_connector_default_is_a_floor():
    # A connector that declares 'confidential' raises an otherwise-internal doc.
    out = intake_classify.classify(
        text="ordinary meeting agenda", connector_default="confidential"
    )
    assert intake_classify.rank(out["label"]) >= intake_classify.rank("confidential")
    assert out["floor"] == "confidential"


def test_classifier_admin_override_wins():
    out = intake_classify.classify(
        text="STRICTLY CONFIDENTIAL trade secret", admin_override="public"
    )
    assert out["label"] == "public"
    assert out["override"] == "public"


def test_classify_file_reads_disk(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    p = _write_input(root, "secret.txt", "Confidential proprietary M&A term sheet.")
    out = intake_classify.classify_file(p)
    assert intake_classify.rank(out["label"]) >= intake_classify.rank("confidential")


# --------------------------------------------------------------------------- #
# ingest_pipeline orchestrator
# --------------------------------------------------------------------------- #
def test_pipeline_txt_produces_chunks_and_source_card(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    body = "Intro paragraph about the company.\n\n" + ("Detail sentence. " * 200)
    src = _write_input(root, "memo.txt", body)

    result = ingest_pipeline.run(root, src, connector="manual", actor="tester")

    assert result["ok"] is True
    # chunks produced
    assert result["chunk_count"] >= 1
    assert len(result["chunks"]) == result["chunk_count"]
    first = result["chunks"][0]
    assert {"text", "start", "end", "index"} <= set(first.keys())

    # source-record-SHAPED dict emitted
    rec = result["source_record"]
    assert isinstance(rec, dict)
    for key in ("type", "sha256", "sensitivity", "origin_filename", "as_of", "chunk_count"):
        assert key in rec, f"source record missing {key!r}"
    assert rec["type"] == "source"
    assert rec["origin_filename"] == "memo.txt"
    assert len(rec["sha256"]) == 64
    assert rec["chunk_count"] == result["chunk_count"]

    # input file is NOT destroyed by the pipeline (non-destructive contract)
    assert src.exists()
    assert src.read_text(encoding="utf-8") == body

    # The optional indexing stage is present in this kernel and must use the
    # public knowledge_index.index_chunks API rather than silently skipping.
    assert result["index"]["status"] == "indexed"
    assert result["index"]["count"] == result["chunk_count"]
    indexed_rows = knowledge_index.list_chunks(root, source_id=result["sha256"][:12])
    assert len(indexed_rows) == result["chunk_count"]
    with knowledge_index.KnowledgeIndex(root) as idx:
        hits = idx.search(
            "company detail sentence",
            max_sensitivity=result["sensitivity"],
        )
    assert hits


def test_pipeline_persists_authority_metadata_and_captured_hash(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    body = "Revenue export for account ACME, invoice INV-1, amount 123."
    src = _write_input(root, "revenue.txt", body)

    result = ingest_pipeline.run(
        root,
        src,
        connector="accounting",
        actor="tester",
        business_object="Revenue",
        source_system="accounting/ERP",
        authority_id="erp-prod",
    )

    rec = result["source_record"]
    assert rec["business_object"] == "Revenue"
    assert rec["authoritative_for"] == ["Revenue"]
    assert rec["source_system"] == "accounting/ERP"
    assert rec["authority_id"] == "erp-prod"
    assert rec["sha256"] == result["sha256"]

    persisted = result["source_persist"]
    assert persisted["persisted"] is True
    note = root / persisted["record"]["path"]
    lines = note.read_text(encoding="utf-8").splitlines()
    end = lines.index("---", 1)
    fm = oracle_yaml.safe_load("\n".join(lines[1:end]))
    assert fm["captured_sha256"] == result["sha256"]
    assert fm["business_object"] == "Revenue"
    assert fm["authoritative_for"] == ["Revenue"]
    assert fm["source_system"] == "accounting/ERP"


def test_pipeline_user_authority_metadata_becomes_candidate(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _write_input(
        root,
        "revenue-user.txt",
        "User supplied revenue export for account ACME.",
    )

    result = ingest_pipeline.run(
        root,
        src,
        connector="accounting",
        actor="user1",
        role="user",
        business_object="Revenue",
        source_system="accounting/ERP",
        authority_id="erp-prod",
    )

    persisted = result["source_persist"]
    assert persisted["persisted"] is True
    assert persisted["authority_candidate"] is True
    note = root / persisted["record"]["path"]
    text = note.read_text(encoding="utf-8")
    lines = text.splitlines()
    end = lines.index("---", 1)
    fm = oracle_yaml.safe_load("\n".join(lines[1:end]))

    assert "authority-candidate" in fm["tags"]
    assert "business_object" not in fm
    assert "authority_id" not in fm
    assert "Authority candidate" in text
    assert "business_object: Revenue" in text


def test_pipeline_csv_extracts_and_chunks(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    rows = "name,role,note\n" + "\n".join(
        f"Person{i},Engineer,Notes about person {i}" for i in range(50)
    )
    src = _write_input(root, "people.csv", rows)

    result = ingest_pipeline.run(root, src, connector="manual")

    assert result["ok"] is True
    assert result["chunk_count"] >= 1
    # csv row markers should have been threaded through to at least one chunk
    any_markers = any(c.get("markers") for c in result["chunks"])
    assert any_markers
    assert result["source_record"]["suffix"] == ".csv"


def test_pipeline_classifies_confidential_csv_at_log_time(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    rows = (
        "employee,salary,ssn\n"
        "Jane Doe,185000,123-45-6789\n"
        "John Roe,172000,987-65-4321\n"
    )
    src = _write_input(root, "payroll.csv", rows)

    result = ingest_pipeline.run(root, src)

    # salary + SSN content -> restricted (>= confidential), set at log time.
    assert intake_classify.rank(result["sensitivity"]) >= intake_classify.rank("confidential")
    assert result["source_record"]["sensitivity"] == result["sensitivity"]


def test_pipeline_rejects_input_outside_input_dir(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # A file that lives OUTSIDE _INPUT must be refused (containment).
    outside = tmp_path / "loose.txt"
    outside.write_text("not in _INPUT", encoding="utf-8")
    with pytest.raises(ValueError):
        ingest_pipeline.run(root, outside)
    # The refused source is left intact.
    assert outside.exists()


def test_pipeline_optional_stages_skip_cleanly(tmp_path, minimal_oracle):
    """index/persist/derive degrade to 'skipped' (not crash) when siblings absent.

    We assert the result carries those sections with a status, regardless of
    whether the sibling modules are present in this build.
    """
    root = minimal_oracle(tmp_path)
    src = _write_input(root, "note.md", "# Title\n\nSome internal notes here.\n")
    result = ingest_pipeline.run(root, src, derive=True)

    assert "status" in result["index"]
    assert "status" in result["derive"]
    assert "persisted" in result["source_persist"]
    # The source card always reports whether it was persisted.
    assert "persisted" in result["source_record"]


def test_pipeline_missing_file_raises(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    ghost = root / "Workproduct.nosync" / "_INPUT" / "does-not-exist.txt"
    with pytest.raises(ValueError):
        ingest_pipeline.run(root, ghost)
