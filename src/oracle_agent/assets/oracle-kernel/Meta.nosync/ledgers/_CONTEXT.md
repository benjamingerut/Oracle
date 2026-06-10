# ledgers — the oracle's durable append-only registries

These `*.jsonl` files are the machine-readable spine of the oracle's
self-knowledge: every export, redaction, autonomous action, and loop run is
recorded here. They are written **only** through `_tools/ledger.py` (never
hand-edited) and read by the policy, actions, loops, and self-improvement layers.

## TRACKED in git, by design

Unlike the rest of `Meta.nosync/`, these ledgers are **tracked in git** — the
`.gitignore` carves them out of the `*.nosync` ignore so a single-machine
institutional store is backed up and recoverable. The Workproduct registries
(`_INPUT/.registry.jsonl`, `_OUTPUT/.registry.jsonl`) are tracked for the same
reason.

## METADATA ONLY — never payloads, never secrets

Tracking these in git is only safe because every row carries **metadata only**.
A ledger row never contains an exported payload, a document body, a credential,
or any confidential content. Secrets live exclusively in `.env.nosync`,
referenced by variable name. If you ever feel tempted to put content into a
ledger row, put it in the appropriate `Memory.nosync/` or `Workproduct.nosync/`
note instead and reference its id. (`oracle_lint` and the secret scanner guard
this; do not rely on them as an excuse to be careless.)

## The ledgers and their row shapes

Every row is one JSON object on one line, with at least `drop_id` (str) and `ts`
(ISO-8601 seconds).

| File | Written by | Row shape (keys) |
| --- | --- | --- |
| `export_event.jsonl` | `policy.gate_export` | `drop_id, ts, actor, role, classification, destination, approval, purpose` |
| `redaction_event.jsonl` | `policy.record_redaction` | `drop_id, ts, actor, reason, approved_by, action, stub_location` |
| `action_event.jsonl` | `actions.with_action` | `drop_id, ts, action, scope, phase, caps, result` |
| `loop_runs.jsonl` | `loops.record` | `drop_id, ts, loop_id, status, last_run, next_review, health_signal, notes` |

Notes:

- `export_event` has **no** payload/content field — metadata only.
- `action_event.phase` is `intended` (logged before the action) or `actual`
  (logged after); `result` and `caps` capture the outcome and blast-radius limits.
- `loop_runs.status` is `ok` or `fail`; the row updates the loop's `last_run` and
  `next_review`.

These files are created lazily on first append, so an empty folder at spawn is
expected and correct.

## Durability & corruption tolerance

`ledger.append` writes under `fcntl.flock` + `os.fsync` (no lost or interleaved
rows under concurrent writers). `ledger.load` is corruption-tolerant: a bad /
non-JSON line is moved to `<name>.jsonl.quarantine` and counted as a warning
rather than bricking the file. `next_id` mints collision-safe ids
(`PREFIX-YYYYMMDD-NNN`) under the same lock. Use the `ledger.py` CLI
(`verify` / `repair` / `render`) to inspect or fix a ledger; never edit by hand.
