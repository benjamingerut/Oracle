# _tools

Deterministic, **stdlib-only** helpers for the oracle kernel: containment,
durable ledgers, config parsing, schema validation, secret scanning, contained
I/O, linting, the knowledge pipeline, the answer protocol, the loop/execution
layer, autonomy, connectors, backup, and tool-layer upgrade.

Each tool derives the oracle root from its own path or an explicit `--root`
argument. **Do not hardcode machine-specific paths.** No third-party imports
live here — a fresh oracle runs on bare `python3` with zero `pip` installs
(`pytest` is a SKILL-side dev dependency, not a kernel requirement).

**Single chokepoints, structurally enforced.** All filesystem writes go through
`safe_paths.contain` / `safe_copy_verify_delete`; all ledger mutations through
`ledger.py`; all autonomous side effects through `actions.py`; all material
answers through `answer_protocol.py`. `tests/test_no_bypass_guard.py` greps every
`_tools/**/*.py` (except `safe_paths.py`) and FAILS the build on any raw
`shutil.move/copy/copy2` or `open(...,'w'/'a')` against a path target — making
containment a non-recurring structural invariant.

Run anything through the root-local dispatcher: `./oracle <group> <cmd> …`.

## Tier 1 — security + reliability floor (everything imports these)

- `safe_paths.py` — path-containment chokepoint: `contain`, `safe_slug`,
  `assert_lane`, `safe_copy_verify_delete`, `is_within`.
- `ledger.py` — durable append-only JSONL: `append` (flock+fsync), `load`
  (corruption-tolerant, quarantines bad lines), `rewrite_atomic`, `next_id`,
  `verify`, `repair`.
- `oracle_yaml.py` — conservative safe-subset YAML loader (`safe_load`); pyyaml
  fast-path; RAISES `UnsupportedYAML` on anchors/aliases/tags/flow/multi-doc.
- `schema_check.py` — tiny stdlib JSON-Schema validator (`validate`).
- `secret_scan.py` — entropy-scored secret scanner (`scan_text`, `scan_tree`).
- `oracle_cli.py` — backend dispatcher used by the root-local `./oracle` wrapper.

## Tier 1.5 — contained I/O, lint, audit, policy

- `artifact_io.py` — contained, policy-gated `scan/log/ingest/emit/render`.
- `oracle_lint.py` — schema/enum/field/registry/immutability + Doctrine→Enforcer
  cross-check; honors `known-failures.txt`; invokes `secret_scan`.
- `setup_audit.py` — deep bootstrap audit (valid config, version stamp, the 5
  active loops as runnable records, ingested rows have Sources, backup verified).
- `skills.py` — managed Oracle-local skills repository under
  `AgentResources.nosync/Skills/`; lifecycle actions append `skill_event` rows
  and archive instead of deleting.
- `policy.py` — processing/export/role gate; writes `export_event` /
  `redaction_event` ledgers.
- `schemas/` — JSON Schemas consumed by lint and the validators.

## Tier 2 — knowledge + accuracy engine

- `ingest_pipeline.py` — orchestrate extract → chunk → index → source-record →
  derive → classify.
- `extractors/` — `text_md`, `csv_tsv`, `html` (stdlib) and `office`
  (best-effort docx/xlsx/pdf, degrades gracefully).
- `chunker.py` — offset-tracked overlapping chunker.
- `knowledge_index.py` — sqlite FTS5 index with a pure-python inverted-index
  fallback; index lives at `_data.nosync/index/` (derived, rebuildable).
- `source_record.py` — immutable, content-hashed `Sources/` note generator.
- `derive.py` — review-gated Finding/Question/Contradiction candidate emitter.
- `session_memory.py` — capture material sessions, decompose them into existing
  Memory/Meta record types, and refresh derived session recall/graph artifacts.
- `intake_classify.py` — intake sensitivity classifier (stricter-row-wins).
- `truth_map.py` — parse `TRUTH-MAP.md` → rows; resolve object → authority.
- `answer_protocol.py` — material-answer envelope + refusal exit codes (0/3/4).
- `contradiction.py` / `recommendation.py` — open-contradiction and
  recommendation adjudicators.

## Tier 3 — execution + self-improvement (autonomy OFF by default)

- `loops.py` — deterministic loop runner + due-ness engine (`list|due|run|record`).
- `capture.py` — feedback / value / failure event writers.
- `standing_deliverables.py` — dated artifacts on cadence; every claim routed
  through `answer_protocol`.
- `actions.py` — autonomy chokepoint: kill-switch first, allowlist, blast-radius
  caps, `action_event` log.
- `harness.py` — headless scheduler entrypoint (due → run under autonomy).
- `connectors/` — runtime (`base`, `localfolder` reference connector).
- `backup.py` — tiered backup + real restore-verify (round-trip hash-diff).
- `upgrade.py` — tool-layer-only, hash-verified kernel migration.
- `migrations/` — ordered migration discovery/apply.
