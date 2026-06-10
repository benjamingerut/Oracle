---
id: "fnd-{{DATE}}-rename-me"
type: finding
title: <the claim, in one line>
created: "{{DATE}}"
updated: "{{DATE}}"
sensitivity: confidential
status: needs_review
claim_tier: OBS
confidence: 0.5
evidence: <link to the Source(s) that support this claim>
decision_relevance: <which decision or model this claim informs>
disconfirmer: <what observation would overturn this claim>
as_of: "{{DATE}}"
source_id: <id of the Source this was derived from>
evidence_offsets: <char offsets into the source, if from ingestion>
tags:
  - finding
---

## Claim

<State the claim precisely.>

## Claim tier

`claim_tier:` must be one of: OBS (directly observed), INF (inferred from evidence), SPEC (speculative), SPEC-horizon (long-range speculation).

## Evidence

- <Link the Source(s); cite offsets if from ingestion.>

## Confidence

- <State as a range, e.g. "0.4-0.6", and explain what drives the uncertainty. The `confidence:` field above is the point estimate within that range.>

## Disconfirmer

- <The specific observation that would force this finding to be superseded or retired.>

## Decision relevance

- <The decision or model that depends on this claim.>

## Supersession

- <If this corrects an earlier finding, set `supersedes:` above and link it here.>
