---
id: loop-memory-matriculation
type: loop
title: Memory matriculation
created: "{{DATE}}"
updated: "{{DATE}}"
sensitivity: internal
status: active
tags:
  - meta
  - loop
  - memory
cadence: every-session
runner: agent-worklist
last_run: "{{DATE}}"
next_review: "{{DATE}}"
trigger_conditions:
  - new material logged to Workproduct.nosync/_INPUT
  - a source record was created or superseded
  - a finding, question, or contradiction was added or changed
health_signal: green when no _INPUT row older than its cadence lacks a Source record
---

> This is a **concrete, lint-clean active loop record**, not a blank template.
> It models the `memory-matriculation` loop the oracle ships with. To create a
> new loop, copy this file, change the `id`/`title`/`tags`, set `status:
> proposed` until you have wired a real `runner` and it has run once, then
> promote to `active`. An `active` loop MUST keep `runner`, `last_run`, and
> `next_review` populated (real ISO dates) or `oracle_lint` will fail the build.

## Purpose

Keep company memory current and trustworthy. As new material arrives, decompose
it into the right behavioral-type notes, link them, verify the links, and
refresh anything that has decayed. This is the loop that turns raw intake into
matriculated, queryable, immutable-where-it-matters memory.

## Cadence

`every-session` — runs whenever the oracle is engaged and at the start of any
headless harness pass. The due-ness engine treats `every-session` as always-due
when there is unmatriculated `_INPUT`.

## Trigger Conditions

- New material logged to `Workproduct.nosync/_INPUT`.
- A source record was created or superseded.
- A finding, question, or contradiction was added or changed.

## Inputs

- `Workproduct.nosync/_INPUT/` and its `.registry.jsonl`.
- Existing `Memory.nosync/` notes (to link against and to detect supersession).
- The knowledge index at `_data.nosync/index/`.

## Process

1. List `_INPUT` rows that lack a `Memory.nosync/Sources/` record.
2. For each, run the ingestion pipeline (extract → chunk → index →
   immutable Source record) via `_tools/ingest_pipeline.py`.
3. Emit review-gated Finding / Question / Contradiction candidates
   (`status: needs_review`) — never auto-trust.
4. Link new notes to existing entities, sources, and contradictions.
5. Verify on-disk content hashes against the ledger; flag any mismatch for
   supersession rather than silent edit.

## Outputs

- New immutable `Sources/` records registered with a content hash.
- Review-gated candidate notes for human/agent triage.
- A `loop_runs` ledger row recording status, `last_run`, and `next_review`.

## Runner

`agent-worklist` — the agent works the structured worklist above. The
deterministic steps (1, 2, 5) call `_tools/` modules directly; steps 3-4 require
judgment and are agent-driven. `loops.run` dispatches this loop and `loops.record`
appends the `loop_runs` row.

## Health Signal

Green when no `_INPUT` row older than this loop's cadence lacks a corresponding
`Sources/` record, and no on-disk/ledger hash mismatch is outstanding. Amber when
a backlog exists; red when matriculation has not run despite due triggers.
