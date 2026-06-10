# Backup and Recovery

Backups are architecture, and a backup you have never restored is a hope, not a
backup. Recoverability is **provable**:
`_tools/backup.py verify-restore` performs a genuine round-trip — back up to a
temp tree, restore into a *second* temp tree, hash-diff every file — and only on
a clean diff stamps the `last_verified_restore:` line below. The claim "we can
recover" is therefore earned, not asserted.

Configure live backup destinations only after admin approval (an
`enable`-class architecture change; see `DOCTRINE.md` §3).

## Tiers

These tiers mirror `_tools/backup.py` exactly (`TIER_DIRS` + `TIER0_FILES_GLOB`).
Tier membership is a constant internal location set, never user-derived.

- **Tier 0 — control plane.** `oracle.yml`, root `*.md` docs, `.gitignore`,
  `.env.example`, `load-env.sh`, `Memory.nosync/`, `Meta.nosync/` (including the
  tracked `ledgers/`), `Connectors/`, `_tools/`. Lose this and the oracle's
  identity, memory, and policy are gone — Tier 0 is the highest priority.
- **Tier 1 — artifacts.** `Workproduct.nosync/`, `Analysis.nosync/`,
  `dashboards.nosync/`. The produced work; valuable, regenerable with effort.
- **Tier 2 — raw data.** `_data.nosync/` (derived FTS index and bulk material).
  **Admin decision required** — it can be large and is often rebuildable.
- **Tier 3 — secrets.** **Never plaintext** — enforced by `backup.py`:
  `.env.nosync`, any `KILL-SWITCH` payload, and `*.pem` / `*.key` files are
  EXCLUDED from every tier, and `backup.py`
  refuses to write secret-tier bytes in the clear. Secrets are re-provisioned
  from their source of truth (a password manager / KMS), never from a backup.
  An encrypted secret backup is permitted only if explicitly configured by an
  admin with a managed key.

## Non-destructive by construction

Every backup copy is **copy → fsync → sha256-verify** (never a bare move); the
source is always preserved. This is the same durable-copy primitive as
`safe_paths.safe_copy_verify_delete`, minus the delete step. A failed or
escaping backup write can never destroy an original.

## Restore-verify (the real check)

```
python3 _tools/backup.py --root <root> run [--tier all|0|1|2] --dest <dir>
python3 _tools/backup.py --root <root> verify-restore [--tier all|0|1|2] [--keep]
```

`verify-restore` is the loop-bearing command. On a clean round-trip it updates
the `last_verified_restore:` line in this file with the timestamp and the count
of files proven. If any file's restored hash differs from its source, the verify
FAILS, the line is **not** advanced, and the `backup-restore-check` loop reports
the failure.

## Wired to the backup-restore-check loop

`backup-restore-check` (see `PLAYBOOKS/loops.md`, cadence `monthly`, intended runner
`backup:run_restore_check_loop`) exists so recoverability is re-proven on a
schedule rather than assumed. Until it is promoted to `active` by an admin, run
`verify-restore` manually. `setup_audit` treats a never-populated
`last_verified_restore` as RED unless backup has been explicitly deferred by the
admin (a `pending`/`deferred` marker is accepted as an honest "not yet").

## Policy fields

Backup policy is recorded here once chosen. Block style only; no real secret
values ever appear in this file.

```yaml
backup_mode:
cadence:
destination:
encryption:
admin_approval:
```

## Verification stamp

This line is read and updated by `_tools/backup.py verify-restore`. The sentinel
value below means recoverability has **not yet been proven** on this oracle.

last_verified_restore: never — run `_tools/backup.py verify-restore` to prove it
