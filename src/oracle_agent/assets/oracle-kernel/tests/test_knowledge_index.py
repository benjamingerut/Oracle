#!/usr/bin/env python3
"""Tests for knowledge_index.py -- the retrieval index.

Covers (per the build plan):
  * add docs, search returns hits;
  * FTS5 build+query (when this interpreter's sqlite3 has FTS5);
  * FORCED pure-python fallback path (always exercised regardless of FTS5);
  * parity: the fallback returns the same top hit as the FTS5 engine;
  * sensitivity-filtered query excludes over-ceiling rows;
  * stats + reindex + persistence across reopen.

These tests use bare ``import knowledge_index`` (conftest injects _tools on
sys.path) and the ``minimal_oracle`` fixture for a containment root. They do not
depend on any sibling module still being built.
"""
from __future__ import annotations

import sqlite3

import pytest

import chunker
import knowledge_index as ki


# --------------------------------------------------------------------------- #
# fixtures / corpus
# --------------------------------------------------------------------------- #
_CORPUS = [
    {
        "doc_id": "doc-revenue",
        "text": "Quarterly revenue grew as enterprise customers expanded their contracts.",
        "source_id": "SRC-1",
        "sensitivity": "internal",
        "provenance": "finance-export",
        "chunk_index": 0,
        "start": 0,
        "end": 70,
        "title": "Revenue note",
    },
    {
        "doc_id": "doc-churn",
        "text": "Customer churn rose in the small-business segment during the same period.",
        "source_id": "SRC-2",
        "sensitivity": "confidential",
        "provenance": "crm-export",
        "chunk_index": 0,
        "start": 0,
        "end": 72,
        "title": "Churn note",
    },
    {
        "doc_id": "doc-hiring",
        "text": "The engineering team plans to hire additional backend developers next quarter.",
        "source_id": "SRC-3",
        "sensitivity": "secret",
        "provenance": "hr-plan",
        "chunk_index": 0,
        "start": 0,
        "end": 78,
        "title": "Hiring note",
    },
]


def _new_index(root, *, force_fallback):
    return ki.KnowledgeIndex(root, force_fallback=force_fallback)


# --------------------------------------------------------------------------- #
# tokenizer
# --------------------------------------------------------------------------- #
def test_tokenize_lowercases_drops_stopwords_and_stems():
    toks = ki.tokenize("The Revenues are growing for Customers")
    # stopwords 'the' and 'are' and 'for' dropped; 'revenues'->'revenue',
    # 'customers'->'customer', 'growing'->'grow'.
    assert "the" not in toks
    assert "are" not in toks
    assert "revenue" in toks
    assert "customer" in toks
    assert "grow" in toks


def test_tokenize_inflection_parity():
    """The load-bearing property: singular/plural and base/gerund co-locate."""
    for singular, inflected in [
        ("revenue", "revenues"),
        ("customer", "customers"),
        ("contract", "contracts"),
        ("grow", "growing"),
        ("ship", "shipping"),
        ("plan", "planned"),
        ("company", "companies"),
    ]:
        assert ki.tokenize(singular) == ki.tokenize(inflected), (singular, inflected)


# --------------------------------------------------------------------------- #
# basic add + search (default engine, whatever this build provides)
# --------------------------------------------------------------------------- #
def test_add_and_search_returns_hits(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=False) as idx:
        idx.add_chunks(_CORPUS)
        hits = idx.search("revenue customers")
        assert hits, "expected at least one hit for 'revenue customers'"
        top = hits[0]
        assert top["doc_id"] == "doc-revenue"
        assert top["source_id"] == "SRC-1"
        assert top["score"] > 0


def test_search_empty_query_returns_nothing(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=False) as idx:
        idx.add_chunks(_CORPUS)
        assert idx.search("") == []
        assert idx.search("   ") == []


def test_search_unknown_term_returns_empty(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=False) as idx:
        idx.add_chunks(_CORPUS)
        assert idx.search("zzzznonexistenttoken") == []


