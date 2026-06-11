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
example every connector copies — **with the stress-pass corrections below: its
classification call and landing-name scheme are NOT copied as-is**, see
P7S-14/P7S-16), `_tools/schemas/connector.schema.json`,
`Meta.nosync/Autonomy/autonomy.yml` + `_tools/actions.py` (the gate), and
`_tools/ingest_pipeline.py` (where pulled bytes become source records).

Depends on: Phase 1 only (upgrade plumbing to re-vendor kernel work; doctor
substrate; `testkit.py`, which every shell test plan in this phase uses). Runs
in parallel with P2/P3. Should land before P4 completes — gateway reach without
content is an empty channel. Per **I3**, tasks marked *(kernel)* are upstream
Oracle Spawn kit work re-vendored via `oracle upgrade`; tasks marked *(shell)*
live in `src/oracle_agent/`.

## The core idea

Every remote connector is a thin, dumb adapter over a shared safety core. A new
`RemoteConnector` base owns everything that must be identical across systems
(gate-first authorization → scope-allowlist enforcement → metadata listing →
per-item fetch to a private stage through the one capped download primitive →
content-based sensitivity classification → policy check → contained landing in
`Workproduct.nosync/_INPUT/<id>/` under a stable per-item name → atomic cursor
advance); subclasses only implement "list items within scope" and "fetch one
item's bytes." This is the same shape as `localfolder.pull` and P4's
`GatewayCore`: an adapter bug can drop a document but cannot widen access, skip
classification, escape containment, follow a redirect off-host, or exceed the
byte cap (**I5**) — because the primitives that could do those things live only
in the core. Connectors are kernel-side and **pull-only** — no connector
exposes any write path to the upstream system. All five target systems speak
plain HTTPS+JSON (or stdlib IMAP / a local zip), so the design target is
**zero new dependencies, optional or required**: REST via `urllib`, OAuth 2.0
in stdlib — the device-code flow for Microsoft Graph, and the loopback
installed-app flow (one-shot `http.server` on literal `127.0.0.1`) for Google,
whose device flow cannot grant Drive read scopes (P7S-1). I1's
graceful-degradation clause is held in reserve — if a connector ever needs an
optional lib, it degrades to disabled-with-doctor-warning, never a hard
dependency.

## Frozen interfaces

### The existing kernel contract (frozen — consumed, not changed)
From `_tools/connectors/base.py` and `_tools/connectors/__init__.py`:
```python
Connector.pull(ctx) -> list[dict]; probe(ctx) -> dict; freshness(ctx) -> dict; health(ctx) -> dict
ConnectorContext(root, manifest, *, actor, role, max_files, now, dry_run, sensitivity_override, gated)
load_manifest(root, id)        # Connectors/<id>/<id>.manifest.yaml, oracle_yaml subset,
                               # validated against schemas/connector.schema.json
connectors.register(key, factory)   # registry; NEW connectors register by ID ONLY (see below)
# CLI: ./oracle connector health [ID] | pull ID [--dry-run] | probe ID | freshness ID
```
Gated pulls already flow through `actions.authorize` (kill-switch → enabled →
allowed_loops → writable_lanes → readonly_connectors → blast_radius_caps; see
`autonomy.yml`), and `ingest_pipeline.run(root, file, *, connector=, sensitivity=)`
already records connector provenance on the immutable source record and emits
review-gated authority proposals. Phase 7 changes none of this contract — but
it DOES fix two shipped bugs in the surrounding plumbing (kernel work, in
P7-T1): `_planned_pull_scope` hardcodes the loop id `"connector-health"` and
declares `bytes: 0` whenever the file cap clamps the plan (P7S-17/P7S-20);
both are corrected as specced below.

