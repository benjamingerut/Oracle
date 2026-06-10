# SUB-2 — Shell Core Fixes (Phase 2)

Shell paths relative to repo root. Tasks S1–S4 are file-disjoint and run in
parallel. Shell tests live in `tests/shell/`. Invariants I1–I6 bind. Every
fix fails closed.

**File ownership (to keep tasks disjoint):**
- S1: `src/oracle_agent/agentloop/policy_bridge.py`, `src/oracle_agent/llm/client.py`,
  `src/oracle_agent/agentloop/builder.py`, `tests/shell/test_policy_bridge.py`,
  `tests/shell/test_llm_client.py`
- S2: `src/oracle_agent/gateway/telegram.py`, `src/oracle_agent/service/serve.py`,
  `src/oracle_agent/service/scheduler.py`, `tests/shell/test_telegram.py`,
  `tests/shell/test_scheduler.py`
- S3: `src/oracle_agent/cli.py`, `src/oracle_agent/doctor.py`,
  `src/oracle_agent/wizard.py`, `src/oracle_agent/config.py`,
  `src/oracle_agent/spawn.py`, `Makefile`, `tests/shell/test_cli.py`,
  `tests/shell/test_config.py`
- S4: `src/oracle_agent/agentloop/verbtools.py`, `src/oracle_agent/agentloop/loop.py`
  (only if footer/dispatch shape requires), `tests/shell/test_verbtools.py`,
  `tests/shell/test_agentloop.py`

If a task believes it must touch another task's file, it reports the conflict
instead of editing.

---

## S1 — Endpoint classification: close the TOCTOU; fail-closed overrides

**Findings (verified):**
- `policy_bridge.environment_for()` (`policy_bridge.py:37-71`) resolves the
  endpoint hostname once at build time; `LLMClient.chat()` re-resolves per
  request. DNS rebinding/short-TTL gets `local_agent` (internal ceiling) with
  requests going off-box. Gateway loop cache keeps stale classification
  indefinitely.
- `sensitivity_rank()` (`policy_bridge.py:104-113`) maps unknown labels to
  strictest, so `min_sensitivity(computed, "Public")` silently returns the
  computed (higher) ceiling — operator intent to LOWER is ignored. Violates
  fail-closed-on-ambiguity (the ambiguity must error, not no-op).
- `local_is_confined` parameter (`policy_bridge.py:116`, plumbed from config
  via `builder.py:32-33`) is never read — a dead security knob advertised in
  config.
- `client.py:199-201`: server-supplied `Retry-After` sleep is uncapped.
- `client.py:113-117`: Bearer token sent over plaintext `http://` to
  non-loopback hosts with no refusal.

**Required behavior:**
1. `local_agent` classification is granted ONLY for: literal IPv4 loopback
   (`127.0.0.0/8`), literal `::1` (incl. bracketed forms), or the exact
   hostname `localhost`. Any other hostname — even if it currently resolves
   to loopback — classifies `external`. (This removes the DNS-dependent
   proof; `localhost` may keep a resolution sanity check but the per-request
   guard below is the enforcement.)
2. Per-request guard in `LLMClient`: the client knows its classification; if
   classified `local_agent`, it refuses to send any request whose URL host is
   not in the literal-loopback set above (cheap string/IP check, no DNS).
   This makes the property hold at use time, not just check time.
3. Sensitivity overrides (`--max-sensitivity` CLI value, gateway
   `max_sensitivity` config) are validated against the kernel's known label
   order; unknown/miscased labels raise a clear error (CLI exits non-zero;
   gateway refuses to start), never silently ignored.
4. Remove the dead `local_is_confined` parameter and its config plumb in
   `builder.py`; leave a one-line seam comment for roadmap Phase 2
   (confidential tier) which will reintroduce it with real semantics.
5. Cap `Retry-After` honoring at 30s; total retry sleep budget ≤ 120s.
6. Refuse (raise with actionable message) any non-loopback `http://` base_url
   when an API key would be attached. Loopback http stays allowed.

**Tests:** rebinding-shaped hostname (e.g. `localhost.attacker.example`
resolving loopback via monkeypatched resolver) classifies external;
per-request guard blocks a swapped URL; invalid override errors on both
surfaces; Retry-After capped; http+key to non-loopback refused; existing
classification tests updated for the stricter rule.

---

