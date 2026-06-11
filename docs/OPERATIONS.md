# Operations Runbook

The operator's guide to running an Oracle in production: the daily/weekly
rhythm, how to read `oracle doctor`, the backup/restore drill, the out-of-band
secret backup + rotation drill, the autonomy-promotion ceremony, the dream
cadence opt-in, the curator workflow, and the ledger-rotation verify check.

This file is operational. The binding security guarantees it leans on live in
`docs/SECURITY.md` (shell) and the kernel's `DOCTRINE.md` (kernel); this
document tells you which commands to run and what their output means, not what
the code promises.

Everything here uses the global `oracle` command. Instance resolution is:
explicit `NAME` > cwd inside a registered root > `default_instance` > the sole
instance > an error with guidance.

---

## 1. Daily / weekly rhythm

**Daily**

1. `oracle doctor` — one health pass over the install, the profile, every
   instance, and the provider. Read it top to bottom (§2). A `[fail]` is a
   stop-and-fix; a `[warn]` is a note-and-plan.
2. `oracle curate` — work the Review Inbox: each item arrives ranked, with a
   *prepared* disposition you approve one at a time (§7). Derived items stay
   `needs_review` until you decide; nothing auto-applies on a fresh install.
3. Confirm `oracle serve` is running (the scheduler daemon) if you rely on
   scheduled loops, gateway delivery, or dream convocation.

**Weekly**

1. Run a real backup and, periodically, a restore drill (§3). A backup you
   have never restored is a hope, not a backup.
2. Skim `oracle grounding-report` if you run forced grounding, to see what the
   model is asserting vs. grounding.
3. Verify the audit-critical ledger chains (§8) — cheap insurance that
   rotation has not silently dropped a segment.
4. `oracle upgrade --check` — per-instance kernel-version direction report;
   apply upgrades deliberately (never headless).

---

## 2. Reading `oracle doctor`

`oracle doctor [NAME]` prints `[ok]` / `[warn]` / `[fail]` lines, each with a
one-line fix. Exit code is non-zero iff any `[fail]` fired. Scope to one
instance with `oracle doctor NAME`.

What to look for:

- **Provider** — `provider env: <environment> (<base_url>)` names the
  classification (`local_agent` / `external`). `provider API key resolvable
  via <ENV>` confirms the credential is present. A `[fail]` on a non-HTTPS
  non-loopback endpoint means the model picker would refuse it.
- **API key resolvable** — if it reads `provider API key env '<ENV>' is
  unset`, the credential is missing from the resolution path; re-enter it (§4).
  This is the line you re-check after a secret rotation.
- **config holds no inline secrets** — `config.json` must hold only env-var
  *names*. A `[fail]` here means a literal secret leaked into config and must
  move to `.env` immediately.
- **Surfaces** — telegram / slack / email / http surface lines confirm each
  enabled gateway's token env resolves and its allowlist is sane. Email caps
  at `public` without a configured `authserv_id`.
- **Briefings** — a scheduled briefing target must resolve to an
  already-allowlisted private identity on its surface, or doctor refuses it
  (deny-by-default).

A clean `oracle doctor` (exit 0, no `[fail]`) is the green light for the day.

---

## 3. Backup and restore drill

Backups are tiered and **never contain secrets** (§4 covers the secret story):

```sh
oracle backup [NAME] [--tier 0|1|2|all] [--out DIR]
oracle backup --profile                 # config.json only
```

- Tier 0 (default) is the control plane: `oracle.yml`, root docs, the tracked
  `Memory.nosync/` and `Meta.nosync/` (including `ledgers/`), `Connectors/`,
  `_tools/`. Lose Tier 0 and the oracle's identity, memory, and policy are
  gone — back it up first and most often.
- Tier `all` adds `_data.nosync/` (the rebuildable FTS/derived index) — large;
  back it up on a schedule, not every run.
- Every copy is **copy → fsync → sha256-verify** (never a bare move); the
  source is always preserved. `oracle backup` takes the per-root lock
  non-blockingly and refuses if `oracle serve` is running on that root — stop
  serve (or retry) so a backup never races a live tick.

**Backups under serve.** There is no separate `backup schedule` verb; scheduled
backups ride `oracle serve`. Run periodic backups from your own scheduler (cron
/ launchd) invoking `oracle backup` while serve is paused, or pause serve in
its maintenance window. The lock refusal is the guardrail either way.

**Restore drill** (do this on a cadence, not just in a crisis):

```sh
oracle restore NAME --from PATH         # restore a tier from a backup dir
```

