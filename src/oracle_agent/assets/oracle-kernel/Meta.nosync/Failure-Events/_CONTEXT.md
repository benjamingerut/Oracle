# Failure-Events

Narrative notes that contextualize raw `failure_event` rows. A failure-event
records where the oracle fell short — a wrong answer, a missed contradiction, a
broken connector, a blast-radius cap hit, a loop that did not run when due.
Failures are not hidden; they are first-class fuel for improvement.

## Ledger vs note

The durable record is a `failure_event` row written by `_tools/capture.py`
(`oracle capture failure ...`). A note here is **optional** narrative for a
failure worth a post-mortem; it links back to the ledger `drop_id`.

`capture failure` records `--target`, `--polarity`, `--strength`, an `--excerpt`,
and `--actor`. The harness and autonomy layer also write `failure_event` rows
automatically when a run aborts or a cap is exceeded.

## Discipline

- Record the failure honestly and promptly — a buried failure cannot be learned
  from.
- Feed material failures into `Improvements/` and, where the failure is a
  recurring check, into an `invariant-expansion` tripwire.
- The `architecture-retrospective` loop reads these to decide whether the
  oracle's shape itself should change.

## Type

`type: failure_event`. Usually `internal`; raise if it references confidential
material.
