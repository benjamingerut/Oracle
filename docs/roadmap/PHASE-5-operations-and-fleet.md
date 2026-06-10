# Phase 5 — Operations & Fleet

**Closes limits #4, #5, #7.** Makes the Oracle something you can *run* for real:
summarization-based context instead of blunt eviction, real per-user identity
(not just an allowlist), the **operating agent** that turns the kernel's
self-improvement machinery into actual unattended actuation, ledgers that
survive years of appends, and a complete secrets/backup lifecycle.
Multi-instance fleet operations remain in this phase as a stretch goal. This
is the phase that turns a working agent into operable infrastructure.
(Scheduled briefing delivery moved to Phase 4 — see G4 below.)

Read first: `docs/roadmap/ROADMAP.md`, Phase 1 (upgrade/backup plumbing, config
versioning), Phase 4 (adapter identity).

Depends on: Phase 1, Phase 4 (adapter identity; scheduled briefing delivery
itself now ships in Phase 4 as P4-T8 — SUB-5 D3).

## Goals

- G1. **Summarization context** — replace whole-group eviction with a
  kernel-backed running summary so long sessions keep relevant history.
- G2. **Real identity** — promote the gateway allowlist into an identity model
  with per-user roles honored by the kernel's `--actor/--role`, so audit names
  *who*, and role actually gates capability across surfaces.
- G3. **Fleet ops (STRETCH — optional).** Manage many instances: `oracle fleet
  status`, bulk doctor, bulk scheduled ticks, per-instance ceilings/surfaces.
  *Demoted per SUB-5 D3: the audience is a single company running one
  instance; multi-instance fleets are rare enough that this must not displace
  the funded goals below.*
- G4. **Scheduled briefing delivery — MOVED to Phase 4 (P4-T8, SUB-5 D3).**
  A gateway that can push is the leverage feature, so delivery ships with the
  adapters. See `PHASE-4-gateway-platform.md`.
- G5. **Secrets & backup lifecycle** — rotation, scoped backup including/
  excluding secrets, scheduled backups, restore drills.
- G6. **Operating agent (self-improvement actuator)** — wire the kernel's
  existing, gated self-improvement machinery (dream sessions, the Review
  Inbox) to an actual actuator: a wizard-configured headless agent command
  plus a curator workflow on the local attended surface. **Honesty clause
  (I6):** until this group ships, the product may not claim unattended
  self-improvement — README/marketing language stays stamped "machinery
  present, unattended actuation is roadmap work".
- G7. **Ledger scale** — rotation/compaction + windowed reads for the
  append-only ledgers, preserving the row_hash chain's auditability across
  rotation.

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

### Fleet — `cli.py` (STRETCH)
```
oracle fleet status              # one line per instance: rung, inbox, due loops, kernel ver, surfaces
oracle fleet doctor              # doctor across all instances; non-zero if any fail
oracle fleet tick                # one harness pass per instance (autonomy-gated)
oracle fleet upgrade --check     # version skew across the fleet
```

### Briefing delivery — MOVED to Phase 4
`service/briefer.py` and its delivery semantics now live in
`PHASE-4-gateway-platform.md` (P4-T8). Nothing in this phase depends on it.

