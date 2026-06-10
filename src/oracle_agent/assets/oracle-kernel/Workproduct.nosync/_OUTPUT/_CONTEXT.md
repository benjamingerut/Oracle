# _OUTPUT

Human-facing outbound pickup. Files here are **duplicates** of canonical artifacts that live in a routing lane's `created/` folder (or in `_STANDING/` for recurring deliverables).

## Flow

`./oracle artifact emit --src F --lane L --slug S --sensitivity SENS [--classification C] [--approval REF]` produces the outbound copy. `emit` calls `policy.check` **before** anything lands in `_OUTPUT/` and appends an `export_event` to the ledger. Export of `confidential` / `restricted` / `secret` material is refused without admin approval; nothing is written on refusal.

## Invariants

- Do not auto-delete or silently refresh output copies. Emit a revised artifact as a **new version** (a new dated file) rather than overwriting.
- Every emit is gated and logged: the policy decision and the `export_event` row are the audit trail. The `export_event` carries metadata only (actor, role, classification, destination, approval, purpose) — never the payload.
- The emit ledger lives at `_OUTPUT/.registry.jsonl`. `REGISTRY.md` is a rendered view of it — do not hand-edit the table.
