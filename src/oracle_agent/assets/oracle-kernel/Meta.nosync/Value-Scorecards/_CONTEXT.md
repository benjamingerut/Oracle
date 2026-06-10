# Value-Scorecards

Periodic scorecards answering the only question that matters: **is the oracle
actually helping?** Each scorecard covers a window (e.g. a month) and scores the
oracle against the five value dimensions, grounded in the captured events — not
in vibes.

This hub is the output of the `value-scorecard` loop (ACTIVE at spawn,
`builtin:value-scorecard` → `_tools/scorecard.py`), which consumes the
`value_event`, `feedback_event`, `failure_event`, and `answer_event` ledger
rows. Generation is deterministic: `./oracle scorecard gen` computes the KPIs
(grounded-rate, net value, failure recurrence, signal latency, improvement
throughput, admin leverage), stamps the trend verdict vs the prior window, and
writes the dated note here. A `regressing` trend makes the
`architecture-retrospective` loop due immediately.

## Behavioral type

`type: value_scorecard`.

## The five value dimensions

The oracle exists to help the user:

1. **Understand** — clearer picture of the business / situation.
2. **Decide** — better, faster, more defensible decisions.
3. **Act** — execution carried out or unblocked.
4. **Avoid risk** — a danger surfaced or averted.
5. **Discover opportunity** — an upside found that was not being looked for.

## What a good scorecard captures

- The window covered.
- Per-dimension evidence: which `value_event` / `feedback_event` rows support
  the score, by `drop_id`.
- Net direction vs the prior scorecard (improving / flat / regressing).
- Failures in the window (`failure_event` rows) and whether they were addressed.
- One concrete improvement to carry into the next window.

## Discipline

Score from captured events, citing `drop_id`s — never from memory or optimism. A
scorecard with no event citations is not a scorecard. Anchor each window to the
last known-good baseline and improve from it; surface, never silently bury, a
regression.
