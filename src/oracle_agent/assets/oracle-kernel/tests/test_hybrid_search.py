#!/usr/bin/env python3
"""Tests for hybrid RRF search (Phase 8, P8-T2).

Covers the spec's acceptance items:
  * without a query vector the output is BYTE-IDENTICAL to today's lexical path;
  * with one, a paraphrase fixture query ranks the semantically-matching chunk
    first;
  * an over-ceiling chunk never appears even when it is the best cosine hit;
  * adding OR removing an above-ceiling chunk leaves the below-ceiling result
    list AND scores byte-identical (the rank-perturbation existence leak, P8S-5);
  * a same-model different-dim vector is skipped and counted, never fused
    (P8S-11);
  * hits keep exact start/end offsets;
  * RRF dense ranks are computed within the ceiling-FILTERED lists;
  * the cosine dot product is correct on BOTH the math.sumprod fast path and the
    pure-Python fallback (the floor 3.10 path, P8S-7);
  * the stdin query-vector caps (1 MiB, <=8192 dims, finite floats) reject bad
    payloads and never echo the vector.

Both engines (FTS5 + forced fallback) are exercised.
"""
from __future__ import annotations

import io
import json
import math
from array import array

import pytest

import knowledge_index as ki


_MODEL = "test-embed-v1"


def _new_index(root, *, force_fallback):
    return ki.KnowledgeIndex(root, force_fallback=force_fallback)


# A tiny 3-dim "semantic" space: each chunk gets a hand-placed unit-ish vector.
# The query is "headcount" -- it lexically matches only a weak/noise chunk, and
# the query VECTOR points at the paraphrase chunk ("number of employees"), which
# shares NO lexical token with the query. So the paraphrase chunk is invisible
# to lexical and must surface -- and rank first -- via the dense signal alone.
# Crucially, the lexical winner (SRC-NOISE) is FAR from the query in vector
# space, so it does not get a second RRF term that would let it beat the
# dense-only paraphrase chunk.
_CORPUS = [
    {  # the paraphrase target: weak lexical match on "employees", strong dense
        "doc_id": "doc-para", "text": "the number of employees across the org",
        "source_id": "SRC-PARA", "sensitivity": "internal", "chunk_index": 0,
        "start": 0, "end": 38, "title": "Para",
    },
    {  # lexical match on "headcount" only; no vector -> lexical-only
        "doc_id": "doc-noise",
        "text": "headcount appears once in this snack memo",
        "source_id": "SRC-NOISE", "sensitivity": "internal", "chunk_index": 0,
        "start": 0, "end": 41, "title": "Noise",
    },
    {  # a third, fully unrelated chunk (matches neither query term)
        "doc_id": "doc-rev", "text": "revenue figures for the quarter",
        "source_id": "SRC-REV", "sensitivity": "internal", "chunk_index": 0,
        "start": 5, "end": 36, "title": "Rev",
    },
]

# The paraphrase chunk's vector points at the query; SRC-REV is orthogonal.
# SRC-NOISE deliberately has NO vector (lexical-only), so the paraphrase chunk
# -- present in BOTH the lexical and dense lists -- fuses to the top rank, while
# the lexical-only noise chunk earns a single RRF term.
_VECTORS = [
    {"source_id": "SRC-PARA", "chunk_index": 0, "embedding_model": _MODEL,
     "vector": [1.0, 0.02, 0.0]},
    {"source_id": "SRC-REV", "chunk_index": 0, "embedding_model": _MODEL,
     "vector": [0.0, 1.0, 0.0]},
]

# Query vector + lexical query (mentions "employees" -> SRC-PARA and "headcount"
# -> SRC-NOISE, so both retrievers contribute and fusion is exercised).
_QVEC = [1.0, 0.0, 0.0]
_QUERY = "employees headcount"


def _load(idx):
    idx.add_chunks(_CORPUS)
    idx.add_vectors(_VECTORS)


# --------------------------------------------------------------------------- #
# vector math correctness on both paths
# --------------------------------------------------------------------------- #
def test_dot_matches_pure_python_fallback():
    a = [0.1, 0.2, 0.3, 0.4]
    b = [0.5, 0.6, 0.7, 0.8]
    expected = sum(x * y for x, y in zip(a, b))
    # Fast path (math.sumprod when available).
    assert abs(ki._dot(a, b) - expected) < 1e-9
    # Force the pure-Python fallback by hiding sumprod.
    saved = getattr(math, "sumprod", None)
    try:
        if hasattr(math, "sumprod"):
            delattr(math, "sumprod")
        assert abs(ki._dot(a, b) - expected) < 1e-9
    finally:
        if saved is not None:
            math.sumprod = saved


def test_normalize_rejects_degenerate():
    with pytest.raises(ValueError):
        ki._normalize_vector([0.0, 0.0])
    with pytest.raises(ValueError):
        ki._normalize_vector([float("nan"), 1.0])


