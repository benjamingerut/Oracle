"""tests/shell/test_eval_harness.py -- harness core acceptance (P6-T1).

Acceptance:
  * a trivial 2-scenario catalog scores correctly;
  * the scorecard renders BYTE-IDENTICAL across two consecutive runs (date
    passed in; no wall-clock in the body; pinned tie-breaks + round(.,4));
  * derived severity -- run_catalog rejects a scenario carrying an explicit
    severity (P6S-13);
  * per-scenario root-copy isolation -- two fresh_root copies are independent;
  * DimensionScore / Verdict / Observation shapes hold.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from oracle_agent.eval.harness import (  # noqa: E402
    DimensionScore,
    Observation,
    Scenario,
    Scorecard,
    SEVERITY_BY_DIMENSION,
    Verdict,
    fresh_root,
    render_scorecard,
    run_catalog,
)


def _pass_scenario(sid, dimension="leak", guarantee="SH-001"):
    return Scenario(
        id=sid, dimension=dimension, guarantee=guarantee,
        setup=lambda H: {},
        run=lambda ctx: Observation(user_visible=["ok"]),
        assert_outcome=lambda obs: Verdict(True, "clean"),
        fault_point=None,
    )


def _fail_scenario(sid, dimension="leak", guarantee="SH-001"):
    return Scenario(
        id=sid, dimension=dimension, guarantee=guarantee,
        setup=lambda H: {},
        run=lambda ctx: Observation(),
        assert_outcome=lambda obs: Verdict(False, "broke"),
        fault_point=None,
    )


# --------------------------------------------------------------------------- #
# trivial 2-scenario catalog scores correctly
# --------------------------------------------------------------------------- #
def test_trivial_catalog_scores():
    sc = run_catalog([_pass_scenario("EVAL-A"), _pass_scenario("EVAL-B")])
    assert isinstance(sc, Scorecard)
    d = sc.by_dimension["leak"]
    assert isinstance(d, DimensionScore)
    assert d.passed == 2 and d.total == 2 and d.rate == 1.0
    assert d.failed_ids == []
    assert sc.safety_floor_breaches == []


def test_failing_safety_scenario_is_a_breach():
    sc = run_catalog([_pass_scenario("EVAL-A"), _fail_scenario("EVAL-B")])
    d = sc.by_dimension["leak"]
    assert d.passed == 1 and d.total == 2
    assert d.rate == 0.5
    assert d.failed_ids == ["EVAL-B"]
    assert sc.safety_floor_breaches == ["EVAL-B"]


def test_failing_quality_scenario_is_not_a_safety_breach():
    sc = run_catalog([
        _pass_scenario("EVAL-A", dimension="behavior", guarantee=None),
        _fail_scenario("EVAL-B", dimension="behavior", guarantee=None),
    ])
    # behavior is quality -> a failure is tracked but NOT a safety breach.
    assert sc.safety_floor_breaches == []
    assert sc.by_dimension["behavior"].failed_ids == ["EVAL-B"]


# --------------------------------------------------------------------------- #
# byte-identical render across two runs
# --------------------------------------------------------------------------- #
def test_scorecard_renders_byte_identical():
    scenarios = [
        _pass_scenario("EVAL-A", "leak"),
        _fail_scenario("EVAL-B", "leak"),
        _pass_scenario("EVAL-C", "policy", guarantee="SH-009"),
    ]
    sc1 = run_catalog(scenarios)
    sc2 = run_catalog(scenarios)
    r1 = render_scorecard(sc1, "2026-06-11")
    r2 = render_scorecard(sc2, "2026-06-11")
    assert r1 == r2, "two consecutive renders are not byte-identical"
    # date is an INPUT -- a different date changes the output.
    assert render_scorecard(sc1, "2026-01-01") != r1


def test_render_carries_no_wall_clock_or_raw_rows():
    # ledger_rows carry wall-clock ts; the renderer must consume only counts.
    obs_with_rows = Observation(
        user_visible=["x"],
        ledger_rows=[{"ts": "2026-06-11T12:00:00Z", "secret": "leak"}])
    scn = Scenario(
        id="EVAL-X", dimension="leak", guarantee="SH-001",
        setup=lambda H: {}, run=lambda ctx: obs_with_rows,
        assert_outcome=lambda obs: Verdict(True, "ok"), fault_point=None)
    sc = run_catalog([scn])
    out = render_scorecard(sc, "2026-06-11")
    assert "2026-06-11T12:00:00Z" not in out
    assert "leak" in out  # the dimension name, fine
    assert "secret" not in out  # the raw row content must never render


def test_no_seam_is_rendered_not_hidden():
    scn = _pass_scenario("EVAL-NS", "leak")  # fault_point=None -> no_seam
    sc = run_catalog([scn])
    assert sc.no_seam == ["EVAL-NS"]
    out = render_scorecard(sc, "2026-06-11")
    assert "No-seam" in out
    assert "EVAL-NS" in out


# --------------------------------------------------------------------------- #
# derived severity (P6S-13)
# --------------------------------------------------------------------------- #
def test_severity_is_derived_from_dimension():
    assert _pass_scenario("E", "leak").severity == "safety"
    assert _pass_scenario("E", "behavior", guarantee=None).severity == "quality"
    assert SEVERITY_BY_DIMENSION["gateway"] == "safety"


def test_severity_property_has_no_setter():
    """Direct assignment of severity is blocked at the type level (P6S-13)."""
    scn = _pass_scenario("EVAL-CHEAT", "leak")
    with pytest.raises(AttributeError):
        scn.severity = "quality"  # type: ignore[attr-defined]


def test_run_catalog_rejects_smuggled_severity():
    """Even a severity smuggled directly into __dict__ is rejected by
    run_catalog -- the derived-severity invariant is enforced, not just typed
    (P6S-13)."""
    scn = _pass_scenario("EVAL-CHEAT", "leak")
    # Bypass the read-only property by writing the instance dict directly.
    scn.__dict__["severity"] = "quality"
    assert "severity" in vars(scn)
    with pytest.raises(ValueError, match="severity"):
        run_catalog([scn])


def test_run_catalog_rejects_unknown_dimension():
    scn = Scenario(
        id="EVAL-BAD", dimension="bogus", guarantee="SH-001",
        setup=lambda H: {}, run=lambda ctx: Observation(),
        assert_outcome=lambda obs: Verdict(True, "x"), fault_point=None)
    with pytest.raises(ValueError, match="unknown dimension"):
        run_catalog([scn])


# --------------------------------------------------------------------------- #
# per-scenario root-copy isolation (P6-T1)
# --------------------------------------------------------------------------- #
def test_fresh_root_copies_are_independent(spawned_root, tmp_path):
    a = fresh_root(spawned_root, tmp_path / "a")
    b = fresh_root(spawned_root, tmp_path / "b")
    assert a != b
    assert (a / "oracle.yml").exists()
    assert (b / "oracle.yml").exists()
    # mutate a; b is untouched (isolation).
    (a / "scratch.txt").write_text("only in a")
    assert (a / "scratch.txt").exists()
    assert not (b / "scratch.txt").exists()
