# STRESS — adversarial review of the spec, with resolutions

Two independent reviews ran against `SPEC.md`/`DESIGN.md` and the real kernel
(`_tools/`): a security review (attacker mindset) and an architecture review
(spec-vs-kernel correctness). Findings below, each with the resolution folded
into the spec (✅ = spec amended, ⏸ = accepted as documented v1 limit). Every
kernel claim here was verified by executing the kernel CLI, not by reading.

## Security findings

### C1 — ceiling gated only `oracle_search`; other read verbs were uncapped ✅ CRITICAL
`answer`/`brief`/`review`/`status` return confidential content the kernel runs
as `local_deterministic` (it can't see the LLM's environment); the shell then
shipped that text to an external model. **Resolution (S4):** every read verb's
*output* is filtered against the ceiling before it enters context. Verbs whose
output sensitivity can't be self-attested are dropped from the schema when
`environment == external`. `answer`/`brief` route through the kernel's own
`sensitivity_ceiling` (the envelope already carries it) and are refused to the
model when above ceiling. `review` uses `summary` (counts by kind, no titles)
on the gateway/external surface; `list` (titles) only at `local_agent`.

### C2 — loopback ≠ data-confinement; urllib follows redirects ✅ CRITICAL
A loopback listener can forward off-box; urllib re-sends body + Authorization on
3xx. **Resolution (S2):** redirects disabled (raise on any 3xx). **(S3):**
classification resolves the host and requires *all* resolved addresses loopback;
adds `provider.local_is_confined` (default **false**) — until the operator
opts in, even `local_agent` is capped at `internal` (no confidential unlock).

### C3 — importing the root's `policy.py` = arbitrary code execution ✅ CRITICAL
`spec_from_file_location` executes module-level code from an
operator-registered (possibly shared/poisoned) root, in the process holding the
API keys. **Resolution (S3):** the bridge never imports root code. It shells out
to the root's own `oracle policy check --sensitivity S --env E` (rc 0=allow,
1=deny) — verified to exist — and parses `SENSITIVITY_ORDER` as data. Fails
closed to `public` on any error.

### H1 — `./oracle status` snapshot in the system prompt leaked metadata ✅ HIGH
`status --json` carries `review_inbox.most_urgent` (item title) and could carry
object names. The prompt is sent to the external model every call. **Resolution
(S5):** the embedded snapshot is minimized to rung + bare counts only
(`memory.*`, `authority.*`, `review_inbox.total`) — never `most_urgent`, never
due-loop titles, never object names — regardless of surface.

### H2 — `allow-minimized` was treated as a grant, over-unlocking secret ✅ HIGH
"Highest label not denied" yielded `secret` for `local_agent` (all of
confidential/restricted/secret are `allow-minimized` there), but no minimizer
exists. **Resolution (S3):** ceiling = highest label whose verdict is exactly
`allow`. `allow-minimized` is never auto-released. Net: `external`→`public`,
`local_agent`→`internal` (→`confidential` only with `local_is_confined` AND a
future minimizer; deferred). This also corrects DESIGN D3's overclaim.

### H3 — gateway authorized a user but replied to a chat (group leak) ✅ HIGH
**Resolution (S7):** gateway serves **private chats only** —
`chat.type=="private"` AND `chat.id==from.id`; any group/channel/forwarded/
anonymous/`from`-less update is denied (logged id only, no reply, no LLM call).

### H4 — `oracle_ingest` accepted any resolvable path (injection → secret theft) ✅ HIGH
A prompt injection could make the local model ingest `~/.oracle/.env`, a sibling
instance, or `~/.ssh/...`. **Resolution (S4):** ingest paths must resolve under
a configured `ingest_roots` allowlist; paths under `profile_dir()`, any *other*
registered instance root, or matching secret/key name patterns are hard-denied.

### M1 — env scrub by suffix missed operator-named key vars ✅
**(S4):** scrub by suffix AND explicitly drop the resolved `provider.api_key_env`
and every `gateway.*.token_env` name.

### M2 — `.env` create-then-chmod perms race ✅
**(S1):** `os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)` under `0o077` umask,
atomic tmp+rename preserving 0o600. Never post-hoc chmod.

### M3 — config secret-regex too narrow ✅
**(S1):** also reject `://user:pass@` userinfo in any value and scan values for
`Bearer `/`sk-`/high-entropy tokens; broaden key list (authorization, cookie,
bearer).

### M4 — gateway `capture`/`remember` = stored injection / poisoning ✅
**(S7):** gateway-written memory tagged provenance `gateway_user:<id>`, lower
trust tier, excluded from authority-bearing retrieval by default; per-user
write rate limit.

### M5 — `--max-sensitivity` last-wins smuggling ✅
**(S4):** dispatcher strips any sensitivity token from model args and appends
exactly one `--max-sensitivity <ceiling>` last.

### L1/L2/L3 ✅ — `localhost` resolved+checked (C2 fix); classification uses
`.hostname` lowercased, drop dead `[::1]` set entry; cap/idle-evict per-(user,
instance) loops.

## Architecture findings

### A1 — `search` natural form is unusable programmatically ✅ P0
`_translate` collects flag *values* as query terms and `query` has no `--json`.
**Resolution (S4):** pin `["search","query","--q="+TERMS,"--k",K,
"--max-sensitivity",CEILING]` (subcommand form passes through untouched; `query`
already prints JSON; `--q=` form so terms starting with `-` are safe).

