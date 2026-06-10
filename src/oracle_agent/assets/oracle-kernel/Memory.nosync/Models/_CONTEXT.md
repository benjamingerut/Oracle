# Models

The oracle's current best explanatory compressions of how {{COMPANY_NAME}} actually works — the mental models that let it predict, not just describe.

## What belongs here

One note per model. Use `type: model`. A model is held accountable to reality: every active model must state a `core_claim`, the `predictions` it makes, what it `explains`, its `known_residuals`, and a `review_cadence` with a concrete `next_review` date. Models that predict nothing are not models — they are descriptions, and belong as `Findings/`.

## Mutability

Mutable hub, but disciplined. Refine in place for small updates and bump `updated:`. A **major reversal** (the core claim flips, or predictions repeatedly fail) is a supersession: write a new model, set `status: superseded` on the old one, and link them. `status:` is one of `active`, `challenged`, `superseded` — move a model to `challenged` the moment a prediction misses, and let the review loop force resolution by `next_review`.

## Sensitivity

Models often encode strategy and competitive insight, so default to `confidential`; `restricted` when the model would be damaging if leaked. Internal-only mechanical models can be `internal`. Classify up when the core claim itself is sensitive. No secrets in the body.
