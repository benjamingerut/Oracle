# DOCTRINE.md — binding security, governance, and analytic rules

The governing meta-rule: **every guarantee below names its enforcing
tool+subcommand or is stamped `advisory: agent-obeyed, not code-enforced`.**
`oracle_lint`'s Doctrine→Enforcer check (`./oracle lint`) FAILS the build on
any unenforced guarantee. One guarantee per line, verb and enforcer together.
This file consolidates what v1 split across SECURITY / GOVERNANCE /
PROCESSING-MATRIX / ANALYTIC-DOCTRINE. Backup doctrine lives in
`BACKUP-RECOVERY.md` (machine-coupled to `backup.py`).

## 1. Security floor

- All filesystem writes touching a user-/config-influenced path are **contained** inside the root by `safe_paths.contain` (realpath-resolve, reject `..`/absolute/`os.sep` segments, refuse symlinked components); a CI grep-guard (`tests/test_no_bypass_guard.py`) FAILS the build on any kernel bypass.
- Ingest/emit/connector-pull **never destroys** the original: `safe_paths.safe_copy_verify_delete` (copy → fsync → sha256-verify → delete-source) and `ingest_pipeline.stage_external` (copy → sha256-verify, no delete) leave sources intact on any failure.
- Secrets are **never** stored in docs, memory, notes, ledgers, or manifests; they live only in `.env.nosync` (sourced by `load-env.sh`); detection is enforced by `secret_scan.scan_tree` via `./oracle lint`.
- `oracle.yml` `security.scan_exclude:` globs may exempt a large immutable raw-export subtree from the whole-tree secret/external-path scans, but sovereign roots (`Memory.nosync`, `Meta.nosync`), `oracle.yml` and root doctrine `*.md` are **always** scanned regardless of globs — enforced by `oracle_lint` (`_scan_exclude_predicate`).
- Remote connectors are **pull-only** and **default-deny**: every gated fetch authorizes through `actions` before any network call (`tests/test_connectors_remote.py`), a `None`/missing/`[]`/non-list scope allowlist **refuses** the pull, and a `read_write` manifest is **refused** — the FINAL `RemoteConnector.pull` template is not overridable, enforced by `oracle_lint` siblings and `tests/test_connectors_remote.py`.
- Connector network egress is **confined** to two https-only primitives in `_tools/connectors/remote.py`: `http_json` **never** follows a redirect (any 3xx is refused), `http_download` follows **at most one** redirect to an enumerated download-host suffix and **strips** the Authorization header cross-host, streaming-capped at `max_bytes` while reading (Content-Length never trusted); subclasses **cannot** import `urllib` — enforced by `oracle_lint` and `tests/test_connectors_remote.py`.
- The kernel secret scan **exempts** exactly the one literal path `<root>/.env.nosync` (never a glob, never a nested path) as the sanctioned connector-secret store; the rotated-token writer `connectors.remote.persist_rotated_token` is the **only** kernel secret write (contained, atomic, 0o600) — enforced by `oracle_lint` (`check_secrets`, `tests/test_connectors_remote.py::test_lint_exempts_exactly_env_nosync`).
- Immutable records (Source/Finding/Decision/Directive) **cannot** be silently edited: `oracle_lint`'s ledger hash check FAILS on any on-disk vs ledger hash mismatch, forcing supersession (`supersedes:`/`superseded_by:`).
- Event ledgers (`action_event`, `export_event`, `redaction_event`) are **forbidden** to carry content/payload fields by the event schemas (`schema_check` via `./oracle lint`); rows are metadata only, so tracking them in git is safe.

## 2. Processing matrix (the decision table `policy.check_processing` implements)

Environments are the literal arguments to `policy.check_processing(sensitivity,
environment)`: `local_deterministic` (stdlib tools, no inference), `local_agent`
(a model running locally), `external` (any cloud LLM/API/off-machine service).

| Sensitivity | `local_deterministic` | `local_agent` | `external` |
|---|---|---|---|
| public | `allow` | `allow` | `allow` |
| internal | `allow` | `allow` | `deny` — by `policy.py` |
| confidential | `allow` | `allow-minimized` | `deny` — by `policy.py` |
| restricted | `allow-minimized` | `allow-minimized` | `deny` — by `policy.py` |
| secret | `allow-minimized` | `allow-minimized` | `deny` — by `policy.py` |

