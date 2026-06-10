# Phase 5 — Operations & Fleet

**Closes limits #4, #5, #7.** Makes the Oracle something you can *run* for real:
summarization-based context instead of blunt eviction, real per-user identity
(not just an allowlist), multi-instance fleet operations, scheduled briefings
the admin actually receives, and a complete secrets/backup lifecycle. This is
the phase that turns a working agent into operable infrastructure.

Read first: `docs/roadmap/ROADMAP.md`, Phase 1 (upgrade/backup plumbing, config
versioning), Phase 4 (adapter identity).

Depends on: Phase 1, Phase 4 (identity + delivery surfaces).

## Goals

- G1. **Summarization context** — replace whole-group eviction with a
  kernel-backed running summary so long sessions keep relevant history.
- G2. **Real identity** — promote the gateway allowlist into an identity model
  with per-user roles honored by the kernel's `--actor/--role`, so audit names
  *who*, and role actually gates capability across surfaces.
- G3. **Fleet ops** — manage many instances: `oracle fleet status`, bulk
  doctor, bulk scheduled ticks, per-instance ceilings/surfaces.
- G4. **Scheduled briefing delivery** — the kernel's `leadership-briefing` loop
  already *produces* briefs; deliver them to the admin through a Phase 4 surface
  on cadence.
- G5. **Secrets & backup lifecycle** — rotation, scoped backup including/
  excluding secrets, scheduled backups, restore drills.

## Frozen interfaces

### Summarization context — `agentloop/summary.py` (new)
```python
def summarize_turns(client, turns: list[dict], *, max_chars: int) -> str
class AgentLoop:                       # replace _evict_if_needed strategy
    history_strategy: str              # "evict" (v1) | "summarize" (default P5)
```
- When over budget, instead of dropping whole groups (P1/v1), fold the oldest
  groups into a single `system`-adjacent summary message (a `user`-role
  "conversation so far:" note kept stable), preserving the tool-pairing
  invariant for the *retained* tail. The summary call uses the SAME client +
  ceiling (so it never sends above-ceiling content anywhere); on a model error
  it falls back to `evict` (I4 — never block the turn).
- Summary content is itself subject to grounding/ceiling: it is model-produced
  prose ABOUT the conversation, never a channel to smuggle above-ceiling text
  (the turns being summarized already passed the ceiling on the way in).

### Identity — `oracle_agent/identity.py` (new)
```python
@dataclass
class Principal:
    surface: str
    user_id: str
    display: str | None
    role: str            # "user" | "admin"  (admin still NOT exposed to models)
def resolve(cfg, surface: str, user_id: str) -> Principal | None   # deny-by-default
```
- The allowlist entry gains `display` and a real `role`. `GatewayCore` and
  `oracle chat` both resolve a `Principal` and pass `--actor "<surface>:<id>"`
  / `--role <role>` to every kernel write verb, so ledgers name the human.
- **Role still never widens the model's tool surface** (I2): an `admin`
  principal chatting still gets the user-role verb set; admin role only affects
  kernel-side `--role` gating for writes the human performs via `oracle
  kernel`/CLI, and audit attribution. (Control-plane stays human-only.)

### Fleet — `cli.py`
```
oracle fleet status              # one line per instance: rung, inbox, due loops, kernel ver, surfaces
oracle fleet doctor              # doctor across all instances; non-zero if any fail
oracle fleet tick                # one harness pass per instance (autonomy-gated)
oracle fleet upgrade --check     # version skew across the fleet
```

### Briefing delivery — `service/briefer.py` (new)
```python
def due_briefings(cfg, instances) -> list[Delivery]   # reads each root's leadership-briefing cadence
def deliver(cfg, delivery, gateways) -> None          # send the latest brief to the admin surface
```
- Driven by `serve`: when an instance's `leadership-briefing` loop has produced
  a new dated brief, deliver it to the configured admin channel (a Phase 4
  surface) — every claim already passed the kernel's claim-gate, and delivery
  re-checks the ceiling for the target surface.

### Secrets/backup lifecycle — `cli.py`
```
oracle secrets rotate --key-env E        # prompt new value, write atomically, confirm old superseded
oracle backup --include-secrets          # explicit opt-in; default excludes .env
oracle backup schedule [--cadence ...]   # register a backup as a serve-driven job
```

## Tasks

- **P5-T1 — summarization context.** Implement `summary.py` and the
  `history_strategy` switch; default `summarize`; fall back to `evict` on model
  error; preserve tool-pairing on the retained tail; never exceed ceiling in
  the summary call. *Acceptance:* a long scripted session keeps early facts
  available (a question about turn-1 content answered after eviction-pressure);
  summary call uses the session client+ceiling; model-error → clean evict
  fallback. *Tests:* `test_summary.py` via testkit. *Deps:* P1-T2.

