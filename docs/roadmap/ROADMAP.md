# Oracle — Forward Architecture Roadmap

**Status: planning.** This is the arc from v1.0 (shipped) to the final best
state: a governed, sovereign company oracle that is genuinely trustworthy with
confidential data, reachable everywhere its users already are, operable by a
non-technical admin, and provably correct. Each phase below has a standalone
spec in this directory (`PHASE-1-*.md` … `PHASE-6-*.md`) written to drive
agentic team development: frozen interfaces, task breakdown with IDs,
acceptance criteria, test plans, and a definition of done.

This document is the index and the *why*. The phase specs are the *what* and
the *how*. Read this first; then a team picks up one phase spec.

---

## Where v1.0 stands

Shipped and green (`make check`: 626 tests, CI across Linux/macOS × Py
3.10–3.13): the vendored kernel (graduated answer authority, truth map,
immutable ledgers, sensitivity×environment policy matrix, review inbox, loops,
earned autonomy, doctrine→enforcer lint) plus a stdlib-only shell (global
`oracle` CLI, model-agnostic agent loop whose only tools are kernel verbs,
policy bridge, scheduler daemon, Telegram gateway, wizard, doctor, installer).

### The honest limits v1 documented (these are the roadmap's raw material)

1. **Confidential data never reaches *any* model.** The ceiling caps
   `local_agent` at `internal` because `allow-minimized` tiers have no
   minimizer. The oracle is therefore mute on its most valuable knowledge.
2. **Protocol use in free prose is advisory.** The footer makes *labeling*
   honest; it does not force the model to consult the protocol before
   asserting.
3. **One gateway (Telegram), one transport, no voice.**
4. **Eviction-based context management**, not summarization.
5. **Single-node, single-tenant.** No multi-user identity beyond the gateway
   allowlist; no cross-instance fleet; `--actor` is advisory.
6. **No upgrade path** for re-vendoring kernel updates into the package or
   into already-spawned roots from the shell.
7. **Setup is a 1-instance wizard.** No migration, no config versioning, no
   backup/restore from the shell.

## The final best state (the destination)

An Oracle that:

- **Speaks confidentially, safely.** A real minimizer + a verifiable local
  confinement story let a local model reason over confidential material with
  redaction enforced and audited in code — closing limit #1 without ever
  leaking to an external provider.
- **Forces grounding, not just labels it.** A claim-gating layer makes the
  answer protocol structurally unavoidable for material assertions on every
  surface — closing limit #2.
- **Is reachable everywhere** through a clean multi-adapter gateway (Telegram,
  Slack, email, a local HTTP/MCP surface) with per-surface ceilings — closing
  limit #3, the biggest leverage feature for the actual audience.
- **Operates itself responsibly** with summarization-based context, real
  identity, scheduled briefings the admin actually receives, and a backup/
  restore + kernel-upgrade story driven from the shell — closing limits #4–7.
- **Proves its own correctness** continuously: an evaluation harness scoring
  grounded-rate, refusal-correctness, leak-attempts-blocked, and policy
  conformance on every change, so "trustworthy" is measured, not asserted.

## Design invariants (hold across every phase)

These are non-negotiable. Any phase task that would violate one is wrong.

- **I1 — stdlib-only.** No runtime third-party dependency, kernel or shell.
  CI proves it (pytest-only install). New transports/extractors degrade
  gracefully when an optional lib is absent; they never become required.
- **I2 — the model acts only through kernel chokepoints.** Every new
  capability the model gains is a kernel verb run as an argv subprocess of the
  root's own `./oracle`, never `shell=True`, never an in-process kernel call,
  never a control-plane verb.
- **I3 — the kernel stays sovereign and unmodified by the shell.** Kernel
  changes land upstream (the Oracle Spawn kit) and are re-vendored. Spawned
  roots remain self-contained.
- **I4 — fail closed.** Every ambiguity in environment, sensitivity, identity,
  or policy resolves to the *stricter* outcome. New code defaults to deny.
- **I5 — enforce in code, not in prompt.** Security properties are mechanical
  (dispatch-layer checks, lint, tests), not requests to the model.
- **I6 — every guarantee names its enforcer or is stamped advisory.** Extend
  the kernel's doctrine→enforcer discipline to the shell: a new SECURITY.md
  maps each shell guarantee to the test/lint that backs it; CI fails on an
  unbacked guarantee.

## Phase arc and dependencies

```
v1.0 ──► P1 Foundation Hardening ──► P2 Confidential Tier ──► P3 Forced Grounding
              │                            │                        │
              └────────────► P4 Gateway Platform ◄────────────────┘
                                   │
                                   ▼
                             P5 Operations & Fleet ──► P6 Trust & Evaluation
```

- **P1** is a prerequisite for everything (it builds the SECURITY.md enforcer
  map, the eval harness skeleton, kernel-upgrade plumbing, and config
  versioning — the scaffolding the later phases stand on).
- **P2** (confidential minimizer) and **P3** (forced grounding) are the two
  highest-value correctness phases; both depend only on P1 and can run in
  parallel by two teams.
- **P4** (multi-adapter gateway) depends on P1's config versioning and benefits
  from P3's claim-gating but can start after P1.
- **P5** (operations, identity, fleet, backup) depends on P4's adapter
  abstraction for identity and on P1's upgrade plumbing.
- **P6** (continuous evaluation) depends on P1's eval skeleton and consumes
  signals from all prior phases; it is last because it scores the whole.

## Phase summaries

| Phase | Title | Closes | Headline deliverable |
|---|---|---|---|
| **1** | Foundation Hardening | #6, #7 (partial); enables all | SECURITY.md enforcer map, `testkit.py` eval substrate, `oracle upgrade`, config versioning + migration, backup/restore from shell |
| **2** | Confidential Tier | #1 | a real, audited minimizer + verified local confinement so a local model can reason over confidential material |
| **3** | Forced Grounding | #2 | claim-gating: material assertions structurally require an answer-protocol envelope, on every surface |
| **4** | Gateway Platform | #3 | adapter abstraction; Slack, email, and a local HTTP/MCP surface alongside Telegram; per-surface ceilings; optional typing indicators |
| **5** | Operations & Fleet | #4, #5, #7 | summarization context, real per-user identity, multi-instance fleet ops, scheduled briefing delivery, secrets/backup lifecycle |
| **6** | Trust & Evaluation | measures all | continuous eval harness: grounded-rate, refusal correctness, leak-attempt blocking, policy conformance, gated in CI |

## How a team uses a phase spec

1. Read this roadmap and `docs/DESIGN.md`/`docs/SPEC.md`/`docs/STRESS.md` for
   context. The phase spec assumes that grounding.
2. The phase spec lists tasks `P<n>-T<k>` with explicit inputs, frozen
   interfaces, and acceptance criteria. Each task is sized for one agent /
   one PR.
3. Tasks within a phase declare their dependencies; independent tasks run in
   parallel.
4. Every task ships with tests (the spec names them) and must keep
   `make check` green. A phase is done when its DoD checklist passes.
5. Each phase begins with its own adversarial stress pass (same discipline as
   `STRESS.md`) before code; findings are appended to that phase spec.