- An unknown/blank sensitivity label is **forced** to the strictest row (`secret`) by `policy.py` (`_normalize_sensitivity`); an unknown environment is **rejected** with `ValueError` (`_normalize_environment`).
- When two sensitivities apply the stricter **must** win on `policy.SENSITIVITY_ORDER` (`public < internal < confidential < restricted < secret`), enforced by `policy.py`.
- An `external` export of confidential/restricted/secret material **requires** a meaningful admin approval reference; without one `policy.gate_export` (`./oracle policy export`) raises `PermissionError` and writes nothing; placeholder approvals (`none`/`tbd`/`pending`) are **rejected** by `policy.py` (`_is_admin_approval`).
- Every export through `policy.gate_export` is **required** to be logged as a metadata-only `export_event` via `ledger.append`.
- `allow-minimized` minimization levels (advisory: agent-obeyed, not code-enforced): 1 remove names/ids → 2 aggregate to counts/ranges → 3 redact sentences → 4 use public sources only; the kernel records the verdict but does not measure the span.
- advisory: prefer `local_deterministic` over `local_agent` when a deterministic tool gives the same result; in genuine doubt treat the operation as `secret` and ask the admin.

## 3. Roles and capabilities (enforcer: `policy.require_role`)

Roles live in `oracle.yml` → `governance.roles`; `policy.require_role(actor,
role, capability)` raises on denial. `admin` is the authority root (any
capability not in its `cannot` list). `user` is default-deny:

- A `user` capability absent from the role's `can` list is **denied** when `policy.require_role` raises `PermissionError` (default-deny).
- A `user` is **forbidden** its explicit `cannot` list — `change_architecture`, `install_connector`, `change_truth_authority`, `approve_raw_data_export`, `change_security_policy`, `enable_autonomy`, `approve_kernel_upgrade` — each denied by `policy.require_role`.
- Truth-map promotion (draft → confirmed) **requires** `change_truth_authority` via `truth_map.promote_row` (`./oracle admin truth promote`) and `policy.require_role`.
- A Source carrying authority-bearing fields **requires** `change_truth_authority` via `source_record.py` + `policy.require_role`; non-admin ingestion with authority metadata is **downgraded** to an `authority-candidate` Source by `ingest_pipeline.py` (it surfaces in the Review Inbox, never as answer authority).
- Connector pulls **require** `provide_documents` via `connectors/__init__.py` + `policy.require_role` before any bytes are written.
- Schema, security-policy, canonical-move, raw-export-policy, and kernel-upgrade changes each **require** their admin capability (`approve_schema_migration`, `change_security_policy`, `approve_canonical_folder_moves`, `approve_raw_data_export`, `approve_kernel_upgrade`) via `policy.require_role`; kernel upgrades run only through `upgrade.py` (`./oracle admin upgrade apply`), never headless.

### Actor identity (honest limitation)

- advisory: `--actor`/`--role` are flags an agent sets — advisory-plus-logged, not cryptographic identity; every gated action records them to a ledger via `ledger.append`, but the kernel does not verify the person behind the flag. The role GATE is real; the IDENTITY is advisory until session-context identity exists. On shared deployments, wrap privileged commands with external identity checks.

## 4. Session interfaces (enforcer: `session_interface.py` + `policy.py`)

- advisory: a new session starts in the User interface with no startup mode prompt; agent-obeyed unless routed through `./oracle session default` (`_tools/session_interface.py`).
- A blocked control-plane capability is **denied** when routed through `./oracle session gate` (`_tools/session_interface.py`), which returns the Admin-approval prompt: `This requires the Admin interface. Do you approve entering Admin mode for this request?`
- Admin-interface approval is **not** authentication; privileged writes still **require** `policy.require_role` (`./oracle policy role`).
- advisory: goal clarity before execution scales with ambiguity, scope, compute cost, reversibility, and risk; the machine-readable policy is exposed by `./oracle session contract --json` but the dialectic itself is agent-obeyed (one question at a time, recommended answer, inspect local material first).

## 5. Autonomy (enforcer: `actions.py`) — OFF by default, earned by ladder

Config: `Meta.nosync/Autonomy/autonomy.yml`; kill switch: its
`kill_switch_file` sentinel in the same folder. The graduated ladder
(`level: 0..3`): 0 nothing headless / 1 deterministic builtin loops /
2 + dream sessions / 3 + enumerated auto-apply classes.