### A2 — `oracle_answer` exit code IS the verdict (2/3/4 not failure) ✅ P0
**Resolution (S4/S5):** rc ∈ {0,2,3,4} is a verdict, not an error; capture
stdout/stderr **separately** and parse the envelope from stdout only.

### A3 — `review --json` errors; flags live on `list` ✅ P0
**Resolution (S4):** pin `["review","list","--json","--limit","15"]` for the
local surface; `["review","summary","--json"]` for gateway/external.

### A4 — per-root serialization: flock on ledger appends isn't enough ✅ P0
`loops.record` does an unlocked read-modify-write of the loop note; `chat`
running `checkpoint` can race a `serve` tick on the same root → lost updates +
sqlite "database is locked". **Resolution (S4/S6):** one `fcntl.flock` per root
at `~/.oracle/locks/<instance>.lock`, held around every `run_verb` and every
`tick_instance`. `serve.lock` only prevents double daemons; the per-root lock
prevents chat/serve overlap.

### A5 — "autonomy off ⇒ no-op" still appends deny rows ✅ P1
`harness` logs an intended `action_event` per blocked loop each tick → ledger
bloat at 300s. **Resolution (S6):** `tick_instance` checks `autonomy.enabled`
cheaply (read `Meta.nosync/Autonomy/autonomy.yml`) and skips the harness spawn
entirely when off; S10 pins "rc==0, ledger-append-only side effects."

### A6 — version skew unchecked ✅ P1
**Resolution (S8.2):** doctor compares root manifest `tools_version` vs vendored
(`[warn]` on skew). Dispatcher degrades to error-text for verbs a root predates.

### A7 — conversation state was unspecified ✅ P1
**Resolution (S5/S7):** the `AgentLoop` owns a message list mutated only by
append + overflow-eviction; the REPL holds one loop for its lifetime; the
gateway caches one loop per (user, instance) for the daemon's lifetime — so
byte-stability holds by construction.

### A8 — `remember`/`capture` arg shapes ✅ P1
**Resolution (S4):** documented exact argv per kind; enums lowercase; arrays →
repeated flags; `failure` uses `--severity`+`--failure-mode` (no `--polarity`).

### A9 — subcommands pinned in code, not just verbs ✅ P1
**Resolution (S4):** dispatcher builds the FULL argv including subcommand; model
args fill value slots only. Closes `loops run --no-gate` and `brief publish`
escapes.

### A10 — verified working argv table for all 10 tools ✅
Folded into S4 as the canonical dispatch table.

## Scope cuts (accepted)

- ⏸ **Fallback model chain dropped for v1** (A-review #13): a second client whose
  base_url could differ in environment would let a loopback fallback *raise* a
  ceiling computed for an external primary — a real hazard. One client per
  session; on exhausted retries the turn fails cleanly. `fallback_model` stays
  in the config schema (reserved) but is not wired.
- ⏸ **No minimizer in v1** → `allow-minimized` content is simply out of reach
  (H2). Confidential-to-local-model unlock is deferred behind both
  `local_is_confined` and a real minimization implementation.
- ⏸ **Wizard Telegram allowlist seeding kept** (cheap, idempotent) but config is
  hand-editable as the real path.
- ⏸ **Telegram-only gateway**; adapter interface is the extension seam.

## Implementation-phase hardening (post-code review)

Reviewing the written code surfaced four more issues, all fixed:

### I1 — context eviction could split a tool-call/tool-reply pair ✅ HIGH
`_evict_if_needed` popped individual messages, which could leave an assistant
`tool_calls` message without its `tool` replies (or vice versa) — a dangling
`tool_call_id` makes EVERY subsequent OpenAI-format call fail mid-session.
**Fix (loop.py):** evict whole turn groups (user → next user), never splitting
a pair; system prompt and current group always retained. Regression tests
added.

### I2 — model-driven ingest was not fail-closed ✅ HIGH
With the default empty `ingest_roots`, the allowlist check was skipped, so a
prompt-injection could make the local model ingest any non-secret-named path.
**Fix (verbtools._ingest_denied):** empty `ingest_roots` now DENIES all
model-driven ingest (operator can still ingest directly via the kernel CLI).
Symlink escapes are already closed (`resolve()` precedes the containment check;
test added).

### I3 — two subprocess paths didn't scrub secrets ✅ MEDIUM
`scheduler.tick_instance` (harness) and `cli kernel` passthrough inherited the
full environment — including the LLM/Telegram key vars — into the kernel
process, which never needs them. **Fix:** both now pass `_scrubbed_env()`, as
does `policy_bridge._cli_policy_check`.

### I4 — access-refusal keywords were over-broad ✅ LOW
"approve"/"grant me" would refuse legitimate questions containing those words.
**Fix (telegram):** narrowed to access-specific phrases.

Confirmed correct as implemented: redirect blocking (urllib opener raises on
3xx), IPv4-mapped IPv6 loopback classification (`::ffff:8.8.8.8` → external),
the answer-envelope ceiling withholding, deny-by-default gateway, metadata-only
ledger, atomic 0600 secret writes.
