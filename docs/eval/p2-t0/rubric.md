# P2-T0 Scoring Rubric — Minimized-Usefulness Validation

**Status:** Written and frozen BEFORE fixture authoring began (method discipline:
rubric first, fixtures second, no fixture tuning after model results are seen).
**Spec:** `docs/roadmap/PHASE-2-confidential-tier.md`, task P2-T0.
**Date frozen:** 2026-06-10.

## 1. Purpose

Measure whether documents minimized per the frozen provisional category rules
remain *useful* — i.e. whether a real local model can still answer the
questions a business leader would ask of the original confidential document —
and whether minimization fails to suppress confidential *conclusions*
(P2S-5: entity-span removal alone is not the bar).

## 2. Frozen provisional redaction category set

Per the spec's frozen interface (`Redaction.category`) and the pre-T0
provisional rules: the following spans are replaced with **typed, per-document
indexed placeholders** (same entity → same placeholder within a document,
numbered by first appearance):

| Category | Spans covered | Placeholder |
|----------|--------------|-------------|
| person   | personal names | `[PERSON_n]` |
| name/org | legal-entity, company, product-codename names (sub-type of the spec's "name" category) | `[ORG_n]` |
| money    | currency amounts (any currency, any magnitude) | `[MONEY_n]` |
| account  | account numbers, customer/contract/deal IDs | `[ACCOUNT_n]` |
| email    | email addresses | `[EMAIL_n]` |
| date     | explicit calendar dates (full or partial, e.g. "March 14, 2026", "3/14") | `[DATE_n]` |

Provisional boundary decisions (recorded so P2-T1 cannot silently diverge):

- **Retained (NOT redacted):** percentages, durations ("90 days", "14 months"),
  headcounts and other bare quantities, fiscal-period labels ("Q3", "FY26"),
  job titles ("CFO"), internal department names ("the data platform group").
- **Fail-closed extension:** an unrecognized-but-suspicious span (capitalized
  multi-token run adjacent to a category hit, unusual identifier formats) is
  redacted with the nearest category placeholder, per the spec's fail-closed
  posture.

## 3. Definitions

### 3.1 Answerable-correctly

A fixture is **answerable-correctly** iff the model, given ONLY the minimized
document and the question, produces an answer whose substance matches the
ground-truth answer:

- All material facts the ground truth requires are present and not
  contradicted. Paraphrase is fine.
- Where the ground truth's identifying detail was legitimately redacted, an
  answer phrased in terms of the placeholder (or "the redacted
  person/amount") that otherwise carries the ground truth's substance counts
  as correct **only if the ground truth itself does not consist of the
  redacted value** (a question whose entire answer is a redacted value cannot
  be answered correctly from the minimized view — it scores incorrect; that is
  the gutting we are measuring, not a judging artifact).
- A hedged answer containing the correct substance counts. A refusal, an "I
  cannot tell from this document" (when the ground truth was recoverable), a
  guess at a redacted value, or a materially wrong answer scores incorrect.
- An "insufficient information" answer is correct **only** when the ground
  truth is genuinely unrecoverable from the minimized view AND the model
  correctly identifies what is missing — but such a fixture still does not
  count toward the usefulness numerator (see 3.4): usefulness measures
  recovered answers, not graceful failure.

### 3.2 Conclusion-level leakage (NO-GO category, P2S-5)

Each fixture declares a `confidential_conclusion`: the sensitive inference the
minimization to `internal` tier is supposed to suppress (e.g. "an acquisition
of a specific target is in progress", "the company breached a debt covenant",
"a named executive is being terminated").

A fixture scores **LEAK** iff the model's answer asserts that conclusion in
substance — paraphrased, hedged, or stripped of named entities all count.
Asserting the conclusion *about a placeholder* still counts as leakage when
the conclusion itself (not the identity) is the confidential fact.

Leakage is a **no-go category, not a success mode**: a leaked fixture can
never count as a usefulness success, even if the answer is also correct.

### 3.3 Category gutted

A question category is **gutted** if fewer than 30% of its fixtures survive
(survive = answerable-correctly AND not LEAK), with minimum N = 10 fixtures
per category. Any gutted category ⇒ NO-GO regardless of the overall rate.

### 3.4 Go/no-go computation

- Per fixture: `pass = correct AND NOT leak`.
- Overall answerable-correctly rate = passes / total fixtures.
- **GO** = overall rate ≥ 70% (threshold stamped *arbitrary — revisit once
  data exists*, per spec) AND no category gutted AND the leakage pattern does
  not itself constitute a systemic failure (any category where >50% of
  fixtures LEAK is treated as a no-go signal even if "correct" rates are
  high — minimization that reliably fails to suppress conclusions is not
  shippable as specced).
- **NO-GO** = anything else.

## 4. Judge protocol

**Variant used in this run:** confined model-judge + mandatory human
spot-check (the spec's alternative to ≥2 human raters; chosen because this
run executes autonomously).

1. **Answering model:** `qwen3.6-32k:latest` via Ollama loopback
   (127.0.0.1:11434, OpenAI-compatible endpoint), temperature 0, fixed seed.
   It sees ONLY the minimized document + question, with an instruction not to
   guess redacted values.
2. **Judge model:** the SAME local model, separate judge prompt. The judge
   sees the question, the ground-truth answer, the confidential conclusion,
   and the candidate answer — **never** the raw source document, and it runs
   under the same loopback confinement as the answering pass (ground truth is
   itself confidential material; the spec requires the judge to see it only
   under the same confinement rules — satisfied: same on-box model, no
   egress).
3. **Judge output:** strict JSON `{"correct": bool, "leak": bool,
   "rationale": str}`. Unparseable output → one retry; still unparseable →
   **fail-closed**: scored `correct=false, leak=true`, flagged for mandatory
   human review.
4. **Mandatory human spot-check:** a stratified sample (≥3 per category,
   mixing passes, fails, and leaks, plus ALL fail-closed items) is listed in
   `REPORT.md` for human adjudication. **The verdict is PROVISIONAL until the
   spot-check is done.** Disagreement rule: if human spot-check overturns
   >20% of sampled judgments, the entire run must be re-judged (≥2 human
   raters) before any GO is recorded in the phase spec.
5. **Independence discipline:** this rubric was written before any fixture;
   fixtures carry an authoring-intent `design_note` for transparency, which
   is NEVER shown to the answering model or the judge; fixtures are not
   modified after the first eval run under any circumstances.

## 5. Context-regime validity

Results are valid only for the pinned model's 32k context regime. Every
fixture's (minimized doc + prompt) length is recorded; any fixture exceeding
the regime would be flagged, never truncated. (All P2-T0 fixtures are short
business documents ≪ 32k tokens; the short-fixture bias toward an optimistic
verdict is acknowledged in the report.)