### `_tools/connectors/remote.py` (new, kernel — the shared safety core)
```python
@dataclass(frozen=True)
class RemoteItem:
    item_id: str; name: str; modified: str; size: int; meta: dict   # metadata only, never bodies
    # item_id is the upstream system's STABLE identifier; it keys both the
    # landing filename and the cursor (P7S-14). size may be -1 (unknown).

class RemoteConnector(Connector):
    def list_items(self, ctx) -> Iterable[RemoteItem]: ...   # subclass: metadata WITHIN scope allowlist
    def fetch_item(self, ctx, item) -> Path: ...             # subclass: bytes to a private temp stage,
                                                             # via http_download ONLY (P7S-8)
    def pull(self, ctx) -> list[dict]   # FINAL template method (subclasses must not override):
    # 0. gated=True ⇒ actions.authorize FIRST with a cap-derived declared scope —
    #    NO network call (probe/list/fetch) happens before the grant (P7S-18);
    #    declared files/bytes are the caps themselves when probe data is absent
    #    (unknown never declares 0 — fail closed, P7S-17).
    # 1. scope allowlist: None / missing / [] / non-list ALL refuse (I4, P7S-13)
    # 2. _assert_read_only -> list_items -> max_files cap, PLUS a running
    #    landed-byte counter that ABORTS the pull at max_bytes and logs a
    #    failure_event (runtime enforcement, not just plan-time; P7S-17)
    # 3. classify via intake_classify.classify_file(path, connector_default=floor)
    #    — content signals, not filename-only; the manifest floor is the
    #    classification FLOOR; UP on ambiguity (P7S-16)
    # 4. policy.check_processing (deny = SKIP)
    # 5. safe_paths-contained landing at
    #    _INPUT/<id>/<sha256(item_id)[:12]>_<slug><suffix> — STABLE per item
    #    across pulls (supersession keys on origin_filename) and UNIQUE per
    #    item (no cross-item overwrite; P7S-14)
    # 6. save_cursor (atomic). dry_run performs 0–5 and reports the plan only.
    #
    # Result vocabulary (P7S-12):
    #   ingested | planned | failed
    #   skipped_policy           (sensitivity denied — expected, rc 0)
    #   skipped_out_of_scope     (API returned shares/deltas/links outside the
    #                             allowlist — EXPECTED, rc 0, never a
    #                             failure_event / demotion signal)
    #   refused                  (containment, zip-slip, read_write manifest —
    #                             security violations only; rc 1;
    #                             failure-ledger-worthy)

def http_json(method, url, *, headers, body=None, timeout=30) -> dict
    # urllib; https-only (refuse http://, I4); NO redirects, ever — any 3xx
    # raises (token endpoints and JSON APIs never legitimately redirect);
    # bounded retry/backoff on 429 (honoring Retry-After) and 5xx.

def http_download(url, dest_stage, *, headers, max_bytes, timeout=60) -> Path
    # THE one byte-fetch primitive; subclasses never import urllib (enforced
    # by test; P7S-8). https-only. Streams to the private stage enforcing
    # max_bytes WHILE reading — Content-Length is never trusted (P7S-15/17).
    # Redirect policy (P7S-7): follows AT MOST ONE redirect, only to https,
    # only to the calling connector's enumerated download-host suffixes
    # (e.g. *.googleusercontent.com, *.sharepoint.com), and ALWAYS strips the
    # Authorization header on any cross-host hop. Everything else raises.

def device_flow(endpoints, client_id, *, client_secret=None, out) -> dict
    # stdlib OAuth2 device-code flow — used by msgraph (public client, no
    # secret). Prints user_code + verification URL, polls, returns token dict.
    # client_secret accepted for providers that require it (P7S-1).

def loopback_flow(endpoints, client_id, client_secret, *, scopes, out) -> dict
    # stdlib installed-app flow — used by gdrive (P7S-1: Google's device flow
    # cannot grant Drive read scopes). One-shot http.server bound to literal
    # 127.0.0.1 on an ephemeral port (refuses any non-loopback bind), state +
    # PKCE, prints the browser URL, captures the code, exchanges, returns the
    # token dict. Consistent with the literal-loopback discipline (STRESS C2/L1).

def resolve_auth(root, manifest) -> dict   # manifest auth.vars NAMES -> values from os.environ then
                                           # <root>/.env.nosync (0o600); raises ConnectorError if unresolved; never logged

def persist_rotated_token(root, var_name, value) -> None
    # The ONE sanctioned kernel-side secret write (P7S-2): when a provider
    # rotates a refresh token mid-pull (Microsoft does), the new value is
    # upserted into <root>/.env.nosync via a contained, atomic, 0o600
    # temp+rename writer carrying the no-bypass marker — an explicitly
    # documented exception, mirroring the kernel's backup exception pattern.
    # The value is never logged, never echoed, never placed in a result dict.

def redact(text) -> str
    # Strips URL query strings and Bearer/token-shaped substrings. EVERY
    # result/error/exception string that leaves pull (results, CLI payloads,
    # action_event reasons) passes through it — pre-signed download URLs
    # (Graph @microsoft.graph.downloadUrl, Drive export links) carry
    # credentials in their query strings (P7S-9).

def load_cursor(root, cid) -> dict; def save_cursor(root, cid, cur)
    # Connectors/<id>/state.json via safe_paths.contain(base="Connectors").
    # save_cursor is atomic (temp + os.replace). An unparseable/torn cursor
    # loads as {} with a logged warning — fail-closed means a full re-pull,
    # which is safe (idempotent landing names) if quota-expensive (P7S-23).
```
Tokens/credentials exist only inside `pull`'s process frame plus the two
sanctioned stores (`<root>/.env.nosync`, process env); they never appear in
manifests, results, ledger rows, `config.json` (the shell's `save_config`
secret-guard already refuses literals), or model context — connector verbs are
kernel subprocesses; the agent loop sees only the verb's redacted metadata
output.

**Registry, loop identity, freshness (kernel fixes pinned here):**
- New connectors `register(id, factory)` by **id only** — never by
  `access_mode` (`"api"` would collide across four connectors and `"file_drop"`
  would capture every future drop connector; P7S-6). Unknown-id resolution
  falls back to the manifest's required `system` field against a
  system→factory map, so a second account (`id: gdrive-finance`,
  `system: gdrive`) resolves to the gdrive class, never to another "api"
  connector. Multi-account = one `Connectors/<id>/` dir per account, each with
  its own manifest, allowlist, cursor, and auth var names.
