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
still a failure. The deterministic usefulness metrics are scored on the gold
fixture set Phase 8 shipped (`eval/fixtures/retrieval_gold.json`, with
vendored real vectors and the every-5th-id hold-out frozen by P8S-12) and on a
connector fixture corpus this phase ships.

Read first: `docs/roadmap/ROADMAP.md`, Phase 1 (`src/oracle_agent/testkit.py`,
`src/oracle_agent/security_map.py` — 84 guarantees, `verify_enforcers`),
`STRESS.md` (the attack classes to encode as scenarios), the landed fixture
artifacts: `eval/fixtures/retrieval_gold.json` (+ `regen_retrieval_gold.py`),
`tests/shell/fixtures/grounding/corpus.json` (the labeled smuggle-class
corpus), `tests/shell/test_retrieval_gold.py` (whose eval body is the seed
behavior scenario), the kernel scorecard's `retrieval` KPI section
(`_tools/scorecard.py`, P8-T7), `docs/eval/p2-t0/` (the real-model local-run
protocol and its limits), and `src/oracle_agent/grounding_report.py` (the
real-traffic budget protocol, P3-T7).

Depends on: the **landed guarantee set** — P1 (testkit, security map), P3
(grounding gate + shadow capture), P4 (GatewayCore + four adapters), P7
(connectors), P8 (hybrid retrieval + gold fixtures), and the landed remnant of
P2 (the egress veto; the phase itself is gated NO-GO). **P5 is explicitly NOT
a dependency**: it is open, and when it lands its guarantees land with
scenarios here per the forward invariant below — that invariant carries the
load, not a "built last" ordering claim.

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

### The three metric classes (honesty pin, P6S-1)

A scripted `FakeLLM`'s "grounded-rate" is whatever its author scripts — scoring
it would measure the script, not the system. So every metric in this phase
belongs to exactly one class, and the class determines where it may appear:

1. **CI-gated deterministic safety floors** — fake-driven scenarios asserting
   what reached a sink (model context, embed request, gateway reply, user).
   This is the correct use of a scripted model: the assertion is about the
   *enforcement code*, not model quality. 100% or the build fails.
2. **CI-tracked deterministic pipeline metrics** — quality numbers that are
   honest under fakes because they measure *shell/kernel code* over fixed
   fixtures: extractor recall per smuggle class on `corpus.json`, gold hit@k /
   MRR over the vendored real vectors, repair-loop convergence in **counted
   model round-trips** (never wall-clock). Tracked + trend-charted, never
   gated.
3. **Real-model / real-traffic metrics** — grounded-rate, refusal-correctness,
   latency, and the kernel-named traffic KPIs. These CANNOT be computed
   honestly in hermetic CI. They are measured only via the landed protocols:
   the P2-T0-style local-run harness (`docs/eval/p2-t0/run_eval.py` —
   confinement preflight, model-judge with fail-closed unparseable verdicts,
   **mandatory human spot-check** before any verdict is formal), the P3-T7
   shadow capture (`grounding_report.py`, pinned budgets, human labels), and
   the kernel scorecard on real ledgers. Class-3 numbers **never enter the CI
   scorecard**; humans run and commit them under `docs/eval/`.

Nothing is gated on a metric CI cannot honestly compute.

## Frozen interfaces

