# Phase 1 — Foundation Hardening

**Prerequisite for every later phase.** Builds the scaffolding the correctness
and platform work stands on: the shell's doctrine→enforcer map, the evaluation
harness skeleton, a real kernel-upgrade path, and config versioning/migration +
shell-driven backup. No new user-facing capability ships here; what ships is
the ability to evolve safely.

Read first: `docs/roadmap/ROADMAP.md` (invariants INV-I1–I6), `docs/DESIGN.md`,
`docs/SPEC.md`, `docs/STRESS.md`.

> **Amended 2026-06-10** by the phase-opening stress pass (findings P1S-1…14,
> P1F-1…14, summarized at the end of this file). The original draft
> overclaimed what the kernel's `upgrade.py`/`backup.py` provide; every task
> below now states the real contract.

**Naming note:** ROADMAP invariants are cited as **INV-I1…I6**; STRESS.md's
implementation-phase findings as **STRESS-I1…I4**. They are different
namespaces.

## Goals

- G1. Every shell security/correctness guarantee names its enforcing test or
  lint, cross-checked in CI (extends kernel I6 discipline to the shell).
- G2. A test-fixture harness that can spin a spawned oracle + a scripted fake
  LLM + a fake gateway and assert end-to-end behavior — the substrate Phase 6
  grows into a scoring eval.
- G3. `oracle upgrade` — re-vendor a newer kernel into the package and apply
  the kernel's own `admin upgrade` to a registered root, hash-verified, never
  touching sovereign data.
- G4. `config.json` carries a schema version; loads migrate forward in memory;
  a corrupt/old config is clearly rejected with guidance, never silently
  mis-read and never clobbered.
- G5. `oracle backup` / `oracle restore` from the shell. Backup wraps the
  kernel's `backup.py run` per instance; restore is shell-owned,
  manifest-hash-verified, origin-bound, and path-contained. **Secrets are
  never archived** — not `.env`, not `.env.nosync`, no opt-in (fail closed;
  the kernel categorically refuses secrets in plaintext and the shell follows;
  key rotation, not key archival, is the recovery story). Profile backup
  covers `config.json` only.

## Frozen interfaces

### `oracle_agent/security_map.py` (new)
```python
GUARANTEES: list[Guarantee]   # each: id, statement, enforcer, kind
@dataclass
class Guarantee:
    id: str            # "SH-001"
    statement: str     # "external models never receive content above public"
    enforcer: str      # "tests/shell/test_verbtools.py::test_answer_above_ceiling_is_withheld"
    kind: str          # "test" | "lint" | "advisory"
ADVISORY_ALLOWED: frozenset[str]   # pinned ids permitted to be advisory
def verify_enforcers(repo_root: Path) -> list[str]   # returns violations
```
- `verify_enforcers` returns a violation for: a non-advisory guarantee whose
  enforcer is not a collected pytest node; an enforcer node marked
  skip/skipif/xfail; a `kind="lint"` enforcer that does not name a real
  Makefile gate target; a guarantee with `kind="advisory"` whose id is not in
  `ADVISORY_ALLOWED`. Guarantees derived from STRESS C*/H* findings are
  forbidden from `ADVISORY_ALLOWED` (P1S-10).
- A test (`test_security_map.py`) asserts `verify_enforcers()` is empty and
  that the rendered `docs/SECURITY.md` matches `GUARANTEES` (drift-tested).
  The pytest suite itself is the CI gate — `make check` runs it on every CI
  cell; no new workflow step is needed (P1F-14).

