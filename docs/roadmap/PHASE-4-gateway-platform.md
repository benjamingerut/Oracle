# Phase 4 — Gateway Platform

**Closes limit #3.** Today there is exactly one messaging surface (Telegram),
hard-wired. This phase generalizes it into a clean multi-adapter gateway and
adds the surfaces the actual audience (business leaders) already live in: Slack,
email, and a local HTTP/MCP surface — each with its own ceiling and identity
mapping, all sharing the same agent loop, policy bridge, and (from Phase 3)
forced grounding. This is the single highest-leverage *reach* feature: "ask the
company oracle from Slack."

**Scope addition (SUB-5 D3): scheduled briefing delivery moves INTO this
phase** (it previously lived in Phase 5 as G4 / task P5-T4). A gateway that
can *push*, not just reply, is the leverage feature: the kernel's
`leadership-briefing` loop already produces briefs, and this phase's adapters
are exactly the delivery surfaces. PHASE-5 now points here for briefing
delivery; Phase 5's dependency on this phase is otherwise unchanged.

Read first: `docs/roadmap/ROADMAP.md`, `SPEC.md` S7 (current Telegram contract),
`STRESS.md` H3/M4 (private-chat-only, write provenance/rate limit), and the
**stress pass at the bottom of this file** (P4S-1…20, all adjudicated and
folded into the interfaces below).

Depends on: Phase 1 (config versioning). Composes with Phase 3 (grounding) and
Phase 2 (ceilings). Identity work here feeds Phase 5 — the actor-namespacing
and allowlist-key semantics frozen here (P4S-17) are the seam P5's
`identity.py` consumes.

## The core idea

