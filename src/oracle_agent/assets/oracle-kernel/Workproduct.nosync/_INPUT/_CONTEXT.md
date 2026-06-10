# _INPUT

Human-facing inbound drop. Files placed here are **transient** until they are logged and ingested — do not leave files loose.

## Flow

1. `./oracle artifact scan` (or `_tools/artifact_io.py --root R scan`) lists loose drops.
2. `./oracle artifact log --file F --sensitivity S` records the drop in `REGISTRY.md` (rendered from the append-only ledger). `--sensitivity` is **required** — sensitivity is set at log time, never inferred silently later.
3. `./oracle artifact ingest --file F --lane L --slug S` moves the file into its routing lane's `received/` folder via `safe_copy_verify_delete` (copy → fsync → sha256-verify → delete-source). The `--lane` is validated against `oracle.yml` routing_lanes; an out-of-allowlist lane (e.g. `../../ESCAPE`) is rejected and nothing is written.

## Invariants

- A file must already be **inside** `_INPUT/` to be ingested; `ingest` of a source outside `_INPUT/` is refused.
- Ingest is non-destructive: a failed or escaping write leaves the original `_INPUT/` file intact.
- The drop ledger lives at `_INPUT/.registry.jsonl` (the durable append-only record). `REGISTRY.md` is a rendered view of it — do not hand-edit the table.