Restore is shell-owned and ends with an optional kernel `verify-restore`
self-check (a genuine round-trip: back up to temp, restore into a *second*
temp, hash-diff every file). A clean diff is the only proof that "we can
recover." After any restore, run `oracle doctor NAME` and re-provision secrets
(§4) — the restored tree carries no `.env`.

> A restore from an **untrusted** archive can never silently install a dream
> command or flip autonomy: `autonomy.yml` is not writable from any restore
> path, only via the admin-only `set-dream` verb (§5).

---

## 4. Out-of-band secret backup + rotation drill

Secrets are **never archived — no opt-in exists.** `backup_shell` refuses to
put `.env`/key material in any backup tier, and that hard rule is load-bearing
and tested. The recovery story for credentials is therefore **out-of-band**.

**Out-of-band secret backup.** Secrets live only in the instance root's
`.env.nosync` (0600) and the profile `.env` (0600), holding env-var *values*;
`config.json` / `oracle.yml` hold only the *names*. Back the secret values up
to your own source of truth — a password manager or KMS — never into an Oracle
backup. The env-var name is in config; the value is in your vault.

**Rotation / recovery drill** (re-enter the value, confirm doctor resolves it):

1. Provision the new credential at the provider (rotate the upstream key, mint
   a new token). The old upstream key stays valid until you revoke it *at the
   provider*.
2. Re-enter the value into the root's `.env.nosync`. The supported path is the
   wizard's secret-collection step (run `oracle setup` / re-run the relevant
   wizard step), which reads the value via `getpass` (never echoed) and writes
   it with `config.write_root_env_secret` — an atomic 0600 upsert that replaces
   the key in place, so the **old value is not retained** (no append, no second
   line). For a provider key, `oracle model set --key-env <ENV>` re-points the
   name; the value is re-entered the same way.
3. `oracle doctor NAME` — confirm `provider API key resolvable via <ENV>` (or
   the relevant surface token line) is `[ok]` again.
4. Revoke the old credential at the provider.

This is the full secret lifecycle: out-of-band backup, re-enter, doctor
confirms resolution, revoke old. No secret ever transits a backup archive.

---

## 5. Autonomy promotion ceremony

Autonomy ships **off** (level 0). The kernel enforces a graduated ladder
(`Meta.nosync/Autonomy/autonomy.yml`): 0 nothing headless / 1 deterministic
builtin loops / 2 + dream sessions / 3 + enumerated auto-apply classes.
Promotion is an **earned, admin-approved** kernel flow — never a config edit,
never headless:

```sh
oracle kernel NAME -- admin autonomy status        # current level + gate state
oracle kernel NAME -- admin autonomy promote        # requires a pending,
                                                     # evidence-cited proposal
```

- A level promotion **without** a pending evidence-cited proposal is refused by
  the kernel; proposals are drafted from ledger evidence, never self-applied.
- The kill-switch file (in the same Autonomy folder) is checked first at every
  level — a sovereign hard stop. `admin autonomy kill` / `resume`.
- A critical failure, blast-cap breach, or granted-then-failed action forces a
  one-level demotion automatically.
- Truth promotion, schema, security policy, exports, and connector changes stay
  **admin-only at every autonomy level** — autonomy never widens the
  control-plane.

Dream sessions (the self-improvement actuator) require **level 2**. Promote to
level 2 deliberately, and only after a clean failure history, before turning on
the dream cadence (§6).

### Configuring the dream actuator (`set-dream`)

The dream actuator command is the agent-harness invocation the kernel runs for
a dream session. It is **code execution for whoever writes it**, so it is
writable **only** through the constrained admin-only verb — never a raw
`autonomy.yml` edit, never from any model or gateway path:

```sh
oracle kernel NAME -- admin autonomy set-dream \
    --command "claude -p" --max-minutes 30 --max-inbox-items 10
```

The wizard's dream step does exactly this (it calls `set-dream`; it never
writes `autonomy.yml` and never raises the level). `set-dream` touches only the
`dream.*` subtree — it can never alter `level`, caps, or the kill-switch.

---

## 6. Dream cadence opt-in (unattended self-improvement)

Self-improvement **actuation ships, off by default**, and is unlocked by an
explicit admin autonomy promotion (§5) **plus** a cadence opt-in here. Two
independent switches must both be on:

1. **Autonomy ≥ level 2** (the ceremony in §5) — the kernel's `dream.session`
   authorize gate.
2. **A dream cadence** in `config.json`:

   ```jsonc
   "serve": { "tick_seconds": 300, "dream_tick_seconds": 0 }
   ```

   `dream_tick_seconds` defaults to **0 == convocation OFF**. A level-2 root
   still convenes *nothing* until you set a positive cadence here (e.g.
   `86400` for daily). This key only controls *timing* — every convocation is
   still independently autonomy-gated and per-root `LOCK_NB`-skipped, so it can
   never widen access and the safe direction is off.

