"""tests/shell/test_eval_catalog.py -- the parametrized catalog gate (P6-T6/P6S-4).

Every safety scenario is ALSO a collected pytest node here, so `make check`
runs the whole catalog with ZERO new CI machinery (P6S-5) and
`security_map.Guarantee.enforcer` can name a node
(`tests/shell/test_eval_catalog.py::test_scenario[EVAL-LEAK-001]`) under the
existing `verify_enforcers` contract.

The catalog wall-clock budget is also asserted here (<= 60 s added to the suite,
P6S-5). The template root is spawned once per process and copied per scenario
(P6-T1 isolation), so the per-scenario cost is a copytree, not a spawn.

Stdlib + pytest. The eval package is the sanctioned testkit-importing exception
(P6S-12).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from oracle_agent.eval import all_scenarios, run_scenario  # noqa: E402
from oracle_agent.eval.scenarios import _support  # noqa: E402

_SCENARIOS = all_scenarios()
_SCENARIO_BY_ID = {s.id: s for s in _SCENARIOS}
_IDS = [s.id for s in _SCENARIOS]


@pytest.fixture(scope="module", autouse=True)
def _eval_template_lifecycle():
    """Drop the once-spawned template + per-scenario copies after the module."""
    yield
    _support.reset_template_cache()


@pytest.mark.parametrize("scenario_id", _IDS)
def test_scenario(scenario_id):
    """One safety/quality scenario, run as a collected pytest node.

    A safety scenario that fails turns the suite (= the CI gate) red. Quality
    scenarios are also collected here so the catalog is exercised on every cell,
    but the eval CLI's --ci gate only fails on SAFETY breaches; a failing
    quality node here would still surface as a normal test failure (the catalog
    is run, not silently skipped).
    """
    scenario = _SCENARIO_BY_ID[scenario_id]
    verdict = run_scenario(scenario)
    assert verdict.passed, (
        f"scenario {scenario_id} ({scenario.dimension}/{scenario.severity}) "
        f"failed: {verdict.evidence}"
    )


def test_catalog_within_wall_clock_budget():
    """The whole catalog adds <= 60 s to the suite (pinned budget, P6S-5).

    Wall-clock here is a TEST-SUITE budget assertion, NOT a scored metric -- it
    never enters the scorecard (the scorecard carries no wall-clock, P6S-1).
    """
    _support.reset_template_cache()
    start = time.monotonic()
    for scenario in all_scenarios():
        run_scenario(scenario)
    elapsed = time.monotonic() - start
    _support.reset_template_cache()
    assert elapsed <= 60.0, (
        f"the eval catalog took {elapsed:.1f}s, over the 60s budget (P6S-5)"
    )


def test_every_safety_scenario_has_fault_point_or_is_no_seam():
    """Every safety scenario declares a fault_point OR is honestly no_seam.

    The planted-fault discipline is mandatory for safety scenarios WITH a seam;
    a None fault_point is allowed only for kernel/structural logic and is
    rendered in Scorecard.no_seam (never hidden, P6S-7).
    """
    from oracle_agent.eval.harness import SEVERITY_BY_DIMENSION

    for scenario in all_scenarios():
        if SEVERITY_BY_DIMENSION.get(scenario.dimension) != "safety":
            continue
        # fault_point is either a dotted path (seam) or None (no_seam). Both are
        # valid; the meta-tests (test_eval_faults.py) prove the seam ones flip.
        assert scenario.fault_point is None or isinstance(scenario.fault_point, str)


def test_every_scenario_names_a_guarantee():
    """Net-new-only rule (P6S-9): every scenario names its SH-xxx guarantee.

    A None guarantee is allowed ONLY while the matching NEW Guarantee lands in
    security_map.GUARANTEES in the same change-set; this repo lands them, so
    every scenario must name one.
    """
    unnamed = [s.id for s in all_scenarios() if not s.guarantee]
    assert not unnamed, f"scenarios with no guarantee: {unnamed}"