## S2 — Gateway & daemon resilience

**Findings (verified):**
- Gateway turns unlocked: `telegram.py:128-137` calls `loop.run_turn` with no
  `root_lock` (STRESS A4 claims it holds). Races concurrent `oracle chat` /
  `oracle kernel` writes on the same root.
- Offset in-memory only (`telegram.py:74,91`): `serve --once` (cron pattern)
  re-handles the same updates every run → duplicate kernel writes/replies;
  crash/restart replays the last batch.
- Busy-spin: failed `getUpdates` returns 0 immediately, serve loop re-invokes
  with no sleep (`serve.py:93-102`, `telegram.py:82-88`) → 100% CPU during
  outages, log line per iteration, and `serve.log` has no rotation
  (`serve.py:22-28`).
- One malformed update or a string allowlist entry raises out of `poll_once`
  and kills the daemon (`telegram.py:90-93`).
- Loop cache "LRU" is FIFO (`telegram.py:145-148`): most-active user can be
  evicted.
- Telegram HTTP uses the default opener (follows redirects), unlike the LLM
  client; bot token rides the URL path (`telegram.py:50-62`).
- Scheduler blocks unboundedly on a busy root flock (`scheduler.py:63-76,104`),
  stalling all instances + gateway polling while another process holds it.

**Required behavior:**
1. Wrap each gateway turn (the `run_turn` call and its kernel-writing path in
   `_handle`) in `root_lock(instance)`.
2. Persist the Telegram update offset atomically (temp+replace) under the
   profile dir, keyed per instance/bot; load on start. `--once` runs advance
   it so repeats never re-handle a batch. Handle offset-file corruption by
   falling back to in-memory behavior with a logged warning (no crash).
3. Per-update isolation: exception while handling one update logs it,
   (optionally) notifies the sender of an internal error, advances past the
   update, and never kills the daemon. Malformed allowlist entries (wrong
   types) are logged and treated as not-allowlisted (deny).
4. Failure backoff: consecutive gateway poll failures sleep with exponential
   backoff (base ~2s, cap 60s), reset on success. Normal empty long-polls do
   NOT sleep extra.
5. `serve.log`: size-capped rotation (e.g. rotate at 5 MiB to a single `.1`),
   stdlib only.
6. Loop cache becomes true LRU (re-order on access; `OrderedDict.move_to_end`).
7. Telegram HTTP calls reuse the same no-redirect opener discipline as the
   LLM client.
