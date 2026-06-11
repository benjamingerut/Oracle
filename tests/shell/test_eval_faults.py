"""tests/shell/test_eval_faults.py -- planted-fault meta-tests (P6-T2/T3/T4, P6S-7).

Planted-fault checks are the honest name (stdlib-only forbids mutation
frameworks; kernel subprocesses are beyond monkeypatch). For every safety
scenario that DECLARES a fault_point, two properties must hold:

  1. no-op'ing the declared shell seam FLIPS the scenario to fail (the seam is
     load-bearing -- without it the enforcement is gone);
  2. the patched seam is actually ON the scenario's code path -- a call-recording
     wrapper records >= 1 call (patching a DEAD seam would pass for the wrong
     reason; this meta-test catches it).

Scenarios with fault_point=None are kernel/structural logic with no patchable
shell seam; they are covered by parity comparison and rendered in
Scorecard.no_seam -- this module asserts they are honestly enumerated, not that
they flip.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from oracle_agent.eval import all_scenarios, run_catalog, run_scenario  # noqa: E402
from oracle_agent.eval.harness import SEVERITY_BY_DIMENSION  # noqa: E402
from oracle_agent.eval.scenarios import _support  # noqa: E402

_SEAM_SCENARIOS = [s for s in all_scenarios() if s.fault_point is not None]
_NO_SEAM_SAFETY = [
    s for s in all_scenarios()
    if s.fault_point is None
    and SEVERITY_BY_DIMENSION.get(s.dimension) == "safety"
]


@pytest.fixture(autouse=True)
def _clean_template():
    _support.reset_template_cache()
    yield
    _support.reset_template_cache()


@pytest.mark.parametrize("scenario", _SEAM_SCENARIOS,
                         ids=[s.id for s in _SEAM_SCENARIOS])
def test_planted_fault_flips_scenario(scenario):
    """No-op'ing the declared fault_point flips the scenario to FAIL."""
    # Baseline: it passes clean.
    baseline = run_scenario(scenario)
    _support.reset_template_cache()
    assert baseline.passed, (
        f"{scenario.id} must pass clean before the fault test: "
        f"{baseline.evidence}")

    # Faulted: the enforcer is defeated -> the scenario must fail.
    with _support.patched_noop(scenario.fault_point):
        faulted = run_scenario(scenario)
    _support.reset_template_cache()
    assert not faulted.passed, (
        f"{scenario.id}: no-op'ing fault_point {scenario.fault_point!r} did "
        f"NOT flip the scenario to fail -- the seam is not load-bearing, or "
        f"the scenario passes for the wrong reason (P6S-7). Evidence: "
        f"{faulted.evidence}")


@pytest.mark.parametrize("scenario", _SEAM_SCENARIOS,
                         ids=[s.id for s in _SEAM_SCENARIOS])
def test_fault_point_is_on_the_code_path(scenario):
    """The declared fault_point seam is actually CALLED by the scenario.

    A call-recording wrapper records every call to the seam while the scenario
    runs; an empty record means the seam is DEAD (not on the path) and the
    planted-fault proof above would pass for the wrong reason (P6S-7).
    """
    with _support.recording_patch(scenario.fault_point) as calls:
        run_scenario(scenario)
    _support.reset_template_cache()
    assert calls, (
        f"{scenario.id}: declared fault_point {scenario.fault_point!r} was "
        f"NEVER called during the scenario -- it is a DEAD seam, so the "
        f"planted-fault check is meaningless (P6S-7)")


def test_no_seam_safety_scenarios_are_enumerated():
    """Every safety scenario with fault_point=None lands in Scorecard.no_seam.

    The honest no_seam enumeration (P6S-7): a safety scenario with no patchable
    shell seam is covered by parity comparison instead of a planted fault, and
    the scorecard renders it -- never hidden.
    """
    sc = run_catalog(all_scenarios())
    _support.reset_template_cache()
    expected = {s.id for s in _NO_SEAM_SAFETY}
    assert set(sc.no_seam) == expected, (
        f"no_seam enumeration drifted: scorecard={sorted(sc.no_seam)} "
        f"expected={sorted(expected)}")