- The canonical gated-pull loop id is **`connector-pull`**.
  `_planned_pull_scope` is fixed to declare it (today it hardcodes
  `"connector-health"`, which would make every admin allowlist entry for
  `connector-pull` a permanent deny; P7S-20) and to declare fail-closed bytes:
  when the probe cannot price the plan, the declared bytes are the cap itself,
  never 0 (P7S-17). `connector-pull` is **never** added to
  `actions.DETERMINISTIC_LOOPS` — credentialed network egress is not a level-1
  deterministic loop; only an explicit `allowed_loops` entry admits it.
- `RemoteConnector.freshness` derives its verdict from the cursor's
  `last_success_ts`, not from manifest `freshness.last_verified` — a pull
  never rewrites its own manifest (an autonomous write to admin config), and
  without this a scheduled connector reports stale forever (P7S-23).
- API base URLs are **pinned in connector code**, never read from the manifest
  — otherwise a manifest edit exfiltrates any resolvable env secret to an
  arbitrary host (P7S-5). The IMAP server host is the single
  manifest-supplied endpoint, and it requires certificate-verified
  `IMAP4_SSL` (below).

### Shell-side interface (new, `src/oracle_agent/config.py`)
```python
config.write_root_env_secret(root, key, value) -> None
    # NEW (P7S-4): upserts KEY=VALUE into <root>/.env.nosync via the existing
    # _atomic_write (0o600 under 0o077 umask, temp+rename, no chmod race).
    # set_env_secret targets the PROFILE ~/.oracle/.env and is WRONG for
    # connector creds: the shell scrubs *_KEY/_TOKEN/_SECRET/_PASSWORD vars
    # from every kernel subprocess env (verbtools._scrubbed_env), so only the
    # root's own .env.nosync is visible to a scheduled kernel pull.
```

