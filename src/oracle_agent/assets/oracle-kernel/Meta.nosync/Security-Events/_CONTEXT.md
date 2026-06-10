# Security-Events

Narrative notes that contextualize raw security-relevant ledger rows:
`export_event`, `redaction_event`, and `action_event`. A security-event note is
written when an export, a redaction, or an autonomous action deserves a recorded
explanation beyond the ledger metadata.

## Ledger vs note

The durable records are ledger rows under `Meta.nosync/ledgers/`:

- **`export_event`** — `{drop_id, ts, actor, role, classification, destination,
  approval, purpose}`. Written by `policy.gate_export` whenever something leaves
  the oracle. **Metadata only — never the exported payload.**
- **`redaction_event`** — `{drop_id, ts, actor, reason, approved_by, action,
  stub_location}`. Written by `policy.record_redaction`.
- **`action_event`** — `{drop_id, ts, action, scope, phase, caps, result}`.
  Written by `actions.with_action` before (`intended`) and after (`actual`) every
  autonomous action.

A note here is **optional** and links back to the relevant `drop_id`.

## Critical discipline — no secrets, no payloads

These ledgers are **tracked in git** (they live outside `*.nosync` coverage) so a
single-machine institutional store is backed up and recoverable. That is only
safe because the rows carry **metadata only**. Never write a secret value, an
exported payload, or confidential content into a security-event note or any
ledger row. Secrets live in `.env.nosync`, referenced by variable name. See
`DOCTRINE.md` §1 for the ledger-tracking sensitivity statement.

## Type

These notes use the matching event type (`export_event` / `redaction_event`).
Often `confidential` given what they reference.