### `oracle_agent/eval/harness.py` (new)
```python
@dataclass
class Scenario:
    id: str                    # "EVAL-LEAK-001"
    dimension: str             # "leak" | "grounding" | "policy" | "gateway" | "behavior" | "usefulness"
    guarantee: str | None      # the SH-xxx this scenario enforces, or None ONLY
                               # while the matching NEW Guarantee lands in
                               # security_map.GUARANTEES in the same change-set
    setup: callable            # (Harness) -> context (ingest, promote authority, ...)
    run: callable              # (context) -> Observation
    assert_outcome: callable   # (Observation) -> Verdict (pass/fail + evidence)
    fault_point: str | None    # dotted path of the in-process shell callable
                               # that, no-op'd, MUST flip this scenario to fail
                               # (mandatory for safety scenarios with a seam;
                               # None => the scenario lands in Scorecard.no_seam)

SEVERITY_BY_DIMENSION = {      # DERIVED, frozen — severity is NOT a Scenario
    "leak": "safety",          # field, so a scenario cannot declare itself
    "grounding": "safety",     # out of the gate (P6S-13)
    "policy": "safety",
    "gateway": "safety",
    "behavior": "quality",
    "usefulness": "quality",
}

@dataclass
class Verdict:
    passed: bool
    evidence: str              # WHY — rendered into the scorecard on failure

@dataclass
class Observation:
    user_visible: list[str]    # everything the user/model actually received
    ledger_rows: list[dict]    # for assertions only — NEVER rendered raw
                               # (rows carry wall-clock ts; rendering them
                               # would break scorecard reproducibility)
    verdicts: list[dict]       # answer-protocol envelopes seen

@dataclass
class DimensionScore:
    passed: int
    total: int
    rate: float                # round(passed/total, 4)
    failed_ids: list[str]      # sorted ascending (pinned total order)

@dataclass
class Scorecard:
    by_dimension: dict[str, DimensionScore]
    safety_floor_breaches: list[str]   # scenario ids below the 100% floor
    no_seam: list[str]                 # safety scenarios with no patchable
                                       # fault_point — honest enumeration,
                                       # rendered, never hidden

def run_catalog(scenarios: list[Scenario]) -> Scorecard
def render_scorecard(sc: Scorecard, date: str) -> str   # markdown; date is an
                                                        # INPUT; counts/ids/rates
                                                        # only, fixed precision
```

