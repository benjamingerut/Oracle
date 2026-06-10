# Analysis.nosync

Durable but **non-canonical** workbench for exploratory analysis, intermediate joins, drafts, QA, charts, and reproducibility notes.

This is the oracle's thinking space — distinct from `Workproduct.nosync/` (the canonical document store) and `Memory.nosync/` (atomic durable claims). Work happens here; conclusions graduate elsewhere.

## Discipline

- **Not safe to delete without review** — unlike `tmp.nosync/`, work here may be load-bearing for a finding or contradiction in flight.
- **Durable claims must matriculate into memory.** An analysis that produces a finding, contradiction, or model writes that record into `Memory.nosync/` (review-gated, `status: needs_review`); the workbench file is the working note, not the institutional record.
- **Final artifacts go through workproduct I/O.** A finished deliverable is emitted via `artifact_io.emit` under the policy gate, not copied by hand out of here.
- Keep reproducibility notes alongside intermediates so an analysis can be re-run and audited.
