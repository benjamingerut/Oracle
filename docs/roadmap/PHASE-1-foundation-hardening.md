# Phase 1 — Foundation Hardening

**Prerequisite for every later phase.** Builds the scaffolding the correctness
and platform work stands on: the shell's doctrine→enforcer map, the evaluation
harness skeleton, a real kernel-upgrade path, and config versioning/migration +
shell-driven backup. No new user-facing capability ships here; what ships is
the ability to evolve safely.

Read first: `docs/roadmap/ROADMAP.md` (invariants I1–I6), `docs/DESIGN.md`,
`docs/SPEC.md`, `docs/STRESS.md`.

## Goals

- G1. Every shell security/correctness guarantee names its enforcing test or
  lint, cross-checked in CI (extends kernel I6 to the shell).
- G2. A test-fixture harness that can spin a spawned oracle + a scripted fake
  LLM + a fake gateway and assert end-to-end behavior — the substrate Phase 6
  grows into a scoring eval.
- G3. `oracle upgrade` — re-vendor a newer kernel into the package and apply
  the kernel's own `admin upgrade` to a registered root, hash-verified, never
  touching sovereign data.
- G4. `config.json` carries a schema version; loads migrate forward; a
  corrupt/old config is repaired or clearly rejected, never silently mis-read.
- G5. `oracle backup` / `oracle restore` from the shell, wrapping the kernel's
  `backup.py`, covering the profile (config + .env policy) and each instance.

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
def verify_enforcers(repo_root: Path) -> list[str]   # returns unbacked guarantees
```
A test (`test_security_map.py`) asserts `verify_enforcers()` is empty: every
non-advisory guarantee resolves to a real, collected test node or lint rule.
A markdown `docs/SECURITY.md` is generated from `GUARANTEES` (rendered, checked
in, drift-tested).

### `oracle_agent/testkit.py` (new — the eval substrate)
```python
@dataclass
class Harness:
    root: Path                 # a spawned oracle
    def chat(self, script: list[ScriptedResponse], surface="local",
             environment="local_agent") -> AgentLoop: ...
    def gateway(self, updates: list[dict], allowlist: dict) -> TelegramGateway: ...
class ScriptedResponse: ...    # content / tool_calls, as the fake client returns
class FakeLLM:                 # records messages, replays a script, asserts on calls
    def assert_no_content_above(self, ceiling: str, order: list[str]) -> None
```
This formalizes the ad-hoc fakes in the current `tests/shell/` into a reusable
kit so Phase 6 can drive scenarios at scale.

### `oracle upgrade` (cli.py)
```
oracle upgrade [--check]            # report packaged vs each instance kernel version
oracle upgrade kernel NAME          # apply `admin upgrade apply` to instance NAME
oracle upgrade self                 # re-vendor: copy a newer kernel tree into the
                                    # package (dev/maintainer path; --from-dir)
```
- `upgrade kernel` runs the root's own `_tools/upgrade.py apply --from-kernel
  <vendored-asset-dir> --approve <admin>` under the per-root lock (source dir
  is the shell package's vendored `assets/oracle-kernel/`); never headless;
  prints the kernel's verdict; rolls back on its failure.
- `upgrade self` is maintainer tooling: re-vendor + re-render the manifest +
  run `make check`; refuses if the resulting tree fails lint/tests.

### config versioning (config.py)
```python
CONFIG_VERSION = 2
def load_config() -> dict      # migrates v1 (no "version") forward, stamps version
MIGRATIONS: dict[int, callable]  # n -> (cfg) -> cfg
```

### backup (cli.py + service or a new `oracle_agent/backup_shell.py`)
```
oracle backup [NAME] [--out DIR]   # wraps root _tools/backup.py run per instance
oracle restore NAME --from PATH    # shell-implemented restore: copy backup tree back to root,
                                   # then run _tools/backup.py verify-restore to prove integrity;
                                   # refuses on hash mismatch (I4). No kernel "restore" subcommand
                                   # exists — the kernel provides only "run" and "verify-restore".