When both are on, `oracle serve` convenes dream sessions on the cadence:
autonomy-gated (skipped below level 2), `LOCK_NB`-skipped when the root is busy
(never stalls the daemon), and the dream subprocess runs under the **narrow-env
contract** — exactly the one resolved `provider.api_key_env` credential crosses
into the agent harness; every other secret and every gateway token is scrubbed.

Everything a dream session derives lands `status: needs_review` and surfaces in
the Review Inbox — you still curate it (§7). Nothing auto-applies below
autonomy level 3.

To turn unattended improvement **off**, set `dream_tick_seconds` back to `0`
(timing off) or demote autonomy below level 2 (gate off). Either alone stops
convocation.

---

## 7. Curator workflow (working the Review Inbox)

```sh
oracle curate [NAME] [--prepare-only] [--limit N]
```

The curator lists the ranked Review Inbox, prints a *prepared* disposition per
item, and applies through existing kernel verbs only:

- **Apply is a fixed kind→verb mapping.** The curator **never** executes an
  item's free-text `action` string — item *kinds* map to allowlisted verbs
  pinned in code, with item fields filling value slots only. A poisoned item
  whose action text smuggles a command does nothing.
- **Control-plane items are never applyable.** Contradiction / promotable-row /
  authority-candidate / autonomy kinds yield no verb and stay
  Admin-interface-only; truth promotion and every control-plane change remain
  off the curator path. Unmapped kinds are default-denied.
- **Autonomy-gated.** Below the required autonomy level the curator *prepares*
  but never *applies* (`--prepare-only` forces this regardless). Above it, you
  approve each apply `y/N` interactively.
- **Ledgered attribution.** Every apply records the resolving local Principal
  (`local_user:<id>`) as `--actor`/`--role` — audit names who acted.

Typical loop: `oracle curate` → read each prepared disposition → approve the
ones you want → leave the rest in the inbox (`needs_review`).

---

## 8. Ledger rotation verify (`verify_chain`)

The audit-critical ledgers (`action_event`, `dream_session`, `gateway_event`)
rotate automatically: a segment seals at a size/age threshold under the same
append lock, ending with a rotation marker (no row may ever follow it), and the
`row_hash` chain re-anchors into a tamper-evident segment manifest with a
chained HEAD pointer.

Verify a ledger's integrity:

```sh
oracle kernel NAME -- ledger verify Meta.nosync/ledgers/<name>.jsonl
```

`verify` reports hash-chain breaks within a file; the manifest-driven
`verify_chain` (which `verify` consults for a rotated ledger set) discovers
segments via the manifest only — so a removed/renumbered **middle** segment AND
a removed **HEAD** segment are both detected, distinguishing "rotated here,
chain re-anchored" from "rows deleted." A non-zero exit is a tamper signal:
treat it as an incident, restore from a verified backup (§3), and investigate.

> Note: the P8 `retrieval_event-*` monthly ledger is **accepted best-effort
> search telemetry** with a fresh chain per file — a removed month there is not
> tamper-evident by design and is never the audit-critical precedent.

---

## 9. What to do when…

- **`oracle doctor` shows a `[fail]`** — fix the named cause before relying on
  the instance. Provider-key fails → re-enter the secret (§4). Non-HTTPS
  provider → fix the endpoint. Inline secret in config → move it to `.env`.
- **A secret leaked / must rotate** — run the rotation drill (§4): provision
  new at the provider, re-enter via the wizard secret step, `oracle doctor`
  confirms, revoke old.
- **Restore needed** — `oracle restore NAME --from PATH`, then re-provision
  secrets (§4) and `oracle doctor` (§3). Secrets are not in the archive.
- **`ledger verify` exits non-zero** — treat as a tamper/corruption incident:
  restore from a verified backup and investigate the chain break (§8).
- **Dream sessions are not running** — check both switches: autonomy ≥ 2 (§5)
  AND `dream_tick_seconds > 0` (§6), and that `oracle serve` is up.
- **A queue item looks malicious** — it is safe to curate: free-text action
  text is never executed and control-plane kinds are never applyable (§7).
  Leave it `needs_review` or resolve it through the proper admin flow.
- **`oracle backup` refuses (root busy)** — stop `oracle serve` on that root
  (or retry); the non-blocking lock prevents a backup racing a live tick (§3).

---

*Binding guarantees: `docs/SECURITY.md` (shell) and `DOCTRINE.md` (kernel).
This runbook tells you which commands to run; those files state what the code
promises.*
