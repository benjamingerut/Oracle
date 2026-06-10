# Playbook: the session protocol and memory capture

Memory is the product. A session that isn't captured never happened, as far as
the oracle's future selves are concerned.

## Opening: `./oracle status`

One screen: maturity rung, memory counts, authority coverage, Review Inbox
size, due loops, and concrete "do next" suggestions. Act on it — the
suggestions are computed from real state, not boilerplate.

## During: capture as you go

**Session memory** (facts, questions, retrieval strategies):

```
./oracle remember \
  --user-request "what was asked" \
  --answer-summary "what you concluded" \
  --business-object "<object touched>" \
  --source-id "<evidence used>" \
  --learned-claim "a durable fact learned this session" \
  --open-question "what remained unresolved" \
  --query "a retrieval strategy worth reusing"
```

Repeatable flags; supply what applies. Capture when the session produced any
durable business information, decision context, or unresolved question — not
for trivial mechanical exchanges.

**Signals** (the self-improvement fuel):

```
./oracle capture feedback --target <artifact/answer> --polarity <+/-> \
  --strength <0..1> --excerpt "what they said" --actor <who>
./oracle capture value    --target ... # realized value, a win, a saved hour
./oracle capture failure  --target ... # a miss, a wrong answer, a complaint
```

Capture on every praise, correction, missed call, or realized outcome. These
events drive the user-feedback-learning and skill-repository-learning loops;
an oracle without signal capture cannot improve.

**Procedure** — when a signal implies a durable better way of working, write
it to the oracle-local skills repository: `./oracle skills` (it travels with
this oracle; it is not the host machine's skill store).

## Closing: `./oracle checkpoint`

Runs the due builtin loops — memory matriculation (decomposes captured
sessions into review-gated Findings/Questions/Contradictions/Queries),
insight synthesis (model worklists), leadership briefing (when due) — then
re-surfaces the inbox and what remains. If a loop returns a worklist, finish
it (or explicitly hand it to the next session) before closing.

The dreaming pass is owned by the `memory-matriculation` loop. Do not create a
separate "memory-dreaming" loop, and do not decompose sessions by hand —
capture, then let checkpoint matriculate.

## Goal clarity before execution

Policy: `./oracle session contract --json`. Scale clarification to the work:

- Trivial, reversible, cheap → proceed on reasonable assumptions.
- Material ambiguity in goal/scope/constraints/audience → targeted questions.
- Broad, costly, risky, or architecture-level → one-question-at-a-time
  dialectic with a recommended answer each time, until you can state goal,
  output shape, constraints, non-goals, and success criteria without guessing.
  Inspect local material first; never ask what the oracle can already answer.
- If the user chooses speed despite uncertainty, record the assumptions in the
  workproduct and move.
