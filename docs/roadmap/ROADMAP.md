# Oracle — Forward Architecture Roadmap

**Status: planning.** This is the arc from v1.0 (shipped) to the final best
state: a governed, sovereign company oracle that is genuinely trustworthy with
confidential data, **filled with the company's actual knowledge**, reachable
everywhere its users already are, operable by a non-technical admin, and
provably correct *and useful*. Each phase below has a standalone spec in this
directory (`PHASE-1-*.md` … `PHASE-8-*.md`) written to drive agentic team
development: frozen interfaces, task breakdown with IDs, acceptance criteria,
test plans, and a definition of done.

This document is the index and the *why*. The phase specs are the *what* and
the *how*. Read this first; then a team picks up one phase spec.

> **Phase numbers are identity, not order.** PHASE-7 and PHASE-8 were added
> by the 2026-06 roadmap amendment and run EARLY (see the arc diagram). The
> numbering of the original six phases is preserved so existing references
> stay valid.

---

## Where v1.0 + remediation stands

Shipped and green (`make check`: 753 tests, CI across Linux/macOS × Py
3.10–3.13): the vendored kernel (graduated answer authority, truth map,
tamper-evident hash-chained ledgers, sensitivity×environment policy matrix,
review inbox, loops, earned autonomy, doctrine→enforcer lint) plus a
stdlib-only shell (global `oracle` CLI, model-agnostic agent loop whose only
tools are kernel verbs, policy bridge, scheduler daemon, Telegram gateway,
wizard, doctor, installer).

The 2026-06 **pre-roadmap remediation** (`docs/remediation/`) hardened this
baseline before any phase work: truth-map injection closed and writes made
atomic; ledger rows hash-chained with tamper detection; knowledge-index
dedup/upsert/supersession; literal-loopback-only endpoint classification with
a per-request guard (no DNS, no TOCTOU); gateway turns locked; Telegram
offsets persisted; daemon backoff/isolation/rotation; fail-closed sensitivity
override validation; checkpoint/loops_due dropped from external surfaces; the
no-bypass guard extended to every kernel write form; and the core docs
re-stamped so every guarantee names its enforcer or is marked advisory.

### The honest limits (these are the roadmap's raw material)

1. **Confidential data never reaches *any* model.** The ceiling caps
   `local_agent` at `internal` because `allow-minimized` tiers have no
   minimizer. The oracle is therefore mute on its most valuable knowledge.
2. **Protocol use in free prose is advisory.** The footer makes *labeling*
   honest; it does not force the model to consult the protocol before
   asserting.
3. **One gateway (Telegram), one transport, no voice, no push.**
4. **Eviction-based context management**, not summarization.
5. **Single-node, single-tenant.** No multi-user identity beyond the gateway
   allowlist; no cross-instance fleet; `--actor` is advisory.
6. **No upgrade path** for re-vendoring kernel updates into the package or
   into already-spawned roots from the shell.
7. **Setup is a 1-instance wizard.** No migration, no config versioning, no
   backup/restore from the shell.
8. **Intake is manual.** One reference connector (localfolder); everything
   else enters via hand-run `ingest batch`. An oracle nobody fills is an
   empty brain with perfect governance.
9. **Retrieval is lexical-only.** FTS5 misses paraphrase ("refund policy" vs
   "returns and exchanges"); every miss degrades the grounded-rate that the
   scorecard calls the master signal.
10. **Self-improvement requires an attended session.** The kernel's
    improvement lifecycle, review queue, and dream-session harness are real,
    but no operating agent ships to work them unattended; autonomy ships off
    with no configured actuator.

## The final best state (the destination)

An Oracle that:

- **Is actually full.** Connectors pull the company's real knowledge —
  Drive, SharePoint/OneDrive, Notion, mail, Slack history — through the
  ingest pipeline's immutable, review-gated, fail-closed intake, on a
  schedule, governed by the autonomy gate — closing limit #8.
- **Finds what leaders mean, not just what they typed.** Hybrid
  lexical+vector retrieval with the embedding path policy-gated exactly like
  chat (an embedding call is content egress), falling back cleanly to
  lexical — closing limit #9.
- **Speaks confidentially, safely.** A real minimizer + a verifiable local
  confinement story let a local model reason over confidential material with
  redaction enforced and audited in code — closing limit #1 without ever
  leaking to an external provider — *after* validating the minimized answers
  are actually useful.
- **Forces grounding, not just labels it.** A claim-gating layer makes the
  answer protocol structurally unavoidable for material assertions on every
  surface — closing limit #2.
- **Is reachable everywhere** through a clean multi-adapter gateway
  (Telegram, Slack, email, a local HTTP/MCP surface) with per-surface
  ceilings, and **delivers** — scheduled leadership briefings pushed to the
  surfaces the admin chooses — closing limit #3.
- **Operates and improves itself responsibly** with summarization-based
  context, real identity, a shipped operating agent working the review queue
  and dream sessions inside the autonomy gate, and a backup/restore +
  kernel-upgrade story driven from the shell — closing limits #4–7 and #10.
