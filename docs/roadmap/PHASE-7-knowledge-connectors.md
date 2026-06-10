# Phase 7 — Knowledge Connectors

**Fills the corpus.** The kernel already ships a complete connector discipline —
the `pull/probe/freshness/health` runtime contract, manifest schema, the
`localfolder` reference implementation, and a `readonly_connectors` autonomy
class — but exactly one connector exists, and it only reads a local folder. The
company's actual knowledge lives in Google Drive, SharePoint/OneDrive, Notion,
mailboxes, and Slack history. This phase builds that connector ecosystem on the
existing contract, *unchanged*: an oracle that is reachable everywhere (P4) but
knows nothing is an empty channel, so this phase serves the memory-scale and
source-of-truth goal dimensions and feeds Phase 8 the corpus it tunes against.

Read first: `docs/roadmap/ROADMAP.md`; the vendored kernel's
`_tools/connectors/base.py` + `_tools/connectors/localfolder.py` (the worked
example every connector copies), `_tools/schemas/connector.schema.json`,
`Meta.nosync/Autonomy/autonomy.yml` + `_tools/actions.py` (the gate), and
`_tools/ingest_pipeline.py` (where pulled bytes become source records).

Depends on: Phase 1 only (upgrade plumbing to re-vendor kernel work; doctor
substrate). Runs in parallel with P2/P3. Should land before P4 completes —
gateway reach without content is an empty channel. Per **I3**, tasks marked
*(kernel)* are upstream Oracle Spawn kit work re-vendored via `oracle upgrade`;
tasks marked *(shell)* live in `src/oracle_agent/`.

## The core idea

Every remote connector is a thin, dumb adapter over a shared safety core. A new
`RemoteConnector` base owns everything that must be identical across systems
(scope-allowlist enforcement → metadata listing → per-item fetch to a private
stage → sensitivity classification → policy check → contained landing in
`Workproduct.nosync/_INPUT/<id>/` → cursor advance); subclasses only implement
"list items within scope" and "fetch one item's bytes." This is the same shape
as `localfolder.pull` and P4's `GatewayCore`: an adapter bug can drop a
document but cannot widen access, skip classification, or escape containment
(**I5**). Connectors are kernel-side and **pull-only** — no connector exposes
any write path to the upstream system. All five target systems speak plain
HTTPS+JSON (or stdlib IMAP / a local zip), so the design target is **zero new
dependencies, optional or required**: REST via `urllib`, OAuth 2.0 device flow
in stdlib. I1's graceful-degradation clause is held in reserve — if a connector
ever needs an optional lib, it degrades to disabled-with-doctor-warning, never
a hard dependency.

## Frozen interfaces

### The existing kernel contract (frozen — consumed, not changed)
From `_tools/connectors/base.py` and `_tools/connectors/__init__.py`:
```python
Connector.pull(ctx) -> list[dict]; probe(ctx) -> dict; freshness(ctx) -> dict; health(ctx) -> dict
ConnectorContext(root, manifest, *, actor, role, max_files, now, dry_run, sensitivity_override, gated)
load_manifest(root, id)        # Connectors/<id>/<id>.manifest.yaml, oracle_yaml subset,
                               # validated against schemas/connector.schema.json
connectors.register(key, factory)   # registry keyed by id and access_mode
# CLI: ./oracle connector health [ID] | pull ID [--dry-run] | probe ID | freshness ID
```
Gated pulls already flow through `actions.authorize` (kill-switch → enabled →
allowed_loops → writable_lanes → readonly_connectors → blast_radius_caps; see
`autonomy.yml`), and `ingest_pipeline.run(root, file, *, connector=, sensitivity=)`
already records connector provenance on the immutable source record and emits
review-gated authority proposals. Phase 7 changes none of this.

