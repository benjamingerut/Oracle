# Playbook: the leadership brief

The brief is the oracle's proactive voice — what leadership should know NOW,
without being asked. It runs on cadence via the `leadership-briefing` loop and
on demand:

```
./oracle brief                 # print to stdout
./oracle brief --days 14       # wider change window
./oracle brief publish         # file into Workproduct.nosync/_STANDING via the policy gate
```

## What the deterministic skeleton contains

1. **State of the oracle** — evidence counts, authority coverage
   (confirmed/total rows, promotable), inbox size.
2. **What changed** — new Sources and Findings in the period.
3. **Decisions waiting** — the top of the Review Inbox, each with its action.
4. **Contradictions** — open conflicts, must_resolve flagged.
5. **Authority coverage** — per-object verdict table
   (grounded/supported/caveated/withheld).
6. **Questions going stale** — open questions past budget.
7. **Needs authority appendix** — objects whose claims were WITHHELD (exit 4),
   each with the exact commands that unlock them. Withheld objects are never
   silently omitted.

Every object-level claim is routed through `answer_protocol.preflight`; the
brief cannot ship an unauthorized claim (enforced in `briefing.py` +
`standing_deliverables.py`).

## Agent enrichment (your half)

The skeleton is honest but mechanical. Below the "Agent enrichment" marker,
add what a chief-of-staff would: interpretation, connections between sections,
momentum vs last period, and the 1-3 things that most deserve the leader's
attention. Rules:

- Any NEW material claim you introduce must pass `./oracle answer` first and
  carry its verdict labeling (supported/caveated stated inline).
- Interpretation and prioritization are judgment, not claims — no preflight
  needed, but anchor them to the cited items above.
- Keep enrichment under one screen. The leader reads this in two minutes.

## Delivery and capture

- `publish` lands the brief in `Workproduct.nosync/_STANDING/` (policy-gated,
  registry-logged). Deliver it through whatever channel the admin configured;
  the file is the canonical copy.
- When the leader reacts — acts on an item, corrects one, praises one —
  capture it: `./oracle capture feedback|value ...` referencing the brief.
  That signal feeds the user-feedback and value loops; skipping it blinds the
  oracle to its own usefulness.

## Other standing deliverables

`./oracle deliverables gen contradiction-digest|rec-scorecard|freshness-report`
produce the focused standing artifacts. The brief supersedes none of them; it
is the synthesis layer on top.