- **Proves its own correctness *and usefulness*** continuously: an evaluation
  harness scoring grounded-rate, refusal-correctness, leak-attempts-blocked,
  policy conformance, retrieval hit-rate, and time-to-first-grounded-answer
  on every change, so "trustworthy and valuable" is measured, not asserted.

## Design invariants (hold across every phase)

These are non-negotiable. Any phase task that would violate one is wrong.

- **I1 — stdlib-only.** No runtime third-party dependency, kernel or shell.
  CI proves it (pytest-only install). New transports/extractors/connectors
  degrade gracefully when an optional lib is absent; they never become
  required.
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
  unbacked guarantee. This includes product claims: "self-improving" may not
  be asserted while the actuator is unshipped.

## Phase arc and dependencies

```
v1.0 + remediation
        │
        ▼
   P1 Foundation Hardening
        │
        ├──────────────┬───────────────┬──────────────┐
        ▼              ▼               ▼              │
  P7 Knowledge    P2 Confidential  P3 Forced         │
  Connectors      Tier             Grounding         │
        │              │               │              │
        ▼              │               │              │
  P8 Retrieval        │               │              │
  Quality             │               │              │
        │              │               │              │
        └──────┬───────┴───────┬───────┘              │
               ▼               │                      │
        P4 Gateway Platform ◄──┘ ◄────────────────────┘
        (incl. briefing delivery)
               │
               ▼
        P5 Operations & Operating Agent
               │
               ▼
        P6 Trust & Evaluation
```

- **P1** is a prerequisite for everything (SECURITY.md enforcer map,
  `testkit.py` eval substrate, kernel-upgrade plumbing, config versioning —
  the scaffolding the later phases stand on).
- **P7** (connectors) is the highest-leverage *value* phase: it fills the
  oracle. It depends only on P1 and should land before P4 completes — reach
  without content is an empty channel. **P2** and **P3** are the two
  highest-value *correctness* phases; all three run in parallel by separate
  teams.
- **P8** (retrieval quality) depends on P1 and tunes against P7's corpus.
- **P4** (multi-adapter gateway + briefing delivery) depends on P1's config
  versioning, benefits from P3's claim-gating, and pushes P7's content to
  people.
- **P5** (operations, identity, operating agent, backup; fleet as stretch)
  depends on P4's adapter abstraction and P1's upgrade plumbing. It ships the
  self-improvement actuator.
- **P6** (continuous evaluation) consumes signals from all prior phases —
  safety floors *and* usefulness metrics — and is last because it scores the
  whole.

## Phase summaries

| Phase | Title | Closes limit | Goal dimensions served | Headline deliverable |
|---|---|---|---|---|
| **1** | Foundation Hardening | #6, #7 (partial); enables all | security honesty, low admin | SECURITY.md enforcer map, `testkit.py` eval substrate, `oracle upgrade`, config versioning + migration, backup/restore from shell |
| **7** | Knowledge Connectors | #8 | plugins/extensibility, source-of-truth value, low admin | Drive, SharePoint/OneDrive, Notion, IMAP, Slack-export connectors on the kernel contract; wizard setup; scheduled autonomy-gated pulls; connector health in doctor/dashboard |
| **2** | Confidential Tier | #1 | security, source-of-truth value | usefulness-validated minimizer + verified local confinement; `enterprise` tier ADR (decision, not silent build) |
| **3** | Forced Grounding | #2 | trust, source-of-truth correctness | claim-gating with measured FP/latency budgets; gateway-first ENFORCE |
| **8** | Retrieval Quality | #9 | scalable/efficient memory, answer quality | policy-gated hybrid lexical+vector retrieval in the same SQLite; retrieval KPIs; gold fixture set |
| **4** | Gateway Platform | #3 | conversational reach | adapter abstraction; Slack, email, local HTTP/MCP; per-surface ceilings; optional typing indicators; scheduled briefing delivery |
| **5** | Operations & Operating Agent | #4, #5, #7, #10 | self-improving, low admin | summarization context, real per-user identity, operating agent (dream + curator), ledger rotation/compaction, secrets/backup lifecycle; fleet ops as stretch |
| **6** | Trust & Evaluation | measures all | proof of value | continuous eval: grounded-rate, refusal correctness, leak blocking, policy conformance + retrieval hit-rate, time-to-first-grounded-answer, intake throughput |

## How a team uses a phase spec

1. Read this roadmap and `docs/DESIGN.md`/`docs/SPEC.md`/`docs/STRESS.md` for
   context (plus `docs/remediation/` for the hardened baseline). The phase
   spec assumes that grounding.
2. The phase spec lists tasks `P<n>-T<k>` with explicit inputs, frozen
   interfaces, and acceptance criteria. Each task is sized for one agent /
   one PR.
3. Tasks within a phase declare their dependencies; independent tasks run in
   parallel.
4. Every task ships with tests (the spec names them) and must keep
   `make check` green. A phase is done when its DoD checklist passes.
5. Each phase begins with its own adversarial stress pass (same discipline as
   `STRESS.md`) before code; findings are appended to that phase spec.