### Manifest conventions (one per connector, validating against `connector.schema.json`)
Each connector ships `Connectors/<id>/<id>.manifest.yaml` (from the existing
template) plus a `source` block holding its **default-deny scope allowlist**.
The schema gains the `source` block (per-connector allowlist key names,
`default_sensitivity` enum, `max_files`, `cadence`) so a typo'd allowlist key
fails validation instead of silently meaning "missing" (P7S-13 — the schema is
kernel-owned but is NOT in this phase's frozen set). Allowlist semantics: a
value of `None` (bare `key:` in the oracle_yaml subset), a missing key, `[]`,
or any non-list ALL refuse the pull with a doctor-friendly message — empty
never means "everything":
```yaml
gdrive:       access_mode: api;       source.folder_ids: [...]          # Drive folder allowlist
msgraph:      access_mode: api;       source.sites/drives: [...]        # SharePoint sites / OneDrive drives
notion:       access_mode: api;       source.page_ids/database_ids: [...]
imap-mailbox: access_mode: api;       source.host/folders: [...]; source.since_days: N
slack-export: access_mode: file_drop; source.path: <export.zip>; source.channels: [...]
```
All declare `permissions: read_only` (the base's `_assert_read_only` refuses
anything else) and `source.default_sensitivity` as the classification FLOOR
(`internal` default; `confidential` for `imap-mailbox` — mail is presumptively
sensitive, and ambiguity always classifies UP, **I4**). Scheduling cadence is
a pinned grammar, not free prose (P7S-24): `source.cadence` ∈
`hourly | daily | weekly | <N>h | <N>d` (default `daily`); the scheduled-pull
loop parses exactly this vocabulary and falls back to `daily` on anything else
(logged, never a crash).

## Tasks

- **P7-T1 — RemoteConnector safety core** *(kernel)*. `remote.py` per the
  frozen interface; extract the staging/landing helpers shared with
  `localfolder.py` — but FIX, do not copy, the two latent bugs the stress pass
  found in the worked example: pull-time classification calls
  `intake_classify.classify_file(path, connector_default=floor)` (localfolder's
  `classify(path, …)` call raises TypeError and silently degrades to
  filename-keywords-only; P7S-16), and landing names are
  `<sha256(item_id)[:12]>_<slug><suffix>`, not date-prefixed (date prefixes
  break supersession and allow same-day cross-item overwrite; P7S-14). Fix
  `_planned_pull_scope` (loop id `connector-pull`; fail-closed bytes) and
  register subclasses id-only with system-fallback resolution. *Acceptance:*
  a toy subclass pulls only allowlisted items; `None`/missing/`[]` allowlist
  all refuse; `http://` URL refused; a 3xx from `http_json` raises; an
  `http_download` redirect to a non-enumerated or cross-host target raises and
  Authorization is stripped on the one allowed hop; a stream exceeding
  `max_bytes` aborts mid-read regardless of Content-Length; a gated pull
  performs ZERO network calls when `authorize` denies (kill-switch / OFF /
  unlisted); a file whose CONTENT (not name) carries restricted signals
  classifies up at pull time; policy-denied sensitivity skipped; landed paths
  provably under `_INPUT/<id>/` with stable names (re-pull of a changed item
  reuses its name; two same-named items land separately); out-of-scope items
  report `skipped_out_of_scope` with rc 0 while a containment violation
  reports `refused` with rc 1; every emitted string is `redact()`-clean given
  a poisoned URL; cursor round-trips, survives a torn write (loads `{}` +
  warning), and `freshness` reflects cursor `last_success_ts`; `pull` not
  overridable without test failure; a subclass module importing `urllib`
  fails the no-direct-urllib test; localfolder's existing tests stay green.
  *Tests:* kernel `tests/test_connectors_remote.py` (fake `http_json` /
  `http_download`). *Deps:* P1.

- **P7-T2 — Google Drive connector** *(kernel)*. `gdrive.py`: **loopback
  installed-app OAuth flow** (`loopback_flow`; Google's device flow cannot
  grant Drive read scopes — P7S-1) + refresh; `files.list` within `folder_ids`
  (per-parent recursion only inside them, with `supportsAllDrives`/
  `includeItemsFromAllDrives`/`corpora` pinned so shared drives actually
  list); export Google-native docs per a pinned export matrix (docs→docx/text,
  sheets→csv, slides→text), with the ~10 MB `files.export` limit handled as
  skip-with-result-row (never a pull failure; P7S-25); download binaries via
  `http_download` (`alt=media`); incremental via per-folder `modifiedTime`
  cursor PLUS a periodic full re-list (or seen-id set) so files *moved into*
  scope with old timestamps are not silently missed (P7S-11). Shortcuts
  (`application/vnd.google-apps.shortcut`) and multi-parent files resolve by
  target/path: out-of-allowlist targets are `skipped_out_of_scope`.
  *Acceptance:* fake-API pull lands only in-scope files; a shared/shortcut
  file outside the allowlist is skipped-out-of-scope (rc 0), never fetched;
  an oversized native doc is skipped with a result row; a moved-in old file
  is caught by the re-list; expired access token refreshes once then fails
  clean; `health` reports `broken` on unresolved auth vars. *Tests:*
  `tests/test_connector_gdrive.py`. *Deps:* P7-T1.

- **P7-T3 — Microsoft Graph connector** *(kernel)*. `msgraph.py`: device-code
  flow against `login.microsoftonline.com` (public client), SharePoint site /
  OneDrive drive allowlist, incremental via Graph delta links persisted in the
  cursor; a 410 Gone on the delta link resets the cursor and performs a full
  resync with one logged ledger row (P7S-11); `/content` downloads go through
  `http_download` (Graph 302s to a pre-authenticated host by design — the
  single enumerated-host hop, Authorization stripped; P7S-7); **rotated
  refresh tokens are persisted via `persist_rotated_token`** (Microsoft
  rotates them and expires idle ones on a ~90-day sliding window; without
  persistence every scheduled pull dies ≤90 days after setup; P7S-2).
  *Acceptance:* delta response items outside the allowlisted site/drive are
  skipped-out-of-scope per item; delta link survives restart; 410 triggers
  reset+resync (not a crash, not a silent skip); a rotated refresh token in a
  token response lands in `.env.nosync` atomically at 0o600 and is used on
  the next refresh; throttling (429 + Retry-After) backs off bounded.
  *Tests:* `tests/test_connector_msgraph.py`. *Deps:* P7-T1.

- **P7-T4 — Notion connector** *(kernel)*. `notion.py`: static integration
  token, page/database allowlist with **pinned scope semantics** (P7S-10): an
  item is in scope iff its parent chain reaches an allowlisted page/database
  via `child_page`/`child_block` edges — allowlisted roots and their
  transitive children are pulled; `link_to_page`, mentions, and linked
  databases are NEVER followed, and the parent-chain check runs per item
  (integration-level sharing is not trusted as the boundary); block-tree →
  markdown rendering (stdlib), incremental via `last_edited_time` cursor.
  *Acceptance:* a child page of an allowlisted page IS pulled; a linked
  database / mentioned page outside the chain is NOT followed
  (skipped_out_of_scope); rendered markdown carries the source page URL in
  provenance meta (redacted of query strings); pagination cursors honored;
  429 + Retry-After honored. *Tests:* `tests/test_connector_notion.py`.
  *Deps:* P7-T1.

- **P7-T5 — IMAP mailbox connector** *(kernel)*. `imap_mailbox.py`: stdlib
  `imaplib.IMAP4_SSL` with a default-verifying `ssl.create_default_context()`
  — never plain `IMAP4`, never optional STARTTLS (the host is the one
  manifest-supplied endpoint; P7S-5); **v1 auth = username + app password**
  (consumer Gmail/O365 have retired basic auth except app passwords, and
  Google's device flow cannot grant the mail scope — P7S-26; the doctor
  fix-line says "app password"); folder allowlist, UID cursor + `since_days`
  window, with **UIDVALIDITY persisted in the cursor and a mismatch resetting
  it** (UIDs are meaningless across UIDVALIDITY changes; P7S-23); message →
  text body + per-attachment files (each classified individually); opens
  folders with `EXAMINE` (read-only) so flags are never mutated.
  *Acceptance:* fake-IMAP pull is read-only (no STORE/SELECT-writes), honors
  the folder allowlist and UID cursor, resets cleanly on a UIDVALIDITY
  change, refuses an unverified/plain connection, default sensitivity floor
  is `confidential`. *Tests:* `tests/test_connector_imap.py`. *Deps:* P7-T1.

- **P7-T6 — Slack export connector** *(kernel)*. `slack_export.py`: reads an
  admin-downloaded workspace export zip from `source.path` (no token, no
  network), channel allowlist, renders per-channel-per-day markdown
  transcripts. Zip member validation is the full checklist (P7S-15), not just
  traversal: reject `../` and absolute member names; reject symlink members
  (external_attr S_IFLNK); enforce per-member and total decompressed caps
  WHILE streaming (zipinfo declared sizes are never trusted); cap member
  count; nested archives are not descended into (landed as opaque members,
  subject to the same caps). *Acceptance:* crafted malicious zips (traversal,
  absolute, symlink member, decompression bomb lying about its size,
  excessive member count) are refused, never written; only allowlisted
  channels land; re-pull of the same export is idempotent (cursor by export
  hash). *Tests:* `tests/test_connector_slack_export.py`. *Deps:* P7-T1.

- **P7-T7 — secrets lifecycle** *(shell + kernel)*. Manifests carry env-var
  NAMES only (`auth.vars`); values live in the **root's** `.env.nosync`,
  written by the shell via the NEW `config.write_root_env_secret(root, key,
  value)` (the existing `_atomic_write` 0o600 no-chmod-race path;
  `set_env_secret` writes the profile `.env`, which the kernel subprocess can
  never see — P7S-4); `resolve_auth` reads env then `.env.nosync`; rotation =
  re-run the wizard step (upsert) for static creds, `persist_rotated_token`
  for provider-rotated refresh tokens (P7S-2); revocation = remove the var
  AND revoke at the provider (removing the var disables the connector; the
  upstream token stays valid until revoked there — the doctor fix-line says
  both; P7S-6 honesty clause) and set manifest `status: deprecated`. Kernel
  sub-task (P7S-3): `oracle_lint.check_secrets` exempts **exactly the one
  literal path `<root>/.env.nosync`** (never a glob, never a directory) with
  a named enforcer test, and DOCTRINE.md gains the line stating the exemption
  and its enforcer — without this, the first real token turns `./oracle lint`
  red forever. *Acceptance:* a literal token offered to `config.json` is
  refused by the existing secret-guard; `.env.nosync` lands 0o600 atomically;
  `secret_scan scan` of a root carrying a real-shaped placeholder token in
  `.env.nosync` stays clean while the same token in ANY other file is
  flagged; a kernel `connector pull` spawned with `_scrubbed_env()` still
  resolves its auth vars (proving the root-file path works where process env
  cannot); a removed var flips `health` to `broken` with a doctor fix-line.
  *Tests:* shell `test_connector_secrets.py` (on `testkit.spawn_test_root`);
  kernel lint-exemption test. *Deps:* P7-T1.

- **P7-T8 — wizard connector step** *(shell)*. `oracle setup` gains an
  optional "Connect knowledge sources?" step: pick connector(s), write the
  manifest from the template (scope allowlist prompted explicitly — never
  defaulted to "everything"), collect secrets via getpass (never echoed) into
  the root's `.env.nosync` via `write_root_env_secret`, run the auth flow
  where applicable (loopback flow for gdrive, device flow for msgraph) with
  provider-app provisioning instructions printed (Google: GCP OAuth client +
  consent screen; Microsoft: app registration — P7S-25), then `probe` +
  `pull --dry-run` and show the plan before any bytes move. The confirmed
  first pull + ingest runs **under the per-root flock** (`~/.oracle/locks/
  <instance>.lock`) so it cannot race a `serve` tick on the same root
  (P7S-22; a direct kernel-CLI pull by an admin is inherently outside the
  shell's locks — documented, admin-at-own-risk). Idempotent like the rest of
  the wizard. *Acceptance:* wizard-produced manifest validates against the
  schema (including the new `source` block); blank answers skip cleanly;
  dry-run plan shown before first real pull; the lock is held across the
  pull+ingest. *Tests:* shell `test_wizard_connectors.py` (testkit scripted
  streams). *Deps:* P7-T1, P7-T7; per-connector prompts as T2–T6 land.

- **P7-T9 — scheduled pulls through the autonomy gate** *(kernel)*. A builtin
  **`connector-pull`** loop (the canonical id everywhere — scope, allowlist,
  loop note; P7S-20; deliberately NOT in `DETERMINISTIC_LOOPS`, so level 1
  alone never admits it) the harness can run headless: for each manifest-due
  connector (its `source.cadence` per the pinned grammar), pull with
  `gated=True` so `actions.with_action` enforces kill-switch / `enabled` /
  `allowed_loops` / `writable_lanes` (`_INPUT`) / `readonly_connectors` /
  blast caps — authorization runs BEFORE any probe/list network call
  (P7S-18) — then ingest the newly landed files via
  `ingest_pipeline.run_batch(..., connector=<id>,
  sensitivity=<manifest floor>)` under the same grant, so the floor survives
  into ingest-time re-classification (P7S-16) and source records and
  authority proposals stay review-gated in the inbox, tagged with their
  connector provenance (P7S-19). `skipped_out_of_scope` rows are never
  failure_events and never feed `enforce_demotion_policy` — an outsider
  sharing files at an allowlisted folder must not be able to demote the
  autonomy level (P7S-12). *Acceptance (per path, A5-consistent — P7S-21):*
  via the SHELL SCHEDULER with autonomy OFF, the tick is skipped entirely —
  zero rows, zero bytes, zero network (the A5 shortcut); via a DIRECT kernel
  `harness --once` with autonomy OFF, the loop logs intended/denied action
  events and performs zero network calls and zero bytes (authorize-before-
  probe); with the connector + `connector-pull` allowlisted, a pull+ingest
  runs within caps and every file appears as a source record with connector
  provenance and the manifest floor honored; an over-cap plan (files OR
  bytes, including unknown-size fail-closed pricing) is refused before any
  fetch; a mid-pull byte-cap breach aborts and logs a failure_event.
  *Tests:* kernel `tests/test_connector_pull_loop.py`. *Deps:* P7-T1, ≥1 of
  P7-T2..T6.

- **P7-T10 — doctor + dashboard connector health** *(shell + kernel)*. Shell
  doctor adds per-instance connector rows (manifest schema-valid, auth vars
  resolvable, `health` not `broken` — with one-line fixes); kernel
  `dashboard._panel_connectors` upgrades from "N installed" to per-connector
  health/freshness rows with pull controls — and FIXES discovery: it reuses
  `connectors._known_connector_ids` (manifests live at
  `Connectors/<id>/<id>.manifest.yaml`; the current top-level
  `Connectors/*.manifest.yaml` glob counts zero installed connectors; P7S-28).
  *Acceptance:* a misconfigured connector yields a doctor `[fail|warn]` with
  a fix line; dashboard shows health state + last-pull age (from the cursor,
  per the freshness-from-cursor rule) per connector, discovered from the real
  manifest layout; doctor stays read-only (probes, never pulls — noting that
  a remote probe is authenticated egress, which doctor already performs for
  the LLM provider). *Tests:* extend shell `test_doctor.py`; kernel
  `tests/test_dashboard.py`. *Deps:* P7-T1..T6 (any), P7-T7.

- **P7-T11 — first-run experience** *(shell)*. After the wizard step, the
  admin watches the corpus arrive: confirmed first pull + ingest streams
  progress (pulled / ingested / skipped-by-policy / skipped-out-of-scope /
  queued-for-review counts), runs under the per-root flock (P7S-22), and
  doctor/status reflect growth — the existing zero-sources warning clears and
  the source count rises. Review-inbox items arising from connector-sourced
  documents carry their connector tag so a flood of attacker-supplied
  attachments is visibly attributable (P7S-19). The low-admin promise made
  concrete: connect a source, watch the oracle learn, see what awaits review.
  *Acceptance:* fresh instance + one connector ⇒ progress lines during
  pull/ingest; doctor flips from "no ingested sources" to "N source(s)
  ingested"; review inbox lists the new authority proposals with connector
  provenance. *Tests:* shell `test_first_run_connectors.py` (testkit fake
  connector). *Deps:* P7-T8, ≥1 of P7-T2..T6.

## Security invariants for this phase

- Connectors are **pull-only**: every API call each connector makes is
  enumerated in its spec and reviewed read-only; no verb that mutates the
  upstream system exists in the code (IMAP uses `EXAMINE`; the base refuses
  `permissions: read_write` manifests).
- Scope allowlists are **default-deny** (**I4**): `None`/missing/`[]`/non-list
  all refuse the pull; out-of-scope items returned by an API (shares, deltas,
  linked pages) are skipped per item — and that skip is an *expected*
  outcome, never a failure_event, so it cannot be weaponized into an autonomy
  demotion (P7S-12).
- `RemoteConnector.pull` is the single decision point (**I5**); subclasses
  cannot bypass classification, the policy gate, blast caps (plan-time AND a
  runtime byte counter), `safe_paths` containment, or the HTTP discipline —
  `http_json`/`http_download` are the only network primitives, https-only,
  no-redirect (one enumerated-host hop for downloads, Authorization stripped
  cross-host), streaming-capped. API base URLs are pinned in connector code,
  never manifest-supplied (P7S-5); the IMAP host is the sole manifest
  endpoint and requires certificate-verified `IMAP4_SSL`.
- Every fetched document enters through the ingest pipeline as an immutable
  source record with connector provenance; authority is review-gated;
  sensitivity classifies UP on ambiguity, from CONTENT signals at pull time
  (`classify_file`) and again at ingest with the manifest floor. The
  session-ritual `secret_scan` covers anything pulled into the tree.
  **Advisory (P7S-19):** confidential-floor material (mail) IS processable by
  the local agent (`allow-minimized` ≠ deny at the pull gate), and `needs-ocr`
  review items route attacker-controlled attachments to the operating agent
  as multimodal input; the structural defenses are evidence-only landing,
  `answer_authority` gating, review gating, and connector-provenance tags on
  every such item — prompt-injection resistance inside the agent remains
  advisory.
- Credentials live only in `<root>/.env.nosync` / process env
  (`token_env`-style names everywhere else); they never reach manifests,
  `config.json`, ledgers, logs, results, or model context — every string
  leaving a pull is `redact()`-filtered (P7S-9). Exactly two writers exist:
  the shell's `write_root_env_secret` (admin-driven) and the kernel's
  `persist_rotated_token` (the one documented autonomous secret write,
  contained + atomic + 0o600 + no-bypass-marked; P7S-2). The kernel secret
  scan exempts exactly the literal path `<root>/.env.nosync` and nothing else
  (P7S-3). Unattended pulls run only under the `connector-pull` loop +
  `readonly_connectors` action class with autonomy ON, allowlisted, in-cap.
- **Guarantee placement (I6, P7S-27):** kernel-enforced guarantees from this
  phase (pull-only, default-deny, containment, HTTP discipline, lint
  exemption scope) land in the kernel's DOCTRINE.md in guarantee-lint format
  with named enforcers; shell-enforced guarantees (`write_root_env_secret`
  discipline, wizard flock, scheduler gating) land in
  `security_map.GUARANTEES` with shell test node ids. Neither file claims the
  other's enforcers.

## Stress pass (done 2026-06-11 — before coding, as required)

An adversarial review (security + implementation-feasibility lenses, P7S-*)
ran against the original draft AND the shipped kernel/shell code; all 28
findings were adjudicated ACCEPTED and folded into the interfaces/tasks above.
Three were live bugs in already-shipped plumbing (P7S-14/16/20 — localfolder's
dead `intake_classify` call and date-prefixed landing names,
`_planned_pull_scope`'s wrong loop id and zeroed byte pricing); this phase
fixes them rather than copying them. Summary of findings and where each
landed:

| ID | Sev | Finding (one line) | Resolution |
|----|-----|--------------------|------------|
| P7S-1 | CRIT | Google device flow cannot grant Drive read scope; `device_flow` lacked client_secret | gdrive → stdlib loopback installed-app flow (`loopback_flow`); device flow stays for msgraph; signature widened (frozen interface, P7-T2) |
| P7S-7 | CRIT | Redirect policy unspecified; Graph 302s by design, urllib forwards Authorization | no-redirect everywhere; `http_download` single enumerated-host https hop, Authorization stripped cross-host (frozen interface, P7-T1/T3) |
| P7S-2 | HIGH | MS refresh-token rotation had no writer; a kernel secret write hits the no-bypass guard | `persist_rotated_token` — the one documented, contained, atomic, marked exception (frozen interface, P7-T3/T7) |
| P7S-3 | HIGH | `.env.nosync` trips the kernel's own secret scan; T7 acceptance was false | lint exempts exactly the literal `<root>/.env.nosync`, named enforcer + DOCTRINE line (P7-T7) |
| P7S-8 | HIGH | No byte-download primitive → adapters hand-roll urllib outside the safety core | `http_download` added to the frozen interface; no-direct-urllib enforcer test (P7-T1) |
| P7S-10 | HIGH | Notion child-page scope semantics self-contradictory | scope = parent-chain to allowlist via child edges; links/mentions never followed (P7-T4) |
| P7S-14 | HIGH | Date-prefixed landing names break supersession + allow cross-item overwrite | stable `<item_id-hash>_<slug>` names pinned; localfolder scheme not copied (frozen interface, P7-T1) |
| P7S-15 | HIGH | Zip-slip spec omitted symlink members, decompression bombs, member-count caps | full member-validation checklist; streaming caps shared with HTTP (P7-T6, `http_download`) |
| P7S-16 | HIGH | localfolder's `intake_classify` call is dead code (TypeError swallowed) — T1 would copy it | `classify_file(path, connector_default=floor)` pinned at pull time; floor passed through `run_batch` (P7-T1/T9) |
| P7S-17 | HIGH | Declared bytes=0 when file-clamped; no runtime byte enforcement | fail-closed plan pricing (unknown ⇒ cap) + running byte counter abort (frozen interface, P7-T1/T9) |
| P7S-20 | HIGH | Loop id `connector-pull` vs `_planned_pull_scope`'s hardcoded `connector-health` | one canonical id `connector-pull`; scope fixed; excluded from `DETERMINISTIC_LOOPS` (frozen interface, P7-T1/T9) |
| P7S-4 | MED | `set_env_secret` writes the profile `.env`; the scrubbed kernel env can't see connector creds | new `config.write_root_env_secret(root, key, value)`; scrubbed-env acceptance test (shell interface, P7-T7) |
| P7S-5 | MED | Manifest-supplied endpoints would exfiltrate env secrets; IMAP TLS unstated | base URLs pinned in code; IMAP = sole manifest host, verified `IMAP4_SSL` only (invariants, P7-T5) |
| P7S-6 | MED | `access_mode: api` registry collision; multi-account unaddressed; "revocation" didn't revoke | id-only registration + `system`-fallback resolution; multi-account = one dir per id; revocation honesty in the doctor fix-line (frozen interface, P7-T7) |
| P7S-9 | MED | Pre-signed download URLs leak into results / the action ledger | `redact()` on every string leaving pull (frozen interface, P7-T1) |
| P7S-11 | MED | Moved-in files missed by the modifiedTime cursor; shared-drive params; Graph delta 410 unhandled | periodic re-list/seen-ids; `supportsAllDrives`+`corpora` pinned; 410 = reset + logged resync (P7-T2/T3) |
| P7S-12 | MED | Out-of-scope "refusals" → rc 1 → autonomy-demotion DoS by an outsider sharing files | `skipped_out_of_scope` vs `refused` vocabulary; rc + failure-ledger feeds pinned (frozen interface, P7-T9) |
| P7S-18 | MED | Gated pulls probed the remote API before the autonomy gate decided | authorize FIRST with a cap-derived declared scope; probe only after grant (frozen interface, P7-T1/T9) |
| P7S-21 | MED | Autonomy-OFF acceptance contradicted the A5 scheduler skip | per-path acceptance: scheduler = silent skip; direct harness = deny rows, zero network (P7-T9) |
| P7S-22 | MED | Wizard/first-run pull+ingest didn't take the per-root flock (SQLite races with serve) | T8/T11 hold `~/.oracle/locks/<instance>.lock` across pull+ingest; direct kernel-CLI gap documented (P7-T8/T11) |
| P7S-23 | MED | Torn state.json; UIDVALIDITY resets; freshness read a never-updated manifest field | atomic cursor + `{}`-on-unparseable; UIDVALIDITY guard; freshness-from-cursor, manifests never rewritten (frozen interface, P7-T5) |
| P7S-25 | MED | Drive export ~10 MB cap; GCP OAuth client provisioning unscoped | export matrix + skip-with-record; wizard prints provisioning instructions (P7-T2/T8) |
| P7S-13 | LOW | "Empty allowlist" vs YAML `None`; `source` absent from the schema | empty = None/missing/[]/non-list all refuse; `source` block added to `connector.schema.json` (manifest conventions) |
| P7S-19 | LOW | needs-ocr flow = attacker multimodal input; confidential mail IS local-agent-readable | residual risk stamped advisory; connector-provenance tags on review items (invariants, P7-T9/T11) |
| P7S-24 | LOW | Cadence strings had no grammar | pinned vocabulary `hourly\|daily\|weekly\|<N>h\|<N>d`, default `daily` (manifest conventions) |
| P7S-26 | LOW | Gmail/O365 IMAP auth reality (app passwords; no device-flow mail scope) | v1 auth = username + app password; doctor fix-line says so (P7-T5) |
| P7S-27 | LOW | Kernel guarantees can't live in shell security_map as written | pinned split: kernel → DOCTRINE.md guarantee-lint; shell → security_map.GUARANTEES (invariants, DoD) |
| P7S-28 | LOW | testkit unused by the test plans; the dashboard's broken top-level manifest glob would be inherited | testkit required in every shell test plan; T10 reuses `_known_connector_ids` (deps note, P7-T10) |

## Definition of done

- [ ] `RemoteConnector` core landed (gate-first, capped, redacting,
      stable-naming, content-classifying); all five connectors registered
      id-only, each honoring its default-deny scope allowlist and the
      pull-only contract; the P7S-14/16/17/20 plumbing fixes landed.
- [ ] Zero new required dependencies; any optional lib degrades to
      disabled-with-doctor-warning (I1).
- [ ] Wizard step + secrets lifecycle: setup → auth flow (loopback for
      gdrive, device for msgraph) → dry-run plan → first pull under the
      per-root flock, with credentials only ever in the root's
      `.env.nosync`/env, written by exactly the two sanctioned writers; the
      lint exemption for the one literal path is enforced and documented.
- [ ] Scheduled pulls run only through the autonomy gate as `connector-pull`
      (never level-1-preset); OFF-by-default verified per path (scheduler
      skip; direct-harness deny rows, zero network, zero bytes).
- [ ] Doctor + dashboard show per-connector health from the real manifest
      layout; first-run experience demonstrates corpus growth end-to-end on a
      fresh instance with connector-tagged review items.
- [ ] Kernel work landed upstream and re-vendored (I3); the pull-only /
      default-deny / credential-isolation guarantees land per the pinned I6
      split — kernel ones in DOCTRINE.md guarantee-lint format, shell ones in
      `security_map.GUARANTEES` — each with named enforcers (P7S-27);
      `make check` green; CI green.
