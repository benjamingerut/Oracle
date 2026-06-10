# Metrics

Definitions of the quantities the company measures itself by: revenue, churn, margin, NPS, pipeline, headcount, and the like.

## What belongs here

One note per metric *definition* — the stable hub that pins down what the number means: its precise definition, grain, unit, source system, calculation, owner, and known caveats. Use `type: metric`.

A metric note is the definition, not the value. Point-in-time values (this month's MRR, last quarter's churn) are immutable `Findings/` or live in `_data.nosync/` and are linked from here. Keeping the definition separate from the values is what lets the oracle catch drift and contradictions when two systems compute the "same" metric differently.

## Mutability

Mutable hub. Definitions get refined; when a definition materially changes, note it in the change log and, if it breaks comparability, open a `Contradiction` or `Question`. Bump `updated:` on every change.

## Sensitivity

Metric *definitions* are usually `internal`. Raise to `confidential` when the definition itself reveals strategy or the metric is non-public. Metric *values* inherit the sensitivity of their underlying data — classify the linked Finding/Source accordingly, not just this hub.