### `_tools/connectors/remote.py` (new, kernel — the shared safety core)
```python
@dataclass(frozen=True)
class RemoteItem:
    item_id: str; name: str; modified: str; size: int; meta: dict   # metadata only, never bodies
class RemoteConnector(Connector):
    def list_items(self, ctx) -> Iterable[RemoteItem]: ...   # subclass: metadata WITHIN scope allowlist
    def fetch_item(self, ctx, item) -> Path: ...             # subclass: bytes to a private temp stage
    def pull(self, ctx) -> list[dict]                        # FINAL template method (subclasses must not override):
    # scope allowlist (empty = REFUSE, I4) -> _assert_read_only -> list_items -> max_files/blast cap
    # -> classify (intake_classify, manifest floor, UP on ambiguity) -> policy.check_processing
    # (deny = SKIP) -> safe_paths-contained landing in _INPUT/<id>/ -> save_cursor. dry_run plans only.
def http_json(method, url, *, headers, body=None, timeout=30) -> dict  # urllib; https-only (refuse http://, I4);
                                                                       # bounded retry/backoff on 429/5xx
def device_flow(endpoints, client_id, *, out) -> dict     # stdlib OAuth2 device-code flow (Drive/Graph);
                                                           # prints user_code+URL, polls, returns token dict
def resolve_auth(root, manifest) -> dict   # manifest auth.vars NAMES -> values from os.environ then
                                           # <root>/.env.nosync (0o600); raises ConnectorError if unresolved; never logged
def load_cursor(root, cid) -> dict; def save_cursor(root, cid, cur)  # Connectors/<id>/state.json via safe_paths.contain
```
Tokens/credentials exist only inside `pull`'s process frame; they never appear
in manifests, results, ledger rows, `config.json` (the shell's `save_config`
secret-guard already refuses literals), or model context — connector verbs are
kernel subprocesses; the agent loop sees only the verb's metadata output.

### Manifest conventions (one per connector, validating against `connector.schema.json`)
Each connector ships `Connectors/<id>/<id>.manifest.yaml` (from the existing
template) plus a `source` block holding its **default-deny scope allowlist** —
an empty/missing allowlist refuses the pull, it never means "everything":
```yaml
gdrive:       access_mode: api;       source.folder_ids: [...]          # Drive folder allowlist
msgraph:      access_mode: api;       source.sites/drives: [...]        # SharePoint sites / OneDrive drives
notion:       access_mode: api;       source.page_ids/database_ids: [...]
imap-mailbox: access_mode: api;       source.folders: [...]; source.since_days: N
slack-export: access_mode: file_drop; source.path: <export.zip>; source.channels: [...]
```
All declare `permissions: read_only` (the base's `_assert_read_only` refuses
anything else) and `source.default_sensitivity` as the classification FLOOR
(`internal` default; `confidential` for `imap-mailbox` — mail is presumptively
sensitive, and ambiguity always classifies UP, **I4**).

## Tasks

- **P7-T1 — RemoteConnector safety core** *(kernel)*. `remote.py` per the
  frozen interface; extract the staging/landing/classification helpers shared
  with `localfolder.py` rather than duplicating them; register subclasses via
  the existing registry. *Acceptance:* a toy subclass pulls only allowlisted
  items; empty allowlist refuses; `http://` URL refused; policy-denied
  sensitivity skipped; landed paths provably under `_INPUT/<id>/`; cursor
  round-trips; `pull` not overridable without test failure. *Tests:* kernel
  `tests/test_connectors_remote.py` (fake `http_json`). *Deps:* P1.

- **P7-T2 — Google Drive connector** *(kernel)*. `gdrive.py`: OAuth device
  flow + refresh, `files.list` within `folder_ids` (recursing only inside
  them), export Google-native docs to text/office formats, download binaries;
  incremental via per-folder `modifiedTime` cursor. *Acceptance:* fake-API
  pull lands only in-scope files; a shared/shortcut file outside the allowlist
  is refused; expired token refreshes once then fails clean; `health` reports
  `broken` on unresolved auth vars. *Tests:* `tests/test_connector_gdrive.py`.
  *Deps:* P7-T1.

- **P7-T3 — Microsoft Graph connector** *(kernel)*. `msgraph.py`: device-code
  flow against `login.microsoftonline.com`, SharePoint site / OneDrive drive
  allowlist, incremental via Graph delta links persisted in the cursor.
  *Acceptance:* delta response items outside the allowlisted site/drive are
  refused; delta link survives restart; throttling (429 + Retry-After) backs
  off bounded. *Tests:* `tests/test_connector_msgraph.py`. *Deps:* P7-T1.