- **P5-T2a — kernel write-verb `--role` support (upstream).** Add `--role`
  flag to the kernel write verbs that currently only accept `--actor`:
  specifically `session_memory.py capture` and `capture.py` (feedback/value/
  failure sub-commands). Other kernel write verbs (`source_record`, `actions`,
  `truth_map`, `ingest_pipeline`) already accept `--role`. Lands in the Oracle
  Spawn kit; re-vendored via P1-T5. *Acceptance:* each affected verb accepts
  `--role` without error; unknown/missing role falls back to a safe default
  (e.g. "unknown") rather than failing. *Tests (kernel):* extend
  `test_session_memory.py`, `test_capture.py`. *Deps:* P1-T5.

- **P5-T2 — identity model.** `identity.py`; extend allowlist schema
  (display+role) with a P1 migration; thread `Principal` → `--actor/--role` on
  every write verb in `verbtools` and the gateway (depends on P5-T2a for the
  verbs that did not previously accept `--role`). Assert role NEVER changes the
  model's tool schema. *Acceptance:* a gateway write ledgers the resolving
  user's actor string; an admin principal still gets the user tool set; unknown
  user denied. *Tests:* `test_identity.py`, extend `test_verbtools`/gateway.
  *Deps:* P5-T2a, P1-T3, P4-T1.

- **P5-T3 — fleet commands.** `oracle fleet status|doctor|tick|upgrade`.
  *Acceptance:* with 2+ instances, `fleet status` lists both with live
  rung/inbox/version; `fleet doctor` non-zero if any instance fails; `fleet
  tick` respects per-root locks + autonomy gate. *Tests:* `test_fleet.py`.
  *Deps:* P1-T4 (upgrade check), existing scheduler.

- **P5-T4 — briefing delivery.** `service/briefer.py`; `serve` delivers new
  briefs to the admin surface on cadence; re-checks ceiling for the delivery
  surface; ledgers a delivery row. *Acceptance:* a root with a fresh brief and
  a configured admin surface delivers exactly once per new brief; nothing
  delivered when no new brief; above-ceiling content for the surface withheld.
  *Tests:* `test_briefer.py`. *Deps:* P4 (surfaces), P1.

- **P5-T5 — secrets rotation + backup lifecycle.** `oracle secrets rotate`
  (atomic, no echo, old value not retained); `backup --include-secrets`
  (explicit; default excludes); `backup schedule` as a serve job. *Acceptance:*
  rotate replaces the key atomically and doctor still resolves it; default
  backup excludes `.env`; `--include-secrets` includes it 0600; scheduled
  backup runs under serve. *Tests:* extend `test_config`/`test_backup_shell`,
  `test_secrets.py`. *Deps:* P1-T6.

- **P5-T6 — SECURITY.md + docs.** Guarantees: "summary never exceeds session
  ceiling", "role never widens the model tool surface", "briefs re-check
  ceiling per delivery surface", "default backup excludes secrets". Operator
  runbook (`docs/OPERATIONS.md`): deploy, schedule, rotate, restore-drill,
  upgrade-fleet. *Acceptance:* `verify_enforcers()` empty; runbook steps
  exercised by a smoke test. *Deps:* all P5, P1-T1.

## Security invariants for this phase

- Summarization is a model call ABOUT already-ceiling-bounded content; it can
  never become a path for above-ceiling text and always honors the session
  ceiling.
- Identity raises *attribution and human-side role gating*, never the model's
  capability surface (I2). Admin remains human-only and control-plane remains
  off every model surface.
- Briefing delivery is an *export*: it re-runs the ceiling check for the
  destination surface (a confidential brief is never delivered to a
  public-ceiling channel).
- Secrets rotation never writes the new value anywhere but `.env` (0600), never
  logs it, and confirms the old one is gone.

## Stress pass (before coding)

Can the summary be steered (by a prompt injection in mid-session content) to
restate above-ceiling material it shouldn't? Can a role be escalated via the
identity migration or a crafted allowlist entry? Can a scheduled backup leak
secrets into a world-readable location or an off-box destination? Append
findings.

## Definition of done

- [ ] Summarization context default; ceiling-safe; evict fallback.
- [ ] Identity model with attribution + human role gating; model surface
      unchanged by role.
- [ ] Fleet status/doctor/tick/upgrade across many instances.
- [ ] Scheduled briefing delivery, ceiling-re-checked per surface.
- [ ] Secrets rotation + backup lifecycle (secrets excluded by default).
- [ ] `docs/OPERATIONS.md` runbook; SECURITY.md guarantees added.
- [ ] `make check` green; CI green.
