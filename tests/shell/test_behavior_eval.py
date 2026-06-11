"""tests/shell/test_behavior_eval.py -- behavior (pipeline-quality) catalog + trend (P6-T5).

Class-2 deterministic pipeline metrics, tracked NOT gated:

  * extractor recall per smuggle class on corpus.json (EVAL-BEHAVIOR-001);
  * repair-loop convergence in COUNTED model round-trips (EVAL-BEHAVIOR-002);
  * pipeline refusal-correctness under scripted envelopes (EVAL-BEHAVIOR-003).

Plus the trend renderer (shared with P6-T7): compare the current scorecard
against the LAST COMMITTED docs/eval/*.md; class-1/2 only; missing baseline =>
"no trend", stated; a quality regression is a WARNING, never a CI failure.

Acceptance pins asserted here:
  * scores computed and dated; tracked, not gated;
  * NO WALL-CLOCK number anywhere in the behavior scorecard / verdicts (P6-T5).

Stdlib + pytest. The eval package is the sanctioned testkit-importing exception
(P6S-12).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from oracle_agent.eval import (  # noqa: E402
    DimensionScore,
    Scorecard,
    last_committed_scorecard,
    parse_scorecard_rates,
    render_scorecard,
    render_trend,
    run_catalog,
    run_scenario,
)
from oracle_agent.eval.scenarios import _support  # noqa: E402


def _behavior_scenario(scenario_id):
    from oracle_agent.eval.scenarios import behavior
    for s in behavior.scenarios():
        if s.id == scenario_id:
            return s
    raise AssertionError(f"no behavior scenario {scenario_id}")


@pytest.fixture(autouse=True)
def _clean_template():
    yield
    _support.reset_template_cache()


# --------------------------------------------------------------------------- #
# the three behavior scenarios
# --------------------------------------------------------------------------- #
def test_extractor_recall_per_class():
    v = run_scenario(_behavior_scenario("EVAL-BEHAVIOR-001"))
    assert v.passed, v.evidence
    assert "recall" in v.evidence


def test_repair_convergence_counted():
    v = run_scenario(_behavior_scenario("EVAL-BEHAVIOR-002"))
    assert v.passed, v.evidence
    # Counts only -- no seconds anywhere in the evidence.
    assert "round-trip" in v.evidence
    assert "no wall-clock" in v.evidence


def test_pipeline_refusal_correctness():
    v = run_scenario(_behavior_scenario("EVAL-BEHAVIOR-003"))
    assert v.passed, v.evidence
    assert "refus" in v.evidence.lower()


def test_behavior_scenarios_are_quality_not_gated():
    from oracle_agent.eval.scenarios import behavior
    from oracle_agent.eval.harness import SEVERITY_BY_DIMENSION
    for s in behavior.scenarios():
        assert s.dimension == "behavior"
        assert SEVERITY_BY_DIMENSION[s.dimension] == "quality"
        # A failing behavior scenario is NOT a safety floor breach.


def test_behavior_failure_is_not_a_safety_breach():
    from oracle_agent.eval import Observation, Scenario, Verdict
    fail = Scenario(
        id="EVAL-BEHAVIOR-FAKE", dimension="behavior", guarantee=None,
        setup=lambda H: {}, run=lambda ctx: Observation(),
        assert_outcome=lambda obs: Verdict(False, "broke"), fault_point=None)
    sc = run_catalog([fail])
    assert sc.safety_floor_breaches == []
    assert sc.by_dimension["behavior"].failed_ids == ["EVAL-BEHAVIOR-FAKE"]


# --------------------------------------------------------------------------- #
# NO WALL-CLOCK in the rendered behavior scorecard (P6-T5 hard pin)
# --------------------------------------------------------------------------- #
def test_behavior_scorecard_has_no_wall_clock():
    from oracle_agent.eval import scenarios_for_dimension
    sc = run_catalog(scenarios_for_dimension("behavior"))
    out = render_scorecard(sc, "2026-06-11")
    # No seconds / ms / wall-clock-shaped tokens in the rendered card.
    for pat in (r"\d+\s*s\b", r"\d+\s*ms\b", r"seconds", r"latency",
                r"\d+:\d\d"):
        assert not re.search(pat, out), f"wall-clock-shaped token matched {pat!r}"


# --------------------------------------------------------------------------- #
# trend renderer (shared P6-T5 / P6-T7)
# --------------------------------------------------------------------------- #
def _card(rates: dict[str, tuple[int, int]]) -> Scorecard:
    by_dim = {
        dim: DimensionScore(passed=p, total=t,
                            rate=round(p / t, 4) if t else 0.0, failed_ids=[])
        for dim, (p, t) in rates.items()
    }
    return Scorecard(by_dimension=by_dim, safety_floor_breaches=[], no_seam=[])


def test_trend_missing_baseline_is_stated(tmp_path):
    sc = _card({"behavior": (3, 3)})
    out = render_trend(sc, last_committed_scorecard(tmp_path / "nope"))
    assert "No committed baseline" in out
    assert "no trend" in out.lower()


def test_trend_parses_committed_rates_and_directions(tmp_path):
    # Write a "committed" baseline scorecard.
    baseline_card = _card({"behavior": (3, 3), "usefulness": (2, 2)})
    docs_eval = tmp_path / "docs" / "eval"
    docs_eval.mkdir(parents=True)
    (docs_eval / "2026-06-10.md").write_text(
        render_scorecard(baseline_card, "2026-06-10"), encoding="utf-8")

    rates = parse_scorecard_rates((docs_eval / "2026-06-10.md").read_text())
    assert rates["behavior"] == 1.0
    assert rates["usefulness"] == 1.0

    # Current run regresses usefulness (a quality dimension).
    current = _card({"behavior": (3, 3), "usefulness": (1, 2)})
    base = last_committed_scorecard(docs_eval)
    assert base is not None and base.name == "2026-06-10.md"
    out = render_trend(current, base)
    assert "regressing" in out
    # A quality regression is a WARNING, never a CI failure.
    assert "WARNING" in out
    assert "NOT a CI failure" in out


def test_trend_picks_most_recent_committed(tmp_path):
    docs_eval = tmp_path / "docs" / "eval"
    docs_eval.mkdir(parents=True)
    for d in ("2026-06-01.md", "2026-06-09.md", "2026-06-10.md"):
        (docs_eval / d).write_text("x", encoding="utf-8")
    # An underscore-prefixed file is ignored.
    (docs_eval / "_index.md").write_text("y", encoding="utf-8")
    assert last_committed_scorecard(docs_eval).name == "2026-06-10.md"


def test_trend_flat_when_equal(tmp_path):
    docs_eval = tmp_path / "docs" / "eval"
    docs_eval.mkdir(parents=True)
    card = _card({"behavior": (3, 3)})
    (docs_eval / "2026-06-10.md").write_text(
        render_scorecard(card, "2026-06-10"), encoding="utf-8")
    out = render_trend(card, last_committed_scorecard(docs_eval))
    assert "flat" in out
    assert "No quality regression" in out
