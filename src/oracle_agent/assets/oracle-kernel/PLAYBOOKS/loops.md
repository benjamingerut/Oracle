# Playbook: the loop engine (runbook)

Loops are how the oracle re-evaluates as information is added, contradicted,
or decays. A loop is a real runnable record in `Meta.nosync/Loops/` (schema:
`loop.schema.json`); every `status: active` loop MUST carry a `runner:` and
real `last_run`/`next_review` (lint fails otherwise).

```
./oracle loops list            # all loops + status
./oracle loops due             # what should run now (incl. unconsumed events)
./oracle loops run <id>        # execute one loop's runner, record the run
./oracle loops complete <id> --status ok|fail [--consume-all]
```

## The 12 active loops at spawn — what success looks like

| Loop | Runner | Verifies as |
|---|---|---|
| memory-matriculation | builtin | Captured sessions decomposed: records in `Meta.nosync/Sessions/` marked processed; new `needs_review` notes in Findings/Questions/Contradictions/Queries; derived recall artifacts refreshed. |
| source-capture | agent-worklist | New material in `_INPUT` (or due connector pulls) ingested as Sources; nothing pending in `_INPUT` without a Source record. |
| workproduct-io | agent-worklist | `_INPUT` routed to lanes; promised `_OUTPUT`/`_STANDING` artifacts produced; registries render clean. |
| user-feedback-learning | builtin | Pending feedback/value/failure events consumed into User-Model/Improvement notes (incl. the structured `preferences:` counters); `loops due` no longer lists the event backlog. |
| skill-repository-learning | builtin | Recurring procedural signals turned into oracle-local skill updates (`./oracle skills list` shows them); events consumed. |
| insight-synthesis | builtin | Worklist emitted; for each item a Model was proposed/updated/revalidated (status `needs_review`, `last_validated` stamped). Empty worklist = healthy no-op. |
| leadership-briefing | builtin | A dated brief exists in `_STANDING/` with a fresh registry row; enrichment + delivery done or handed off explicitly. |
| value-scorecard | builtin | A dated, drop_id-cited scorecard exists for the window in `Value-Scorecards/` with an explicit trend verdict vs the prior one. |
| improvement-lifecycle | builtin | Applied improvements adjudicated against event ledgers (verified/regressed); no proposal aging silently; worklist items decided. |
| meta-health | builtin | Repeat-failing loops paused (visibly); no unconsumed signal past its age budget; skill/autonomy hygiene candidates decided. |
| stale-finding-refresh | builtin | No confirmed finding past its staleness budget without re-validation, supersession, or retirement. |
| architecture-retrospective | builtin | Within cadence (or after a regression trigger): a Retrospectives/ note with a verdict; changes filed as ready-to-approve Improvements/ADRs. |

Builtin runners do the deterministic half and may hand back a worklist — the
agent half. A run that returns a worklist is not finished until the worklist
is done and `loops complete` is called (agent-worklist loops never
auto-record).

## Debugging a loop

- `./oracle loops list` — is it `active`? Only active loops run.
- `./oracle loops due` — the `reason` says why it's due (cadence, never-run,
  event backlog).
- A `fail` result carries `error`; the run row is in
  `Meta.nosync/ledgers/loop_runs.jsonl` (`./oracle ledger render` to view).
- Events not clearing? They consume per-loop via
  `Meta.nosync/ledgers/event_consumption.jsonl`; complete with
  `--consume-all` only after actually processing them.

## Creating or activating a loop

Create a loop when a process benefits from repeated re-evaluation (new
information, decay, contradiction). Copy `Meta.nosync/Loops/loop-template.md`,
fill cadence/trigger/process/health signals, set `status: proposed` with the
*intended* runner. Activation (proposed → active, with real dates) is an admin
decision. Still proposed in the registry (activate as the oracle matures):
connector-health, schema-refresh, contradiction-resolution, question-review,
recommendation-adjudication, model-review, invariant-expansion,
routing-evolution, security-access-review, backup-restore-check.

A loop meta-health paused (`status: paused`, three consecutive failed runs)
stays visible in the Review Inbox; fix the cause, then set it back to active.
The toggle is `./oracle loops set-status <id> active|paused --reason "<why>"`
(the admin dashboard's Controls table prints it per loop).
The architecture-retrospective also becomes due EARLY on a regressing
scorecard, a paused loop, or a critical failure (`loops due` shows the
`regression-trigger-*` reason).

Model policy: deterministic code first; cheapest fully-capable model for agent
work; premium models only with documented complexity or admin approval
(`loops.loop_model_policy`).

## Headless / scheduled runs

`scheduler/` ships launchd/cron templates that invoke due loops headlessly.
Headless runs pass through the autonomy gate (`actions.py`): autonomy OFF,
kill-switch, allowlists, and blast-radius caps all deny before any side
effect (`DOCTRINE.md` §5). Autonomy grows by the evidence-gated ladder
(level 0→3): meta-health drafts a promotion when the scorecards earn it; the
admin approves with `./oracle admin autonomy promote`. Demotion on critical
failure is automatic. At level 2+, `harness.py --dream` convenes a bounded
headless agent session on the Review Inbox (outputs land `needs_review`).
