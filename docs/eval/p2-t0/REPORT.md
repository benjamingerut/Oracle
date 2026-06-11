# P2-T0 — Minimized-Usefulness Gate: Provisional Verdict

**Provisional verdict: NO-GO** (pending the formal human spot-check the spec
requires before the verdict is final).

## Result vs the gate

| Category | Answerable-correctly | Conclusion leaks | Gutted (<30%)? |
|---|---|---|---|
| customer_account | 7/10 (70%) | 0 | no |
| finance_figures | 6/10 (60%) | 0 | no |
| legal_ma | 4/10 (40%) | 0 | no (but worst) |
| people_compensation | 5/10 (50%) | 0 | no |
| **Overall** | **22/40 (55%)** | **0** | — |

Gate: ≥70% overall answerable-correctly AND no category gutted AND zero
conclusion-leak no-gos. **55% < 70% → the usefulness threshold fails.** The
safety side held: zero conclusion-level leaks across all 40 fixtures.

## Conditions of measurement

- Model: `qwen3.6-32k:latest` (verified genuinely local via `/api/tags` — no
  `remote_host`), temperature 0, judged by the same local model against
  ground truth with the rubric in `rubric.md` (confined model-judge variant).
- Fixtures: 4 categories × 10 synthetic confidential Q&A fixtures
  (`fixtures.json`), hand-minimized per the frozen provisional category rules.
  Rubric written before fixtures; fixtures never tuned after model results.
- Validity stamp: results hold for this model class and the ≤32k context
  regime only. A stronger local model (or better minimization rules that
  preserve more answerable structure) could change the verdict — re-run this
  exact harness (`run_eval.py`, resumable) to re-gate.

## What this means (per the PHASE-2 spec)

P2-T1…T6 (the minimizer build) **do not proceed** on these numbers — the gate
exists precisely to prevent spending the phase on technically-safe but
practically-empty answers. The phase remains open behind the gate with three
re-entry paths:

1. **Better local models** — re-run the gate when a materially stronger
   genuinely-local model is available on this hardware.
2. **Better minimization rules** — the provisional category rules redact
   aggressively; a rules revision that preserves relational structure
   (consistent pseudonyms instead of bare `[person]` placeholders) may
   recover answerability, especially in legal_ma (40%) where cross-entity
   reasoning died. This is a bounded experiment against the same fixtures.
3. **The enterprise tier** (P2-T7 ADR, delivered separately) — frontier-model
   quality under contractual zero-retention, default-off, admin-attested.

## Human spot-check queue (required before the verdict is formal)

`results.json` carries every fixture's question, minimized context, model
answer, ground truth, and the judge's reasoning. Review at minimum: all 18
fails (confirm they are genuine usefulness failures, not judge harshness) and
a sample of 6 passes (confirm no missed conclusion-leaks). If spot-check
overturns ≥4 fails to passes, the overall crosses 65% — still NO-GO, but
close enough to justify re-entry path 2 immediately.
