# Improvements

Concrete improvements to the oracle — proposed, in progress, or applied. This is
where feedback, value, and failure events turn into action. An improvement is
accountable: it names the problem, the change, the expected signal, and whether
the signal actually showed up. The `improvement-lifecycle` loop (ACTIVE at
spawn, `_tools/improvements.py`) closes the loop mechanically: it adjudicates
applied improvements against the event ledgers and ages stalled proposals into
the Review Inbox.

## What a good improvement note captures

- **Trigger** — the `feedback_event` / `failure_event` / retrospective that
  prompted it, by `drop_id`.
- **Change** — exactly what was (or should be) changed.
- **Expected signal** — a MACHINE-CHECKABLE predicate when possible
  (`verify: auto`), else an explicit `verify: manual` stamp:

  ```
  status: applied
  applied: "2026-06-10"
  verify: auto
  expected_signal:
    event: value_event          # value_event | feedback_event | failure_event
    target: leadership-brief    # what the events reference
    polarity: positive          # for value/feedback
    min_count: 1                # 0 = absence predicate ("no recurrence")
    within_days: 30             # window after `applied`
  ```

- **Status** — `proposed` | `applied` | `verified` | `regressed` | `rejected`.
- **Adjudication** — written by the loop (verdict + cited drop_ids); never
  edit the original trigger/expected_signal fields after applying.

## Discipline

- Tie each improvement to captured events, not to a hunch.
- Verify an improvement by observed reality, not by asserting it is done. An
  "applied" improvement with no observed signal is still unverified — and one
  with neither a checkable `expected_signal` nor `verify: manual` FAILS lint
  (`improvement-unverifiable`).
- Presence predicates that expire without evidence surface for a manual call
  (absence of good news is not automatically bad news); absence predicates
  (`min_count: 0`) verify only after the full window elapses clean.
- If an improvement touches the tool layer, record it as / alongside an
  `architecture_decision` and update the relevant `Architecture-Components/`
  note.

## Type

`type: improvement`. Usually `internal`.