### Operating agent — wizard + curator (G6)
The kernel side already exists and is gated: `run_dream` (kernel
`_tools/harness.py`) authorizes each session through
`actions.with_action("dream.session", ...)` — denied below **autonomy level
2** and on kill-switch/caps before any side effect — and exits
`unconfigured` when `autonomy.yml` `dream.command` is empty. Inside the
session the agent works as actor `system:dream` with the USER capability set;
everything it derives lands `status: needs_review`, and the outcome is a
metadata-only `dream_session` ledger row. What is missing is the shell-side
actuator:
```yaml
# autonomy.yml (kernel-owned; the wizard writes these keys)
dream:
  command: "claude -p"      # the operator's agent-harness invocation
  max_minutes: 30           # session timeout (kernel default)
  max_inbox_items: 10       # charter size (kernel default)
```
Curator capability rides the local attended surface (`oracle chat`/CLI) and
works the kernel Review Inbox (`review_queue.py`'s ranked items) through
existing kernel verbs only — autonomy-gated, every action ledgered with the
resolving Principal, admin approval flows untouched.

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

- **P5-T3 — fleet commands (STRETCH — optional).** `oracle fleet
  status|doctor|tick|upgrade`. Demoted per SUB-5 D3: single-company audience,
  multi-instance fleets are rare; build only after the funded tasks land.
  *Acceptance:* with 2+ instances, `fleet status` lists both with live
  rung/inbox/version; `fleet doctor` non-zero if any instance fails; `fleet
  tick` respects per-root locks + autonomy gate. *Tests:* `test_fleet.py`.
  *Deps:* P1-T4 (upgrade check), existing scheduler.

- **P5-T4 — briefing delivery: MOVED to Phase 4 (P4-T8).** The task, its
  frozen `service/briefer.py` interface, acceptance criteria, and
  `test_briefer.py` now live in `PHASE-4-gateway-platform.md`. The ID is
  retired here to avoid ambiguity; do not reuse it.

- **P5-T5 — secrets rotation + backup lifecycle.** `oracle secrets rotate`
  (atomic, no echo, old value not retained); `backup --include-secrets`
  (explicit; default excludes); `backup schedule` as a serve job. *Acceptance:*
  rotate replaces the key atomically and doctor still resolves it; default
  backup excludes `.env`; `--include-secrets` includes it 0600; scheduled
  backup runs under serve. *Tests:* extend `test_config`/`test_backup_shell`,
  `test_secrets.py`. *Deps:* P1-T6.

- **P5-T7a — operating agent: dream-command wizard + doctor.** A setup-wizard
  step that configures the headless actuator for `autonomy.yml` dream
  sessions: `dream.command` (the operator's agent-harness invocation, e.g.
  `claude -p` — the kernel's own `run_dream` suggests exactly this when
  unconfigured), `dream.max_minutes`, `dream.max_inbox_items`. Doctor
  validates the command resolves, shows the current autonomy level against
  the level-2 gate, and surfaces the harness dry-run verdict (the kernel
  harness's `--dream --dry-run` mode computes the `dream.session` authorize
  verdict with zero side effects; live runs report `blocked` /
  `unconfigured` cleanly). The wizard configures; it never raises the autonomy
  level itself (promotion stays an earned, admin-approved kernel flow).
  *Acceptance:* wizard writes the dream keys; doctor on a level<2 root
  explains dream sessions remain blocked and why; doctor on an unconfigured
  root points at this wizard step; dry-run verdict surfaced verbatim.
  *Tests:* extend wizard/doctor tests (`test_wizard.py`, `test_cli.py`).
  *Deps:* P1.

- **P5-T7b — operating agent: curator on the local attended surface.** A
  curator capability for working the kernel Review Inbox from the local
  surface: list the ranked queue (`review_queue.py`), prepare resolutions,
  and apply them **through existing kernel verbs only** (I2) — autonomy-gated
  (below the required level the curator prepares but never applies), every
  action ledgered with the resolving Principal's `--actor/--role` (P5-T2),
  and admin approval flows preserved: derived items stay `needs_review`,
  truth promotion and all control-plane verbs remain Admin-interface-only.
  *Acceptance:* a queue item is worked end-to-end with ledger attribution
  naming the curator; a control-plane action attempted via the curator path
  is denied; with autonomy below the gate, apply is refused and prepare still
  works. *Tests:* `test_curator.py`. *Deps:* P5-T2, P1.

  **Honesty clause (I6):** until P5-T7a AND P5-T7b ship, the product may not
  claim unattended self-improvement; README/marketing language stays stamped
  "machinery present, unattended actuation is roadmap work". Flipping that
  language is part of this group's done-ness, not a separate task.

- **P5-T8 — ledger rotation/compaction + windowed reads (upstream).** The
  append-only ledgers (`Meta.nosync/ledgers/*.jsonl`, written solely through
  the kernel's `_tools/ledger.py`) grow without bound, and `load()`/`verify()`
  read whole files. Add rotation (close a segment at a size/age threshold,
  open a fresh one), optional compaction of rotated segments, and windowed
  reads (load only the rows newer than a cutoff without parsing the full
  history). **Rotation must re-anchor the row_hash chain auditably:**
  `ledger.append` chains rows as `row_hash = sha256(canonical(row) +
  prev_hash)` and `ledger.verify` walks that chain (tolerating only a legacy
  unhashed *prefix*), so a naive cut would read as tampering. The closed
  segment must end with a rotation-marker row (same auditable pattern as
  `rewrite_atomic`'s `REWRITE-MARKER`), and the new segment's first row must
  record the predecessor segment's name and terminal `row_hash` so `verify`
  can validate continuity across segments end-to-end. Lands in the Oracle
  Spawn kit; re-vendored via P1-T5. *Acceptance:* rotation at threshold
  produces a verifiable closed segment + a new segment whose chain anchors to
  the predecessor's terminal hash; cross-segment `verify` passes on an intact
  pair and fails when a rotated segment is altered or a segment is removed
  from the middle; windowed read returns the same rows as a full `load`
  filtered to the window. *Tests (kernel):* extend `test_ledger.py`.
  *Deps:* P1-T5.

- **P5-T6 — SECURITY.md + docs.** Guarantees: "summary never exceeds session
  ceiling", "role never widens the model tool surface", "the operating agent
  acts only through autonomy-gated kernel verbs and every action is
  ledgered", "ledger rotation preserves chain verifiability", "default backup
  excludes secrets". (The briefing-delivery guarantee moved to Phase 4 with
  P4-T8.) Operator runbook (`docs/OPERATIONS.md`): deploy, schedule, rotate
  secrets, restore-drill, configure the operating agent, rotate ledgers;
  fleet-upgrade steps only if the stretch G3 shipped. *Acceptance:*
  `verify_enforcers()` empty; runbook steps exercised by a smoke test.
  *Deps:* all funded P5 tasks, P1-T1.

## Security invariants for this phase

- Summarization is a model call ABOUT already-ceiling-bounded content; it can
  never become a path for above-ceiling text and always honors the session
  ceiling.
- Identity raises *attribution and human-side role gating*, never the model's
  capability surface (I2). Admin remains human-only and control-plane remains
  off every model surface.
- The operating agent never widens the model's capability surface: dream
  sessions run as `system:dream` with the USER capability set behind the
  kernel's level-2 autonomy gate, the curator acts only through existing
  kernel verbs, and control-plane verbs stay denied to both no matter what
  the wizard configured (I2).
- Ledger rotation is itself auditable: a rotation can never silently truncate
  history — `verify` must distinguish "rotated here, chain re-anchored" from
  "rows deleted".
- Secrets rotation never writes the new value anywhere but `.env` (0600), never
  logs it, and confirms the old one is gone.
- (Briefing delivery's export invariant moved to Phase 4 with P4-T8.)

## Stress pass (before coding)

Can the summary be steered (by a prompt injection in mid-session content) to
restate above-ceiling material it shouldn't? Can a role be escalated via the
identity migration or a crafted allowlist entry? Can a scheduled backup leak
secrets into a world-readable location or an off-box destination? Can a
crafted Review Inbox item steer the dream session or the curator into a
control-plane action (charter/queue injection — the kernel's role gate must
hold regardless)? Can ledger rotation be abused to drop rows from the middle
while `verify` stays green across segments? Append findings.

## Definition of done

- [ ] Summarization context default; ceiling-safe; evict fallback.
- [ ] Identity model with attribution + human role gating; model surface
      unchanged by role.
- [ ] Operating agent: dream-command wizard step + doctor checks (P5-T7a) and
      curator on the local attended surface (P5-T7b) — autonomy-gated, every
      action ledgered, admin approval flows preserved; "unattended
      self-improvement" claims unlocked only now (I6).
- [ ] Ledger rotation/compaction + windowed reads; row_hash chain verifiable
      across rotated segments (P5-T8, upstream).
- [ ] Secrets rotation + backup lifecycle (secrets excluded by default).
- [ ] `docs/OPERATIONS.md` runbook; SECURITY.md guarantees added.
- [ ] (Briefing delivery: done in Phase 4 / P4-T8, not here.)
- [ ] (Stretch, optional) Fleet status/doctor/tick/upgrade across many
      instances — single-company audience; only after funded tasks land.
- [ ] `make check` green; CI green.