### `oracle_agent/testkit.py` (new — the eval substrate)
```python
@dataclass
class Harness:
    root: Path                 # a spawned oracle
    def chat(self, script: list[ScriptedResponse], surface="local",
             environment="local_agent") -> AgentLoop: ...
    def gateway(self, updates: list[dict], allowlist: dict) -> TelegramGateway: ...
class ScriptedResponse: ...    # converts to a real llm.client.ChatResponse
class FakeLLM:                 # records messages, replays a script, asserts on calls
    def assert_no_content_above(self, ceiling: str, order: list[str]) -> None
def spawn_test_root(dest: Path, name: str = "testco") -> Path   # pure helper, no pytest
```
- Ships in the package (Phase 6 builds on it) under constraints (P1S-11,
  P1F-11): module scope imports stdlib + `oracle_agent` only (extend
  `test_stdlib_only.py` to cover it); **no production module**
  (cli/builder/loop/serve/gateway/scheduler/config/doctor/wizard/spawn) may
  import `testkit` — enforced by a dedicated test and a `security_map`
  guarantee. The pytest fixture shim (session-scoped `spawned_root`,
  `pytest.skip`) STAYS in `tests/shell/conftest.py`; testkit exposes only the
  pure `spawn_test_root` helper. Harness consumers that mutate the root use
  per-test spawns.
- `ScriptedResponse` constructs real `llm.client.ChatResponse`/`ToolCall`
  objects (P1F-12). `Harness.chat(environment=...)` synthesizes the provider
  config (`http://127.0.0.1:1/v1` for `local_agent`) so the real
  `policy_bridge.environment_for` derivation is exercised, not bypassed.

### `oracle upgrade` (cli.py)
```
oracle upgrade [--check]            # per instance: equal | ahead | behind | diverged
                                    # (manifest aggregate hash + file diff, not just
                                    #  tools_version strings — P1F-5)
oracle upgrade kernel NAME [--approve ADMIN] [--force-downgrade]
oracle upgrade self --from-dir DIR  # maintainer re-vendor (git checkout only)
```
- `upgrade kernel` resolves `SRC` to the shell package's own vendored
  `src/oracle_agent/assets/oracle-kernel/` tree — never a user-supplied path.
  Before anything else it recomputes the vendored tree's hashes against the
  shipped `.kernel-manifest.json` and refuses on mismatch (P1S-2: catches a
  locally tampered/corrupted vendored tree; the distribution channel itself is
  the root of trust and is documented as out of automated scope).
- It first runs the kernel's `check`; if zero changed/added/removed, reports
  "already current" and exits without `apply` (P1F-4). Otherwise it runs
  `_tools/upgrade.py apply --from-kernel SRC --approve <admin>` with scrubbed
  env, printing the kernel's JSON verdict.
- **Approval source (P1S-7):** `<admin>` comes ONLY from the operator — the
  `--approve` flag, or an interactive TTY prompt. Never a constant, never
  config. Non-TTY without `--approve` refuses (the shell must not become the
  headless bypass of upgrade.py's never-headless guarantee).
- **Lock (P1S-13):** acquires the per-root lock non-blocking; if busy
  (serve running), refuses with "root busy — stop `oracle serve` or retry".
  Held for the whole apply (the kernel runs migrations+lint+tests inside).
  `--check` is lock-free.
- **Failure contract (P1S-8, P1F-2):** `upgrade.py apply` swaps `_tools/`
  BEFORE lint/tests and does NOT roll back on their failure; it leaves a
  timestamped copy in `Meta.nosync/tool-backups/<ts>/`. The shell is
  explicitly sanctioned (the one INV-I3 exception, mirror of the kernel's own
  backup) to perform a hash-verified copy-back from that backup dir when
  `apply` reports `ok:false` or raises mid-swap, then re-run `check` to prove
  recovery; if copy-back itself fails, print the backup path and the manual
  recovery command. A half-swapped root is detected by the post-recovery
  `check`.
- **Downgrade (P1S-12):** refuses when the vendored `tools_version` is older
  than the root's (and, since version strings may not move — see P1-T5 —
  also reports `diverged` when hashes differ with equal versions);
  `--force-downgrade` overrides with a printed warning.
