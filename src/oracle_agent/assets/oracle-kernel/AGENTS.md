# AGENTS.md — {{COMPANY_NAME}} Oracle (the operating card)

This root is the sovereign company oracle for **{{COMPANY_NAME}}** — governed
institutional memory, evidence, insight, and workproduct. Spawned {{DATE}};
bootstrap admin **{{ADMIN_NAME}}**. This card is the ONLY document you must
read every session. Each workflow has a self-contained playbook in
`PLAYBOOKS/`; binding rules live in `DOCTRINE.md`. Read a folder's
`_CONTEXT.md` before writing in it.

## Session protocol (three beats)

```
./oracle status        # 1. where things stand + what to do next
...work...             # 2. follow the decision tree below
./oracle checkpoint    # 3. close: matriculate memory, run due loops
```

Skipping checkpoint starves the oracle's memory and self-improvement. Do not.

## Decision tree — what kind of request is this?

1. **A question about the company** (a claim, number, conclusion, or
   recommendation someone could act on) → run
   `./oracle answer --object "<business object>"` FIRST, then obey the verdict
   table below. → `PLAYBOOKS/answer.md`
2. **Public/exploratory research** (no private company context leaves the
   root) → `./oracle answer research --question "..."` → cite public sources,
   label as non-authoritative. → `PLAYBOOKS/answer.md`
3. **Material arrives** (files, folders, exports, pastes, transcripts) →
   `./oracle ingest <paths...>` — outside paths are staged in
   non-destructively; evidence auto-proposes draft truth-map rows.
   → `PLAYBOOKS/ingest.md`
4. **"What needs attention?" / periodic upkeep** → `./oracle review` (the
   Review Inbox) and work the items top-down. → `PLAYBOOKS/review.md`
5. **"Brief me" / proactive value to leadership** → `./oracle brief`
   (`publish` to file it). → `PLAYBOOKS/brief.md`
6. **Architecture, config, connectors, security, autonomy, truth authority**
   → Admin interface + `./oracle admin <area> ...`. → `PLAYBOOKS/admin-setup.md`
7. **Anything you learned, that worked, or that failed** → capture it before
   the session ends (see Capture, below). → `PLAYBOOKS/session.md`

## The answer verdict table (graduated authority ladder)

| Exit | Verdict | What you must do |
|---|---|---|
| 0 | grounded | Answer; cite the source; state confidence as a range. |
| 2 | supported | Answer, but SAY "supported — authority not confirmed" and include the envelope's upgrade command. |
| 3 | caveated | Answer ONLY with the caveat surfaced (stale evidence / open contradiction / no evidence yet). |
| 4 | refused | Do NOT make the claim. Relay the envelope's `suggested_fix` commands — they change the verdict. |

The protocol is a tool you must call, not a harness interceptor (advisory at
the harness level; `standing_deliverables.py` and `briefing.py` enforce it for
everything they emit). Search first when you need evidence:
`./oracle search "<terms>"` (results are reranked by authority + recency).

## Capture — memory is real only if recorded

```
./oracle remember --user-request "..." --answer-summary "..." \
  --learned-claim "..." --open-question "..."          # session facts
./oracle capture feedback|value|failure --target <id> \
  --polarity <+/-> --strength <0..1> --excerpt "..." --actor <who>
```

`checkpoint` runs the matriculation (dreaming) pass that decomposes captured
sessions into review-gated Findings/Questions/Contradictions. User testimony is
evidence, not truth — derivations land `status: needs_review` and surface in
the Review Inbox. Durable procedure improvements go to `./oracle skills`
(oracle-local skills, not the host machine's). The oracle scores itself from
these ledgers monthly (`./oracle scorecard`) — capture honestly or it flies blind.

## Interfaces and authority

Sessions start in the **User interface** (business work, answer-protocol-bound;
no architecture/config/security changes). Control-plane work needs the **Admin
interface** — ask exactly:

```
This requires the Admin interface. Do you approve entering Admin mode for this request?
```

Approval is consent, not authentication: privileged writes still pass
`policy.require_role` (roles in `oracle.yml` → `governance.roles`; `--actor` is
advisory-plus-logged, not verified identity — see `DOCTRINE.md`).

**Goal clarity before execution** (policy: `./oracle session contract --json`):
scale clarification to ambiguity, scope, cost, reversibility, and risk. Trivial
reversible work proceeds on reasonable assumptions; broad/costly/risky work
gets one-question-at-a-time dialectic with a recommended answer each time —
after inspecting local material for what the oracle can already answer.

## Security floor (each clause names its enforcer — full map in DOCTRINE.md)

- Writes stay inside the root — `safe_paths.contain()`; CI no-bypass guard.
- Ingest/copy never destroys originals — `safe_copy_verify_delete` / staged copies.
- External processing/export is policy-gated — `policy.check_processing` /
  `gate_export` (sensitivity × environment matrix in `DOCTRINE.md`).
- Secrets live only in `.env.nosync` — `secret_scan` via `./oracle lint`.
- Immutable records can't be silently edited — ledger hash check in lint.

When in doubt about sensitivity, classify UP and prefer deterministic local
tools. Never represent an advisory rule as machine-enforced.

## Command index

| Verb | Purpose |
|---|---|
| `./oracle status` / `checkpoint` | open / close every session |
| `./oracle answer [research]` | graduated answer preflight |
| `./oracle search "<terms>"` | retrieval (authority+recency reranked) |
| `./oracle ingest <paths...>` | batch ingest anything, staged in safely |
| `./oracle review` | the Review Inbox — everything pending |
| `./oracle brief [publish]` | leadership brief |
| `./oracle remember` / `capture` | session memory / signal capture |
| `./oracle loops list\|due\|run\|complete` | the improvement loop engine |
| `./oracle check` | audit + lint, one verification gate |
| `./oracle dashboard` | admin systems dashboard: subsystem health + every toggle's flip command (`publish` renders HTML) |
| `./oracle admin truth\|policy\|backup\|upgrade\|autonomy\|connector\|session` | control plane |

Map of the root: `oracle.yml` (config) · `Memory.nosync/` (company memory by
behavioral type; company nouns are `subtype:`) · `Meta.nosync/` (self-memory,
loops, ledgers) · `TRUTH-MAP.md` (authority by business object) ·
`Workproduct.nosync/` (`_INPUT`/`_OUTPUT`/`_STANDING` + lanes) ·
`Connectors/` (external systems) · `_data.nosync/` (rebuildable index/derived)
· `_tools/` (stdlib kernel) · `PLAYBOOKS/` (workflow guides) ·
`ORACLE-ARCHITECTURE.md` + `BOOTSTRAP-STATUS.md` (reference).

Self-contained rule: nothing outside this root is load-bearing — connectorize
it or ingest it (lint fails external paths in `oracle.yml`).
