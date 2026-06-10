#!/usr/bin/env python3
"""Tests for derived_memory.py -- optional Graphify/MemPalace boundary.

The key contract is that optional external memory engines are derived-only:
they can receive sensitivity-capped exports from the rebuildable Oracle index,
but they do not become answer authority or canonical memory.
"""
from __future__ import annotations

import json

import derived_memory
import knowledge_index
import oracle_cli


def test_status_defaults_keep_oracle_authoritative(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    out = derived_memory.status(root)

    assert out["ok"] is True
    assert out["canonical_authority"] == "oracle"
    assert out["answer_boundary"] == "oracle_answer_protocol_only"
    assert out["artifact_scope"] == "derived_rebuildable"
    assert out["contract_version"] == derived_memory.CONTRACT_VERSION
    assert out["engines"]["mempalace"]["answer_authority"] == "never"
    assert out["engines"]["graphify"]["answer_authority"] == "never"


def test_validate_rejects_non_oracle_authority(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    cfg = root / "oracle.yml"
    text = cfg.read_text(encoding="utf-8")
    cfg.write_text(
        text
        + "\n"
        + "derived_memory:\n"
        + "  canonical_authority: mempalace\n"
        + "  answer_boundary: oracle_answer_protocol_only\n"
        + "  artifact_scope: derived_rebuildable\n",
        encoding="utf-8",
    )

    problems = derived_memory.validate_config(root)

    assert any(p["code"] == "canonical-authority" for p in problems)


def test_prepare_engine_exports_sensitivity_capped_corpus(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with knowledge_index.KnowledgeIndex(root, force_fallback=True) as idx:
        idx.add_chunks([
            {
                "doc_id": "public-doc",
                "source_id": "SRC-PUB",
                "sensitivity": "public",
                "title": "Public note",
                "text": "public launch language",
                "chunk_index": 0,
                "start": 0,
                "end": 22,
            },
            {
                "doc_id": "internal-doc",
                "source_id": "SRC-INT",
                "sensitivity": "internal",
                "title": "Internal note",
                "text": "internal operating context",
                "chunk_index": 0,
                "start": 0,
                "end": 26,
            },
            {
                "doc_id": "confidential-doc",
                "source_id": "SRC-CONF",
                "sensitivity": "confidential",
                "title": "Confidential note",
                "text": "confidential acquisition target",
                "chunk_index": 0,
                "start": 0,
                "end": 31,
            },
        ])

    manifest = derived_memory.prepare_engine(
        root, "graphify", max_sensitivity="internal"
    )

    assert manifest["engine"] == "graphify"
    assert manifest["canonical_authority"] == "oracle"
    assert manifest["answer_boundary"] == "oracle_answer_protocol_only"
    assert manifest["contract_version"] == derived_memory.CONTRACT_VERSION
    assert manifest["max_sensitivity"] == "internal"
    assert manifest["chunk_count"] == 2
    assert manifest["exported_chunk_count"] == 2
    md = (root / "_data.nosync" / "derived" / "graphify" / "raw" / "oracle-index-chunks.md")
    jsonl = (root / "_data.nosync" / "derived" / "graphify" / "raw" / "oracle-index-chunks.jsonl")
    assert md.exists()
    assert jsonl.exists()
    md_text = md.read_text(encoding="utf-8")
    assert "public launch language" in md_text
    assert "internal operating context" in md_text
    assert "confidential acquisition target" not in md_text
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert {r["source_id"] for r in rows} == {"SRC-PUB", "SRC-INT"}
    ledger_path = root / "Meta.nosync" / "ledgers" / "derived_memory.jsonl"
    ledger_text = ledger_path.read_text(encoding="utf-8")
    assert manifest["ledger_id"] in ledger_text
    assert "public launch language" not in ledger_text
    assert "internal operating context" not in ledger_text


def test_prepare_engine_applies_external_policy_denial(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with knowledge_index.KnowledgeIndex(root, force_fallback=True) as idx:
        idx.add_chunks([
            {
                "doc_id": "public-doc",
                "source_id": "SRC-PUB",
                "sensitivity": "public",
                "title": "Public note",
                "text": "publishable launch language",
            },
            {
                "doc_id": "internal-doc",
                "source_id": "SRC-INT",
                "sensitivity": "internal",
                "title": "Internal note",
                "text": "internal-only operating detail",
            },
        ])

    manifest = derived_memory.prepare_engine(
        root,
        "mempalace",
        max_sensitivity="internal",
        environment="external",
    )

    assert manifest["chunk_count"] == 2
    assert manifest["exported_chunk_count"] == 1
    assert manifest["verdict_counts"]["allow"] == 1
    assert manifest["verdict_counts"]["deny"] == 1
    jsonl = root / "_data.nosync" / "derived" / "mempalace" / "raw" / "oracle-index-chunks.jsonl"
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert [r["source_id"] for r in rows] == ["SRC-PUB"]


def test_prepare_engine_minimizes_local_agent_confidential_text(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    confidential_text = "confidential strategy " + ("detail " * 200)
    with knowledge_index.KnowledgeIndex(root, force_fallback=True) as idx:
        idx.add_chunks([
            {
                "doc_id": "confidential-doc",
                "source_id": "SRC-CONF",
                "sensitivity": "confidential",
                "title": "Confidential note",
                "text": confidential_text,
            },
        ])

    manifest = derived_memory.prepare_engine(
        root,
        "graphify",
        max_sensitivity="confidential",
        environment="local_agent",
    )

    assert manifest["exported_chunk_count"] == 1
    assert manifest["verdict_counts"]["allow-minimized"] == 1
    jsonl = root / "_data.nosync" / "derived" / "graphify" / "raw" / "oracle-index-chunks.jsonl"
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["minimized"] is True
    assert "[...minimized by Oracle policy...]" in rows[0]["text"]
    assert len(rows[0]["text"]) < len(confidential_text)


def test_oracle_cli_dispatches_derived_memory(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)

    rc = oracle_cli.main(["derived-memory", "--root", str(root), "status", "--json"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert "mempalace" in out["engines"]
    assert "graphify" in out["engines"]
