# Findings

Atomic claims at a point in time — the irreducible units of what the oracle believes, each carrying its own epistemics.

## What belongs here

One claim per note. Use `type: finding`. A material finding REQUIRES: a `claim_tier` (OBS observed | INF inferred | SPEC speculative | SPEC-horizon long-range speculation), a numeric `confidence` in [0,1] stated as a range in the body (not a false point), non-empty `evidence` (link the Source), `decision_relevance`, a non-empty `disconfirmer` (what would change the conclusion), and an `as_of` date. The schema enforces every one of these.

Do not bury multi-claim studies here. Decompose a report into its constituent findings, models, contradictions, questions, and recommendations — one claim each.

## Mutability

Immutable. If a claim changes, **write a new finding** and supersede the old one (`supersedes:` / `superseded_by:`); never edit the bytes. The content hash is registered in the Findings ledger and `oracle_lint` FAILS on mismatch. Findings begin life at `status: needs_review` (derivation is never auto-trusted) and move to `active` only after review; later they become `superseded` or `retired`.

## Sensitivity

A finding inherits the strictest sensitivity of its evidence — a claim derived from `confidential` data is at least `confidential`. Set `sensitivity:` accordingly at creation; never under-classify to make sharing easier. The answer protocol uses this as the sensitivity ceiling when the finding grounds an answer.
