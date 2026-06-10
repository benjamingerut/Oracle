# Bootstrap Status

Generated: {{DATE}} for **{{COMPANY_NAME}}** (admin: **{{ADMIN_NAME}}**).

This file states, honestly, **how mature this oracle is** — so that "installed and safe"
is never mistaken for "done and valuable." A freshly spawned oracle is **inert but safe**:
the floor is real and test-green, but it knows nothing about the company yet and takes no
autonomous action. Read the maturity ladder before you trust an answer.

## Maturity ladder (where this oracle is, explicitly)

The oracle climbs these rungs in order. It is **inert-but-safe** at spawn and only becomes
**productive** after material is ingested and truth-map rows are confirmed. Do not skip a
rung; each lower rung is a precondition for the one above.

| Rung | Name | Meaning | How to verify |
|---|---|---|---|
| 0 | **Inert-but-safe** | Floor installed; containment, ledger, policy, secret-scan, lint all green. Knows nothing about the company. Autonomy OFF. | `make check` green; `./oracle audit` + `./oracle lint` PASS |
| 1 | **Configured** | `oracle.yml` confirmed (roles, lanes, ontology), kernel version stamped, 7 active loops are real runnable records. | `./oracle audit` confirms loop runners + version stamp |
| 2 | **Seeded** | First material ingested; immutable Source records exist; evidence-backed answers ship as **supported (exit 2)** with the label. | `./oracle ingest ...` then `./oracle answer --object <X>` exits 2 |
| 3 | **Grounded** | Truth-map rows promoted to `confirmed` against live sources; the answer protocol grounds (exit 0). | `./oracle admin truth promote` then `./oracle answer --object <X>` exits 0 |
| 4 | **Productive** | Standing deliverables ship clean; contradictions tracked; recommendations adjudicated against observed decisions/value. | standing deliverables emit; scorecards populate |
| 5 | **Self-improving** | Feedback/value/failure events are captured and consumed by user-model, skill-repository, value-scorecard, and retrospective loops; improvements and local skills recorded. | `event_consumption`, `skill_event`, and `loop_runs` rows append; scorecards update from captured events |

> **Inert-but-safe ≠ done.** The floor passing its tests proves the oracle cannot lose or
> leak your data. It does **not** prove the oracle knows anything useful. Value begins at
> rung 2 and compounds upward. At rung 0–1 the answer protocol refuses material claims
> (exit 4) and tells you the exact commands that change the verdict; from the first
> ingest onward it answers **supported (exit 2)**, honestly labeled. `./oracle status`
> computes and reports the current rung every session.

## Installed (rung 0 — present and test-green at spawn)

- Root operating card + doctrine + playbooks (`AGENTS.md`, `CLAUDE.md`, `DOCTRINE.md`,
  `PLAYBOOKS/`, `ORACLE-ARCHITECTURE.md`).
- `oracle.yml` (block-style, schema-valid; kernel version + autonomy pointer present).
- Company memory schema (`Memory.nosync/`, behavioral types + subtype enum).
- Meta self-memory schema (`Meta.nosync/`, including `Sessions/` and the tracked `ledgers/`).
- Workproduct I/O folders (lanes + `_INPUT`/`_OUTPUT`/`_STANDING`).
- Connector registry scaffold + the `localfolder` reference connector.
- Security/governance doctrine, each guarantee wired to an enforcer or stamped advisory.
- Loop registry with the 7 active loops as real runnable records (`memory-matriculation`
  owns session/daily dreaming; `insight-synthesis` and `leadership-briefing` are the
  intelligence loops); no separate active `memory-dreaming` loop.
- The stdlib-only `_tools/` floor (containment, ledger, policy, secret-scan, lint, answer
  protocol) and its pytest suite.

## Provisional (real but not yet confirmed — climb to rung 2–3)

- Company kernel facts (identity, ownership, structure).
- Truth-map rows (ship as `draft`; flip to `confirmed` against live sources).
- Workproduct routing lanes (`routing_status: provisional`).
- Connector manifests (the reference connector aside).
- Initial models / questions / contradictions.
- Backup policy (`last_verified_restore` empty until a real restore round-trip runs).
- Scheduled loops (installed DISABLED; not yet enabled).

## Needs Admin (gates a human must clear)

- Confirm the user/admin roster (`governance.roles`).
- Confirm the processing matrix (`DOCTRINE.md` §2 == `policy.check_processing`).
- Approve the connector list and credentials (secrets in `.env.nosync` only).
- Approve the backup policy and run the first verify-restore.
- Approve any external processing environments and the retention/export policy.
- **Enable autonomy** — see below.

## Autonomy state: OFF

Autonomy is **OFF by default** and ships last on purpose: a headless action chokepoint on
top of any path bug would be an automated exfiltration engine, so it stays dark until the
floor is proven and an admin opts in.

- `Meta.nosync/Autonomy/autonomy.yml` ships with `enabled: false` and empty allowlists. In
  that state `harness.py` runs **zero** loops headless.
- A `Meta.nosync/Autonomy/KILL-SWITCH` file, if present, is checked **first** by
  `actions.py` and hard-stops everything regardless of config.
- Enabling autonomy is an **admin-only** capability (`enable_autonomy` in
  `governance.roles`), requires editing `autonomy.yml` (allowed loops, writable lanes,
  read-only connectors, blast-radius caps), and every autonomous side effect is logged to
  the `action_event` ledger.

## Next Actions

1. Run `./oracle audit` and `./oracle lint` — confirm both PASS for real (a hand-corrupted
   `oracle.yml`, a missing loop runner, or an ingested row lacking a Source record must
   turn them RED).
2. Ingest seed material (`./oracle ingest <files or folders>` with
   `--business-object`/`--source-system`) or create connector manifests.
3. Record the initial admin directive and the unresolved setup questions.
4. Draft source-specific schema maps for each wired source.
5. Work `./oracle review` and promote evidenced rows
   (`./oracle admin truth promote`) so answers climb from supported (exit 2) to
   grounded (exit 0).
6. Leave autonomy OFF until the floor is proven and an admin explicitly opts in.