- `upgrade self` is maintainer tooling and **refuses to run outside a git
  checkout** of this repo (P1S-1/2: the external trust anchor is git review —
  the re-rendered `.kernel-manifest.json` lands as a reviewable diff; in an
  installed package there is no such anchor, so the command is unavailable).
  It copies the kernel tree from `--from-dir`, diffs the incoming
  `oracle_lint.py` and `tests/` against the CURRENT vendored copies and
  requires explicit interactive confirmation if they changed (the gate code
  must not be silently replaced by the tree it gates), re-renders the
  manifest, and runs `make check` — which executes the CURRENT repo's gate.
  Lint/tests prove **conformance, not trustworthiness**; vetting kernel
  intent is the maintainer's manual code-review responsibility (stated, not
  implied).

### config versioning (config.py)
```python
CONFIG_VERSION = 2
MIGRATIONS: dict[int, Callable[[dict], dict]]   # key n migrates version n -> n+1
SECURITY_KEYS = (...)   # dotted paths: gateway.telegram.enabled/.allowlist/
                        # .max_sensitivity/.token_env, providers.*.api_key_env,
                        # ingest_roots, default_instance/provider
def load_config() -> dict
```
Pinned semantics (P1S-6, P1S-14, P1F-9):
- Version detection, future-version rejection, and migrations all operate on
  the **raw parsed JSON, before `_deep_merge` with `DEFAULT_CONFIG`**.
  `"version"` is NOT in `DEFAULT_CONFIG` (the merge must not stamp it).
- A raw config without `"version"` is v1; `version > CONFIG_VERSION` is
  rejected with guidance (fail closed, INV-I4). Migrations are pure and
  idempotent; key `n` maps n → n+1, applied in sequence.
- **Security-key preservation:** after migrating, every `SECURITY_KEYS` path
  present in the raw config must be present and unchanged in the migrated
  config (or changed only by a transform the migration documents by name);
  violation is a hard load error, never masked by the defaults merge.
- Load migrates **in-memory only** — it never writes. Persistence of the
  migrated form happens only when an explicit operator path saves
  (`save_config` stamps `CONFIG_VERSION`). A migrated config that then fails
  `save_config`'s secret-scan surfaces the error without touching the
  original file. A corrupt config is rejected with guidance and never
  overwritten or "repaired" in place.

### backup/restore (cli.py + new `oracle_agent/backup_shell.py`)
```
oracle backup [NAME] [--out DIR] [--tier TIER]   # default out: ~/.oracle/backups/<name>/<ts>/
oracle backup --profile                          # config.json only; NEVER .env
oracle restore NAME --from PATH [--allow-cross-origin] [--trust-archive]
```
- `backup` wraps `_tools/backup.py run --dest <out>` per instance under the
  per-root lock. Default `--out` is `~/.oracle/backups/<name>/<ts>/` (created
  0700). Default tier EXCLUDES the kernel's admin-decision-required
  `_data.nosync` tier (rebuildable index/derived data; `--tier all` includes
  it) (P1F-7). After the kernel run, the shell chmods every produced file
  0600 and directory 0700 — the kernel writes at umask and the spec invariant
  binds the shell (P1S-9, P1F-8). The shell then records
  `{instance, root, ts, dest, manifest_sha256}` in a profile-side index
  (`~/.oracle/backups/index.json`, 0600) — the external anchor for restore.
- `restore` is shell-owned (`backup_shell.py`; the kernel has only `run` and
  `verify-restore`). Order of checks, all fail-closed (INV-I4):
  1. Read `backup-manifest.json` from `--from`. If the backup is recorded in
     the profile index, its manifest hash must match (tamper-evident anchor);
     if it is NOT in the index (e.g. kernel-made backup), require the
     explicit `--trust-archive` flag (P1S-5).
  2. **Origin binding (P1S-3):** the manifest's recorded `root` must resolve
     to the same instance as `NAME`; otherwise refuse unless
     `--allow-cross-origin` is passed, which prints both roots and requires
     interactive confirmation.
  3. **Containment (P1S-4):** every rel path in the manifest is rejected if
     absolute or containing a `..` component, and must resolve strictly under
     the target root after normalization; first violation aborts the whole
     restore with nothing written.
  4. Copy file-by-file, verifying each file's sha256 against the manifest
     DURING the copy; any mismatch aborts (this — not `verify-restore` — is
     the tamper check; P1F-1).
  5. Optionally run the kernel's `verify-restore` afterwards as a self-check
     of the restored root (repositioned: it is a live-root round-trip test
     and cannot validate the archive).
  The whole restore runs under `root_lock(NAME)` acquired non-blocking
  (refuse if serve holds it) (P1S-13).

