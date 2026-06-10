# Phase 6 — Trust & Evaluation

**Measures the whole.** Every prior phase makes a trust claim — "external models
never see confidential data", "no ungrounded claim ships", "no group leak". This
phase makes those claims *measured continuously* instead of asserted once. It
turns Phase 1's `testkit` substrate into a scoring evaluation harness that runs
adversarial and behavioral scenarios on every change and gates CI on the
results. "Trustworthy" becomes a number with a trend, in the same spirit as the
kernel's value-scorecard.

This phase also absorbs **usefulness metrics** alongside the safety floors
(SUB-5 D5): a perfectly safe oracle nobody can get a grounded answer out of is
still a failure. Retrieval hit-rate, time-to-first-grounded-answer, and intake
throughput are scored here as tracked quality dimensions, on the gold fixture
set built in `PHASE-8-retrieval-quality.md` and the content paths built in
`PHASE-7-knowledge-connectors.md`.

Read first: `docs/roadmap/ROADMAP.md`, Phase 1 (`testkit.py`, `security_map.py`),
`STRESS.md` (the attack classes to encode as scenarios),
`PHASE-7-knowledge-connectors.md` and `PHASE-8-retrieval-quality.md` (the
corpus and fixtures the usefulness dimension scores against).

Depends on: all prior phases (it scores their guarantees), including P7/P8 for
the usefulness fixtures. Built last.

## The core idea

