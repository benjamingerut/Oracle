---
id: "rec-{{DATE}}-rename-me"
type: recommendation
title: <the recommended action, in one line>
created: "{{DATE}}"
updated: "{{DATE}}"
sensitivity: confidential
status: open
action: <the specific action being recommended — IMMUTABLE once set>
rationale: <why this action, given the evidence — IMMUTABLE>
evidence: <link to the Findings/Sources that support it — IMMUTABLE>
baseline: <the known-good state this improves on, never silently degrade from it — IMMUTABLE>
expected_signal: <what observable outcome would show this was right — IMMUTABLE>
adjudication:
  verdict: pending
tags:
  - recommendation
---

## Action

<Restate the recommended action. This and the four fields below are immutable; to revise, write a new recommendation and supersede this one.>

## Rationale

<Why this action follows from the evidence.>

## Evidence

- <Link the Findings/Sources backing this.>

## Baseline

<The last known-good state. Improve on it; never silently degrade from it.>

## Expected signal

<The observable outcome (a Decision taken, a value_event) that would confirm or contradict this. The adjudicator watches for it.>

## Adjudication (mutable)

- Verdict: pending | supported | contradicted | inconclusive
- Observed against: <Decisions/ and value_events the adjudicator matched>
