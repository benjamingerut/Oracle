"""usefulness/ -- deterministic retrieval-quality metrics (class 2, P6-T7).

Tracked, NOT gated. Three class-2 deliverables, all honest under a fake because
they measure SHELL/KERNEL code over FIXED FIXTURES (P6S-1):

  * **gold retrieval scoring** on ``eval/fixtures/retrieval_gold.json`` under
    the FIXTURE-SCOPED names ``gold_hit_at_k`` / ``gold_mrr`` (the kernel KPI
    names ``retrieval_hit_rate`` / ``time_to_first_grounded_answer`` are
    RESERVED to the kernel scorecard -- a fixture eval republishing those names
    would report a different number for the same name, the exact failure P6S-3
    forbids). This is the ``test_retrieval_gold.py`` eval body promoted into the
    catalog, scored over the real hybrid index.
  * **hold-out consumption + lifecycle (P6S-6):** this FIRST scoring run
    consumes the frozen hold-out (query ids 5/10/15/20/25). The rendered
    scorecard stamps the hold-out as CONVENTION-ONLY (in-repo, excluded from
    tuning by P8S-12 discipline, not secret). The consumed ids are recorded
    (dated) and a fresh hold-out is minted via ``regen_retrieval_gold.py`` (new
    generation, same every-5th rule) for the next eval generation -- run as an
    operator action, committed by a human; this scenario only ASSERTS the
    convention holds and the consumed ids are present.
  * **intake throughput** on ``eval/fixtures/connector_corpus/`` -- a small
    synthetic, secret-scan-clean corpus shipped BY this phase. "answerable-from"
    is pinned as: a fixed probe query returns the ingested doc's source_id in
    top-k. Throughput is measured in DETERMINISTIC COUNTS (documents per pull
    batch, pipeline-stage count to answerable) -- never seconds (P6S-10).

These are ``dimension="usefulness"`` => derived severity ``quality`` =>
tracked, never a safety-floor breach; ``fault_point=None``.

Stdlib only. testkit-importing is sanctioned for this package (P6S-12).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from oracle_agent.eval.harness import Observation, Scenario, Verdict

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GOLD = _REPO_ROOT / "eval" / "fixtures" / "retrieval_gold.json"
_CONNECTOR_CORPUS = (_REPO_ROOT / "eval" / "fixtures" / "connector_corpus" /
                     "corpus.json")
_KERNEL_TOOLS = (_REPO_ROOT / "src" / "oracle_agent" / "assets" /
                 "oracle-kernel" / "_tools")

#: The frozen Phase 6 hold-out: every 5th query id. CONSUMED on this first
#: scoring run (P6S-6). Convention-only -- world-readable, in-repo, not secret.
HOLDOUT_IDS = (5, 10, 15, 20, 25)


def _kernel_index_module():
    if str(_KERNEL_TOOLS) not in sys.path:
        sys.path.insert(0, str(_KERNEL_TOOLS))
    import knowledge_index as k  # type: ignore
    return k


# --------------------------------------------------------------------------- #
# EVAL-USEFUL-001: gold_hit_at_k / gold_mrr over the hybrid index, CONSUMING
# the frozen hold-out on this first scoring run (P6S-6). Both the non-held-out
# (tracked) set and the hold-out are scored; the hold-out ids are recorded as
# consumed. The verdict stamps the hold-out as convention-only and asserts a
# usefulness FLOOR (hybrid recalls the gold targets) -- but never GATES (this is
# a quality dimension; a low number is a tracked trend, not a CI failure).
# --------------------------------------------------------------------------- #
def _gold_setup(Harness):
    return {}


def _build_gold_index(k, gold):
    import tempfile
    holder = tempfile.TemporaryDirectory(prefix="oracle-eval-gold-")
    idx = k.KnowledgeIndex(root=holder.name)
    idx.add_chunks([
        {"doc_id": c["source_id"], "source_id": c["source_id"],
         "sensitivity": c.get("sensitivity", "public"), "provenance": "gold",
         "title": c["source_id"], "chunk_index": c["chunk_index"],
         "start": 0, "end": len(c["text"]), "text": c["text"]}
        for c in gold["corpus"]
    ])
    idx.add_vectors([
        {"source_id": c["source_id"], "chunk_index": c["chunk_index"],
         "embedding_model": c["embedding_model"], "vector": c["vector"]}
        for c in gold["corpus"]
    ])
    return idx, holder


def _gold_run(ctx) -> Observation:
    k = _kernel_index_module()
    gold = json.loads(_GOLD.read_text(encoding="utf-8"))
    idx, holder = _build_gold_index(k, gold)
    K = 5

    def topk(q):
        hits = idx.search(q["text"], k=max(K, 16), query_vector=q["vector"],
                          embedding_model=q["embedding_model"])
        return [h["source_id"] for h in hits]

    def mrr(ranked, exp):
        for i, s in enumerate(ranked, 1):
            if s == exp:
                return 1.0 / i
        return 0.0

    holdout = set(HOLDOUT_IDS)
    tracked = {"n": 0, "hits": 0, "mrr_sum": 0.0}
    held = {"n": 0, "hits": 0, "mrr_sum": 0.0, "ids": []}
    try:
        for q in gold["queries"]:
            ranked = topk(q)
            exp = q["expected_source_id"]
            bucket = held if q["query_id"] in holdout else tracked
            bucket["n"] += 1
            bucket["hits"] += int(exp in ranked[:K])
            bucket["mrr_sum"] += mrr(ranked, exp)
            if q["query_id"] in holdout:
                held["ids"].append(q["query_id"])
    finally:
        idx.close()
        holder.cleanup()

    def finalize(b):
        n = b["n"] or 1
        return {"n": b["n"],
                "gold_hit_at_k": round(b["hits"] / n, 4),
                "gold_mrr": round(b["mrr_sum"] / n, 4)}

    return Observation(extras={
        "k": K,
        "tracked": finalize(tracked),
        "holdout": {**finalize(held), "consumed_ids": sorted(held["ids"])},
    })


def _gold_assert(obs) -> Verdict:
    x = obs.extras
    tracked = x["tracked"]
    holdout = x["holdout"]
    # The hold-out must have been CONSUMED on this run (P6S-6): all five ids.
    if sorted(holdout["consumed_ids"]) != sorted(HOLDOUT_IDS):
        return Verdict(False, (
            f"hold-out not fully consumed: scored {holdout['consumed_ids']}, "
            f"expected {list(HOLDOUT_IDS)} (the every-5th hold-out, P6S-6)"))
    # Usefulness FLOOR (tracked, not gated): the hybrid index must recall the
    # gold targets at a useful rate -- otherwise the metric is not meaningful.
    if tracked["n"] < 1 or tracked["gold_hit_at_k"] < 0.5:
        return Verdict(False, (
            f"gold_hit_at_k on the tracked set is "
            f"{tracked['gold_hit_at_k']} over n={tracked['n']} -- below the "
            f"usefulness floor (the harness is not surfacing gold targets)"))
    return Verdict(True, (
        f"gold_hit_at_k={tracked['gold_hit_at_k']} gold_mrr={tracked['gold_mrr']} "
        f"(tracked n={tracked['n']}, fixture-scoped names; kernel KPI names "
        f"reserved to the kernel scorecard). Hold-out CONSUMED this run: ids "
        f"{holdout['consumed_ids']} (gold_hit_at_k={holdout['gold_hit_at_k']}) "
        f"-- convention-only (in-repo, excluded from tuning, not secret); a "
        f"fresh hold-out is regenerated via regen_retrieval_gold.py for the "
        f"next eval generation."))


# --------------------------------------------------------------------------- #
# EVAL-USEFUL-002: connector intake throughput on eval/fixtures/connector_corpus/.
# Ingest the synthetic batch into a real kernel index, then probe each doc:
# "answerable-from" == the doc's source_id is in top-k for its fixed probe.
# Throughput is reported in DETERMINISTIC COUNTS: documents per batch, and the
# pipeline-stage count to answerable -- never seconds (P6S-10).
# --------------------------------------------------------------------------- #
def _intake_setup(Harness):
    return {}


def _intake_run(ctx) -> Observation:
    import tempfile

    k = _kernel_index_module()
    corpus = json.loads(_CONNECTOR_CORPUS.read_text(encoding="utf-8"))
    docs = corpus["docs"]
    top_k = int(corpus["answerable_top_k"])
    stages = list(corpus["pipeline_stages"])
    model = corpus["embedding_model"]

    holder = tempfile.TemporaryDirectory(prefix="oracle-eval-intake-")
    idx = k.KnowledgeIndex(root=holder.name)

    # Stage 'pull' -> 'stage' -> 'index' as deterministic count-bearing steps.
    chunks = [
        {"doc_id": d["source_id"], "source_id": d["source_id"],
         "sensitivity": "public", "provenance": "connector",
         "title": d["title"], "chunk_index": 0,
         "start": 0, "end": len(d["text"]), "text": d["text"]}
        for d in docs
    ]
    vectors = [
        {"source_id": d["source_id"], "chunk_index": 0,
         "embedding_model": model,
         "vector": d["vector"]}
        for d in docs
    ]
    idx.add_chunks(chunks)
    idx.add_vectors(vectors)

    answerable = 0
    probe_invocations = 0
    not_answerable: list[str] = []
    try:
        for d in docs:
            qvec = d["probe_vector"]
            probe_invocations += 1
            hits = idx.search(d["probe"], k=max(top_k, 16),
                              query_vector=qvec, embedding_model=model)
            top_ids = [h["source_id"] for h in hits][:top_k]
            if d["source_id"] in top_ids:
                answerable += 1
            else:
                not_answerable.append(d["source_id"])
    finally:
        idx.close()
        holder.cleanup()

    return Observation(extras={
        "documents_in_batch": len(docs),
        "answerable_count": answerable,
        "not_answerable": not_answerable,
        "probe_invocations": probe_invocations,
        "pipeline_stage_count": len(stages),
        "top_k": top_k,
    })


def _intake_assert(obs) -> Verdict:
    x = obs.extras
    if x["documents_in_batch"] < 1:
        return Verdict(False, "connector corpus is empty -- nothing to intake")
    # Every doc must be answerable-from after intake (the throughput floor): the
    # pipeline moved all docs to answerable in a fixed, counted set of stages.
    if x["not_answerable"]:
        return Verdict(False, (
            f"{len(x['not_answerable'])}/{x['documents_in_batch']} docs not "
            f"answerable-from after intake (source_id absent from top-{x['top_k']} "
            f"for its probe): {x['not_answerable']}"))
    return Verdict(True, (
        f"intake throughput (counts only): {x['answerable_count']}/"
        f"{x['documents_in_batch']} documents answerable-from in "
        f"{x['pipeline_stage_count']} pipeline stages, "
        f"{x['probe_invocations']} probe invocation(s); no wall-clock "
        f"(seconds are class 3)"))


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def scenarios() -> list[Scenario]:
    return [
        Scenario(
            id="EVAL-USEFUL-001",
            dimension="usefulness",
            guarantee=None,
            setup=_gold_setup,
            run=_gold_run,
            assert_outcome=_gold_assert,
            fault_point=None,
        ),
        Scenario(
            id="EVAL-USEFUL-002",
            dimension="usefulness",
            guarantee=None,
            setup=_intake_setup,
            run=_intake_run,
            assert_outcome=_intake_assert,
            fault_point=None,
        ),
    ]
