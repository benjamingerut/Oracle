# Pre-Roadmap Remediation — Master Spec

**Status: COMPLETE (2026-06-10).** All five phases executed and committed;
gate green at 753 tests. One adjudicated architectural exception
(backup.py manifest write, see SUB-3) and one stamped-not-implemented claim
(SPEC S4 brief line-scan — kernel briefing output has no per-line markers;
re-stamped in docs, upstream kernel work if ever needed). This spec tree remediates the findings of the 2026-06-10
four-track review (shell security, kernel, docs consistency, goal alignment)
so the forward roadmap (`docs/roadmap/`) starts from a sound, honest baseline.

Goal restated (unchanged): a self-improving, self-correcting, low-maintenance,
conversational, extensible, scalable, secure institutional source of truth for
business leaders.

## Structure

| Sub-spec | Scope | Phase |
|---|---|---|
| [SUB-1-kernel-core.md](SUB-1-kernel-core.md) | Kernel data-integrity + scale fixes (truth map, ledger, index, secret scan) | 1 |
| [SUB-2-shell-core.md](SUB-2-shell-core.md) | Shell security + resilience fixes (policy bridge, gateway, daemon, CLI, verb tools) | 2 |
| [SUB-3-guard.md](SUB-3-guard.md) | Kernel no-bypass AST guard extension + marker sweep | 3 |
| [SUB-4-docs.md](SUB-4-docs.md) | Docs reconciliation (README/DESIGN/SPEC/STRESS + phase-spec defects) | 4 |
| [SUB-5-roadmap.md](SUB-5-roadmap.md) | Roadmap amendment (connectors, retrieval quality, operating agent, re-sequencing) | 5 |

## Execution rules

- Tasks within a phase are file-disjoint and run in parallel (one agent per
  task). Phases run sequentially.
- **Gate per phase:** `make check` green (zero regressions; new tests added by
  the phase pass), plus an orchestrator diff review. Commit per green phase.
- Design invariants I1–I6 from `docs/roadmap/ROADMAP.md` bind every task:
  stdlib-only, argv-only chokepoint, kernel sovereignty, fail closed, enforce
  in code, every guarantee names its enforcer.
- Kernel changes land in the vendored asset tree
  (`src/oracle_agent/assets/oracle-kernel/`), with tests in its `tests/`.
  Shell changes in `src/oracle_agent/` with tests in `tests/shell/`.
- Every behavioral change ships with tests. A task that cannot meet its
  acceptance criteria reports the blocker instead of merging a partial fix.

## Phase order and rationale

1. **Kernel core** first: data-integrity bugs (truth-map injection, ledger
   tamper-evidence, index dedup) are upstream of everything.
2. **Shell core** second: security/resilience fixes, some of which observe
   kernel behavior fixed in phase 1.
3. **Guard** third: the AST-guard sweep adds markers to kernel files phases 1
   touched — must follow them.
4. **Docs** fourth: STRESS/SPEC re-stamping depends on which gaps were fixed
   in code (phases 1–3) vs. remain advisory.
5. **Roadmap** last: amendment builds on the corrected docs baseline.