- Any autonomous action is **denied** by `actions.py` (`./oracle actions status`) when autonomy is not explicitly enabled; a missing/empty `autonomy.yml` means OFF.
- Every action is **denied** by `actions.py` while the kill-switch file exists; it is checked FIRST as a sovereign hard stop (`./oracle actions kill` / `resume`), at every autonomy level.
- An action outside `allowed_loops` / `writable_lanes` / `readonly_connectors` is **denied** by `actions.py` (default-deny allowlist); one exceeding `blast_radius_caps.max_files_per_run` or `max_bytes` is **refused** by `actions.py`.
- Enabling autonomy is **forbidden** to a `user`; it **requires** the `enable_autonomy` admin capability via `policy.require_role`.
- A level promotion without a pending evidence-cited proposal is **refused** by `actions.py` (`./oracle admin autonomy promote`); proposals are drafted from ledger evidence (`_tools/meta_health.py`), never self-applied.
- A critical failure_event, blast-cap breach, or granted-then-failed action since the last level transition **forces** a one-level demotion via `actions.py` (`enforce_demotion_policy`, invoked by `harness`, `_tools/meta_health.py`, and `_tools/capture.py` on critical failures); demotion to level 0 also disables `enabled`.
- A dream session is **denied** below level 2 by `actions.py`; it runs as actor `system:dream` with the user capability set, so control-plane capabilities are **denied** by `policy.require_role`. advisory: its outputs landing `status: needs_review` is agent-obeyed (the charter instructs it; the Review Inbox surfaces everything it derives).
- Truth authority, schema, doctrine, security policy, exports, and connector changes remain **admin-only at every autonomy level**: the `user`/`system:dream` `cannot` list is level-invariant, enforced by `policy.require_role`.

## 5b. Self-improvement (closing the loop)

- No captured signal ages silently: an unconsumed critical/high failure_event older than 7 days, or any unconsumed event older than 30 days, is **surfaced** at the top of the Review Inbox by `_tools/review_queue.py` + `_tools/meta_health.py` (`./oracle meta-health aged`).
- A loop whose last 3 recorded runs all failed is **paused** (status: paused, reason recorded, failure_event captured) by `_tools/meta_health.py`; reactivation is an explicit decision, not a timeout.
- An `applied` improvement carrying neither a machine-checkable `expected_signal` nor `verify: manual` **fails** `oracle_lint` (`improvement-unverifiable`); auto-verifiable improvements are **adjudicated** against observed event ledgers by `_tools/improvements.py` (`./oracle improvements adjudicate`).
- The value scorecard is **computed** from ledger rows with every score citing drop_ids by `_tools/scorecard.py` (`./oracle scorecard gen`); a regressing scorecard, a paused loop, or a critical failure **forces** the architecture-retrospective loop due immediately via `loops.due`.
- A material answer preflighted at the CLI is **logged** as a metadata-only `answer_event` row (object + exit code, never claim text) by `answer_protocol.log_answer_event` (`./oracle answer`).

## 6. Analytic doctrine (how the oracle reasons)

- A material answer **must** pass `answer_protocol.preflight` (`./oracle answer`), which resolves object → truth-map row → authority → freshness → sensitivity → confidence → disconfirmers → contradictions and returns the graduated verdict (0 grounded / 2 supported / 3 caveated / 4 refused). advisory: at the harness level calling it is agent-obeyed; `standing_deliverables.py` and `briefing.py` enforce it for every claim they emit (exit-4 claims are dropped and listed under "needs authority").
- A supported (exit 2) answer **must** carry the label "supported — authority not confirmed" — advisory: agent-obeyed; the envelope supplies the required label and the upgrade command.
- advisory: no source is globally authoritative — authority is by business object (`TRUTH-MAP.md`); `Corroborates` sources never substitute for the primary; source echo (one system copying another) is not independent confirmation.
- advisory: never silently average decision-relevant mismatches — preserve both values, record a Contradiction, and surface it; the answer protocol caveats open `must_resolve` contradictions mechanically.
- advisory: derivations from testimony or ingestion land `status: needs_review` and are reviewed through the Review Inbox (`./oracle review`) before being treated as established; `confidence` is stated as a range, never a point estimate of certainty.
- advisory: derived-memory engines (MemPalace/Graphify under `_data.nosync/derived/`) are rebuildable retrieval aids, never answer authority; exports are sensitivity-capped by `derived_memory.py` + `policy.check_processing`.

## 7. Self-containment

- An external absolute path in `oracle.yml` **fails** the build via `oracle_lint` (`./oracle lint`); external systems are represented as connectors or ingested snapshots, never as load-bearing references.
