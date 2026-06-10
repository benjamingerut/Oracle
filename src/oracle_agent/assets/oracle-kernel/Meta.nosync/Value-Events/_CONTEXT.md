# Value-Events

Narrative notes that contextualize raw `value_event` rows. A value-event records
a concrete instance where the oracle helped — the user understood something,
decided faster, acted, avoided a risk, or found an opportunity. These roll up
into the `value-scorecard` loop.

## Ledger vs note

The durable record is a `value_event` row written by `_tools/capture.py`
(`oracle capture value ...`). A note here is **optional** narrative for an event
that deserves a story; it links back to the ledger `drop_id`.

`capture value` records `--target`, `--polarity`, `--strength`, an `--excerpt`,
and `--actor`, mapped to one of the five value dimensions (understand, decide,
act, avoid risk, discover opportunity).

## Discipline

- Capture value at the moment it is observed, with enough specificity that the
  value-scorecard can cite it by `drop_id`.
- A value claim with no captured event behind it does not count toward a
  scorecard. Evidence over optimism.

## Type

`type: value_event`. Usually `internal`; raise if it references confidential
outcomes.
