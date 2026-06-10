# Oracle Architecture

Scaffolded on {{DATE}} for **{{COMPANY_NAME}}**.

This oracle is a self-contained, local, sovereign company filesystem. Its design is a
**three-tier stack governed by one meta-rule**: every security, policy, or accuracy
guarantee in doctrine names its enforcing tool — or is explicitly stamped `advisory:
agent-obeyed, not code-enforced`. Doctrine is binding *because* it is wired to a tool;
unenforced "must"/"required"/"denied" guarantees are caught by `_tools/oracle_lint.py`'s
Doctrine→Enforcer cross-check and fail the gate. This is the single discipline that keeps
the doctrine from drifting back into unbacked prose.

## The three-tier stack

The tiers build bottom-up. A higher tier may rely only on tiers below it, and each tier
lands and is test-green before the next is trusted.

### Tier 1 — Floor (security + reliability)

The structural invariants every other tier stands on. Lands first; proven by the pytest
suite shipped in `tests/`.

- **One containment chokepoint** — `_tools/safe_paths.py`. Every filesystem write touching
  a user-/config-influenced path goes through `contain()` / `safe_copy_verify_delete()`.
  A CI grep-guard (`tests/test_no_bypass_guard.py`) fails the build if any kernel file
  calls raw `shutil.move`/`copy`/`copy2` or `open(...,'w'/'a')` on a user-influenced
  target. Containment is a non-recurring structural invariant, not a re-proven property.
- **A durable append-only ledger** — `_tools/ledger.py` (flock + fsync append, atomic
  rewrite, corruption-tolerant load that quarantines bad lines instead of bricking,
  collision-safe id minting, verify/repair).
- **A schema-validating linter** — `_tools/oracle_lint.py` over a stdlib JSON-Schema
  validator (`schema_check.py`) and the safe-subset YAML loader (`oracle_yaml.py`).
- **A secret scanner** — `_tools/secret_scan.py` (pattern + entropy).
- **A policy gate** — `_tools/policy.py` (processing matrix, export/role enforcement,
  export/redaction ledgers).

Non-destructiveness is built into the floor: ingest/emit/connector-pull copy → fsync →
verify-hash → delete-source, never a bare move, so a failed or escaping write can never
destroy the original.

### Tier 2 — Engine (knowledge + accuracy)

On the floor: how the oracle learns and how it refuses to fabricate.

- A deterministic ingestion pipeline (extractors → chunker → retrieval index → immutable
  Source records → review-gated derivation → sensitivity classifier).
- **The answer protocol as real code** — `_tools/answer_protocol.py` + `truth_map.py`.
  Before any material answer it resolves business object → truth-map row → source
  authority → freshness → sensitivity → confidence → disconfirmers → open contradictions,
  and returns the **graduated authority ladder** verdict: grounded (0) on a confirmed
  row with fresh evidence; **supported (2)** — answerable with a mandatory label — when
  evidence exists but authority is unconfirmed; caveated (3) on stale/contradicted
  authority; refused (4) only when there is nothing, with `suggested_fix` naming the
  exact commands that change the verdict. See `PLAYBOOKS/answer.md` and `DOCTRINE.md` §6.
- **Authority lifecycle as real code** — `truth_map.propose_row/promote_row/validate_rows`
  (`./oracle admin truth ...`): ingest auto-proposes draft rows from evidence metadata;
  promotion to `confirmed` is an explicit, role-gated, evidence-checked admin act,
  recorded in the truth_map ledger.

### Tier 3 — Execution (action + self-improvement)

Ships last, behind the proven floor, **autonomy OFF by default**.

- **The experience layer** — the session protocol (`oracle_status.py`:
  `./oracle status` opens, `./oracle checkpoint` closes) and the **Review
  Inbox** (`review_queue.py`): one ranked, self-cleaning queue of everything
  pending a decision (contradictions, promotable rows, authority candidates,
  needs-ocr sources, unreviewed findings, stale questions/models, event
  backlogs). Nothing rots silently.
- **The intelligence layer** — `synthesis.py` (clusters findings against
  models and emits the insight-synthesis worklist, so memory consolidates
  instead of just accumulating) and `briefing.py` (the leadership brief: the
  oracle's proactive voice, every claim preflighted, withheld objects listed
  with their fix).
- A deterministic loop runner + due-ness engine (`_tools/loops.py`) — each loop carries a
  `runner:` field; the 7 active loops ship as real records (not a TBD template).
- A headless scheduler (`harness.py`) and a scoped autonomous-action chokepoint
  (`actions.py`: kill-switch-first → admin allowlist → blast-radius caps → `action_event`
  log). Autonomy is the highest-blast-radius capability, so it defaults OFF and is gated
  by `Meta.nosync/Autonomy/autonomy.yml` (empty = OFF).
- A reference connector runtime (`connectors/localfolder.py`), standing-deliverable
  generators that route every claim through the answer protocol, and `capture.py` writing
  the feedback/value/failure events the self-improvement loops consume.
- `session_memory.py` captures material sessions as Meta memory and the
  `memory-matriculation` runner decomposes them into the existing Memory/Meta
  behavioral stores plus derived MemPalace/Graphify recall and graph files. This
  is the oracle's session/daily dreaming path; it is not a separate redundant
  active loop.
