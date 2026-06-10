# Autonomy

This folder governs whether — and how far — the oracle may act on its own,
between sessions or headless. Autonomy is the highest-blast-radius capability the
oracle has, so it ships **OFF** and is gated by three independent mechanisms.

## The three gates (defense in depth)

1. **Allowlist** (`autonomy.yml`) — an explicit, admin-curated list of which
   loops may run headless, which lanes may be written, and which read-only
   connectors may be pulled. Empty by default, so nothing is permitted. Adding an
   entry is a deliberate admin act.
2. **Kill-switch** (`KILL-SWITCH` sentinel file) — the presence of a file named
   exactly `KILL-SWITCH` in this folder is a hard stop. `actions.py` checks for
   it **first**, before reading anything else. It overrides `enabled: true` and
   every allowlist entry. To halt the oracle instantly:
   `touch Meta.nosync/Autonomy/KILL-SWITCH`. To resume, delete the file.
   `KILL-SWITCH.example` documents this — it is an example, not the live switch.
3. **Blast-radius caps** — even an allowed action is bounded by
   `max_files_per_run` and `max_bytes`. Exceeding a cap aborts the run and logs a
   `failure_event`.

## The action_event log

Every autonomous action passes through `actions.with_action(...)`, which appends
an `action_event` row to `Meta.nosync/ledgers/action_event.jsonl` **twice**: once
with `phase: intended` (before doing anything) and once with `phase: actual`
(after). The ledger row carries metadata only — `action`, `scope`, `caps`,
`result` — never payloads or secrets. This gives a complete, durable audit trail
of everything the oracle did unattended.

## Actor identity — honest limitation

The `--actor` identity an action runs under comes from a flag, not from a proven
session context. It is **advisory-plus-logged**: it is recorded faithfully in the
`action_event` row but is not cryptographically verified. The real enforcement
boundary is the kill-switch + allowlist + caps, not the actor string. See
`DOCTRINE.md` §5 for the full caveat.

## The graduated ladder (trust earned by ledger)

`autonomy.yml` carries `level: 0..3`:

- **0** — nothing headless (spawn default).
- **1** — the deterministic builtin loops may run headless on schedule.
- **2** — + dream sessions (`harness.py --dream`): the scheduler convenes the
  agent on a bounded Review-Inbox charter as actor `system:dream` with the
  USER capability set; everything it derives lands `needs_review`.
- **3** — + auto-apply for enumerated low-risk improvement classes only.

Promotion is **earned**: `meta-health` drafts a proposal only when the
criteria hold (two non-regressing scorecards, zero critical failures, zero
cap/containment violations — all cited by drop_id), and the admin applies it
with `./oracle admin autonomy promote` (requires `enable_autonomy`; refused
without a pending proposal). Demotion is **automatic and fail-closed**
(`actions.enforce_demotion_policy`): a critical failure_event, cap breach, or
granted-then-failed action drops the level by one immediately and surfaces in
the Review Inbox. Truth authority, schema, doctrine, security policy, exports,
and connectors stay admin-only at every level. Every transition is a row in
`Meta.nosync/ledgers/autonomy_event.jsonl`.

## Default state

`enabled: false`, `level: 0`, all allowlists empty. In this state `harness.py`
runs zero loops and `actions.authorize()` denies everything. The oracle is
fully usable interactively; it simply does nothing unattended until promotion
is earned and admin-approved.