```

## Tasks

- **P1-T1 — SECURITY.md enforcer map.** Build `security_map.py` enumerating
  every shell guarantee already implied by `STRESS.md` (C1–C3, H1–H4, M1–M5,
  I1–I4) and `SPEC.md`, each pointing at its existing test. Generate
  `docs/SECURITY.md`. Add `test_security_map.py` (all enforcers resolvable;
  rendered doc matches). *Acceptance:* `verify_enforcers()` empty; CI fails if a
  guarantee loses its test. *Tests:* `test_security_map.py`. *Deps:* none.

- **P1-T2 — testkit.** Extract the fake LLM client, fake dispatcher, fake
  Telegram API, and spawned-root fixture into `oracle_agent/testkit.py` with
  the frozen interface above; refactor existing shell tests to use it (no
  behavior change). Add `FakeLLM.assert_no_content_above`. *Acceptance:*
  existing 101 shell tests pass unchanged through the kit; the assert helper
  catches a deliberately-planted leak in its own test. *Tests:*
  `test_testkit.py`. *Deps:* none.

- **P1-T3 — config versioning + migration.** Add `CONFIG_VERSION`,
  `MIGRATIONS`, forward-migration on load, version stamp on save. A v1 config
  (no version key) migrates cleanly; an unknown future version is rejected with
  guidance (fail closed, I4). *Acceptance:* round-trip + migration tests; a
  hand-written v1 fixture loads and upgrades. *Tests:* extend
  `test_config.py`. *Deps:* none.

- **P1-T4 — `oracle upgrade kernel`.** Wire the CLI to the root's
  `_tools/upgrade.py --from-kernel SRC` under the per-root lock, scrubbed env,
  printing the kernel verdict; `--check` reports version skew (reuse doctor's
  comparison). `SRC` is the shell package's own vendored
  `src/oracle_agent/assets/oracle-kernel/` tree of the *newer* installed
  package version — not a user-supplied arbitrary path. The upgrade.py
  `check`/`apply` subcommands require a `--from-kernel <dir>` that contains a
  `_tools/` subtree and a `.kernel-manifest.json`; the shell resolves this from
  the vendored asset tree and passes it. `apply` additionally requires
  `--approve <admin>` (upgrade.py guarantee 1: never headless).
  *Acceptance:* against a spawned root, `upgrade --check` reports matching
  versions; a simulated older root (older `.kernel-manifest.json`) reports skew;
  apply is a clean no-op when already current. *Tests:* `test_upgrade.py`.
  *Deps:* P1-T3 (version surface).

- **P1-T5 — `oracle upgrade self` (maintainer re-vendor).** A documented
  script/command that copies a kernel tree from `--from-dir`, re-renders the
  manifest (`oracle_agent.manifest`), and runs the gate; refuses to leave the
  tree if lint/tests fail. *Acceptance:* re-vendoring the current kernel is a
  byte-identical no-op that stays green; a deliberately broken kernel is
  rejected. *Tests:* `test_revendor.py` (guarded/skipped if no source dir).
  *Deps:* none.

- **P1-T6 — shell backup/restore.** `oracle backup` wraps `_tools/backup.py
  run` per instance under the per-root lock. `oracle restore` is a
  shell-implemented operation: it copies the backup tree back to the instance
  root file-by-file (hash-verifying each copy), then calls `_tools/backup.py
  verify-restore` to prove the round-trip integrity; refuses on mismatch (I4).
  The kernel provides only the `run` and `verify-restore` subcommands — there
  is no kernel `restore` subcommand; the shell owns the restore logic.
  Document that secrets in `.env` are backed up only if the operator opts in
  (default: config + instance data, NOT `.env`). *Acceptance:* backup then
  restore of a spawned root reproduces its ledgers/notes; a tampered archive
  is refused by the verify-restore check. *Tests:* `test_backup_shell.py`.
  *Deps:* P1-T3.

## Security invariants for this phase

- Upgrade never touches `oracle.yml` / `Memory.nosync` / `Meta.nosync` /
  doctrine (delegated to the kernel's upgrade, which already guarantees this —
  but P1-T4 must pass through, not reimplement).
- Backup archives are treated as potentially-sensitive: written 0600, never to
  a world-readable temp, never logged by path-with-contents.
- `upgrade self` is the ONLY shell path that writes into the vendored kernel
  tree and is maintainer-only (never reachable from chat/gateway/serve).

## Stress pass (do before coding)

Adversarially review: can `upgrade self` be coerced to vendor a malicious
kernel? Can a migration drop a security-relevant field silently? Can restore be
used to overwrite a *different* instance's root? Append findings here.

## Definition of done

- [ ] `docs/SECURITY.md` generated; `verify_enforcers()` empty; CI gate added.
- [ ] `testkit.py` in use by all shell tests; leak-assert helper proven.
- [ ] config migrates v1→v2; unknown version rejected.
- [ ] `oracle upgrade --check / kernel / self` work as specced; never touch
      sovereign data; maintainer-only self path.
- [ ] `oracle backup/restore` round-trip verified; tamper refused.
- [ ] `make check` green; CI green on all matrix cells; new tests added.
