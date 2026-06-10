---
id: "con-{{DATE}}-rename-me"
type: contradiction
title: <short name of the conflict>
created: "{{DATE}}"
updated: "{{DATE}}"
sensitivity: confidential
status: open
severity: medium
classification: watch
claims_in_conflict: <claim A vs claim B, each with its source>
possible_causes: <grain mismatch, timing, definition drift, data error, etc.>
resolution_plan: <how the oracle intends to resolve this>
decision_relevance: <which decision this conflict affects, or "none yet">
tags:
  - contradiction
---

## The conflict

- Claim A: <statement> — source: <link to Source/Finding>
- Claim B: <statement> — source: <link to Source/Finding>

## Possible causes

- <Hypothesis for why these disagree.>

## Severity and classification

- Severity: <low | medium | high | critical>
- Classification: <must_resolve | bounded_residual | watch | schema_debt>
- Decision relevance: <what depends on resolving this>

## Resolution plan

- <Concrete next step to resolve or bound this contradiction.>

## Resolution

- <When resolved: set status, link resolving_source, and explain why one side won.>

## Change log

- {{DATE}} — created at bootstrap.
