# Feedback

Narrative notes that contextualize raw `feedback_event` rows. Feedback is the
user telling the oracle, in words, what was useful, wrong, missing, or
mis-shaped. It is the primary fuel for the `user-feedback-learning` loop.

## Ledger vs note

The durable record is a `feedback_event` row written by `_tools/capture.py`
(`oracle capture feedback ...`). A note here is **optional** — write one only
when a piece of feedback carries a story worth keeping (a pattern, a strong
correction, a changed expectation). The note links back to the ledger `drop_id`.

`capture feedback` records `--target` (what the feedback is about), `--polarity`
(positive / negative / mixed), `--strength`, an `--excerpt`, and `--actor`.

## Discipline

- **User testimony is evidence, not automatic truth.** Feedback updates the
  oracle's behavior and user-models, but a factual claim inside feedback still
  goes through the normal source / finding discipline before it is trusted.
- Convert recurring feedback into a concrete `Improvements/` note, not just a
  pile of events.
- Never silently degrade from a previously-praised behavior; anchor to the last
  known-good and improve from it.

## Type

`type: feedback_event`. Sensitivity is usually `internal`; raise it if the
feedback quotes confidential material.
