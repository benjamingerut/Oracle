# Retrospectives

Periodic reviews of whether the oracle's **architecture itself** should change —
not whether a single answer was good, but whether the shape of the system is
still right. This is the output of the `architecture-retrospective` loop.

## What a retrospective examines

- Are the loops the right loops? Any that should be created, promoted, paused, or
  retired?
- Are the behavioral types and ontology still fitting the work, or is there
  schema / definition debt?
- Are the chokepoints (`safe_paths`, `policy`, `actions`, `answer_protocol`)
  holding? Any guarantee now advisory that should be enforced?
- What do the period's `failure_event` rows say about systemic weakness?
- Is the oracle delivering value (cross-reference the latest value-scorecard)?

## What a good retrospective produces

- A clear verdict: architecture stays / evolves, with reasons.
- Concrete `Improvements/` notes and, where the shape changes, an
  `architecture_decision` (ADR).
- Cited evidence — `failure_event` / `value_event` / `feedback_event` drop_ids,
  not impressions.

## Discipline

Retrospectives are how the oracle avoids drifting from a known-good baseline.
Reference the last full-system-working state and improve from it; surface any
regression explicitly. Architecture changes that move canonical files, rename
mature folders, delete categories, or change schema require admin approval.

## Type

`type: retrospective`. Usually `internal`.
