# SUB-5 — Roadmap Amendment (Phase 5)

Design-led (orchestrator executes/authors; not delegated to execution
agents). Builds on the corrected docs from Phase 4. Addresses the four
goal-alignment gaps the review found no phase covers, plus re-sequencing.

## Decisions (pinned)

1. **Phase numbering is identity, the arc diagram is order.** Existing
   PHASE-1…6 files keep their numbers. Two NEW phase specs are added and the
   ROADMAP arc re-drawn to schedule them early:
   - **PHASE-7-knowledge-connectors.md** — data-connector ecosystem on the
     existing `connectors/base.py` contract: Google Drive, SharePoint/
     OneDrive, Notion, IMAP mailbox, Slack export; connector setup wizard
     step; scheduled pulls through the existing autonomy gate
     (`readonly_connectors`); doctor/dashboard connector health; optional
     per-connector deps under I1's graceful-degradation clause. Runs after
     P1, in parallel with P2/P3, BEFORE P4 completes (reach without content
     is an empty channel).
   - **PHASE-8-retrieval-quality.md** — hybrid retrieval: optional embedding
     index via the already-configured `/v1` endpoint (embeddings API),
     vectors in the same SQLite DB, hybrid rank with FTS5, clean lexical
     fallback (I1-compliant: zero new required deps); retrieval hit-rate and
     time-to-first-grounded-answer added to `scorecard.py` KPIs (upstream
     kernel work, flagged); eval fixtures feeding P6. Runs after P7 lands
     content (needs a corpus to tune against).
2. **Operating agent (self-improvement actuator) goes into PHASE-5** (ops) as
   a new task group: a wizard-configurable headless agent command for
   `autonomy.yml` dream sessions + curator verbs (review-queue working) on
   the local attended surface, autonomy-gated, every action ledgered.
   Until it ships, README/marketing language stays stamped "machinery
   present, unattended actuation is roadmap work" (D1 did this).
3. **Briefing delivery moves forward**: P5-G4 (scheduled briefing delivery)
   is pulled into PHASE-4's scope (a gateway that can push, not just reply,
   is the leverage feature). PHASE-5 keeps fleet ops but demoted to
   stretch/optional (single-company audience; multi-instance fleet is rare).
4. **Model-quality/confinement tradeoff confronted in PHASE-2**: add a
   phase-opening validation task — measure minimized-answer usefulness with
   real local models on representative confidential Q&A BEFORE building the
   full minimizer; and spec an explicitly opt-in, admin-attested
   `enterprise` environment tier (zero-retention contractual endpoints)
   as a DESIGN DECISION to evaluate, default off, fail closed — documented
   as an option with its policy-matrix column, not silently built.
5. **P3 (forced grounding) scope note**: measured false-positive and
   added-latency budgets on real traffic before ENFORCE becomes a default
   anywhere; gateway-first rollout.
6. **ROADMAP.md rewritten** to reflect the amended arc, the new phases, the
   re-sequencing rationale, the goal dimensions each phase serves (traceable
   to the goal statement), and the corrected P1/P4 row items. Arc diagram
   shows P2/P3 parallel (fixing the old L1 ambiguity) and P7/P8 placement.

## Deliverables

- `docs/roadmap/PHASE-7-knowledge-connectors.md` (full spec, same format as
  existing phases: context, frozen interfaces, task IDs P7-T*, acceptance,
  test plan, DoD, stress-pass requirement)
- `docs/roadmap/PHASE-8-retrieval-quality.md` (same format)
- Amended `docs/roadmap/ROADMAP.md`
- Amendments to PHASE-2 (validation task + enterprise-tier decision task),
  PHASE-4 (briefing delivery in; Slack decision checkpoint from D2 retained),
  PHASE-5 (operating-agent task group in; fleet demoted), PHASE-3 (budgets
  note), PHASE-6 (absorb usefulness metrics: retrieval hit-rate,
  time-to-first-grounded-answer, intake throughput)

**Acceptance:** every goal dimension (self-improving, low-admin,
conversational reach, plugins, memory scale/quality, security, source-of-
truth value) maps to at least one funded phase task; new specs' interfaces
reference real modules; arc has no cycles; `make check` untouched (docs
only).
