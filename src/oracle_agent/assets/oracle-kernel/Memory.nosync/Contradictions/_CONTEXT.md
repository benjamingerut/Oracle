# Contradictions

First-class, unresolved conflicts among sources, findings, models, metrics, or testimony. These are not errors to suppress — they are signposts. Many of the most valuable truths live exactly where two trusted sources disagree.

## What belongs here

One note per live conflict. Use `type: contradiction`. Capture the `claims_in_conflict`, `severity` (low | medium | high | critical), `decision_relevance`, `possible_causes`, and a `resolution_plan`. Optionally set `classification:` (`must_resolve` | `bounded_residual` | `watch` | `schema_debt`) once the contradiction adjudicator has ranked it.

The oracle must **preserve** decision-relevant mismatches rather than averaging them away. A contradiction that touches an active decision and is classified `must_resolve` will cause the answer protocol to caveat or refuse until it is resolved.

## Mutability

Mutable investigation object. Update status through its lifecycle: `open` -> `investigating` -> `resolved` | `accepted_residual` | `superseded`. Preserve the conflicting evidence and the reasoning trail; when resolved, link the `resolving_source` and explain *why* one side won. Bump `updated:` on each step.

## Sensitivity

Set `sensitivity:` to the strictest tier of any claim in conflict — a contradiction note inherently restates both claims, so it can only be as open as the most sensitive one. Default `confidential` when financial or strategic claims are involved; classify up when in doubt.