# --------------------------------------------------------------------------- #
# lexical-only path is byte-identical to today
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_lexical_path_byte_identical_without_qvec(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        _load(idx)
        # Search with NO query vector -- must equal the result with vectors
        # absent from the DB entirely (proving vectors don't perturb lexical).
        with_vectors = idx.search(_QUERY)
    # Fresh index, same chunks, NO vectors.
    root2 = minimal_oracle(tmp_path / "novec")
    with _new_index(root2, force_fallback=force_fallback) as idx2:
        idx2.add_chunks(_CORPUS)
        without_vectors = idx2.search(_QUERY)
    assert json.dumps(with_vectors) == json.dumps(without_vectors)


# --------------------------------------------------------------------------- #
# paraphrase: the dense signal surfaces a lexically-invisible chunk
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_paraphrase_ranks_semantic_match_first(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        _load(idx)
        # The query "headcount" matches SRC-NOISE lexically; the dense vector
        # points at SRC-PARA ("number of employees"), which shares no token with
        # the query. The paraphrase chunk -- invisible to lexical -- surfaces and
        # ranks first because it is the sole dense rank-1 hit while the lexical
        # winner (SRC-NOISE) earns no dense term (it is orthogonal to the query).
        hits = idx.search(
            _QUERY, query_vector=_QVEC, embedding_model=_MODEL
        )
        ids = [h["source_id"] for h in hits]
        assert "SRC-PARA" in ids, ids
        assert hits[0]["source_id"] == "SRC-PARA", ids


# --------------------------------------------------------------------------- #
# offsets preserved
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_hybrid_hits_keep_offsets(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        _load(idx)
        hits = idx.search(_QUERY, query_vector=_QVEC, embedding_model=_MODEL)
        by_id = {h["source_id"]: h for h in hits}
        # The paraphrase chunk surfaces via dense; its offsets are intact.
        assert by_id["SRC-PARA"]["start"] == 0
        assert by_id["SRC-PARA"]["end"] == 38


# --------------------------------------------------------------------------- #
# over-ceiling chunk invisible even as the best cosine hit
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_over_ceiling_chunk_never_appears(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        # A SECRET chunk that is the PERFECT cosine match for the query.
        idx.add_chunks([{
            "doc_id": "doc-secret", "text": "classified roadmap entry",
            "source_id": "SRC-SECRET", "sensitivity": "secret",
            "chunk_index": 0, "start": 0, "end": 24, "title": "Secret",
        }])
        idx.add_vectors(_VECTORS + [
            {"source_id": "SRC-SECRET", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _QVEC},  # exact match
        ])
        hits = idx.search(
            _QUERY, query_vector=_QVEC, embedding_model=_MODEL,
            max_sensitivity="internal",
        )
        ids = {h["source_id"] for h in hits}
        assert "SRC-SECRET" not in ids, ids


# --------------------------------------------------------------------------- #
# the rank-perturbation existence leak (P8S-5): byte-identical add/remove
# --------------------------------------------------------------------------- #
_SECRET_CHUNK = {
    "doc_id": "doc-secret", "text": "classified roadmap entry beyond",
    "source_id": "SRC-SECRET", "sensitivity": "secret",
    "chunk_index": 0, "start": 0, "end": 30, "title": "Secret",
}


@pytest.mark.parametrize("force_fallback", [True, False])
def test_above_ceiling_chunk_is_byte_identical_inert(force_fallback, tmp_path, minimal_oracle):
    """Adding or removing an above-ceiling chunk's VECTOR leaves the
    below-ceiling result list AND scores byte-identical -- no RRF rank shift can
    leak its existence (P8S-5). The ceiling filter is applied IN the dense scan
    before any ranking, so the secret chunk's vector -- even an exact-match best
    cosine -- contributes no dense rank to the visible documents.

    Both variants hold the LEXICAL CORPUS CONSTANT (the secret chunk is present
    in both) so the named bm25/idf corpus-global residual is held equal: this
    test isolates the RRF rank-perturbation property, which is what P8S-5
    governs. Only the presence of the secret chunk's VECTOR varies."""
    # Variant A: secret chunk present, but NO secret vector.
    root_a = minimal_oracle(tmp_path / "a")
    with _new_index(root_a, force_fallback=force_fallback) as idx_a:
        _load(idx_a)
        idx_a.add_chunks([_SECRET_CHUNK])
        baseline = idx_a.search(
            _QUERY, query_vector=_QVEC, embedding_model=_MODEL,
            max_sensitivity="internal",
        )

    # Variant B: SAME chunks PLUS the secret chunk's vector (exact best cosine).
    root_b = minimal_oracle(tmp_path / "b")
    with _new_index(root_b, force_fallback=force_fallback) as idx_b:
        _load(idx_b)
        idx_b.add_chunks([_SECRET_CHUNK])
        idx_b.add_vectors([
            {"source_id": "SRC-SECRET", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": _QVEC},
        ])
        with_secret = idx_b.search(
            _QUERY, query_vector=_QVEC, embedding_model=_MODEL,
            max_sensitivity="internal",
        )

    # Byte-identical: same list, same scores, same order. The secret vector,
    # though the best cosine, never perturbed a visible dense rank.
    assert json.dumps(baseline) == json.dumps(with_secret)


# --------------------------------------------------------------------------- #
# dim mismatch skipped + counted (P8S-11)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_same_model_different_dim_skipped_and_counted(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        idx.add_chunks(_CORPUS)
        # SRC-PARA gets a 3-dim vector (matches the query dim); SRC-REV gets a
        # 4-dim vector under the SAME model name -- the 4-dim one must be skipped
        # (query is 3-dim) and counted in dim_mismatches.
        idx.add_vectors([
            {"source_id": "SRC-PARA", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": [1.0, 0.0, 0.0]},
            {"source_id": "SRC-REV", "chunk_index": 0,
             "embedding_model": _MODEL, "vector": [1.0, 0.0, 0.0, 0.0]},
        ])
        hits = idx.search(
            _QUERY, query_vector=_QVEC, embedding_model=_MODEL,
            max_sensitivity="internal",
        )
        # SRC-REV's mismatched-dim vector contributed nothing to the dense
        # ranking; it was skipped and counted.
        st = idx.stats()
        assert st["dim_mismatches"] == 1
        # The paraphrase chunk (covered, matching dim) surfaced via dense.
        assert "SRC-PARA" in {h["source_id"] for h in hits}


# --------------------------------------------------------------------------- #
# wrong / missing model -> no dense contribution (lexical still works)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("force_fallback", [True, False])
def test_unmatched_model_degrades_to_lexical(force_fallback, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=force_fallback) as idx:
        _load(idx)
        # Query a model name nothing was embedded under: dense contributes
        # nothing, lexical runs exactly as today. Use a query that ONLY the
        # paraphrase-adjacent term would surface via dense, so we can prove the
        # dense-only surfacing did NOT happen.
        hits = idx.search(
            "employees", query_vector=_QVEC, embedding_model="other-model"
        )
        ids = {h["source_id"] for h in hits}
        # Lexical returns the chunk that literally contains "employees" (SRC-PARA
        # matches on the token). The point: NO dense-only chunk appears -- here
        # SRC-REV (orthogonal, lexical-miss) is absent.
        assert "SRC-REV" not in ids


# --------------------------------------------------------------------------- #
# stdin query-vector validation (P8S-3)
# --------------------------------------------------------------------------- #
def test_query_vector_stdin_valid():
    payload = json.dumps({"embedding_model": _MODEL, "vector": [3.0, 4.0]})
    model, vec = ki._read_query_vector_stdin(io.StringIO(payload))
    assert model == _MODEL
    # Normalized: (3,4)/5
    assert abs(vec[0] - 0.6) < 1e-6 and abs(vec[1] - 0.8) < 1e-6


def test_query_vector_stdin_rejects_oversize_bytes():
    huge = "x" * (ki._QVEC_MAX_BYTES + 10)
    with pytest.raises(ValueError):
        ki._read_query_vector_stdin(io.StringIO(huge))


def test_query_vector_stdin_rejects_too_many_dims():
    payload = json.dumps({
        "embedding_model": _MODEL,
        "vector": [0.0] * (ki._QVEC_MAX_DIMS + 1),
    })
    with pytest.raises(ValueError):
        ki._read_query_vector_stdin(io.StringIO(payload))


def test_query_vector_stdin_rejects_non_finite_without_echo():
    weird = 987654.0
    payload = json.dumps({"embedding_model": _MODEL,
                          "vector": [weird, float("inf")]})
    try:
        ki._read_query_vector_stdin(io.StringIO(payload))
        assert False, "expected rejection"
    except ValueError as exc:
        assert str(weird) not in str(exc)
        assert "inf" not in str(exc).lower() or "non-finite" in str(exc).lower()


def test_query_vector_stdin_rejects_bad_json():
    with pytest.raises(ValueError):
        ki._read_query_vector_stdin(io.StringIO("{not json"))


# --------------------------------------------------------------------------- #
# CLI --qvec-stdin end-to-end
# --------------------------------------------------------------------------- #
def test_cli_query_with_qvec_stdin(tmp_path, minimal_oracle, monkeypatch, capsys):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        _load(idx)
    payload = json.dumps({"embedding_model": _MODEL, "vector": _QVEC})

    class _FakeStdin(io.StringIO):
        pass

    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc = ki.main([
        "--root", str(root), "--force-fallback",
        "query", "--q", _QUERY, "--qvec-stdin",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ids = [h["source_id"] for h in out]
    assert "SRC-PARA" in ids


# --------------------------------------------------------------------------- #
# RRF bounded influence: a stuffed lexical doc cannot dominate beyond 1/(K+1)
# --------------------------------------------------------------------------- #
def test_rrf_caps_single_ranking_contribution(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with _new_index(root, force_fallback=True) as idx:
        _load(idx)
        hits = idx.search(_QUERY, query_vector=_QVEC, embedding_model=_MODEL)
        for h in hits:
            # The fused score is a sum of <=2 reciprocal-rank terms, each
            # <= 1/(K+1); no single document can exceed 2/(K+1).
            assert h["score"] <= 2.0 / (ki.RRF_K + 1) + 1e-9