Extract a transport-agnostic `GatewayAdapter` protocol from the existing
Telegram code; the gateway *core* owns everything that must be identical across
surfaces (allowlist resolution → identity → loop, ceiling, grounding, ledger,
rate-limit, repair caps, refusal-on-access-change). Each adapter only
translates its platform's wire format to/from a normalized `InboundMessage` /
`OutboundReply` and declares its own delivery-privacy guarantee (the property
that replaces Telegram's "private chat only" check, per surface).

**Two distinct "surface" concepts (P4S-1).** `InboundMessage.surface` is the
*transport* name (`"telegram" | "slack" | "email" | "http"`) — it exists for
ledger rows, identity namespacing, and per-surface config lookup ONLY. The
*loop* surface — the string passed to `build_loop` — is the literal
`"gateway"` for every message GatewayCore serves, on every transport, always.
Transport names never reach `build_loop`. To make the builder safe even
against a future wiring mistake, `grounding_for` is inverted to fail CLOSED:
any surface that is not exactly `"local"` gets `GroundingPolicy.ENFORCE` and
the gateway wall-clock cap (today `builder.py` returns the local OBSERVE
default for any unknown string — that fail-open branch is removed in P4-T1,
with an enforcer test that `build_loop(surface="http")` yields ENFORCE +
gateway tools + wall clock).

## Frozen interfaces

### `oracle_agent/gateway/core.py` (new — the shared engine)
```python
@dataclass
class InboundMessage:
    surface: str               # TRANSPORT name: "telegram" | "slack" | "email" | "http"
    user_id: str               # platform-native, verified by the adapter (see key semantics)
    channel_id: str            # where a reply goes
    text: str
    is_private: bool           # adapter asserts the delivery target is 1:1 to user_id
    meta: dict                 # adapter-supplied SCALAR metadata only (never bodies,
                               # never the raw platform object — P4S-5)
@dataclass
class OutboundReply:
    channel_id: str
    text: str
class GatewayAdapter(Protocol):
    surface: str
    def poll(self) -> list[InboundMessage]: ...        # or push; see below
    def send(self, reply: OutboundReply) -> None: ...  # adapter owns chunking
    def commit(self) -> None: ...                      # persist cursor AFTER the batch
                                                       # is handled (P4S-4)
    def supports_push(self) -> bool: ...
class GatewayCore:
    def __init__(self, surface_cfg, surface, instances, loop_builder, *,
                 clock=time.time, logger=None, root_lock_factory=None): ...
    def handle(self, msg: InboundMessage) -> OutboundReply | None
    # deny-by-default allowlist; is_private REQUIRED for any above-public reply;
    # grounding ENFORCE; per-user rate limit; per-user repair cap; metadata-only
    # ledger (incl. P3 repair telemetry); access-change refusal; per-message
    # exception isolation.
```

**Core-owned ceiling and write-gate (P4S-2).** `GatewayCore` does NOT accept a
prebuilt loop factory closed over someone else's ceiling. It receives
`loop_builder` with the pinned signature
`loop_builder(user_id, instance, root, *, ceiling_override, write_actor,
write_gate)` (in production a thin shim over `builder.build_loop` with
`surface="gateway"` hard-coded), and **core itself** injects:
`ceiling_override` = its own `surface_cfg["max_sensitivity"]`,
`write_actor` = the namespaced tag below, and `write_gate` = its own
`allow_write` bound method. The `holder` hack in today's
`serve._build_gateway` is deleted. Enforcer test: a loop built by core for
surface X carries `min(provider ceiling, X.max_sensitivity)`, ENFORCE
grounding, a non-None write gate, and the namespaced actor — and no code path
exists by which an adapter or serve wiring can substitute any of the four.

**Actor namespacing + allowlist key semantics (P4S-17, frozen — P5 consumes
this).** Write provenance is `--actor gateway_user:<surface>:<id>` (e.g.
`gateway_user:telegram:12345`, `gateway_user:email:ceo@co.com`). Existing
Telegram ledger rows keep the old un-namespaced tag; new rows are namespaced
(documented, not migrated). Allowlist keys per surface: Telegram — decimal
string of the numeric user id; Slack — the `U…` member id; email — the full
address lowercased, matched exactly (so `user+tag@` is a *different* key and
is denied — fail closed); HTTP — the configured token-principal name. P5's
`identity.resolve(cfg, surface, user_id)` is specified against exactly these
shapes.

**Core-vs-adapter responsibility table (P4S-3).** Pinned; a behavior listed
under "core" may never be reimplemented or overridden in an adapter:

| Behavior | Owner | Notes |
|---|---|---|
| Allowlist resolution, deny-by-default | **core** | malformed-entry guard included |
| `is_private` privacy rule (cap/serve/drop) | **core** decides, adapter asserts | see matrix below |
| Grounding mode (ENFORCE) + wall clock | **core** (via `build_loop`, fail-closed builder) | P4S-1 |
| Ceiling (`max_sensitivity` per surface) | **core** | injected into `loop_builder`, P4S-2 |
| Write rate limit (`allow_write`) | **core** | per (surface, user), hourly window |
| Repair cap (`per_user_repairs_per_hour`, P3S-3) | **core** | check before turn, record after |
| Repair telemetry in the ledger row | **core** | `grounding/repairs/redacted/withheld/added_seconds` |
| LRU loop cache (64, capacity eviction) | **core** | one cache per core |
| Metadata-only ledger append | **core** | whitelisted fields only; `meta` never serialized wholesale |
| Access-change refusal (D7 phrases) | **core** | |
| Per-message exception isolation + best-effort error reply | **core** catches, adapter sends | never kills the daemon |
| Root flock around the whole turn | **core** | telegram/slack/email blocking; HTTP `nb=True` + 503 (P4S-9) |
| Wire parsing, identity extraction, `is_private` assertion | adapter | |
| Reply chunking / platform message limits | adapter | Telegram 4000; Slack ~40k; email unchunked; HTTP n/a |
| Cursor persistence (`commit()`) | adapter | called by the driver AFTER the batch is handled |
| Failure backoff state (non-blocking) | adapter holds, driver schedules | `next_poll_not_before`, never `sleep()` — P4S-18 |
| Typing/"thinking" indicator (P4-T6) | adapter, **only after core authorizes** | P4S-19 |

**Delivery semantics (P4S-4).** `poll()` → core handles each message →
adapter `send()`s replies → driver calls `commit()`. Cursors persist only
after handling, so the contract is **at-least-once**: a crash mid-batch
replays messages, which costs duplicate turns/ledger rows but never loses one.
Adapters must tolerate redelivery (and the spec accepts the duplicate-turn
cost; it is logged, not hidden).

**Privacy matrix (P4S-5).** The core rule — `is_private == false` ⇒ reply
capped at `public` — generalizes H3, but adapters MAY be stricter (refuse
outright, I4), and two are:

| Surface | Non-private inbound | Behavior |
|---|---|---|
| telegram | group/channel/forwarded/anonymous/from-less | **dropped at the adapter** — no `InboundMessage`, no reply, no LLM call (H3/SH-015 preserved byte-for-byte; T1 zero-behavior-change) |
| slack | anything but `channel_type == "im"` (incl. `mpim`, groups) | **dropped at the adapter** — same H3 discipline |
| email | To/Cc beyond the oracle's own single address | served, **capped at `public`**, reply to the single allowlisted From only |
| http | n/a | always `is_private=true` (token-authenticated single principal) |

**Ledger (P4S-5).** Core appends exactly this whitelisted row shape and
nothing else — `meta`/raw platform objects are NEVER serialized:
`{kind:"gateway_turn", surface, user_id, channel_id, chars_in, chars_out,
verdicts, grounding, repairs, redacted, withheld, added_seconds, ts}`.

**Adapter state files (P4S-20).** Every persisted adapter cursor (telegram
offset, email UID cursor, briefing delivery state) lives in the profile dir,
named `<surface>_<kind>_<instance-or-scope>.json`, written atomically
(tmp+rename) — the current hardcoded `telegram_offset_default.json` slug is
grandfathered for telegram but the scheme is pinned for everything new.

### Adapters (each new, each stdlib-only — Slack's optional dep aside)
```python
gateway/telegram_adapter.py   # refactor of today's gateway, long-poll
gateway/slack_adapter.py      # DECIDED at phase opening: Option A, Socket Mode
                               # (optional websocket dep, injectable transport).
gateway/email_adapter.py      # IMAP poll + SMTP send, stdlib imaplib/smtplib
gateway/http_adapter.py       # a localhost-only http.server surface + minimal MCP-shaped endpoint
```
- **Per-surface privacy guarantee (replaces H3's hardcoded check):** each
  adapter sets `is_private` truthfully. Telegram: `chat.type=="private" and
  chat.id==user_id`. Slack: `channel_type=="im"` with the resolving user —
  `mpim` and group DMs are NOT `im` and are dropped (P4S-13). Email:
  `is_private` ⟺ exactly one `From`, allowlisted (lowercased exact match), AND
  `To` is solely the oracle's own address with empty `Cc` (P4S-11); the reply
  goes only to the exact header `From` (never reply-all, never a list, never
  `Reply-To` — P4S-10). HTTP: localhost-bound, token-authenticated single
  principal. `GatewayCore` refuses any above-`public` reply when `is_private`
  is false; telegram/slack drop non-private entirely per the matrix above.

### Config (config.py, migrated via P1-T3; **CONFIG_VERSION → 3**, P4S-16)
```jsonc
"gateway": {
  "telegram": { ... },                         // unchanged
  "slack":    {"enabled":false,"token_env":"...","signing_secret_env":"...",
               "allowlist":{},"max_sensitivity":"internal",
               "per_user_writes_per_hour":20,"per_user_repairs_per_hour":null},
  "email":    {"enabled":false,"imap_host":"...","smtp_host":"...",
               "user_env":"...","pass_env":"...","allowlist":{},
               // P4S-10 layered fail-closed identity: max_sensitivity is
               // HARD-CAPPED at "public" unless authserv_id is set (see P4-T3).
               "max_sensitivity":"public","authserv_id":null,
               "per_sender_turns_per_hour":10,
               "per_user_writes_per_hour":20,"per_user_repairs_per_hour":null,
               "poll_seconds":60},
  "http":     {"enabled":false,"bind":"127.0.0.1","port":8765,
               "token_env":"...","principal":"http-operator",
               "max_sensitivity":"internal",
               "per_user_writes_per_hour":20,"per_user_repairs_per_hour":null}
},
"briefings": {                                  // P4-T8 delivery targets (P4S-15)
  // "<instance>": {"targets": [{"surface":"telegram","user_id":"12345"},
  //                            {"surface":"email","address":"ceo@co.com"}]}
}
```
- **SECURITY_KEYS additions (P4S-16), enumerated:**
  `gateway.slack.{enabled,allowlist,max_sensitivity,token_env,signing_secret_env}`,
  `gateway.email.{enabled,allowlist,max_sensitivity,user_env,pass_env,imap_host,smtp_host,authserv_id}`,
  `gateway.http.{enabled,max_sensitivity,token_env,bind,port}`,
  `briefings` (delivery targets are exfil-critical: a migration that rewrites
  `smtp_host` or a briefing target silently redirects confidential output).
  Wildcard form `gateway.*.allowlist` etc. is acceptable where
  `_get_dotted_wildcard` supports it.
- **Fix the dead wildcard (P4S-16):** `SECURITY_KEYS` today carries
  `"providers.*.api_key_env"` but the config key is singular `"provider"` —
  the wildcard expands against a nonexistent dict and protects nothing.
  Corrected to `provider.api_key_env` with a regression test
  (migration-alters-api_key_env → load refused).
- **CONFIG_VERSION bumps 2 → 3** with a no-op structural migration, so the
  security-key preservation check (which only runs when a migration fires)
  actually exercises every new key on existing configs.

### Briefing delivery — `service/briefer.py` (new; moved here from Phase 5)
```python
def new_briefs(cfg, instances, state) -> list[Delivery]  # reads each root's
        # Workproduct.nosync/_STANDING/.registry.jsonl for rows not yet in state
def deliver(cfg, delivery, cores) -> None      # ceiling-checked send + state update
```
- **Registry-driven, not cadence-cloning (P4S-15):** the kernel's
  `leadership-briefing` loop already runs on cadence via the harness tick (and
  autonomy-off roots produce no briefs — consistently). The briefer does NOT
  re-implement cadence; it watches the standing registry for new
  `leadership-brief` rows (keyed by `drop_id`/`sha256_12`).
- **Exactly-once across restarts (P4S-15):** persisted delivery-state file in
  the profile dir keyed `(instance, surface, drop_id)`, written atomically.
  Corruption ⇒ **no send** + logged + doctor flag (fail closed: a missed brief
  beats a mis-sent one).
- **Push targets must be provably private (P4S-15):** a push has no inbound
  message to assert `is_private`, so targets must resolve to an
  already-allowlisted private identity — Telegram: a `chat_id` equal to an
  allowlisted `user_id`; email: a single allowlisted address. Anything else
  (a group id, an unlisted chat, a list address) is refused at config load AND
  by doctor. Deny-by-default: no configured target ⇒ no delivery.
- **Ceiling re-check at delivery (delivery is an *export*):** compare the
  registry row's document-level `sensitivity` against the target surface's
  `max_sensitivity`; above ⇒ withhold the WHOLE brief (per-line scan is
  blocked upstream — SH-057 — so document-level is the only honest check).
- **Ledger row pinned:** `{kind:"briefing_delivery", surface, target,
  drop_id, sensitivity, ts}` — metadata only, appended to the root's
  `gateway_event.jsonl`.

### serve.py
`_build_gateway` becomes `_build_gateways` returning a list of (adapter, core)
pairs; the serve loop drives each enabled adapter between ticks. Push-capable
adapters (http) run their own listener thread; poll-capable adapters
(telegram/slack-socket/email) are polled. One `GatewayCore` per
(surface, instance-set). The serve loop also drives the briefer (P4-T8)
between ticks: outbound scheduled sends ride the same adapters and the same
ceiling checks as replies.

**Multiplexing discipline (P4S-18 — the P1S-13 class):**
- **No `sleep()` anywhere in a poll path.** Failure backoff becomes a
  per-adapter `next_poll_not_before` timestamp the driver consults; today's
  in-line `self._sleep(delay)` in `telegram.poll_once` (up to 60s, freezing
  every other adapter, the briefer, and tick cadence) is removed. This is the
  one deliberate carve-out from P4-T1's "zero behavior change" — noted there.
- **Every adapter socket op carries an explicit timeout** —
  `IMAP4_SSL(timeout=)`, `SMTP(timeout=)`, urllib `timeout=` — because
  imaplib/smtplib default to *no* timeout and a black-holed host would hang
  the daemon forever.
- **Per-iteration poll budget:** the sum of adapter timeouts per loop
  iteration must leave `tick_seconds` cadence intact (Telegram's long-poll
  timeout is reduced when other adapters are enabled).
- **Per-adapter isolation:** one adapter raising never skips the others or
  the tick (test: adapter A raises, adapter B still polled, tick still fires).

## Tasks

- **P4-T1 — extract GatewayCore.** Refactor today's `TelegramGateway` logic
  into `gateway/core.py` (`InboundMessage`/`OutboundReply`/`GatewayCore`) +
  `telegram_adapter.py`, per the responsibility table (P4S-3) and the
  core-owned ceiling/write-gate injection (P4S-2). Invert
  `builder.grounding_for` to fail closed (P4S-1): any surface ≠ `"local"` ⇒
  ENFORCE + gateway wall clock; the transport name lives only in
  `InboundMessage.surface`. *Acceptance:* zero behavior change **except the
  pinned carve-out** — failure backoff becomes non-blocking
  (`next_poll_not_before`, P4S-18) instead of an in-line sleep; the
  private-chat, allowlist, ledger, rate-limit, refusal tests pass unchanged;
  the refactored ledger row still carries the P3 repair telemetry
  (`grounding/repairs/redacted/withheld/added_seconds`) and the per-user
  repair cap still gates the turn; `build_loop(surface="http")` yields
  ENFORCE + gateway tools + wall clock (enforcer test, new security_map
  guarantee); a loop built by core carries the core-injected ceiling, gate,
  and namespaced actor (P4S-2 enforcer); new actor tags are
  `gateway_user:telegram:<id>` (old ledger rows keep the old tag — documented,
  not migrated). **Testkit amendment (P4S-6):** the P1-frozen
  `Harness.gateway` is deliberately amended to return the adapter+core
  composite with the same assertion hooks (`_loops_ref`, `_api_ref`, `sent`);
  its loop factory stops hand-mirroring builder decisions and goes through the
  same fail-closed path. *Tests:* refactor `test_telegram.py`; add
  `test_gateway_core.py`. *Deps:* P1.

- **P4-T2 — Slack adapter.** **DECIDED at phase opening (P4S-13/14): Option A
  — Socket Mode**, via an OPTIONAL third-party websocket library, unless the
  operator refuses optional deps (in which case Slack stays disabled; Option B
  is the documented contingency below). Per I1's graceful-degradation clause
  the adapter is disabled/skipped when the library is absent — no module-level
  import.

  - **Injectable transport (P4S-14):** the WS connection is an injected
    object; ALL Slack security guarantees (allowlist, `im`-only privacy,
    payload parsing, graceful dep-absent disable) are enforced by **dep-free
    tests** — `security_map.verify_enforcers` rejects skip-marked enforcer
    nodes, so no Slack guarantee may hang off a `skipif(websockets)` test.
  - **stdlib-only test policy amendment (P4S-14):** `test_stdlib_only` walks
    *all* imports via `ast.walk`, so the optional import needs an explicit
    *optional-guarded* allowlist entry (function-local, try/except), plus a
    companion test that `slack_adapter` imports cleanly when the dep is
    absent.
  - **Option A acceptance:** a DM (`channel_type=="im"`) from an allowlisted
    `U…` id round-trips through GatewayCore over the fake transport; `mpim`,
    group, and channel events are dropped at the adapter (no `InboundMessage`);
    unknown sender ignored; adapter cleanly disabled (and doctor says so) when
    the dep is absent. Socket Mode has NO request signatures — none are
    claimed.
  - **Option B contingency (Events API via documented tunnel), if ever
    taken:** raw-body `v0` HMAC verification (computed over the exact bytes)
    + a 5-minute timestamp replay window; the Slack route on `http_adapter`
    is bearer-exempt (Slack can't carry our token) and HMAC-only; SECURITY.md
    must state plainly that the tunnel makes this route internet-facing —
    the loopback bind survives literally but not practically. Acceptance for
    this option includes signature-mismatch and stale-timestamp rejection.

  The "Events API over the local HTTP server via urllib ws-less long-poll" is
  not a viable approach: Slack's Events API is push-only and urllib has no
  WebSocket support. This framing must not appear in the implementation.
  *Tests:* `test_slack_adapter.py` (fake transport, dep-free). *Deps:* P4-T1.
  The dep decision is recorded; **T3/T4 do not wait on anything Slack**
  (P4S-20).

- **P4-T3 — email adapter.** `email_adapter.py`: IMAP poll (stdlib `imaplib`),
  parse From, allowlist-resolve, reply via SMTP to the single sender only.

  - **Identity is layered fail-closed (P4S-10).** Inbound `From` is
    attacker-writable and DKIM is unverifiable in stdlib over IMAP, so: the
    email surface is **hard-capped at `public` by default**. Raising
    `max_sensitivity` to `internal` requires BOTH (a) the operator configuring
    a trusted `authserv_id` and (b) the adapter verifying an
    `Authentication-Results` header from exactly that authserv-id carrying
    `dmarc=pass` (or `spf=pass` where DMARC is absent) on every message —
    no header, wrong authserv-id, or fail ⇒ that message is served at
    `public` at most. A per-sender hourly turn cap
    (`per_sender_turns_per_hour`) is ALWAYS on, configured or not. Write
    provenance marks the surface low-assurance
    (`gateway_user:email:<address>`; documented as low-assurance in
    SECURITY.md).
  - **Reply discipline (P4S-10/11):** the reply goes to the exact header
    `From` — `Reply-To` is read and **ignored** (pinned; Reply-To redirection
    is the one path that converts forgery into direct disclosure); envelope
    recipient == header From == the allowlisted address, never any address
    parsed from content; never reply-all. The reply body is the model output
    only — the inbound message and thread are NEVER quoted or included
    (Re:-chain re-emission of confidential text through a capped surface).
  - **Loop protection (P4S-11):** never reply to messages with
    `Auto-Submitted` ≠ `no`, `Precedence: bulk/list/auto_reply`, or our own
    `Message-ID` in `References`; outbound replies set
    `Auto-Submitted: auto-replied`.
  - **MIME/HTML + size (P4S-11):** prefer the `text/plain` part; HTML-only
    mail is text-extracted (kernel extractor discipline) or refused;
    attachments ignored; inbound size cap enforced before parsing.
  - **Transport (P4S-11):** `IMAP4_SSL` and SMTP STARTTLS (or SMTPS)
    mandatory; explicit socket timeouts on both.
  - **Mailbox + cursor (P4S-12):** a DEDICATED mailbox is required
    (doctor-checked — a shared human mailbox races `\Seen` with the human's
    client); persisted `(UIDVALIDITY, last_UID)` cursor in the profile dir
    (atomic write, P4S-20 naming); cursor corruption or UIDVALIDITY change ⇒
    log + start from the mailbox's current `UIDNEXT` (never replay the
    mailbox unbounded); `commit()` persists the cursor after the batch is
    handled (at-least-once, P4S-4).

  *Acceptance:* an allowlisted sender's clean mail produces a
  single-recipient reply to the exact From; a list/cc'd mail is refused above
  public; unknown sender ignored (and still cursor-advanced); Reply-To never
  used; reply contains no quoted inbound text; an `Auto-Submitted` message
  gets no reply; over the per-sender cap ⇒ refusal before any model call;
  with no `authserv_id` configured the surface ceiling is `public` no matter
  what config says; with `authserv_id` set, a message lacking `dmarc=pass`
  from it is served at `public` at most; restart does not replay handled
  mail. *Tests:* `test_email_adapter.py` (fake imap/smtp objects).
  *Deps:* P4-T1.

- **P4-T4 — local HTTP/MCP surface.** `http_adapter.py`: a `http.server`
  bound to loopback, bearer-token auth, a JSON `POST /ask {text}` → grounded
  reply, and a minimal MCP-shaped tool endpoint so other local agents can
  consult the oracle.

  - **Fail-closed startup (P4S-7):** `token_env` unresolved or empty ⇒ the
    adapter refuses to start (logged, doctor-flagged). There is no
    unauthenticated mode, ever.
  - **Auth on every route (P4S-7):** the bearer token is required on ALL
    routes including the MCP endpoint, compared with `hmac.compare_digest`.
    The token is wizard-generated high-entropy (`secrets.token_urlsafe`).
    **The trust decision, named:** the HTTP surface authenticates the
    *token*, not the OS user — loopback is reachable by every local UID, so
    anyone holding the token IS the configured `principal`. Stated in
    SECURITY.md.
  - **Browser hardening (P4S-7):** `Host` header must be in
    `{127.0.0.1:<port>, localhost:<port>, [::1]:<port>}` or the request is
    refused (kills DNS rebinding); no CORS headers are ever emitted;
    `Content-Length` capped; per-request socket timeout.
  - **Bind validation (P4S-7):** the configured `bind` must parse as a
    literal loopback IP via `ipaddress.ip_address(bind).is_loopback` —
    hostnames are refused (including the string `"localhost"`, which
    `bind()` would resolve via DNS), `0.0.0.0` is refused, consistent with
    SH-032/033's literal-loopback discipline. Refused at startup, I4.
  - **MCP through the Dispatcher ONLY (P4S-8, I2):** the MCP `tools/list` is
    EXACTLY `tool_schemas(surface="gateway", environment)`; every
    `tools/call` routes through `Dispatcher.dispatch` — no parallel verb
    table, no raw kernel passthrough, so dropped verbs
    (`oracle_ingest`/`oracle_brief`/`oracle_checkpoint`/`oracle_loops_due`)
    stay structurally absent and ceiling forcing / `--q=` packing / M5
    stripping / write provenance + rate limits all apply. Enforcer test: a
    dropped verb called via MCP is denied fail-closed.
  - **No control plane (P4S-8):** invariant — no endpoint on `http_adapter`
    mutates allowlists, config, pairing, or instances; the
    control-plane-from-chat hole v1 structurally refused (D7/SH-005) stays
    refused on HTTP, with an SH-005-style structural test.
  - **Concurrency + shutdown (P4S-9):** handler model pinned: a
    single-threaded `HTTPServer` in its own listener thread. HTTP turns take
    the per-root lock with `nb=True` (bounded retry) and return `503 busy`
    on contention — a blocking acquire would pile requests up behind a
    600-second harness tick. Shutdown: the serve loop calls
    `server.shutdown()` from the main thread on stop (the documented
    cross-thread call), then joins the listener.

  *Acceptance:* localhost request with token → grounded reply; missing/bad
  token → 401 (constant-time compare); bad `Host` → 403; non-loopback or
  hostname bind refused at startup; unresolved token ⇒ adapter does not
  start; oversize body → 413; MCP lists exactly the gateway schema and a
  dropped verb is denied; lock-busy → 503; clean start/stop. *Tests:*
  `test_http_adapter.py`. *Deps:* P4-T1. (Proceeds in parallel with P4-T2's
  Slack work — P4S-20.)

- **P4-T5 — serve multi-gateway.** `_build_gateways`; the drive loop honors
  per-adapter `next_poll_not_before` (no in-line sleeps), explicit timeouts
  on every adapter socket op, a per-iteration poll budget that preserves
  `tick_seconds` cadence, and per-adapter exception isolation; push adapters
  get a listener thread; clean shutdown of all. *Acceptance:* `serve --once`
  with telegram+email enabled polls both; one adapter raising does not skip
  the other or the tick; a backed-off adapter does not delay the others; http
  listener starts/stops cleanly; one core per (surface, instance-set).
  *Tests:* extend `test_scheduler`/new `test_serve_gateways.py`.
  *Deps:* P4-T1..T4.

- **P4-T6 — streaming/typing affordances (where cheap).** Optional
  per-adapter "thinking" indicator (Telegram `sendChatAction`, Slack typing)
  emitted before a long turn; never required, degrades silently.
  **Authorization first (P4S-19):** the indicator is emitted only AFTER
  GatewayCore has authorized the message (allowlist + privacy) — never on a
  denied update, which would turn the silent-deny discipline (SH-017) into a
  presence oracle. *Acceptance:* indicator emitted then real reply; absence
  never blocks; a denied update produces no indicator. *Tests:* adapter unit
  tests. *Deps:* P4-T1, P4-T2/T3.

- **P4-T7 — SECURITY.md + doctor.** Guarantees: "above-public replies require
  an adapter-asserted private 1:1 channel", "every surface deny-by-default",
  "http binds loopback only (literal IP)", "email identity is low-assurance
  and public-capped without DMARC verification", "the HTTP token is the
  principal". Doctor validates each enabled surface per this matrix (P4S-20 —
  "allowlist non-empty" does not apply to HTTP, whose identity is the token):

  | Surface | Doctor checks |
  |---|---|
  | telegram | token resolvable; allowlist non-empty |
  | slack | token resolvable; allowlist non-empty; optional dep present (else "[warn] slack configured but websocket lib absent — disabled") |
  | email | creds resolvable; allowlist non-empty; dedicated-mailbox ack; `authserv_id` unset ⇒ "[warn] email capped at public"; TLS hosts set |
  | http | token resolvable (fail if not); bind parses as literal loopback IP; port sane |
  | briefings | every target resolves to an allowlisted private identity; state file readable |

  *Acceptance:* `verify_enforcers()` empty; doctor flags each misconfigured
  surface per the matrix. *Deps:* P4-T1..T5, P1-T1.

- **P4-T8 — scheduled briefing delivery (moved from Phase 5 / P5-T4).**
  `service/briefer.py` per the frozen interface above: registry-driven
  detection, persisted `(instance, surface, drop_id)` delivery state
  (atomic; corruption ⇒ no send + log + doctor flag), targets restricted to
  allowlisted private identities, document-level ceiling re-check per
  delivery surface (withhold whole brief when above), metadata-only
  `briefing_delivery` ledger row. Admin chooses targets in the `briefings`
  config block; deny-by-default (no configured target ⇒ no delivery).
  *Acceptance:* a root with a fresh brief and a configured target delivers
  exactly once per new brief, including across a daemon restart; nothing
  delivered when no new brief; corrupted state file ⇒ no send + doctor flag;
  a target that is a group id / unlisted chat / list address is refused at
  config load and by doctor; above-ceiling brief for the surface withheld
  entirely; the delivery row appears in the ledger. *Tests:*
  `test_briefer.py`. *Deps:* P4-T1, P4-T5, P1.

## Security invariants for this phase

- `GatewayCore` is the ONLY decision point; adapters never decide
  authorization, ceiling, grounding, rate limits, or repair caps. The ceiling
  and write-gate are injected by core itself (P4S-2) — an adapter bug or a
  serve wiring slip can drop a message but cannot widen access.
- The builder fails CLOSED on surface (P4S-1): any surface ≠ `"local"` is
  gateway-class (ENFORCE, gateway tools, wall clock). The transport name
  never reaches `build_loop`.
- `is_private == false` ⇒ reply capped at `public` regardless of allowlist
  ceiling (the multi-recipient leak class from H3, generalized); telegram and
  slack are stricter and drop non-private entirely (P4S-5 matrix).
- Email never reply-alls, never honors `Reply-To`, never quotes inbound text,
  and is public-capped without verified DMARC from a configured authserv-id
  (P4S-10/11). HTTP never binds a non-literal-loopback address, never runs
  unauthenticated, and exposes no control-plane endpoint (P4S-7/8). Slack
  serves `im` channels only.
- All credentials via `*_env` names (config secret-guard from S1 applies);
  adapter subprocesses (none expected) would scrub env if added. The new
  per-surface security keys, hosts, bind, and briefing targets are
  SECURITY_KEYS-protected; CONFIG_VERSION 3's no-op migration makes the
  preservation check bite (P4S-16).
- Access-change refusal, metadata-only whitelisted-field ledger, repair caps,
  and repair telemetry are enforced in core, so every surface inherits them
  (P4S-3); `meta`/raw platform objects are never serialized.
- Write provenance is surface-namespaced (`gateway_user:<surface>:<id>`,
  P4S-17) so M4 attribution survives multi-surface; P5's identity model
  consumes exactly this seam.
- Briefing delivery is an *export*: it re-runs the document-level ceiling
  check for the destination surface on every send, delivers only to
  allowlisted private identities, and fails closed (no send) on state
  corruption; scheduled push gets no privilege that an interactive reply
  would not have (P4S-15).

## Stress pass (done 2026-06-11 — before coding, as required)

An adversarial review (security + implementation-feasibility lenses, P4S-*)
ran against the original draft AND the landed P1/P3/P7 code; all 20 findings
were adjudicated ACCEPTED and folded into the interfaces/tasks above. One was
a live latent bug in already-shipped plumbing (P4S-16's dead
`providers.*.api_key_env` wildcard — `provider.api_key_env` is effectively
unprotected today); this phase fixes it rather than building on it. Decisions
pinned at adjudication: email identity = layered fail-closed
(public hard-cap, `authserv_id`+DMARC to unlock internal); Slack = Option A
(Socket Mode, optional dep, injectable transport); CONFIG_VERSION → 3 with a
no-op migration; `grounding_for` inverted fail-closed. Summary of findings
and where each landed:

| ID | Sev | Finding (one line) | Resolution |
|----|-----|--------------------|------------|
| P4S-1 | HIGH | Builder fails open: any surface ≠ "gateway" gets OBSERVE + no wall clock; transport vs loop surface conflated | `grounding_for` inverted fail-closed (≠"local" ⇒ ENFORCE + wall clock); loop surface is the literal "gateway"; transport only in `InboundMessage.surface`; enforcer test (core idea, P4-T1) |
| P4S-2 | HIGH | Ceiling + write-gate applied by serve wiring (`holder` hack), not structurally core-owned | core injects `ceiling_override`/`write_actor`/`write_gate` into a pinned `loop_builder` signature; holder hack deleted; enforcer test (frozen interface, P4-T1) |
| P4S-3 | MED | Frozen core interface omits repair caps/telemetry, LRU, error isolation, chunking; no responsibility split | core-vs-adapter responsibility table pinned, all behaviors assigned; T1 acceptance enumerates repair telemetry + caps (frozen interface, P4-T1) |
| P4S-4 | MED | `poll()` has no commit/ack; offset/UID cursor can't be sequenced | `commit()` added to the protocol, called after batch handling; at-least-once semantics + duplicate-turn cost pinned (frozen interface) |
| P4S-5 | MED | "is_private=false ⇒ public" vs H3 drop-entirely conflict; `raw` "never bodies" false as drafted | per-surface drop-vs-serve matrix (telegram/slack drop; email serves capped); `raw` → scalar `meta`, ledger appends whitelisted fields only (frozen interface) |
| P4S-6 | LOW | P1-frozen `Harness.gateway → TelegramGateway` breaks under T1; no fakes for new adapters | frozen interface deliberately amended (adapter+core composite, same hooks); Harness fakes for email/http land with their tasks (P4-T1) |
| P4S-7 | HIGH | HTTP auth fail-open risk, DNS rebinding/Host/CORS, bind validation, DoS limits unspecified | fail-closed startup; token on all routes (`compare_digest`); Host allowlist; no CORS; body cap + timeouts; literal-IP-only bind via `ipaddress`; token-is-the-principal named in SECURITY.md (P4-T4) |
| P4S-8 | HIGH | MCP endpoint could bypass the Dispatcher chokepoint, re-expose dropped verbs, re-open control-plane | MCP tools == `tool_schemas("gateway", env)`, dispatch only via `Dispatcher.dispatch` (I2); no-control-plane invariant + structural test; dropped-verb-via-MCP denied test (P4-T4) |
| P4S-9 | MED | Blocking root lock in the listener thread piles requests behind 600s ticks; shutdown unstated | HTTP turns take `nb=True` lock → 503 on busy; handler model pinned; `server.shutdown()` sequence pinned (P4-T4) |
| P4S-10 | HIGH | Email From is forgeable; DKIM unverifiable in stdlib over IMAP; poisoning + trusted-channel phishing; Reply-To redirect | layered fail-closed: public hard-cap by default; `authserv_id`+`dmarc=pass` required to unlock internal; per-sender hourly cap always on; reply to exact header From, Reply-To ignored; low-assurance provenance (P4-T3) |
| P4S-11 | MED | Quote-chain leaks, autoresponder loops, HTML/MIME, TLS, recipient pinning unpinned | never quote inbound; Auto-Submitted/Precedence/References loop guard + outbound Auto-Submitted; text/plain-preferred + size cap; IMAP4_SSL/STARTTLS + timeouts; envelope recipient pinned to allowlisted From; precise `is_private` definition (P4-T3) |
| P4S-12 | MED | No UID/UIDVALIDITY cursor; restart replays mailbox; shared-mailbox `\Seen` races | dedicated mailbox (doctor-checked); persisted atomic `(UIDVALIDITY, last_UID)` cursor; corruption/reset ⇒ start from current UIDNEXT (P4-T3) |
| P4S-13 | MED | Acceptance assumed signatures that don't exist under Socket Mode; Option B tunnel voids loopback-only; no replay window | DECISION RECORDED: Option A; per-option acceptance split; Option B contingency pins raw-body v0 HMAC + 5-min replay window + internet-facing SECURITY.md note; `im`-only pinned (P4-T2) |
| P4S-14 | MED | Optional `websockets` trips the stdlib-only AST walk and the skip-marked-enforcer check | injectable transport ⇒ all Slack guarantees enforced dep-free; stdlib test gains an optional-guarded allowlist + clean-absence import test (P4-T2) |
| P4S-15 | HIGH | No push-target privacy proof (group leak), no exactly-once state, delivery config shape missing | targets must resolve to allowlisted private identities (config-load + doctor refusal); persisted `(instance,surface,drop_id)` state, corruption ⇒ no send; registry-driven (no cadence cloning); `briefings` config block + SECURITY_KEYS; pinned ledger row (briefer interface, P4-T8) |
| P4S-16 | HIGH | SECURITY_KEYS misses all new surfaces + smtp/imap hosts + bind; existing `providers.*` wildcard is dead | new keys enumerated (incl. hosts, bind, authserv_id, briefings); `provider.api_key_env` fixed + regression test; CONFIG_VERSION → 3 with a no-op migration so the preservation check fires (config section) |
| P4S-17 | MED | `gateway_user:<id>` collides across surfaces; allowlist key semantics unfrozen; P5 depends on this seam | actor tag frozen as `gateway_user:<surface>:<id>` (old telegram rows keep the old tag); per-surface allowlist key normalization pinned; P5 `resolve()` specified against it (frozen interface, P4-T1) |
| P4S-18 | HIGH | In-line backoff sleep + timeout-less imaplib/smtplib starve all adapters and ticks (P1S-13 class) | non-blocking `next_poll_not_before` backoff (T1's one zero-behavior-change carve-out); explicit timeouts on every socket op; poll budget; per-adapter isolation + test (serve section, P4-T1/T5) |
| P4S-19 | LOW | Typing indicator before authorization = presence oracle vs the silent-deny discipline | indicator only after core authorizes; denied update produces no indicator; tested (P4-T6) |
| P4S-20 | LOW | T6 dep gap; doctor "allowlist non-empty" wrong for HTTP; Slack decision could stall T3/T4; state-file naming | T6 deps include P4-T1; per-surface doctor matrix; T3/T4 explicitly parallel to the Slack work; adapter-state naming scheme pinned (P4-T6/T7, frozen interface) |

## Definition of done

- [ ] `GatewayCore` extracted per the responsibility table; Telegram
      refactored with zero behavior change except the pinned non-blocking
      backoff carve-out (P4S-18); builder fails closed on surface (P4S-1);
      ceiling/write-gate/actor core-injected (P4S-2); testkit amended (P4S-6).
- [ ] Slack (Option A, injectable transport, dep-free guarantee tests),
      email (layered fail-closed identity, reply/loop/cursor discipline),
      HTTP/MCP (fail-closed auth, Host check, literal-loopback bind,
      Dispatcher-only MCP, no control plane) adapters; each with a truthful
      `is_private` and its own stress-tested privacy guarantee.
- [ ] `serve` drives all enabled surfaces with per-adapter isolation, no
      in-line sleeps, explicit socket timeouts, and tick cadence preserved;
      clean shutdown including the HTTP listener thread.
- [ ] Above-public replies impossible on non-private channels (tested per
      surface, per the drop-vs-serve matrix).
- [ ] Scheduled briefing delivery (P4-T8): exactly-once per new brief across
      restarts, allowlisted-private targets only, document-level ceiling
      re-checked per delivery surface, fail-closed on state corruption,
      ledgered.
- [ ] SECURITY_KEYS extended + `provider.api_key_env` wildcard fixed;
      CONFIG_VERSION 3 migration landed with the preservation check
      exercised (P4S-16).
- [ ] SECURITY.md guarantees added (incl. email low-assurance and
      token-is-the-principal statements); doctor validates each surface per
      the P4-T7 matrix; `verify_enforcers()` empty.
- [ ] `make check` green; CI green (pytest-only install passes with the
      optional Slack dep absent).