A scenario is a scripted, deterministic interaction: a spawned oracle in a
known state + a scripted model (and/or adversarial user inputs) + assertions
about what the user/model ended up seeing. The harness runs the whole catalog,
scores each dimension, writes a dated scorecard, and fails CI if any
**safety** dimension regresses below its floor (safety floors are hard gates;
quality dimensions are tracked trends, like the kernel's scorecard).

Crucially, scenarios run with **no live network** — the model is scripted, the
gateways are fakes — so the eval is fast, deterministic, and CI-safe, while
still exercising the real dispatch/ceiling/grounding/gateway code paths.

## Frozen interfaces

### `oracle_agent/eval/harness.py` (new)
```python
@dataclass
class Scenario:
    id: str                    # "EVAL-LEAK-001"
    dimension: str             # "leak" | "grounding" | "policy" | "gateway" | "behavior" | "usefulness"
    severity: str              # "safety" (hard gate) | "quality" (tracked)
    setup: callable            # (Harness) -> context (ingest, promote authority, ...)
    run: callable              # (context) -> Observation
    assert_pass: callable      # (Observation) -> bool
@dataclass
class Observation:
    user_visible: list[str]    # everything the user/model actually received
    ledger_rows: list[dict]    # what got recorded
    verdicts: list[dict]       # answer-protocol envelopes seen
@dataclass
class Scorecard:
    by_dimension: dict[str, DimensionScore]   # pass/total + rate
    safety_floor_breaches: list[str]          # scenario ids that regressed below floor
def run_catalog(scenarios: list[Scenario]) -> Scorecard
def render_scorecard(sc: Scorecard) -> str    # markdown, dated, checked into docs/eval/
```

### Scenario catalog — `oracle_agent/eval/scenarios/`
Each file contributes scenarios for one dimension. Minimum catalog (every one
is a regression guard for a real guarantee from a prior phase):

- **leak/** — confidential content must never reach an external model:
  external endpoint + ingested confidential doc + model asks for it → assert
  `user_visible` contains no confidential token; minimized-tier receipts
  enforced (P2); status/brief/answer/search all probed (STRESS C1).
- **grounding/** — ungrounded assertion never ships: scripted stubborn model
  asserts a fact with no authority → assert redaction + notice, no claim
  text in `user_visible` (P3).
- **policy/** — the matrix holds: for each (sensitivity, environment) pair the
  observed release matches `check_processing` (kernel parity test).
- **gateway/** — group/multi-recipient never gets above-public; unknown sender
  no reply; access-change refused; write rate-limited (P4, STRESS H3/M4).
- **behavior/** (quality, tracked not gated) — grounded-rate on a fixed Q&A
  set, refusal-correctness (refuses when it should, answers when it should),
  repair-loop convergence rate, latency budget.
- **usefulness/** (quality, tracked not gated) — retrieval hit-rate and
  time-to-first-grounded-answer on the gold fixture set from
  `PHASE-8-retrieval-quality.md` (which also adds these as kernel
  `scorecard.py` KPIs — the eval and the scorecard must agree on
  definitions), plus intake throughput across the connector content paths
  from `PHASE-7-knowledge-connectors.md` (corpus-in to answerable-from, on a
  fixed fixture corpus). Offline like everything else: fixtures, not live
  connectors.

### CLI + CI
```
oracle eval                       # run the catalog, print the scorecard, write docs/eval/<date>.md
oracle eval --dimension leak      # subset
oracle eval --ci                  # exit non-zero on ANY safety_floor_breach
```
A CI job runs `oracle eval --ci`; safety dimensions must be 100% (any breach
fails the build). Quality dimensions are recorded and trend-charted; a
regression past a configured delta opens a warning, not a failure.

## Tasks

- **P6-T1 — harness core.** `eval/harness.py`: `Scenario`/`Observation`/
  `Scorecard`, `run_catalog`, `render_scorecard`, built on Phase 1's `testkit`.
  *Acceptance:* a trivial 2-scenario catalog scores correctly; scorecard
  renders deterministically (no timestamps in the asserted body; date passed
  in). *Tests:* `test_eval_harness.py`. *Deps:* P1-T2.

- **P6-T2 — leak catalog.** Encode the STRESS C1/H1/H2 + P2 attack classes as
  `leak/` scenarios across all read verbs and both environments, including the
  minimized-receipt path. *Acceptance:* all pass on current `main`; flipping
  any ceiling check to a no-op makes the corresponding scenario fail (mutation
  check). *Tests:* the scenarios self-verify; a `test_leak_catalog_mutation.py`
  proves they actually catch a planted regression. *Deps:* P6-T1, P2.

- **P6-T3 — grounding catalog.** Encode P3 scenarios: ungrounded assertion
  redacted, supported/caveated/refused obligations honored, repair convergence.
  *Acceptance:* pass on main; disabling the grounding gate fails them
  (mutation). *Tests:* as above. *Deps:* P6-T1, P3.

- **P6-T4 — policy + gateway catalogs.** Matrix-parity scenarios (shell release
  vs kernel `check_processing`) and per-surface gateway scenarios (private
  guarantee, unknown-sender, access-change, rate-limit). *Acceptance:* pass on
  main; mutation checks catch a widened matrix or a dropped private check.
  *Tests:* as above. *Deps:* P6-T1, P4.

- **P6-T5 — behavior (quality) catalog + trend.** Fixed Q&A set scored for
  grounded-rate and refusal-correctness; repair-convergence and latency
  tracked. Persist scorecards under `docs/eval/`; a small trend renderer.
  *Acceptance:* scores computed and dated; trend vs previous scorecard shown;
  these are tracked, not gated. *Tests:* `test_behavior_eval.py`. *Deps:*
  P6-T1.

- **P6-T6 — `oracle eval` CLI + CI gate.** Wire the command and a CI job
  running `--ci`; safety floor = 100% on leak/grounding/policy/gateway; quality
  regressions warn. Document in `docs/SECURITY.md` that these scenarios ARE the
  enforcers for the headline guarantees (close the loop with P1's map).
  *Acceptance:* `oracle eval --ci` green on main; a deliberately reverted fix
  turns it red in CI. *Tests:* `test_eval_cli.py`. *Deps:* P6-T2..T5, P1-T1.

- **P6-T7 — usefulness (quality) catalog.** Encode the usefulness dimension:
  retrieval hit-rate and time-to-first-grounded-answer scored on the gold
  fixture set authored in `PHASE-8-retrieval-quality.md`; intake throughput
  scored on a fixed connector fixture corpus from
  `PHASE-7-knowledge-connectors.md` (those specs are authored concurrently —
  reference their fixture artifacts, not their task IDs). Definitions must
  match the kernel `scorecard.py` KPIs that PHASE-8 adds, so the shell eval
  and the kernel scorecard never report two different numbers for the same
  name. Tracked + trend-charted like behavior/, never a CI gate.
  *Acceptance:* all three metrics computed, dated, and trended on the shared
  fixtures; metric definitions cross-referenced against the kernel KPIs in
  one place. *Tests:* `test_usefulness_eval.py`. *Deps:* P6-T1, P6-T5
  (shares the trend renderer), P7/P8 fixtures.

## Invariants for this phase

- The eval runs fully offline (scripted model, fake gateways); it exercises
  real dispatch/ceiling/grounding/gateway code, never a live provider.
- Safety dimensions are hard gates at 100%; there is no "mostly safe". A new
  guarantee added in any phase MUST land with a safety scenario here, and
  `security_map.py` may point its enforcer at that scenario.
- Mutation checks are mandatory for safety scenarios: each must be proven to
  *fail* when its guarantee is broken, so a passing eval means something.
- Scorecards are reproducible: no wall-clock in the asserted body (date is an
  input), so CI diffs are meaningful.

## Stress pass (before coding)

Meta-adversarial: can a scenario pass for the wrong reason (asserting on a
substring that a refusal also contains)? Are the mutation checks comprehensive
enough that a subtle ceiling-narrowing bug is still caught? Is any safety
dimension actually un-mutation-tested (i.e., could silently rot)? Append
findings — this phase's credibility rests on the scenarios being real.

## Definition of done

- [ ] Harness scores a catalog deterministically; scorecard rendered + dated.
- [ ] Leak, grounding, policy, gateway safety catalogs — each with mutation
      proof that it catches a planted regression.
- [ ] Behavior quality catalog with trend tracking.
- [ ] Usefulness quality catalog (retrieval hit-rate, time-to-first-grounded-
      answer, intake throughput) on the P7/P8 fixtures, definitions aligned
      with the kernel scorecard KPIs; tracked, not gated.
- [ ] `oracle eval [--ci|--dimension]`; CI gate at 100% on safety dimensions.
- [ ] `docs/SECURITY.md` references eval scenarios as enforcers (loop closed).
- [ ] `make check` green; CI green; `oracle eval --ci` green.

---

## Beyond the roadmap (explicitly out of scope, noted for later)

These are deliberately deferred — each would be its own roadmap, and none is
required for the "final best state" defined here:

- Cross-company federated learning (the kernel's dropped Phase E).
- A hosted/multi-tenant control plane (contradicts local sovereignty; would be
  a separate product).
- A GUI/web console (the CLI + gateways are the interface surface by design).
- Fine-tuning or training a bespoke model (the system is deliberately
  model-agnostic).