## Tasks

- **P1-T1 — SECURITY.md enforcer map.** Build `security_map.py` enumerating
  every shell guarantee implied by `STRESS.md` (C1–C3, H1–H4, M1–M5, L1–L3,
  STRESS-I1–I4) and `SPEC.md`, each pointing at its existing test. Generate
  `docs/SECURITY.md`. Add `test_security_map.py` per the frozen interface
  (incl. the skip/xfail/advisory-allowlist rules). *Acceptance:*
  `verify_enforcers()` empty; a guarantee pointed at a skipped or nonexistent
  test is reported; STRESS C*/H* guarantees cannot be advisory; rendered doc
  drift-tested. *Tests:* `test_security_map.py`. *Deps:* none.

- **P1-T2 — testkit.** Extract the fake LLM client, fake dispatcher, fake
  Telegram API, and a pure spawned-root helper into `oracle_agent/testkit.py`
  per the frozen interface; refactor existing shell tests to use it (no
  behavior change; the pytest fixture shim stays in conftest). Add
  `FakeLLM.assert_no_content_above`. *Acceptance:* all existing shell tests
  (159 at time of writing) pass unchanged through the kit; the assert helper
  catches a deliberately-planted leak in its own test; `test_stdlib_only.py`
  covers testkit; the no-production-import enforcer test passes. *Tests:*
  `test_testkit.py`. *Deps:* none.

- **P1-T3 — config versioning + migration.** Implement the pinned semantics
  above. *Acceptance:* a hand-written v1 fixture loads and migrates in
  memory; raw file untouched after load; unknown future version rejected with
  guidance; a migration that drops any present `SECURITY_KEYS` path is a hard
  error (test plants one deliberately); migrations idempotent (applying twice
  = once); corrupt config rejected, file not clobbered. *Tests:* extend
  `test_config.py`. *Deps:* none.

- **P1-T4 — `oracle upgrade --check` / `upgrade kernel`.** Per the frozen
  interface: vendored-tree self-verification, check-first short-circuit,
  operator-only approval, NB lock, sanctioned copy-back recovery, downgrade
  refusal, direction-aware `--check` (compare manifest aggregate hash and the
  kernel `check()` changed/added/removed sets, not just `tools_version`
  strings). Fix doctor's suggested-command path bug while here (doctor
  currently interpolates a version string where `--from-kernel` needs the
  vendored directory — `doctor.py:171-173`). *Acceptance:* against a spawned
  root, `--check` reports `equal`; a simulated older root (modified
  `.kernel-manifest.json` + one stale tool file) reports `behind` and `apply`
  upgrades it; a NEWER root refuses without `--force-downgrade`; apply
  failure (planted failing kernel test) triggers verified copy-back and
  post-recovery `check` green; non-TTY without `--approve` refuses; busy lock
  refuses. *Tests:* `test_upgrade.py`. *Deps:* none (P1F-10: the old T3 dep
  was wrong — kernel versioning is manifest-based, not config-based).

- **P1-T5 — `oracle upgrade self` (maintainer re-vendor).** Per the frozen
  interface: git-checkout-only, gate-code-diff confirmation, manifest
  re-render, full `make check`, refuses to leave a failing tree. Also: give
  `tools_version` a real source — a `KERNEL_VERSION` constant file in the
  kernel tree read by `manifest.render()` (today it is a hardcoded default
  that never moves, so version-only comparisons are meaningless — P1F-5);
  bump discipline documented as upstream kernel duty. *Acceptance:*
  re-vendoring the CURRENT kernel is a no-op by `aggregate_sha256` + `files`
  equality (the manifest's `generated` timestamp is excluded from the
  comparison — P1F-6) and stays green; a deliberately broken kernel is
  rejected; a tree with modified `oracle_lint.py` demands confirmation;
  running outside a git checkout refuses. *Tests:* `test_revendor.py`
  (guarded/skipped when not in a git checkout). *Deps:* none.

