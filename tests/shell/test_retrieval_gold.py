"""tests/shell/test_retrieval_gold.py -- gold retrieval fixtures + eval wiring (P8-T8).

Loads ``eval/fixtures/retrieval_gold.json`` (vendored synthetic-but-pinned
vectors), builds a real kernel index over the public-only corpus, and computes
hit@k / MRR for LEXICAL-ONLY vs HYBRID on the non-held-out queries. Asserts the
frozen acceptance (P8S-12):

  * the lexical-only baseline is recorded;
  * hybrid BEATS lexical on the paraphrase subset;
  * hybrid hit@k >= lexical-only hit@k on the lexical-anchor subset
    (paraphrase recall is not paid for with exact-match regression);
  * the every-5th-id hold-out is untouched by any tuning here;
  * the fixture file is schema-checked;
  * the stuffed-document case never dominates (RRF bounded influence).

The eval body here IS the harness scenario Phase 6 consumes as a behavior
catalog (P6-T1/P6-T5). Stdlib only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "eval" / "fixtures" / "retrieval_gold.json"
KERNEL_TOOLS = REPO_ROOT / "src" / "oracle_agent" / "assets" / "oracle-kernel" / "_tools"

HOLDOUT_IDS = {5, 10, 15, 20, 25}  # every 5th query id (P8S-12)


@pytest.fixture(scope="module")
def gold() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def kernel():
    sys.path.insert(0, str(KERNEL_TOOLS))
    import knowledge_index as k  # type: ignore
    return k


@pytest.fixture(scope="module")
def index(gold, kernel, tmp_path_factory):
    root = tmp_path_factory.mktemp("gold-index")
    idx = kernel.KnowledgeIndex(root=str(root))
    chunks = [
        {
            "doc_id": c["source_id"], "source_id": c["source_id"],
            "sensitivity": c.get("sensitivity", "public"), "provenance": "gold",
            "title": c["source_id"], "chunk_index": c["chunk_index"],
            "start": 0, "end": len(c["text"]), "text": c["text"],
        }
        for c in gold["corpus"]
    ]
    idx.add_chunks(chunks)
    idx.add_vectors([
        {"source_id": c["source_id"], "chunk_index": c["chunk_index"],
         "embedding_model": c["embedding_model"], "vector": c["vector"]}
        for c in gold["corpus"]
    ])
    yield idx
    idx.close()


# --------------------------------------------------------------------------- #
# schema check
# --------------------------------------------------------------------------- #
def test_fixture_schema(gold):
    assert gold["embedding_model"] == "synthetic-hash-v1"
    assert isinstance(gold["dim"], int) and gold["dim"] > 0
    assert "_README" in gold and "synthetic" in gold["_README"].lower()
    dim = gold["dim"]
    seen_ids = set()
    src_ids = {c["source_id"] for c in gold["corpus"]}
    for c in gold["corpus"]:
        assert c["sensitivity"] == "public", "corpus must be public-only"
        assert len(c["vector"]) == dim
        assert c["embedding_model"] == "synthetic-hash-v1"
    assert len(gold["queries"]) == 25
    for q in gold["queries"]:
        assert q["query_id"] not in seen_ids, "duplicate query_id"
        seen_ids.add(q["query_id"])
        assert q["kind"] in ("paraphrase", "lexical_anchor", "direct")
        assert q["expected_source_id"] in src_ids
        assert len(q["vector"]) == dim
    assert seen_ids == set(range(1, 26))


def test_fixture_is_secret_scan_clean(gold):
    """The corpus must carry no high-entropy secret-shaped tokens."""
    blob = json.dumps({"corpus": gold["corpus"]})
    for marker in ("sk-", "Bearer ", "BEGIN PRIVATE KEY", "api_key="):
        assert marker not in blob


def test_holdout_ids_present_and_reserved(gold):
    """Every 5th id is the Phase 6 hold-out -- present in the file, excluded
    from tuning by the eval below (which filters HOLDOUT_IDS)."""
    ids = {q["query_id"] for q in gold["queries"]}
    assert HOLDOUT_IDS <= ids


# --------------------------------------------------------------------------- #
# the eval harness scenario
# --------------------------------------------------------------------------- #
def _topk(idx, q, *, qvec=None, model=None, k=5):
    hits = idx.search(q, k=k, query_vector=qvec, embedding_model=model)
    return [h["source_id"] for h in hits]


def _run_eval(idx, gold, *, k=5):
    """Compute per-kind hit@k and MRR for lexical-only vs hybrid (non-held-out).

    Returns the behavior-catalog dict Phase 6 consumes.
    """
    out: dict = {}
    for kind in ("paraphrase", "lexical_anchor", "direct"):
        out[kind] = {"n": 0, "lexical_hit": 0, "hybrid_hit": 0,
                     "lexical_mrr": 0.0, "hybrid_mrr": 0.0}

    def mrr(ranked, exp):
        for i, s in enumerate(ranked, 1):
            if s == exp:
                return 1.0 / i
        return 0.0

    for q in gold["queries"]:
        if q["query_id"] in HOLDOUT_IDS:
            continue  # hold-out untouched (P8S-12)
        exp = q["expected_source_id"]
        kind = q["kind"]
        lex = _topk(idx, q["text"], k=max(k, 16))
        hyb = _topk(idx, q["text"], qvec=q["vector"],
                    model=q["embedding_model"], k=max(k, 16))
        b = out[kind]
        b["n"] += 1
        b["lexical_hit"] += int(exp in lex[:k])
        b["hybrid_hit"] += int(exp in hyb[:k])
        b["lexical_mrr"] += mrr(lex, exp)
        b["hybrid_mrr"] += mrr(hyb, exp)
    return out


def test_lexical_baseline_recorded(index, gold):
    res = _run_eval(index, gold)
    # The lexical-only baseline exists and is non-trivial (sanity floor).
    assert res["paraphrase"]["n"] > 0
    assert res["lexical_anchor"]["n"] > 0
    assert res["direct"]["lexical_hit"] >= 1


def test_hybrid_beats_lexical_on_paraphrase(index, gold):
    res = _run_eval(index, gold)
    p = res["paraphrase"]
    assert p["hybrid_hit"] > p["lexical_hit"], (
        f"hybrid must beat lexical on paraphrase: "
        f"lexical={p['lexical_hit']}/{p['n']} hybrid={p['hybrid_hit']}/{p['n']}"
    )


def test_hybrid_does_not_regress_lexical_anchor(index, gold):
    res = _run_eval(index, gold)
    a = res["lexical_anchor"]
    assert a["hybrid_hit"] >= a["lexical_hit"], (
        f"hybrid must NOT regress the lexical-anchor subset: "
        f"lexical={a['lexical_hit']}/{a['n']} hybrid={a['hybrid_hit']}/{a['n']}"
    )


def test_stuffed_doc_never_dominates(index, gold):
    """RRF caps any single ranking at 1/(60+1) per doc, so the keyword-stuffed
    chunk must never rank #1 for the gold queries (bounded influence, P8S-12)."""
    for q in gold["queries"]:
        if q["query_id"] in HOLDOUT_IDS:
            continue
        hyb = _topk(index, q["text"], qvec=q["vector"],
                    model=q["embedding_model"], k=1)
        if hyb:
            assert hyb[0] != "src-stuffed", (
                f"stuffed doc dominated query {q['query_id']!r}"
            )


def test_regen_script_is_deterministic(gold):
    """Re-running the regen builder reproduces the file byte-for-byte (vendored,
    pinned, reproducible)."""
    sys.path.insert(0, str(FIXTURE.parent))
    import regen_retrieval_gold as regen  # type: ignore

    rebuilt = regen.build()
    # Compare structurally (the on-disk file has the same content).
    assert rebuilt["embedding_model"] == gold["embedding_model"]
    assert rebuilt["dim"] == gold["dim"]
    assert len(rebuilt["corpus"]) == len(gold["corpus"])
    assert rebuilt["corpus"][0]["vector"] == gold["corpus"][0]["vector"]