Reproducibility pins: every ranked or listed output uses a pinned total order
(score desc, then id asc — the P8S-5 dense-rank discipline) and fixed precision
(`round(., 4)`, the kernel scorecard's convention). Two consecutive runs must
render byte-identical scorecards on every CI cell.

**Leak-scenario assertion discipline (P6S-8):** every leak-class scenario (a)
plants a **unique generated marker token** in the sensitive fixture content;
(b) asserts the marker's absence from `user_visible` AND from
`FakeLLM.all_messages` and `FakeEmbedClient.all_texts` (the sink-side scans the
testkit already ships); (c) includes a **reachability control** — the same
scenario re-run with the ceiling raised (or the fault planted) must SHOW the
marker, proving the probe path actually reaches the sink. A substring assertion
without a planted marker and a control is not a leak scenario. Marker grammar
is secret-scan-safe (e.g. `EVALMARK-<8 hex>` — never key-shaped or
bearer-shaped tokens; `make secret` scans `docs/`, SH-055).

**Pytest exposure (P6S-4):** every safety scenario is ALSO a collected pytest
node via one parametrized module —
`tests/shell/test_eval_catalog.py::test_scenario[EVAL-LEAK-001]` — so
`make check` runs the catalog with zero new CI machinery and
`security_map.Guarantee.enforcer` can name the node under the existing
`verify_enforcers` contract, unchanged. **`security_map.GUARANTEES` remains the
sole registry; the eval catalog is enforcer supply, not a registry.**

**Import boundary (P6S-12):** `oracle_agent/eval/` ships in the package and
imports `testkit` — the sanctioned exception testkit's docstring promised.
`test_no_production_module_imports_testkit` is amended to allowlist exactly
`oracle_agent/eval/`, and a NEW converse guard asserts no module outside
`oracle_agent/eval/` + `testkit.py` imports either `testkit` **or**
`oracle_agent.eval` (the harness must not become a backdoor through which
production code reaches testkit). The `oracle eval` CLI handler imports the
harness lazily inside the function (the `grounding-report` pattern), so the
production CLI's import path stays testkit-free.

### Scenario catalog — `oracle_agent/eval/scenarios/`
Each file contributes scenarios for one dimension. **Net-new-only rule
(P6S-9):** every scenario names its guarantee; a scenario that merely
re-asserts an existing SH-xxx enforcer at the same level is rejected in review
— P6 scenarios are **composition-level** (multi-turn, cross-surface, through
the real AgentLoop/GatewayCore) or cover a NEW guarantee. Minimum catalog:

- **leak/** — confidential content must never reach an external sink:
  external endpoint + ingested confidential doc (planted marker) + model asks
  for it → marker absent from every chat message AND every embedding request
  (`FakeEmbedClient` is the embedding sink — leak-via-embedding triggered by
  chat-driven backfill, not unit-invoked); status/brief/answer/search all
  probed (STRESS C1); the landed P2 remnants — the egress veto (SH-058)
  reclassifying a loopback `:cloud` endpoint, and the public external floor
  with `allow-minimized` never auto-released (SH-013/014). **Minimized-tier
  receipt scenarios are a conditional catalog extension keyed to P2
  re-entry** (one of its three named re-entry paths) — there is no minimizer
  on `main`, so they are explicitly NOT part of this phase's definition of
  done (P6S-2).
- **grounding/** — ungrounded assertion never ships: scripted stubborn model
  asserts a fact with no authority → redaction + notice, no claim text in
  `user_visible` (P3); the `corpus.json` smuggle classes (table/list/quote/
  code-block/hedge/non-english/footer-lookalike) replayed END-TO-END through
  the real loop and through the gateway ENFORCE path — composition-level, not
  a copy of the extractor unit tests.
- **policy/** — the matrix holds: for each (sensitivity, environment) pair the
  observed release matches the root's own `oracle policy check` verdict
  (kernel parity). This is the honest answer to the kernel having no
  in-process fault seam: parity comparison instead of planted faults; these
  scenarios land in `Scorecard.no_seam` by design.
- **gateway/** — "gateway" means **GatewayCore + all four adapters**, not
  Telegram alone: group/multi-recipient never above-public; unknown sender no
  reply; access-change refused; write rate-limited (the Telegram-era set,
  composition-level); PLUS the landed P4/P7 surface area — MCP dropped-verb
  and missing/wrong-token probes (SH-078..080), email DMARC-spoof (forged
  `Authentication-Results`, wrong authserv-id — SH-081/082), briefing-target
  allowlist refusal (SH-084), connector-credential containment (SH-064),
  Slack socket-mode via `FakeSlackTransport` (SH-066/067).
- **behavior/** (quality, tracked not gated) — **deterministic pipeline
  metrics only** (class 2): extractor recall per smuggle class on
  `corpus.json`; repair-loop convergence measured in counted model
  round-trips; refusal-correctness of the PIPELINE given scripted verdict
  envelopes (refuses when the envelope says refuse, ships when it says
  grounded). No wall-clock metric appears here — latency is class 3
  (`grounding_report.py` budgets on real traffic; the `test_grounding_perf.py`
  pinned-bound discipline covers the CI side).
- **usefulness/** (quality, tracked not gated) — `gold_hit_at_k` / `gold_mrr`
  on `eval/fixtures/retrieval_gold.json` (fixture-scoped names — see the
  naming pin in P6-T7), plus count-based intake throughput on
  `eval/fixtures/connector_corpus/` (shipped by this phase). Offline like
  everything else: fixtures, not live connectors. The kernel-named traffic
  KPIs (`retrieval_hit_rate`, `time_to_first_grounded_answer` — median DAYS
  from ingest to first exit-0 citation) are class 3 and are reported only by
  the kernel scorecard on real ledgers; this phase verifies **definition
  parity**, never republishes the names.

### CLI + CI
```
oracle eval                       # run the catalog, print the scorecard. Writes NOTHING.
oracle eval --dimension leak      # subset
oracle eval --ci                  # exit non-zero on ANY safety_floor_breach. Writes NOTHING.
oracle eval --write               # the HUMAN action: also write docs/eval/<date>.md
```
**No new CI job (P6S-5).** The parametrized pytest nodes run inside `make
check`, so the existing suite IS the gate on every cell — the P1F-14 discipline
("the pytest suite is the CI gate; no separate CI step is needed") is
preserved, and the eval cannot disagree with `make check`. CI never writes
dated scorecards (it cannot commit; a writing `--ci` would dirty every run);
scorecard persistence under `docs/eval/` is an explicit operator action,
committed by a human. Trend comparison reads the last *committed* scorecard
and compares only class-1/2 metrics; a quality regression past a configured
delta renders a warning in the output, never a failure.

## Tasks

- **P6-T1 — harness core + import boundary.** `eval/harness.py`:
  `Scenario`/`Verdict`/`Observation`/`DimensionScore`/`Scorecard`,
  `SEVERITY_BY_DIMENSION` (derived severity — `run_catalog` rejects any
  attempt to carry severity on a scenario), `run_catalog`, `render_scorecard`,
  built on Phase 1's `testkit`. Per-scenario isolation: each scenario receives
  a **fresh copy** of a once-spawned template root (copytree of the session
  spawn — cheap, deterministic), never the shared `spawned_root`. The import
  boundary amendment: allowlist `oracle_agent/eval/` in
  `test_no_production_module_imports_testkit` + the NEW converse guard test.
  *Acceptance:* a trivial 2-scenario catalog scores correctly; scorecard
  renders **byte-identical across two consecutive runs** (date passed in; no
  wall-clock in the body; pinned tie-breaks + `round(.,4)`); the whole catalog
  adds ≤ 60 s to the suite (pinned budget); both boundary tests green.
  *Tests:* `test_eval_harness.py`. *Deps:* P1-T2.

- **P6-T2 — leak catalog.** Encode the STRESS C1/H1/H2 attack classes plus the
  **landed** P2 remnants (egress veto, public floor) as `leak/` scenarios
  across all read verbs and both environments, INCLUDING the embedding sink
  (chat-driven backfill must never egress a planted above-ceiling marker —
  `FakeEmbedClient.all_texts`). Every scenario follows the planted-marker +
  reachability-control discipline and declares its `fault_point`.
  Minimized-receipt scenarios: conditional extension keyed to P2 re-entry,
  not built now (P6S-2). *Acceptance:* all pass on current `main`; for each
  scenario, no-op'ing its declared `fault_point` flips it to fail
  (planted-fault check), and the reachability control shows the marker.
  *Tests:* the scenarios self-verify as pytest nodes;
  `test_leak_catalog_faults.py` proves each catches its planted fault.
  *Deps:* P6-T1.

- **P6-T3 — grounding catalog.** Encode P3 scenarios composition-level:
  ungrounded assertion redacted, supported/caveated/refused obligations
  honored, repair convergence (counted round-trips), and the `corpus.json`
  smuggle classes replayed through the full AgentLoop AND the gateway ENFORCE
  path. *Acceptance:* pass on main; no-op'ing the grounding gate seam
  (`fault_point`) fails them. *Tests:* as above. *Deps:* P6-T1, P3.

- **P6-T4 — policy + gateway catalogs.** Matrix-parity scenarios (shell
  release vs the root's `oracle policy check`, enumerated over the kernel's
  actual environment columns — the no-seam dimension, rendered honestly in
  `Scorecard.no_seam`) and per-surface gateway scenarios across **all four
  adapters**: private guarantee, unknown-sender, access-change, rate-limit,
  MCP dropped-verb/token probes, email DMARC-spoof, briefing-target refusal,
  connector-credential containment, Slack. Each scenario carries its
  `guarantee` (SH-xxx map) and passes the net-new-only review rule.
  *Acceptance:* pass on main; planted faults catch a widened matrix
  comparison or a dropped private check (where a shell seam exists).
  *Tests:* as above. *Deps:* P6-T1, P4, P7.

- **P6-T5 — behavior (pipeline-quality) catalog + trend.** Class-2 metrics
  only: extractor recall per smuggle class on `corpus.json`; repair-loop
  convergence in counted round-trips; pipeline refusal-correctness under
  scripted envelopes. Persist scorecards under `docs/eval/` (via `--write`
  only); a small trend renderer comparing against the last committed
  scorecard. *Acceptance:* scores computed and dated; trend vs previous
  committed scorecard shown; tracked, not gated; **no wall-clock number
  anywhere in the scorecard**. *Tests:* `test_behavior_eval.py`. *Deps:*
  P6-T1, P3.

- **P6-T6 — `oracle eval` CLI + the gate.** Wire the command (lazy import in
  the handler); `--ci` exits non-zero on any breach and writes nothing;
  `--write` is the human persistence action. **No new CI job**: the gate is
  the parametrized pytest nodes inside `make check` (P1F-14). Close the loop
  with P1's map the only sanctioned way: new guarantees land as
  `security_map.GUARANTEES` entries whose `enforcer` is the scenario's pytest
  node id, and `docs/SECURITY.md` is **regenerated** via `render_security_md`
  (it is auto-generated and drift-tested — never hand-edited).
  *Acceptance:* `oracle eval --ci` green on main; a deliberately reverted fix
  turns the suite red in CI; `verify_enforcers()` still empty; the SECURITY.md
  drift test green. *Tests:* `test_eval_cli.py`, extend
  `test_security_map.py`. *Deps:* P6-T2..T5, P1-T1.

- **P6-T7 — usefulness (quality) catalog.** Three deliverables, all class 2:
  1. **Gold retrieval scoring** on `eval/fixtures/retrieval_gold.json` under
     the fixture-scoped names `gold_hit_at_k` / `gold_mrr` (the
     `test_retrieval_gold.py` eval body, promoted into the catalog). The
     kernel KPI names are NOT reused: the kernel's `retrieval_hit_rate` is a
     traffic proxy and `time_to_first_grounded_answer` is median days over
     real ledgers — a fixture eval republishing those names would report a
     different number for the same name, the exact failure the old wording
     forbade (P6S-3).
  2. **Definition parity** instead of value parity: synthetic
     `retrieval_event`/`answer_event` ledger fixtures with controlled
     timestamps are fed to the kernel's `_kpi_retrieval`, asserting the
     computed values match hand-derived expectations (the P8-T7 acceptance
     pattern). The shell eval and the kernel scorecard can then never drift
     on what a name MEANS, while each reports only what it can honestly
     measure.
  3. **Hold-out consumption + lifecycle (P6S-6):** this phase's first scoring
     run consumes the frozen hold-out (query ids 5/10/15/20/25). The rendered
     scorecard stamps the hold-out as **convention-only** (in-repo, excluded
     from tuning by P8S-12 discipline, not secret). On consumption: the ids
     are recorded as consumed (dated, in the committed scorecard), folded
     into the tracked set, and a **fresh hold-out is minted via
     `regen_retrieval_gold.py`** (new ids, same every-5th rule) reserved for
     the next eval generation. Review rule pinned: no change-set may touch
     ranker constants and gold fixtures together.
  Plus **intake throughput** on `eval/fixtures/connector_corpus/` — a small
  synthetic, secret-scan-clean corpus **shipped by this phase** (P7 shipped
  no fixture artifact); "answerable-from" is pinned as: a fixed probe query
  returns the ingested doc's source_id in top-k. Throughput is measured in
  **deterministic counts** (documents per pull batch, pipeline stages /
  verb invocations to answerable) — never seconds in CI; wall-clock intake
  timing, if ever wanted, is class 3. *Acceptance:* gold metrics computed,
  dated, trended; parity test green against the kernel function; hold-out
  consumption stamped + regenerated; connector corpus schema-checked and
  secret-scan clean. *Tests:* `test_usefulness_eval.py`. *Deps:* P6-T1,
  P6-T5 (shares the trend renderer), P8-T8 artifacts.

## Invariants for this phase

- The eval runs fully offline (scripted model, fake gateways, fake embedder);
  it exercises real dispatch/ceiling/grounding/gateway code, never a live
  provider. **Nothing is gated on a metric CI cannot honestly compute**, and
  class-3 (real-model / real-traffic) numbers never enter the CI scorecard.
- Safety dimensions are hard gates at 100%; there is no "mostly safe".
  Severity is DERIVED from dimension — a scenario cannot declare itself out
  of the gate. A new guarantee added in any phase MUST land with a safety
  scenario here (this is how P5's guarantees join when P5 lands), and
  `security_map.py` points its enforcer at that scenario's pytest node.
  **`GUARANTEES` is the sole registry**; the catalog is enforcer supply.
- **Planted-fault checks** (the honest name — stdlib-only forbids mutation
  frameworks, and kernel subprocesses are beyond monkeypatch) are mandatory
  for safety scenarios: each declares its `fault_point`, the meta-test proves
  the patched seam is actually on the scenario's code path (a call-recording
  wrapper — patching a dead seam fails the meta-test), and no-op'ing it flips
  the scenario to fail. Scenarios with no patchable shell seam (kernel-side
  matrix logic) are enumerated in `Scorecard.no_seam` and covered by parity
  comparison instead — stated in the rendered scorecard, never hidden.
- Leak scenarios follow the planted-marker + sink-scan + reachability-control
  discipline; markers are secret-scan-safe.
- Scorecards are reproducible: date is an input; rendering consumes only
  counts/ids/rates (never raw ledger rows); pinned total-order tie-breaks and
  fixed precision make two runs byte-identical on every CI cell.
- The retrieval hold-out is convention-only and single-use: consumed ids are
  stamped and a fresh hold-out is regenerated (P6-T7).
- CI writes nothing under `docs/eval/`; `--write` is the human action.

## Stress pass (done 2026-06-11 — before coding, as required)

Two lenses ran against this spec and the landed repo (attacker mindset;
spec-vs-landed-reality), with every repo claim verified against the working
tree — 1453 collected tests, the 84-guarantee `security_map.py`, the P2 gate
result, the P8 fixtures and kernel KPI code, the CI workflow and Makefile.
The headline: the architecture (deterministic scripted scenarios gating
safety floors at 100%, quality tracked-not-gated) is sound and the substrate
is unusually ready — but the spec promised metrics CI cannot honestly compute
under names the kernel already owns, mandated testing a P2 feature that was
gated NO-GO and never built, and wired into a CI / SECURITY.md / testkit
discipline that landed *after* it was written. All fourteen findings were
accepted and folded into the text above. The seed meta-question ("can a
scenario pass for the wrong reason?") got a structural answer, not a review
habit: planted markers, sink-side scans, reachability controls, declared
fault points with a dead-seam meta-test, and an honest `no_seam` enumeration.
Summary of findings and where each landed:

| ID | Sev | Finding (one line) | Resolution |
|----|-----|--------------------|------------|
| P6S-1 | CRIT | FakeLLM-scored grounded-rate / refusal-correctness / latency measures the script, not the system; the offline invariant contradicted the behavior metrics | three-class metric taxonomy pinned (CI-gated safety floors / CI-tracked deterministic pipeline metrics / real-model-or-real-traffic class 3 via the P2-T0 + P3-T7 protocols); class-3 never in the CI scorecard; latency in counted round-trips only (core idea, P6-T5) |
| P6S-2 | CRIT | Leak catalog mandated "minimized-tier receipts (P2)" — P2 was gated NO-GO; no minimizer exists on `main`; the scenario could only pass vacuously | P2 scenarios scoped to the landed remnants (egress veto SH-058, public floor SH-013/014); minimized-receipt scenarios a conditional extension keyed to P2 re-entry, excluded from this phase's DoD (leak/, P6-T2) |
| P6S-3 | HIGH | "Definitions must match the kernel KPIs" unsatisfiable: kernel `retrieval_hit_rate` is a traffic proxy and `time_to_first_grounded_answer` is median DAYS over real ledgers — fixture metrics are a different population/unit | fixture-scoped names (`gold_hit_at_k`/`gold_mrr`); definition-parity test runs the kernel `_kpi_retrieval` on synthetic ledger fixtures; kernel names reported only by the kernel scorecard (P6-T7) |
| P6S-4 | HIGH | Scenarios aren't pytest nodes so `verify_enforcers` can't point at them; "document in docs/SECURITY.md" hand-edits an auto-generated drift-tested file — a second registry by accident | every safety scenario IS a parametrized pytest node (`test_eval_catalog.py::test_scenario[ID]`); `Scenario.guarantee` maps to SH-xxx; SECURITY.md changes flow only through `GUARANTEES` + `render_security_md`; sole-registry invariant pinned (frozen interfaces, P6-T6) |
| P6S-5 | HIGH | Separate `oracle eval --ci` CI job violates the P1F-14 single-gate discipline, ×8 matrix cells; CI can't commit `docs/eval/<date>.md`; cross-cell trend baselines spurious; no time budget | no new CI job — the suite is the gate; `--ci` writes nothing; `--write` is the human action; trend vs last committed scorecard, class-1/2 only; ≤ 60 s catalog budget pinned (CLI+CI, P6-T1/T6) |
| P6S-6 | HIGH | The "frozen hold-out" ids are world-readable and literally named in test code (convention, not secrecy) and burn on P6's first scoring run | hold-out stamped convention-only in the rendered scorecard; consumed-then-regenerate lifecycle via `regen_retrieval_gold.py`; "no ranker constants + gold fixtures in one change-set" review rule (P6-T7) |
| P6S-7 | MED | "Mutation-tested" overclaims: stdlib-only forbids mutation frameworks; kernel subprocesses are beyond monkeypatch; a wrong-seam patch passes for the wrong reason | renamed planted-fault checks; declared `fault_point` per safety scenario; reachability meta-test proves the seam is on the code path; honest `Scorecard.no_seam` enumeration with parity coverage instead (frozen interfaces, invariants) |
| P6S-8 | MED | Pass-for-the-wrong-reason acknowledged but not prevented: a bare bool `assert_pass` can't prove the probe reached the sink; substring asserts pass on refusal stubs | planted unique markers + `FakeLLM.all_messages`/`FakeEmbedClient.all_texts` sink scans + raised-ceiling reachability control; `Verdict` with evidence replaces bare bool; secret-scan-safe marker grammar (frozen interfaces) |
| P6S-9 | MED | Catalog frozen against a pre-P4/P7/P8 world: Telegram-era gateway list; no embedding-egress, MCP, email-auth, briefing-target, connector-credential scenarios; no scenario→guarantee map; duplication risk vs SH-001..084 | `guarantee` field + net-new-only composition-level rule; catalog updated: embedding sink, MCP probes, DMARC-spoof, briefing targets, connector creds, Slack, gateway smuggle-replay; "gateway" = core + all four adapters (catalog, P6-T2/T3/T4) |
| P6S-10 | MED | "Fixed connector fixture corpus from P7" names an artifact that doesn't exist; "intake throughput" unit undefined; a seconds-rate flakes in CI | `eval/fixtures/connector_corpus/` shipped BY P6, schema-checked, secret-scan clean; "answerable-from" pinned (probe query returns source_id in top-k); throughput in deterministic counts, never CI seconds (P6-T7) |
| P6S-11 | MED | Stale tenses: P7/P8 treated as concurrent/future (both landed — cite artifacts directly); "depends on all prior phases / built last" false with P2 closed NO-GO and P5 open | deps narrowed to the landed guarantee set; P5 explicitly not a dependency — the forward invariant (new guarantee ⇒ scenario) carries the load; direct citations of the landed artifacts throughout (header, read-first) |
| P6S-12 | MED | `eval/harness.py` importing testkit trips the landed AST-walk enforcer `test_no_production_module_imports_testkit`; an `eval` CLI command could pull testkit into every command's import path | deliberate boundary amendment: allowlist exactly `oracle_agent/eval/` + NEW converse guard (nothing else imports testkit OR `oracle_agent.eval`); lazy import in the CLI handler (frozen interfaces, P6-T1/T6) |
| P6S-13 | LOW | A free `severity` field lets a scenario declare `dimension="leak", severity="quality"` and dodge the gate; `DimensionScore` undefined; shared session root cross-contaminates; raw ledger rows leak wall-clock into renders | severity DERIVED via frozen `SEVERITY_BY_DIMENSION` (run_catalog rejects overrides); `DimensionScore`/`Verdict` defined; per-scenario root-copy isolation; counts-only rendering (frozen interfaces, P6-T1) |
| P6S-14 | LOW | Byte-reproducibility asserted across 8 heterogeneous CI cells with no tie-break rule — equal-score rank flips read as regressions | pinned total order (score desc, id asc — the P8S-5 discipline) + fixed `round(.,4)` precision; byte-identical consecutive-run acceptance (frozen interfaces, P6-T1) |

## Definition of done

- [ ] Harness scores a catalog deterministically; scorecard rendered + dated;
      byte-identical across consecutive runs; derived severity; per-scenario
      root-copy isolation; import-boundary amendment + converse guard green;
      ≤ 60 s catalog budget held.
- [ ] Leak, grounding, policy, gateway safety catalogs — composition-level,
      scenario→guarantee mapped, planted-marker discipline, each with a
      planted-fault proof (declared `fault_point` + reachability meta-test)
      or an honest `no_seam` entry. Embedding sink, MCP, email-auth,
      briefing-target, connector-credential scenarios included. P2
      minimized-receipt scenarios explicitly deferred to P2 re-entry.
- [ ] Behavior pipeline-quality catalog (extractor recall per class, counted
      repair convergence, pipeline refusal-correctness) with trend tracking;
      no wall-clock numbers.
- [ ] Usefulness catalog: `gold_hit_at_k`/`gold_mrr` on the P8 gold fixtures;
      kernel-KPI definition-parity test on synthetic ledgers; hold-out
      consumption stamped convention-only + fresh hold-out regenerated;
      `eval/fixtures/connector_corpus/` shipped with count-based intake
      throughput; tracked, not gated.
- [ ] `oracle eval [--ci|--dimension|--write]`; the gate is the parametrized
      pytest nodes inside `make check` (no new CI job); `--ci` writes
      nothing.
- [ ] New guarantees registered in `security_map.GUARANTEES` pointing at
      scenario pytest nodes; `docs/SECURITY.md` regenerated (drift test
      green); `verify_enforcers()` empty.
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