- **P1-T6 — shell backup/restore.** Implement `backup_shell.py` + CLI per the
  frozen interface: lock-held backup, 0600/0700 permission pass, profile
  index anchor, profile backup (config only, never `.env` — the kernel's
  `SECRET_NAME_TOKENS` filter does not match the exact name `.env`, so the
  shell maintains its own deny-exact-names list and a test proves `.env`
  never lands in any archive — P1S-9), and the five-step verified restore
  (index/trust gate → origin binding → containment → per-file hash → optional
  verify-restore self-check). *Acceptance:* backup then restore of a spawned
  root reproduces its ledgers/notes byte-for-byte; a tampered file in the
  archive is refused at step 4 with nothing partially applied beyond already-
  verified files (document the partial-copy contract: verification happens
  before each write, and any abort prints exactly what was already restored);
  restoring instance A's backup into instance B is refused; an archive entry
  with `..` or an absolute path is refused with nothing written; archive
  files are 0600; profile backup contains no `.env`. *Tests:*
  `test_backup_shell.py`. *Deps:* P1-T3 (profile index lives beside config;
  load/save discipline shared).

## Security invariants for this phase

- Upgrade never touches `oracle.yml` / `Memory.nosync` / `Meta.nosync` /
  doctrine — delegated to the kernel's upgrade (which structurally refuses
  paths outside `_tools/`). The shell passes through and does not reimplement
  — with ONE sanctioned exception: the hash-verified copy-back from the
  kernel's own `Meta.nosync/tool-backups/<ts>/` on a failed apply (P1S-8).
