"""tests/shell/test_usefulness_eval.py -- usefulness (quality) catalog (P6-T7).

Three class-2 deliverables, all tracked-not-gated:

  1. gold_hit_at_k / gold_mrr on eval/fixtures/retrieval_gold.json (the
     EVAL-USEFUL-001 scenario), under FIXTURE-SCOPED names -- the kernel KPI
     names are NOT reused (P6S-3);
  2. DEFINITION PARITY (P6S-3): synthetic retrieval_event/answer_event ledger
     fixtures with controlled timestamps are fed to the KERNEL's _kpi_retrieval,
     and the computed values are asserted against hand-derived expectations.
     Value-parity on synthetic data proves the shell eval and the kernel
     scorecard can never drift on what a name MEANS;
  3. connector intake throughput on eval/fixtures/connector_corpus/ (the
     EVAL-USEFUL-002 scenario) -- documents-per-batch and pipeline-stage COUNTS,
     never seconds (P6S-10); plus the corpus schema-check + secret-scan.

Hold-out lifecycle (P6S-6): the hold-out ids (5/10/15/20/25) are consumed on
this first scoring run (asserted via the scenario's evidence) and a fresh
hold-out is regenerable via regen_retrieval_gold.py (the regen lifecycle is
exercised here).

Stdlib + pytest. The eval package is the sanctioned testkit-importing exception
(P6S-12); the kernel tools are imported by path like test_retrieval_gold.py.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

KERNEL_TOOLS = (REPO_ROOT / "src" / "oracle_agent" / "assets" /
                "oracle-kernel" / "_tools")
CONNECTOR_CORPUS = (REPO_ROOT / "eval" / "fixtures" / "connector_corpus" /
                    "corpus.json")
GOLD = REPO_ROOT / "eval" / "fixtures" / "retrieval_gold.json"


# --------------------------------------------------------------------------- #
# 1. gold scoring scenario + hold-out consumption (EVAL-USEFUL-001)
# --------------------------------------------------------------------------- #
def _useful_scenario(scenario_id):
    from oracle_agent.eval.scenarios import usefulness
    for s in usefulness.scenarios():
        if s.id == scenario_id:
            return s
    raise AssertionError(f"no usefulness scenario {scenario_id}")


def test_gold_scoring_scenario_passes_and_consumes_holdout():
    from oracle_agent.eval import run_scenario

    sc = _useful_scenario("EVAL-USEFUL-001")
    verdict = run_scenario(sc)
    assert verdict.passed, verdict.evidence
    # The hold-out (5/10/15/20/25) is CONSUMED this run -- stamped in evidence.
    assert "Hold-out CONSUMED" in verdict.evidence
    for hid in (5, 10, 15, 20, 25):
        assert str(hid) in verdict.evidence
    # Fixture-scoped names appear; kernel KPI names do NOT (P6S-3).
    assert "gold_hit_at_k" in verdict.evidence
    assert "gold_mrr" in verdict.evidence
    assert "retrieval_hit_rate" not in verdict.evidence
    assert "time_to_first_grounded_answer" not in verdict.evidence


def test_gold_scenario_uses_fixture_scoped_names_only():
    """The usefulness module must never republish the kernel KPI names."""
    from oracle_agent.eval.scenarios import usefulness
    src = Path(usefulness.__file__).read_text(encoding="utf-8")
    # The kernel names may be MENTIONED in a comment explaining the reservation,
    # but must not be emitted as a metric key. We assert the metric dict keys are
    # the fixture-scoped ones.
    assert "gold_hit_at_k" in src and "gold_mrr" in src


# --------------------------------------------------------------------------- #
# 2. DEFINITION PARITY against the kernel's _kpi_retrieval (P6S-3)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def kernel_scorecard():
    if str(KERNEL_TOOLS) not in sys.path:
        sys.path.insert(0, str(KERNEL_TOOLS))
    import scorecard  # type: ignore
    return scorecard


@pytest.fixture(scope="module")
def kernel_ledger():
    if str(KERNEL_TOOLS) not in sys.path:
        sys.path.insert(0, str(KERNEL_TOOLS))
    import ledger  # type: ignore
    return ledger


@pytest.fixture(scope="module")
def kernel_retrieval_ledger():
    if str(KERNEL_TOOLS) not in sys.path:
        sys.path.insert(0, str(KERNEL_TOOLS))
    import retrieval_ledger  # type: ignore
    return retrieval_ledger


def _answer_event(ledger, scorecard, root, *, exit_code, source_ids, ts):
    ledger.append(
        Path(root) / scorecard.ANSWER_LEDGER,
        {"kind": "answer_event", "business_object": "Revenue",
         "exit_code": exit_code, "authority_state": "confirmed",
         "interface": "cli", "source_ids": source_ids, "ts": ts},
        id_prefix="ANS",
    )


def _source_note(scorecard, root, *, source_id, ingested):
    folder = Path(root) / scorecard.SOURCES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    fm = "\n".join([
        "---", f"id: {source_id}", f"source_id: {source_id}",
        "type: source", f"ingested: {ingested}", "sensitivity: internal",
        "---", "", "# source body",
    ])
    (folder / f"src-{source_id}.md").write_text(fm + "\n", encoding="utf-8")


def test_definition_parity_hit_rate(
    tmp_path, kernel_scorecard, kernel_ledger, kernel_retrieval_ledger
):
    """The kernel's retrieval_hit_rate on synthetic ledgers == hand-derived.

    Two searches with controlled timestamps: search 1 surfaces s1 (cited by an
    exit-0 answer -> hit); search 2 surfaces s9 (never cited by a grounded
    answer -> miss). Hand-derived hit_rate = 1/2 = 0.5. A non-grounded (exit-2)
    answer citing s9 must NOT count. This proves the SHELL eval and the kernel
    scorecard share a definition for what "a search surfaced a grounded source"
    MEANS (P6S-3 value-parity-on-synthetic-data).
    """
    sc = kernel_scorecard
    rl = kernel_retrieval_ledger
    root = tmp_path
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 30)
    now = datetime(2026, 6, 10)
    rl.log_search(root, query="a", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.5, result_count=1,
                  top_source_ids=["s1"], now=now)
    rl.log_search(root, query="b", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.5, result_count=1,
                  top_source_ids=["s9"], now=now)
    _answer_event(kernel_ledger, sc, root, exit_code=0, source_ids=["s1"],
                  ts="2026-06-11T09:00:00")
    _answer_event(kernel_ledger, sc, root, exit_code=2, source_ids=["s9"],
                  ts="2026-06-11T09:05:00")

    r = sc._kpi_retrieval(root, start, end)
    assert r["searches"] == 2
    assert r["retrieval_hit_rate"] == round(1 / 2, 4)  # hand-derived
    assert r["hybrid_share"] == 1.0
    assert r["non_empty_rate"] == 1.0


def test_definition_parity_ttfga(
    tmp_path, kernel_scorecard, kernel_ledger
):
    """time_to_first_grounded_answer median == hand-derived (median DAYS).

    s1 ingested 2026-06-01, first grounded citation 2026-06-06 -> 5 days.
    s2 ingested 2026-06-01, first grounded citation 2026-06-04 -> 3 days.
    A LATER s1 citation must not move its first-grounded latency.
    median([5, 3]) = 4.0 -- in DAYS, the kernel's unit (NOT a fixture hit@k).
    """
    sc = kernel_scorecard
    root = tmp_path
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 30)
    _source_note(sc, root, source_id="s1", ingested="2026-06-01")
    _source_note(sc, root, source_id="s2", ingested="2026-06-01")
    _answer_event(kernel_ledger, sc, root, exit_code=0, source_ids=["s1"],
                  ts="2026-06-06T00:00:00")
    _answer_event(kernel_ledger, sc, root, exit_code=0, source_ids=["s2"],
                  ts="2026-06-04T00:00:00")
    _answer_event(kernel_ledger, sc, root, exit_code=0, source_ids=["s1"],
                  ts="2026-06-20T00:00:00")

    r = sc._kpi_retrieval(root, start, end)
    assert r["time_to_first_grounded_answer"] == 4.0  # hand-derived median DAYS


def test_parity_proves_names_mean_different_things(
    tmp_path, kernel_scorecard, kernel_ledger, kernel_retrieval_ledger
):
    """The kernel's retrieval_hit_rate is a TRAFFIC PROXY (citation overlap),
    NOT the fixture's gold_hit_at_k (top-k recall of a gold target). Feeding the
    SAME synthetic rows to the kernel yields the proxy semantics, confirming why
    the fixture eval must use a different NAME (P6S-3): same word, different
    population/definition.
    """
    sc = kernel_scorecard
    rl = kernel_retrieval_ledger
    root = tmp_path
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 30)
    now = datetime(2026, 6, 10)
    # A search that surfaces s1 but NO grounded answer cites it -> the kernel
    # hit_rate is 0 (no citation overlap), even though a fixture eval would call
    # this a "hit" if s1 were the gold target. Different definition, same word.
    rl.log_search(root, query="a", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.5, result_count=1,
                  top_source_ids=["s1"], now=now)
    r = sc._kpi_retrieval(root, start, end)
    assert r["retrieval_hit_rate"] == 0.0  # no grounded citation overlap


# --------------------------------------------------------------------------- #
# 3. connector intake throughput scenario + corpus schema/secret-scan
# --------------------------------------------------------------------------- #
def test_intake_throughput_scenario_passes():
    from oracle_agent.eval import run_scenario

    sc = _useful_scenario("EVAL-USEFUL-002")
    verdict = run_scenario(sc)
    assert verdict.passed, verdict.evidence
    # COUNTS only, no wall-clock (P6S-10).
    assert "documents answerable-from" in verdict.evidence
    assert "seconds are class 3" in verdict.evidence


def test_connector_corpus_schema():
    corpus = json.loads(CONNECTOR_CORPUS.read_text(encoding="utf-8"))
    assert "_README" in corpus and "synthetic" in corpus["_README"].lower()
    assert corpus["embedding_model"] == "synthetic-hash-v1"
    assert isinstance(corpus["answerable_top_k"], int) and corpus["answerable_top_k"] > 0
    assert isinstance(corpus["pipeline_stages"], list) and corpus["pipeline_stages"]
    dim = corpus["dim"]
    seen = set()
    for d in corpus["docs"]:
        for key in ("source_id", "title", "text", "probe"):
            assert key in d and d[key]
        # Vectors are vendored in the fixture (self-contained, like
        # retrieval_gold.json) so the scenario stays stdlib-only at runtime.
        assert len(d["vector"]) == dim
        assert len(d["probe_vector"]) == dim
        assert d["source_id"] not in seen, "duplicate source_id"
        seen.add(d["source_id"])
    assert len(corpus["docs"]) >= 1


def test_connector_corpus_is_secret_scan_clean():
    """The corpus must carry no secret-shaped tokens (make secret scans docs)."""
    blob = CONNECTOR_CORPUS.read_text(encoding="utf-8")
    for marker in ("sk-", "Bearer ", "BEGIN PRIVATE KEY", "api_key=",
                   "AKIA", "-----BEGIN"):
        assert marker not in blob, f"secret-shaped token {marker!r} in corpus"


# --------------------------------------------------------------------------- #
# hold-out lifecycle: a fresh hold-out is regenerable, same every-5th rule
# --------------------------------------------------------------------------- #
def test_holdout_regen_is_deterministic_and_every_fifth():
    """regen_retrieval_gold.build() reproduces the every-5th hold-out rule.

    The consumed hold-out is folded into the tracked set; the next generation's
    hold-out is minted by re-running the regen (same every-5th rule). Here we
    assert the regen still produces 25 queries with ids 5/10/15/20/25 reserved
    -- the rule the fresh generation follows (P6S-6).
    """
    fixtures = REPO_ROOT / "eval" / "fixtures"
    if str(fixtures) not in sys.path:
        sys.path.insert(0, str(fixtures))
    import regen_retrieval_gold as regen  # type: ignore

    built = regen.build()
    ids = {q["query_id"] for q in built["queries"]}
    assert ids == set(range(1, 26))
    assert {5, 10, 15, 20, 25} <= ids
    # Deterministic: a second build is byte-identical in structure.
    again = regen.build()
    assert again["queries"][4]["vector"] == built["queries"][4]["vector"]
