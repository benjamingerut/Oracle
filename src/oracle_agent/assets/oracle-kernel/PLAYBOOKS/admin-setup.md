# Playbook: admin control plane

Everything here requires the Admin interface (ask for approval explicitly) and
most actions require admin capabilities via `policy.require_role`
(`DOCTRINE.md` §3). The `--actor` flag is provenance, not authentication.

## The admin dashboard (start here)

`./oracle dashboard` is the one screen for admin oversight: every subsystem's
health glyph (memory, authority, loops, autonomy, signals, scorecard, backup,
...), the per-loop on/off table, and a Controls table mapping each toggle to
the exact command that flips it (`./oracle loops set-status <id> active|paused`,
`./oracle admin autonomy promote|demote`, the kill switch). It is read-only:
every mutation routes through the gated verbs it prints. `publish` renders a
self-contained HTML view into `dashboards.nosync/`; panel order/visibility
evolves via `dashboards.nosync/layout.yml` (see that folder's `_CONTEXT.md`).
Its Portability panel is the migration view: machine-local facts (resolved
root — shown in-session, never stored), the repo relocatability scan (the
`external-path` lint), and every machine coupling to re-wire after a move
(installed scheduler incl. stale-root detection, `.env.nosync`, host
binaries, connector sources).

## Truth-map authority (the heart of grounding)

```
./oracle admin truth rows                  # the parsed map
./oracle admin truth validate              # per-row: authority, evidence, freshness, next step
./oracle admin truth propose --object "<obj>" --source "<authority of record>"
./oracle admin truth promote --object "<obj>" --actor <admin> [--because "..."]
```

- `propose` is idempotent: it creates a draft row, or sets a real source on a
  TBD row. Ingest auto-proposes rows when evidence names an object.
- `promote` flips draft → confirmed. It requires `change_truth_authority` and
  at least one ingested Source resolving to the authority (override with
  `--no-evidence-check` only with cause). Confirmed + fresh evidence = answers
  ground at exit 0. Every change lands in the truth_map ledger.
- Before promoting: verify the source actually is the system of record and the
  join keys hold against live data.

## Bootstrap sequence (fresh oracle → useful oracle)

1. Record the admin directive (authority to operate) — a Directive note.
2. `./oracle ingest <seed material>` with `--business-object`/`--source-system`
   per batch. Watch `./oracle status` climb the maturity ladder.
3. Work `./oracle review` (wire candidates, promote evidenced rows).
4. Customize `oracle.yml`: roles, ontology subtypes, lanes, review budgets.
5. Connectorize live systems (below). 6. Set backup posture
   (`BACKUP-RECOVERY.md`, `./oracle admin backup`). 7. Keep `./oracle check`
   green; update `BOOTSTRAP-STATUS.md` honestly.

## Connectors

A manifest under `Connectors/<id>/` (template provided) declares the source
system, `access_mode`, permissions (read-only default), locality/capture
tier, and freshness SLA. `folder`-mode pulls run deterministically
(`localfolder` reference connector); `api`/`mcp`/`cli` pulls are executed by
the operating agent per the manifest's documented steps, feeding
`./oracle ingest --connector <id>`. Installing or authorizing requires
`install_connector`/`approve_connector`. Health: `./oracle connector health`.

## Policy, exports, sensitivity

- Processing matrix and export gate: `DOCTRINE.md` §2;
  `./oracle admin policy check --sensitivity <s> --environment <e>`,
  `./oracle admin policy export ...` (approval reference required above
  internal).
- Changing the matrix or role lists is a security-policy change
  (`change_security_policy`) — edit `oracle.yml`/`policy.py` deliberately and
  keep DOCTRINE.md's table mirroring the code (lint holds them together).

## Autonomy (OFF by default — keep it that way until mature)

Enable only when: the oracle is at maturity rung 2+, loops have run cleanly
under supervision for a while, and backups verify. Then:

1. Edit `Meta.nosync/Autonomy/autonomy.yml`: `enabled: true`, explicit
   `allowed_loops`, `writable_lanes`, `readonly_connectors`, conservative
   `blast_radius_caps`.
2. Install the scheduler (`scheduler/install_schedule.sh`) for headless runs.
3. Test the kill switch: `./oracle actions kill` must stop everything;
   `resume` restores. Requires `enable_autonomy`.

## Backup / upgrade

- `./oracle admin backup run --tier 0 --dest <dir>`; prove recoverability with
  `verify-restore` (stamps `BACKUP-RECOVERY.md`). An unproven backup is RED in
  `./oracle check`.
- Kernel upgrades are tool-layer-only: `./oracle admin upgrade apply` verifies
  the manifest hash and never touches Memory/Meta/doctrine. Requires
  `approve_kernel_upgrade`.

## Verification

`./oracle check` = deep audit + schema/doctrine/secret lint. Run after any
control-plane change. `known-failures.txt` baselines accepted failures; a NEW
violation always fails.