- Backup archives are potentially-sensitive: 0600 files / 0700 dirs, default
  destination under `~/.oracle/backups/`, never a world-readable temp, never
  logged path-with-contents. **No secret file is ever archived** (deny-exact
  `.env` plus the kernel's token filter).
- `upgrade self` is the ONLY shell path that writes into the vendored kernel
  tree, is maintainer-only, requires a git checkout, and is never reachable
  from chat/gateway/serve (enforced: it is not a kernel verb and no gateway
  surface maps to it; security_map carries the guarantee).
- The vendored `.kernel-manifest.json` is the local integrity reference for
  `upgrade kernel`; its changes are reviewable git diffs. Distribution-channel
  integrity (pip/git transport) is the actual root of trust and is documented
  as out of automated scope (P1S-1/2 honesty clause).

## Stress pass (done 2026-06-10 — before coding, as required)

Two adversarial reviews (security lens P1S-*, feasibility lens P1F-*) ran
against the original draft; all findings were adjudicated and folded into the
interfaces/tasks above. Summary of accepted findings and where each landed:

| ID | Sev | Finding (one line) | Resolution |
|----|-----|--------------------|------------|
| P1S-1 | CRIT | `upgrade self` hash-verify vacuous; hostile kernel passes its own lint/tests | self = git-checkout-only, gate-code diff confirmation, conformance≠trust stated (P1-T5) |
| P1S-2 | CRIT | No trust anchor on vendored manifest → supply chain into instances | committed manifest is the local anchor; `upgrade kernel` self-verifies vendored tree; channel integrity stamped out-of-scope (P1-T4, invariants) |
| P1S-3 | CRIT | Restore overwrites a different instance's root | origin binding via manifest `root` + `--allow-cross-origin` ceremony (P1-T6) |
| P1S-4 | CRIT | Restore trusts archive rel-paths → path escape | containment step 3: reject absolute/`..`, strict-under-root (P1-T6) |
| P1S-5 / P1F-1 | HIGH | `verify-restore` cannot detect tampering; acceptance was false | per-file manifest-hash verification during copy is the tamper check; profile index anchors the manifest; verify-restore repositioned (P1-T6) |
| P1S-6 / P1F-9 | HIGH | Migration can silently weaken security fields; `_deep_merge` masks drops | raw-first pipeline, SECURITY_KEYS preservation check, in-memory-only load (P1-T3) |
| P1S-7 | HIGH | `--approve` source unspecified → headless bypass | operator-only approval, non-TTY refusal (P1-T4) |
| P1S-8 / P1F-2 | HIGH | "rolls back" overclaimed; apply leaves broken/half-swapped tools | sanctioned verified copy-back from kernel tool-backups + post-recovery check (P1-T4, invariants) |
| P1S-9 / P1F-8 | HIGH | Archives world-readable; profile `.env` escapes kernel filter | shell chmod pass; secrets never archived, deny-exact `.env` (P1-T6, G5) |
| P1S-10 | HIGH | `verify_enforcers` gameable (skipped/empty/advisory-downgrade) | skip/xfail rejection + pinned ADVISORY_ALLOWED + C/H prohibition (P1-T1) |
| P1S-11 / P1F-11 | MED | testkit in package widens surface / breaks stdlib walk | stdlib-only module scope, no-production-import enforcer, fixture shim stays in conftest (P1-T2) |
| P1S-12 / P1F-5 | MED | No downgrade refusal; version strings never move; doctor passes version as path | hash+direction `--check`, downgrade refusal, KERNEL_VERSION source, doctor fix (P1-T4/T5) |
| P1S-13 | MED | Restore/upgrade vs running daemon races and stalls | NB lock acquisition with refusal on both paths (P1-T4/T6) |
| P1S-14 | MED | Migration auto-persist could brick/clobber configs | in-memory-only migration, explicit save, never-clobber (P1-T3) |
| P1F-3 | MED | "101 shell tests" stale | 159 at time of writing (P1-T2) |
| P1F-4 | MED | "apply is a clean no-op" false (always re-copies/re-tests) | check-first short-circuit in the shell (P1-T4) |
| P1F-6 | MED | Byte-identical re-vendor impossible (`generated` timestamp) | equality on `aggregate_sha256`+`files`, timestamp excluded (P1-T5) |
| P1F-7 | MED | Backup scope contradictions (profile path, `.env` opt-in vs kernel refusal, `--dest` required, tier default) | G5 reworded: no secret archival ever; default dest/tier pinned; `--profile` added (P1-T6) |
| P1F-10 | LOW | T4's dep on T3 was wrong | dep removed |
| P1F-12 | LOW | ScriptedResponse↔ChatResponse mapping unstated | conversion pinned (frozen interface) |
| P1F-13 | LOW | STRESS L1–L3 omitted; I-number collision | enumeration extended; INV-/STRESS- namespaces (P1-T1, header) |
| P1F-14 | LOW | "CI gate added" misleading | pytest suite IS the gate; stated in frozen interface |

## Definition of done

- [x] `docs/SECURITY.md` generated (57 guarantees); `verify_enforcers()` empty
      (incl. skip/xfail/advisory rules); suite-as-CI-gate documented.
- [x] `testkit.py` shipped and proven (planted-leak test); stdlib-only +
      no-production-import enforced. (Adoption note: conftest's spawned-root
      fixture and new tests use the kit; older test files keep their local
      fakes where refactoring risked node-id churn — acceptable, the kit is
      the substrate going forward.)
- [x] config migrates v1→v2 in memory; unknown version rejected; security-key
      preservation enforced; originals never clobbered.
- [x] `oracle upgrade --check / kernel / self` work as specced: direction-aware
      check, operator-only approval, copy-back recovery proven by a failure
      test, downgrade refusal, git-checkout-only self; KERNEL_VERSION sourced
      from the kernel tree.
- [x] `oracle backup/restore` round-trip verified end-to-end against a real
      spawned root through the real kernel subprocess; tampered file refused;
      cross-origin refused; path escape refused; no secret ever archived;
      0600/0700 enforced.
- [x] `make check` green locally (866 tests collected, 865 passed + 1
      skipped); new tests added. CI matrix
      confirmation pending next push (the suite is the gate on every cell).

**Phase 1 completed 2026-06-10.**
