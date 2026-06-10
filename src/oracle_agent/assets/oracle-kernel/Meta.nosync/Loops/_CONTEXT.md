# Loops

A **loop** is a recurring improvement process: a thing the oracle re-evaluates as
information arrives, changes, contradicts, or decays. Loops are how the oracle
stays current and self-improving instead of going stale.

## Loop record = runnable record (not a wish)

In v2 a loop is not just doctrine prose — it is a **record the engine can run**.
`_tools/loops.py` reads these notes, computes which are due from
`cadence` + `last_run` + `trigger_conditions`, dispatches the `runner`, and
appends a `loop_runs` ledger row that updates `last_run` and `next_review`.

Because of that, an **active** loop is held to the `loop.schema.json` contract:

- `runner` is **required** — either `module:function` (a Python runner) or the
  literal `agent-worklist` (the loop is a structured worklist the agent works).
- `last_run` and `next_review` are **required** and must be real ISO dates
  (`YYYY-MM-DD`), not `null` and not `TBD`.
- `cadence` must be set (`weekly` | `monthly` | `every-session` | `on-event` |
  an ISO-8601 duration).

`oracle_lint` **FAILS** any `status: active` loop that lacks a `runner` or a
`last_run`. A `proposed` or `retired` loop is held only to the looser common
frontmatter — use those statuses for loops you have not wired up yet.

## The twelve loops instantiated at spawn

Spawn writes twelve **active** loop records with real `runner`/`last_run`/
`next_review`:

- `memory-matriculation` — capture, decompose, link, verify, refresh memory.
- `source-capture` — turn pulls / snapshots / testimony into immutable Source
  records.
- `workproduct-io` — scan the `_INPUT` / `_OUTPUT` registries and ensure
  artifacts matriculate.
- `user-feedback-learning` — convert captured value / corrections into meta
  learning (updates the user-model, incl. structured `preferences:` counters).
- `skill-repository-learning` — convert durable procedural feedback/value/failure
  signals into managed Oracle-local skills.
- `insight-synthesis` — cluster findings against Models and drive
  propose/update/revalidate-model worklists (`builtin:insight-synthesis`).
- `leadership-briefing` — publish the proactive leadership brief through the
  standing-deliverables gate (`builtin:leadership-briefing`).
- `value-scorecard` — roll the window's captured events into one cited
  scorecard with a trend verdict (`builtin:value-scorecard`).
- `improvement-lifecycle` — adjudicate applied improvements against observed
  event ledgers; age stalled proposals (`builtin:improvement-lifecycle`).
- `meta-health` — consume the oracle's own telemetry: pause repeat-failing
  loops, enforce signal-age budgets, draft autonomy proposals
  (`builtin:meta-health`).
- `stale-finding-refresh` — sweep confirmed findings past their staleness
  budget (`builtin:stale-finding-refresh`).
- `architecture-retrospective` — quarterly (and on regression triggers)
  evidence-dossier retrospective; output is ready-to-approve change proposals
  (`builtin:architecture-retrospective`).

`memory-matriculation` owns session/daily dreaming through
`builtin:memory-matriculation`; do not create a second active `memory-dreaming`
loop that performs the same decomposition work.

A repeatedly failing loop is auto-paused by meta-health (`status: paused`,
reason recorded, surfaced in the Review Inbox); reactivate by fixing the cause
and setting `status: active`. The full registry of loops (including the
still-`proposed` ones such as `connector-health`, `contradiction-resolution`,
`backup-restore-check`) lives in the kernel `PLAYBOOKS/loops.md`. Promote a
`proposed` loop to `active` only when it has a real runner and a real `last_run`.

## Cost discipline

Scheduled / headless loop runs default to the cheapest capable model; reserve
premium or multi-agent passes for on-demand work. A loop should prefer
deterministic, no-LLM code where it can.

## Creating a loop

Create a loop when a process benefits from repeated re-evaluation. Start it as
`proposed`; promote to `active` once it has a runner and has run at least once.
Use `loop-template.md` as the starting point — it is a complete, lint-clean
active record you can copy and adapt.
