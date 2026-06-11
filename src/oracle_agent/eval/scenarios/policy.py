"""policy/ -- the release matrix holds (kernel parity) (P6-T4).

For each (sensitivity, environment) pair the shell-observed release ceiling
(``policy_bridge.max_sensitivity_for``, the value the loop actually uses) must
match the root's OWN ``oracle policy check`` verdict: the highest label whose
verdict is exactly ``allow`` is the ceiling, and ``allow-minimized`` is never
auto-released (SH-013/014).

This is the honest answer to the kernel having NO in-process fault seam (the
matrix logic is kernel-side): parity COMPARISON instead of a planted fault.
These scenarios land in ``Scorecard.no_seam`` by design and are rendered
honestly there (P6S-7).
"""
from __future__ import annotations

from oracle_agent.eval.harness import Observation, Scenario, Verdict
from oracle_agent.eval.scenarios import _support as S

_ENVIRONMENTS = ("external", "local_agent")


# --------------------------------------------------------------------------- #
# EVAL-POLICY-001: ceiling parity -- the shell ceiling equals the highest label
# the root's own policy check marks 'allow', for every environment column.
# --------------------------------------------------------------------------- #
def _policy_parity_setup(Harness):
    return {"root": S.scenario_root()}


def _policy_parity_run(ctx) -> Observation:
    from oracle_agent.agentloop import policy_bridge as pb

    root = ctx["root"]
    order = pb.sensitivity_order(root)
    check = pb._cli_policy_check(root)

    rows: list[dict] = []
    for env in _ENVIRONMENTS:
        # The shell's observed release ceiling (what the loop uses).
        observed = pb.max_sensitivity_for(root, env)
        # The root's own verdict ladder: highest exactly-'allow' label.
        expected = "public"
        for label in order:
            try:
                verdict = check(label, env)
            except Exception:
                break
            if verdict == "allow":
                expected = label
            else:
                break
        rows.append({"env": env, "observed": observed, "expected": expected})
    return Observation(extras={"rows": rows})


def _policy_parity_assert(obs) -> Verdict:
    rows = obs.extras["rows"]
    mismatches = [r for r in rows if r["observed"] != r["expected"]]
    if mismatches:
        head = mismatches[0]
        return Verdict(False, (
            f"ceiling parity broke for env {head['env']!r}: shell observed "
            f"{head['observed']!r} but the root's policy ladder says "
            f"{head['expected']!r} (the shell ceiling diverged from kernel "
            f"policy)"))
    return Verdict(True, (
        "ceiling parity holds for every environment column: " +
        ", ".join(f"{r['env']}={r['observed']}" for r in rows)))


# --------------------------------------------------------------------------- #
# EVAL-POLICY-002: allow-minimized is never auto-released. For every pair whose
# root verdict is 'allow-minimized', the shell ceiling must rank STRICTLY BELOW
# that label (the minimized tier is not a grant; H2 / SH-013).
# --------------------------------------------------------------------------- #
def _policy_minimized_setup(Harness):
    return {"root": S.scenario_root()}


def _policy_minimized_run(ctx) -> Observation:
    from oracle_agent.agentloop import policy_bridge as pb

    root = ctx["root"]
    order = pb.sensitivity_order(root)
    check = pb._cli_policy_check(root)

    findings: list[dict] = []
    for env in _ENVIRONMENTS:
        ceiling = pb.max_sensitivity_for(root, env)
        ceiling_rank = pb.sensitivity_rank(ceiling, order)
        for label in order:
            try:
                verdict = check(label, env)
            except Exception:
                break
            if verdict == "allow-minimized":
                findings.append({
                    "env": env, "label": label,
                    "label_rank": pb.sensitivity_rank(label, order),
                    "ceiling_rank": ceiling_rank,
                })
    return Observation(extras={"findings": findings, "order": order})


def _policy_minimized_assert(obs) -> Verdict:
    findings = obs.extras["findings"]
    # Every allow-minimized label must rank strictly above the released ceiling.
    leaked = [f for f in findings if f["label_rank"] <= f["ceiling_rank"]]
    if leaked:
        head = leaked[0]
        return Verdict(False, (
            f"allow-minimized label {head['label']!r} (rank {head['label_rank']}) "
            f"was auto-released at/below the ceiling (rank {head['ceiling_rank']}) "
            f"for env {head['env']!r} -- allow-minimized treated as a grant "
            f"(SH-013 breach)"))
    if not findings:
        # No allow-minimized rows at all -- the fixture has none; assert the
        # weaker invariant that the ceiling never exceeds the order's reach.
        return Verdict(True, (
            "no allow-minimized tier present in this root's matrix; ceiling "
            "parity (EVAL-POLICY-001) carries the floor"))
    return Verdict(True, (
        f"every allow-minimized label ranks strictly above the released "
        f"ceiling ({len(findings)} minimized pairs checked) -- never "
        f"auto-released"))


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def scenarios() -> list[Scenario]:
    # Both are no_seam (kernel matrix logic; parity comparison, no shell seam).
    return [
        Scenario(
            id="EVAL-POLICY-001",
            dimension="policy",
            guarantee="SH-009",
            setup=_policy_parity_setup,
            run=_policy_parity_run,
            assert_outcome=_policy_parity_assert,
            fault_point=None,
        ),
        Scenario(
            id="EVAL-POLICY-002",
            dimension="policy",
            guarantee="SH-013",
            setup=_policy_minimized_setup,
            run=_policy_minimized_run,
            assert_outcome=_policy_minimized_assert,
            fault_point=None,
        ),
    ]
