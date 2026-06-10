---
id: "src-{{DATE}}-rename-me"
type: source
title: <what this evidence is>
created: "{{DATE}}"
updated: "{{DATE}}"
sensitivity: confidential
status: active
as_of: "{{DATE}}"
content_sha256: <filled by source_record.py at registration>
tags:
  - source
---

## Provenance

- Origin: <connector pull / document / testimony / meeting / schema snapshot / web capture / manual observation>
- Connector or system: <link to Connectors/ or Systems/, if applicable>
- Raw location: <path under _data.nosync/ or Workproduct.nosync/_INPUT/>
- As-of: {{DATE}}

## Grain card

- What one record means: <the unit of observation>
- Time basis: <as-of, point-in-time, cumulative, etc.>
- Coverage and gaps: <what is and is NOT in this evidence>

## Integrity

- content_sha256: <hash registered in the Sources ledger; do not edit the bytes after registration>

## Notes

<Any context needed to read this evidence correctly. Immutable: to update, write a new source and supersede this one.>