- `_tools/session_interface.py` exposes the User/Admin UX boundary and the
  machine-readable `goal_clarity_policy` through `./oracle session contract
  --json`. The policy is proportional: quick, reversible work needs little
  dialectic; broad, risky, high-compute work needs explicit back-and-forth until
  goal, scope, output shape, constraints, non-goals, and success criteria are
  clear enough to execute. When dialectic is needed, it asks one question at a
  time, includes a recommended answer, and inspects local material first.
- A managed, oracle-local skills repository (`AgentResources.nosync/Skills/`) with
  lifecycle tooling in `_tools/skills.py`. This is not a host-wide Codex/Hermes skill
  store; it is portable procedural memory governed by this oracle's ledgers, lint, and
  backup.

## Universal Memory Kernel — behavioral types, company nouns as subtype

The memory schema is **behavioral and abstract**, and the folders partition by that
behavior, not by company vocabulary. Company-specific concepts are `subtype:` /tags
/config validated against the ontology enum in `oracle.yml` — never new hard types unless
a genuinely distinct behavior emerges. There is one type system, and `oracle_lint`
validates each note's `type` and `subtype` against it.

- **Mutable hubs** — Entities, People, Groups, Assets, Systems, Metrics, Queries, Models,
  Questions, Contradictions (and, in `Meta.nosync/`, User-Models, Architecture-Components,
  Sessions, Value-Scorecards).
- **Portable procedural resources** — `AgentResources.nosync/Skills/` stores managed
  oracle-local skills. Skill lifecycle events are metadata-only ledger rows, and
  archive preserves packages instead of deleting them.
- **Immutable statements** — Sources, Findings, Decisions, and the load-bearing fields of
  Directives/Recommendations. Immutability is **mechanical**: each records a content
  `sha256` in its ledger, and `oracle_lint` fails on any hash mismatch — forcing
  supersession (write-new + `supersedes:`/`superseded_by:`) instead of a silent edit. A
  Recommendation keeps its original `action`/`rationale`/`evidence`/`baseline` immutable
  while a separate adjudication block mutates against observed Decisions and value events.

Two orthogonal axes share one decode table:
**locality(3)** = where the bytes physically live (`external_only` / `snapshot_local` /
`mirror_local`); **capture_tier(3)** = how much we copy (`manifest_only` / `snapshot` /
`mirror`).

## Optional Derived Memory Engines

Oracle can prepare derived corpora for external local memory tools without letting them
become source authority. The default `oracle.yml` declares `derived_memory` engines for
MemPalace (semantic/verbatim recall over indexed chunks) and Graphify (graph analysis
over indexed chunks).

Both are optional and disabled for automatic use by default. `_tools/derived_memory.py`
validates the boundary, reports whether the commands are installed, and exports
sensitivity-capped corpora from the rebuildable `knowledge_index` into
`_data.nosync/derived/<engine>/raw/`. The resulting files are derived artifacts: useful
for retrieval, graph discovery, ontology work, and candidate Finding/Question/
Contradiction creation, but not a substitute for Sources, TRUTH-MAP authority, or the
answer protocol.

## Loop-Native Operation

Create a loop when a process benefits from repeated re-evaluation as new information is
added, altered, contradicted, or decays. Loops govern memory matriculation, source
capture, connector health, schema refresh, contradictions, questions, recommendations,
models, stale findings, invariants, artifact I/O, routing evolution, user feedback, value
scoring, architecture retrospectives, security review, and backup-restore verification.

Each loop record carries a `cadence`, a `runner:` (`module:function` or `agent-worklist`),
`last_run`, `next_review`, and machine-readable `model_policy`. `loops.compute_due(loops,
now)` selects exactly the due loops; `loops.record` appends a `loop_runs` ledger row and
updates `last_run`/`next_review` atomically. `oracle_lint` fails any `status==active` loop
lacking a `runner` or `last_run`, so an "active" loop can never be inert in practice.
The loop model policy is surfaced in agent worklists and scheduled/headless reports:
deterministic code first, cheapest fully capable model if a model is needed, and recorded
rationale for any premium model or multi-agent pass.

## Local Sovereignty Reconciled with Patchability

Sovereignty is non-negotiable: **your data and doctrine are never subject to a formal
upstream upgrade.** Tool-layer updates are scoped **at the tool layer only**:

- `oracle.yml` carries `kernel.tools_version` and `kernel.tools_sha256` (a manifest hash
  of every `_tools/` file, generated into `.kernel-manifest.json`).
- `_tools/upgrade.py` replaces **only** the executable tool layer — hash-verified, never
  touching doctrine, `Memory.nosync/`, `Meta.nosync/`, or business config — runs ordered
  migrations, and re-runs lint + pytest after the swap. It never runs headless and
  requires admin approval.

So a floor fix can reach an already-spawned oracle without a destructive re-spawn, while
the oracle's sovereign content stays byte-identical. Evolution otherwise proceeds through
local admin directives, architecture decisions, retrospectives, failure events, and user
feedback.
