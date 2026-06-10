# Workproduct.nosync

Canonical artifact store for whole files the oracle **receives** or **creates** — the document layer, distinct from `Memory.nosync/` (atomic durable claims) and `Analysis.nosync/` (the exploratory workbench).

## Layout

- `_INPUT/` — human-facing inbound drop. Loose files land here and are transient until logged + ingested.
- `_OUTPUT/` — human-facing outbound pickup. Duplicates of canonical artifacts produced by the oracle.
- `_STANDING/` — home for standing (recurring, cadenced) deliverables emitted by `standing_deliverables.py`.
- `00_…` through `07_…` — routing lanes. Each lane holds `received/` (inbound artifacts) and `created/` (oracle-produced artifacts), created at runtime when first used.

## Routing lanes are the containment surface

The lane list in `oracle.yml` under `workproduct.routing_lanes` is the **single source of truth**. `safe_paths.assert_lane(root, lane)` validates every Workproduct write against it; a write to a lane not on that list is rejected. Lanes are backend organization and may evolve, but only by editing `oracle.yml` — never by writing outside the allowlist.

## Discipline

- Use `_tools/artifact_io.py` (or `oracle artifact …`) to `scan`, `log`, `ingest`, `emit`, and `render` registries. Never hand-move files between lanes.
- All file moves are non-destructive: ingest/emit copy → fsync → sha256-verify → delete-source (`safe_copy_verify_delete`), never a bare move, so a failed write can never destroy the original.
- Significant artifacts should generate a `Sources/` record and decompose their durable claims into `Memory.nosync/`. The artifact is the document; memory is what the oracle actually reasons over.
- Receiving and creating are the only two operations on a lane; both are logged in a registry.