- **P7-T4 — Notion connector** *(kernel)*. `notion.py`: static integration
  token, page/database allowlist, block-tree → markdown rendering (stdlib),
  incremental via `last_edited_time` cursor. *Acceptance:* a linked database /
  child page outside the allowlist is not followed; rendered markdown carries
  the source page URL in provenance meta; pagination cursors honored. *Tests:*
  `tests/test_connector_notion.py`. *Deps:* P7-T1.

- **P7-T5 — IMAP mailbox connector** *(kernel)*. `imap_mailbox.py`: stdlib
  `imaplib` over SSL, folder allowlist, UID cursor + `since_days` window,
  message → text body + per-attachment files (each classified individually);
  opens folders with `EXAMINE` (read-only) so flags are never mutated.
  *Acceptance:* fake-IMAP pull is read-only (no STORE/SELECT-writes), honors
  the folder allowlist and UID cursor, default sensitivity floor is
  `confidential`. *Tests:* `tests/test_connector_imap.py`. *Deps:* P7-T1.

- **P7-T6 — Slack export connector** *(kernel)*. `slack_export.py`: reads an
  admin-downloaded workspace export zip from `source.path` (no token, no
  network), channel allowlist, renders per-channel-per-day markdown
  transcripts; zip members are containment-checked before extraction (a
  zip-slip `../` or absolute member is refused, never written). *Acceptance:*
  crafted malicious zip refused; only allowlisted channels land; re-pull of
  the same export is idempotent (cursor by export hash). *Tests:*
  `tests/test_connector_slack_export.py`. *Deps:* P7-T1.

- **P7-T7 — secrets lifecycle** *(shell + kernel)*. Manifests carry env-var
  NAMES only (`auth.vars`); values live in the root's `.env.nosync`, written
  by the shell via `config.py`'s atomic 0o600 no-chmod-race writer (same
  discipline as `set_env_secret`); `resolve_auth` reads env then `.env.nosync`;
  rotation = re-run the wizard step (upsert), revocation = remove the var and
  set manifest `status: deprecated`. *Acceptance:* a literal token offered to
  `config.json` is refused by the existing secret-guard; `.env.nosync` lands
  0o600 atomically; `secret_scan scan` of the root stays clean after setup;
  a removed var flips `health` to `broken` with a doctor fix-line. *Tests:*
  shell `test_connector_secrets.py`. *Deps:* P7-T1.

- **P7-T8 — wizard connector step** *(shell)*. `oracle setup` gains an
  optional "Connect knowledge sources?" step: pick connector(s), write the
  manifest from the template (scope allowlist prompted explicitly — never
  defaulted to "everything"), collect secrets via getpass (never echoed), run
  the device flow where applicable, then `probe` + `pull --dry-run` and show
  the plan before any bytes move. Idempotent like the rest of the wizard.
  *Acceptance:* wizard-produced manifest validates against the schema; blank
  answers skip cleanly; dry-run plan shown before first real pull. *Tests:*
  shell `test_wizard_connectors.py` (scripted streams). *Deps:* P7-T1, P7-T7;
  per-connector prompts as T2–T6 land.

- **P7-T9 — scheduled pulls through the autonomy gate** *(kernel)*. A builtin
  `connector-pull` loop the harness can run headless: for each manifest-due
  connector (its `freshness`/`health_check` cadence), pull with `gated=True`
  so `actions.with_action` enforces kill-switch / `enabled` /
  `allowed_loops` / `writable_lanes` (`_INPUT`) / `readonly_connectors` /
  blast caps, then ingest the newly landed files via `ingest_pipeline.run_batch`
  under the same grant — source records and authority proposals stay
  review-gated in the inbox. *Acceptance:* with spawn-default autonomy (OFF)
  the loop logs intended/denied action events and moves zero bytes; with the
  connector allowlisted, a pull+ingest runs within caps and every file appears
  as a source record with connector provenance; an over-cap plan is refused
  before any fetch. *Tests:* kernel `tests/test_connector_pull_loop.py`.
  *Deps:* P7-T1, ≥1 of P7-T2..T6.