# --------------------------------------------------------------------------- #
# FORCED fallback path -- always exercised
# --------------------------------------------------------------------------- #
def test_forced_fallback_engine_is_used(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        assert idx.engine == "fallback"
        idx.add_chunks(_CORPUS)
        hits = idx.search("churn segment")
        assert hits
        assert hits[0]["doc_id"] == "doc-churn"


def test_fallback_via_env(monkeypatch, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    monkeypatch.setenv("ORACLE_INDEX_FORCE_FALLBACK", "1")
    with ki.KnowledgeIndex(root) as idx:
        assert idx.engine == "fallback"
        idx.add(
            "doc-x", "hiring backend developers", source_id="SRC-3",
            sensitivity="internal",
        )
        hits = idx.search("backend developers")
        assert hits and hits[0]["doc_id"] == "doc-x"


def test_fallback_builds_postings_table(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        # The inverted index must hold postings in fallback mode.
        con = sqlite3.connect(str(idx.db_path))
        n = con.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        con.close()
        assert n > 0


# --------------------------------------------------------------------------- #
# FTS5 path (skipped if this interpreter lacks FTS5)
# --------------------------------------------------------------------------- #
def test_fts5_build_and_query(tmp_path, minimal_oracle):
    if not ki.fts5_available():
        pytest.skip("sqlite3 built without FTS5 on this interpreter")
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=False) as idx:
        assert idx.engine == "fts5"
        idx.add_chunks(_CORPUS)
        hits = idx.search("revenue")
        assert hits and hits[0]["doc_id"] == "doc-revenue"


# --------------------------------------------------------------------------- #
# parity: forced-fallback and (when available) FTS5 agree on the top hit
# --------------------------------------------------------------------------- #
def test_fallback_and_fts5_top_hit_parity(tmp_path, minimal_oracle):
    root_fb = minimal_oracle(tmp_path / "fb")
    with _new_index(root_fb, force_fallback=True) as idx_fb:
        idx_fb.add_chunks(_CORPUS)
        fb_hits = idx_fb.search("revenue customers")
    assert fb_hits and fb_hits[0]["doc_id"] == "doc-revenue"

    if not ki.fts5_available():
        pytest.skip("no FTS5 to compare against; fallback verified standalone")
    root_fts = minimal_oracle(tmp_path / "fts")
    with _new_index(root_fts, force_fallback=False) as idx_fts:
        assert idx_fts.engine == "fts5"
        idx_fts.add_chunks(_CORPUS)
        fts_hits = idx_fts.search("revenue customers")
    assert fts_hits and fts_hits[0]["doc_id"] == "doc-revenue"
    # Same winning document from both engines.
    assert fb_hits[0]["doc_id"] == fts_hits[0]["doc_id"]


# --------------------------------------------------------------------------- #
# sensitivity-filtered query
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_sensitivity_ceiling_excludes_over_ceiling_rows(
    force_fallback, tmp_path, minimal_oracle
):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        # 'quarter' appears in the secret hiring doc and (via stem) elsewhere.
        # With a ceiling of 'internal', the secret + confidential rows are out.
        hits = idx.search("quarter customers segment", max_sensitivity="internal")
        for h in hits:
            assert h["sensitivity"] in ("public", "internal"), h
        # The secret hiring doc must never appear under an internal ceiling.
        assert all(h["doc_id"] != "doc-hiring" for h in hits)


@pytest.mark.parametrize("force_fallback", [True, False])
def test_sensitivity_ceiling_at_secret_admits_all(
    force_fallback, tmp_path, minimal_oracle
):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        hits = idx.search("quarter", max_sensitivity="secret")
        # 'next quarter' is in the secret doc; at a secret ceiling it is allowed.
        ids = {h["doc_id"] for h in hits}
        assert "doc-hiring" in ids


def test_no_ceiling_returns_all_matches(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        hits = idx.search("quarter")
        ids = {h["doc_id"] for h in hits}
        # Without a ceiling, the secret hiring doc IS returned.
        assert "doc-hiring" in ids


# --------------------------------------------------------------------------- #
# stats / reindex / persistence
# --------------------------------------------------------------------------- #
def test_stats_reports_counts(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        st = idx.stats()
        assert st["engine"] == "fallback"
        assert st["chunks"] == len(_CORPUS)
        assert st["documents"] == len(_CORPUS)
        assert st["by_sensitivity"].get("internal") == 1
        assert st["by_sensitivity"].get("confidential") == 1
        assert st["by_sensitivity"].get("secret") == 1


def test_reindex_wipes_and_rebuilds(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)
        assert idx.stats()["chunks"] == len(_CORPUS)
        # Reindex with a single new chunk.
        n = idx.reindex([
            {"doc_id": "only", "text": "a fresh single chunk about pricing",
             "sensitivity": "internal"},
        ])
        assert n == 1
        assert idx.stats()["chunks"] == 1
        hits = idx.search("pricing")
        assert hits and hits[0]["doc_id"] == "only"
        # Old content is gone.
        assert idx.search("churn") == []


def test_index_persists_across_reopen(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    db_path = ki.default_db_path(root)
    idx = ki.KnowledgeIndex(root, force_fallback=True)
    idx.add_chunks(_CORPUS)
    engine_first = idx.engine
    idx.close()
    # Reopen the SAME db; rows + engine choice must survive.
    idx2 = ki.KnowledgeIndex(root, db_path=db_path)
    try:
        assert idx2.engine == engine_first  # honored what is on disk
        hits = idx2.search("revenue")
        assert hits and hits[0]["doc_id"] == "doc-revenue"
        assert idx2.stats()["chunks"] == len(_CORPUS)
    finally:
        idx2.close()


def test_default_db_path_is_under_data_index(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    p = ki.default_db_path(root)
    assert p.name == "knowledge.db"
    assert p.parent.name == "index"
    assert p.parent.parent.name == "_data.nosync"


def test_add_single_then_search(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add(
            "solo", "the oracle indexes business documents for retrieval",
            source_id="SRC-9", sensitivity="internal", provenance="manual",
            chunk_index=2, start=10, end=58, title="Solo",
        )
        hits = idx.search("business documents retrieval")
        assert len(hits) == 1
        h = hits[0]
        assert h["doc_id"] == "solo"
        assert h["chunk_index"] == 2
        assert h["start"] == 10
        assert h["end"] == 58
        assert h["title"] == "Solo"
        assert h["provenance"] == "manual"


def test_list_chunks_filters_source_and_sensitivity(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        idx.add_chunks(_CORPUS)

    rows = ki.list_chunks(root, source_id="SRC-1", max_sensitivity="internal")

    assert len(rows) == 1
    assert rows[0]["doc_id"] == "doc-revenue"
    assert rows[0]["source_id"] == "SRC-1"
    assert rows[0]["text"].startswith("Quarterly revenue")
    assert ki.list_chunks(root, max_sensitivity="public") == []


def test_module_index_chunks_normalizes_pipeline_chunks(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    text = "Pipeline indexed alpha revenue.\n\nPipeline indexed beta customers."
    chunks = chunker.chunk(text, size=40, overlap=0)

    n = ki.index_chunks(
        root,
        source_id="SRC-PIPE",
        chunks=chunks,
        sensitivity="internal",
        provenance={"connector": "manual", "sha256": "abc123"},
    )

    assert n == len(chunks)
    rows = ki.list_chunks(root, source_id="SRC-PIPE")
    assert len(rows) == len(chunks)
    assert rows[0]["doc_id"] == "SRC-PIPE"
    assert rows[0]["sensitivity"] == "internal"
    assert "\"connector\": \"manual\"" in rows[0]["provenance"]
    with ki.KnowledgeIndex(root) as idx:
        hits = idx.search("beta customers", max_sensitivity="internal")
    assert hits
    assert hits[0]["source_id"] == "SRC-PIPE"