8. Scheduler tick acquires the root lock with `LOCK_NB` + bounded retry; a
   busy root is skipped this tick with a log line (next tick retries), so one
   stuck/long-held lock cannot stall the daemon. Add the missing enforcer
   test: two concurrent processes/threads calling `run_verb`-equivalent under
   the lock serialize (the SPEC S10 "flock serializes two concurrent
   run_verbs" test — make it real).

**Tests:** offset persistence across gateway instances (simulated `--once`
twice handles a batch once); malformed update/allowlist survive; backoff
sleeps called (monkeypatch time); LRU semantics; no-redirect on telegram
opener; LOCK_NB skip; gateway turn takes the root lock (observable via a
lock-spy/fixture).

---

## S3 — CLI/wizard/doctor/config/spawn hygiene + low-admin fixes

**Findings (verified):**
- `oracle doctor NAME` ignores `NAME` (`doctor.py:61,95-115,177`).
- Doctor's suggested fix `oracle kernel NAME -- admin upgrade apply`
  (`doctor.py:113`) fails as written (kernel `upgrade apply` requires
  `--from-kernel <dir>`).
- Wizard never sets `ingest_roots` (`config.py:56` defaults `[]`), so the
  chat agent's ingest tool is dead by default with no warning anywhere.
- Wizard accepts non-numeric Telegram IDs (`wizard.py:101-106`) → silent
  lockout (never matches `str(from.id)`).
- `config.py:66-68` secret-shape guard misses `sk-ant-…` (hyphen breaks the
  run) and Telegram `123456:AA…` bot tokens.
- `oracle spawn` auto-register silently overwrites an existing registry entry
  of the same name (`cli.py:88-92`).
- `spawn.seed_index` inserts the spawned root's `_tools` at `sys.path[0]` and
  never removes it (`spawn.py:919-921`) — kernel module names can shadow
  shell imports in-process (wizard → spawn → doctor).
- `Makefile:79-83` secret scan enumerates shell files by hand; new top-level
  modules are silently unscanned.
- Allowlist `role` field is written by the wizard but read nowhere — leave it
  (roadmap P4/P5 consumes it) but doctor should not imply it does anything.

**Required behavior:**
1. Doctor checks only the named instance when given; all when omitted. Fix
   the upgrade suggestion text to a command that actually runs. Add doctor
   warnings: empty `ingest_roots` ("your oracle cannot ingest from chat —
   add directories to config.json ingest_roots"); zero sources in the root
   ("your oracle knows nothing yet — run `oracle kernel NAME -- ingest batch <path>`");
   non-https non-loopback LLM endpoint (FAIL, matching S1's client refusal).
2. Wizard: prompt for ingest roots (validated: absolute, existing dirs;
   empty allowed but warned); validate Telegram IDs numeric; write them as
   strings (the matching shape).
3. Config secret guard: extend patterns to `sk-ant-`-style hyphenated keys
   and `\d{6,}:[A-Za-z0-9_-]{30,}` bot tokens.
4. `oracle spawn` refuses to overwrite a registry entry pointing at a
   different root (clear error; `--force` not added — fail closed).
5. `seed_index` path hygiene: insert/remove `sys.path` entry in a
   try/finally (or import via importlib spec without touching sys.path).
6. Makefile shell secret scan globs `src/oracle_agent` (excluding `assets/`)
   instead of an enumerated list.

**Tests:** doctor instance filtering + new warnings; wizard ingest-roots and
telegram-id validation; config guard catches the new shapes; spawn collision
refusal; sys.path restored after spawn.

---

## S4 — Verb tools: ceiling enforcement on every output

**Findings (verified):**
- `oracle_checkpoint`/`oracle_loops_due` return raw kernel output (only
  length-capped) on every surface (`verbtools.py:299-305`); kernel checkpoint
  JSON embeds `review_inbox.most_urgent` (item text) and `suggested_next`
  (review text, business-object names) (`oracle_status.py:159-168,244-248`).
  An external model receives metadata STRESS C1/H1 claim is withheld. Only
  `oracle_brief` is dropped on external (`verbtools.py:115-121`).
- SPEC S4 claims `oracle_brief` output is line-scanned for above-ceiling
  envelope markers; no scan exists (`verbtools.py:293-297`).
- `_SENSITIVITY_FLAG_RE` (`verbtools.py:173`) is dead code; the real
  protection is `--q=` argv packing. SPEC S10 names a "smuggled sensitivity
  stripped" test that doesn't exist; also missing: verbtools truncation test,
  agentloop injection-in-tool-output-stays-data test.

**Required behavior:**
1. When `environment == external`, drop `oracle_checkpoint` and
   `oracle_loops_due` from the tool schema exactly like `oracle_brief`
   (structural, not filtered-text). Replace them with nothing — external
   surfaces are public-only and these verbs serve operator awareness.
   Defense in depth: additionally, the dispatcher refuses to execute a
   dropped verb if a model hallucinates the call (deny, log).
2. Implement the brief line-scan to match SPEC S4: inspect the kernel's
   `briefing.py` output for its per-line/per-section sensitivity envelope
   markers and drop any line/section whose marker exceeds the ceiling before
   returning. If, on inspection, briefing output carries NO machine-readable
   sensitivity markers, do not fake it: leave brief gated as today and write
   `BLOCKER:` in your final report so the docs phase re-stamps SPEC instead.
3. Make `_SENSITIVITY_FLAG_RE` real: strip/refuse `--max-sensitivity`-shaped
   tokens inside model-supplied argument values before argv composition (the
   `--q=` packing remains the primary defense; this is the documented second
   layer), or delete the dead regex and rely on packing — choose
   implement-the-claim (strip) since SPEC/STRESS document it.
4. Add the missing enforcer tests: smuggled `--max-sensitivity` in search
   terms stays inert (and is stripped), tool-output truncation respected,
   injection-in-tool-output remains data (agent loop does not execute
   instructions from tool results — assert message-role separation).

**Tests:** schema excludes checkpoint/loops_due/brief when external (and
includes them local); dispatcher denies dropped-verb execution; brief
line-scan drops above-ceiling lines (or BLOCKER reported); smuggle-strip;
truncation; injection-stays-data.