- **P7-T10 — doctor + dashboard connector health** *(shell + kernel)*. Shell
  doctor adds per-instance connector rows (manifest schema-valid, auth vars
  resolvable, `health` not `broken` — with one-line fixes); kernel
  `dashboard._panel_connectors` upgrades from "N installed" to per-connector
  health/freshness rows with pull controls. *Acceptance:* a misconfigured
  connector yields a doctor `[fail|warn]` with a fix line; dashboard shows
  health state + last-pull age per connector; doctor stays read-only (probes,
  never pulls). *Tests:* extend shell `test_doctor.py`; kernel
  `tests/test_dashboard.py`. *Deps:* P7-T1..T6 (any), P7-T7.

- **P7-T11 — first-run experience** *(shell)*. After the wizard step, the
  admin watches the corpus arrive: confirmed first pull + ingest streams
  progress (pulled / ingested / skipped-by-policy / queued-for-review counts),
  and doctor/status reflect growth — the existing zero-sources warning clears
  and the source count rises. The low-admin promise made concrete: connect a
  source, watch the oracle learn, see what awaits review. *Acceptance:* fresh
  instance + one connector ⇒ progress lines during pull/ingest; doctor flips
  from "no ingested sources" to "N source(s) ingested"; review inbox lists the
  new authority proposals. *Tests:* shell `test_first_run_connectors.py`
  (fake connector). *Deps:* P7-T8, ≥1 of P7-T2..T6.

## Security invariants for this phase

- Connectors are **pull-only**: every API call each connector makes is
  enumerated in its spec and reviewed read-only; no verb that mutates the
  upstream system exists in the code (IMAP uses `EXAMINE`; the base refuses
  `permissions: read_write` manifests).
- Scope allowlists are **default-deny** (**I4**): an empty allowlist refuses
  the pull; out-of-scope items returned by an API (shares, deltas, linked
  pages) are refused per item.
- `RemoteConnector.pull` is the single decision point (**I5**); subclasses
  cannot bypass classification, the policy gate, blast caps, or `safe_paths`
  containment — the localfolder discipline, inherited.
- Every fetched document enters through the ingest pipeline as an immutable
  source record with connector provenance; authority is review-gated; the
  session-ritual `secret_scan` covers anything pulled into the tree;
  sensitivity classifies UP on ambiguity.
- Credentials live only in `.env.nosync` / process env (`token_env`-style
  names everywhere else); they never reach manifests, `config.json`, ledgers,
  logs, or model context. Unattended pulls run only under the
  `readonly_connectors` action class with autonomy ON, allowlisted, in-cap.

## Stress pass (before coding)

Per connector, attack: scope-creep paths (Drive shortcuts/shared drives, Graph
delta leakage, Notion linked databases, IMAP wildcard folders, export zip-slip
and symlinked members); token theft surfaces (device-flow phishing text,
refresh-token over-scoping, tokens in tracebacks/ledgers); poisoned content
(prompt-injection text in fetched documents must land as review-gated evidence,
never as instructions or authority); blast-radius abuse (huge mailboxes/drives
vs caps, pathological pagination); and secret-bearing documents (must classify
UP and be caught by `secret_scan`). Append findings to this spec; the
default-deny allowlist and pull-only properties must survive them.

## Definition of done

- [ ] `RemoteConnector` core landed; all five connectors registered, each
      honoring its default-deny scope allowlist and the pull-only contract.
- [ ] Zero new required dependencies; any optional lib degrades to
      disabled-with-doctor-warning (I1).
- [ ] Wizard step + secrets lifecycle: setup → device flow → dry-run plan →
      first pull, with credentials only ever in `.env.nosync`/env.
- [ ] Scheduled pulls run only through the autonomy gate; OFF-by-default
      verified (intended/denied events, zero bytes).
- [ ] Doctor + dashboard show per-connector health; first-run experience
      demonstrates corpus growth end-to-end on a fresh instance.
- [ ] Kernel work landed upstream and re-vendored (I3); SECURITY.md gains the
      pull-only / default-deny / credential-isolation guarantees with named
      enforcers (I6); `make check` green; CI green.
