# Decisions

Observed organizational actions. A Decision is a record of **what happened** — evidence of a choice the organization actually made — not an approval the oracle manufactured by asking.

## What belongs here

One note per observed action. Use `type: decision`. Record the actor, the action taken, the observed date, the source that evidenced it, and links to any `Recommendations/` it conforms to or conflicts with. Decisions are the empirical ground truth that the recommendation adjudicator scores against: the org acting (or not acting) is the accept/reject signal, never a human clicking approve.

Capture decisions you *observe* through connectors and testimony. Do not invent a decision to confirm a recommendation; if you cannot observe it, it did not (yet) happen.

## Mutability

Immutable. A decision is a historical fact. If your understanding of what happened changes, write a new decision (or a corrective Source/Finding) and supersede the old one — do not edit the record of what was observed. The content hash is registered in the ledger; `oracle_lint` FAILS on mismatch.

## Sensitivity

Set `sensitivity:` to the strictest tier the decision's subject warrants — most organizational decisions are `confidential`; board-level or pre-announcement decisions may be `restricted`. The observed actor is often a person, so PII-class care applies. Classify up when in doubt.
