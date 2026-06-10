# Phase 4 — Gateway Platform

**Closes limit #3.** Today there is exactly one messaging surface (Telegram),
hard-wired. This phase generalizes it into a clean multi-adapter gateway and
adds the surfaces the actual audience (business leaders) already live in: Slack,
email, and a local HTTP/MCP surface — each with its own ceiling and identity
mapping, all sharing the same agent loop, policy bridge, and (from Phase 3)
forced grounding. This is the single highest-leverage *reach* feature: "ask the
company oracle from Slack."

Read first: `docs/roadmap/ROADMAP.md`, `SPEC.md` S7 (current Telegram contract),
`STRESS.md` H3/M4 (private-chat-only, write provenance/rate limit).

Depends on: Phase 1 (config versioning). Composes with Phase 3 (grounding) and
Phase 2 (ceilings). Identity work here feeds Phase 5.

## The core idea

Extract a transport-agnostic `GatewayAdapter` protocol from the existing
Telegram code; the gateway *core* owns everything that must be identical across
surfaces (allowlist resolution → identity → loop, ceiling, grounding, ledger,
rate-limit, refusal-on-access-change). Each adapter only translates its
platform's wire format to/from a normalized `InboundMessage` / `OutboundReply`
and declares its own delivery-privacy guarantee (the property that replaces
Telegram's "private chat only" check, per surface).

## Frozen interfaces

### `oracle_agent/gateway/core.py` (new — the shared engine)
```python
@dataclass
class InboundMessage:
    surface: str               # "telegram" | "slack" | "email" | "http"
    user_id: str               # platform-native, verified by the adapter
    channel_id: str            # where a reply goes
    text: str
    is_private: bool           # adapter asserts the delivery target is 1:1 to user_id
    raw: dict                  # adapter-specific, for the ledger (never bodies)
@dataclass
class OutboundReply:
    channel_id: str
    text: str
class GatewayAdapter(Protocol):
    surface: str
    def poll(self) -> list[InboundMessage]: ...        # or push; see below
    def send(self, reply: OutboundReply) -> None: ...
    def supports_push(self) -> bool: ...
class GatewayCore:
    def __init__(self, cfg, instances, loop_factory, *, clock=time.time, logger=None): ...
    def handle(self, msg: InboundMessage) -> OutboundReply | None
    # deny-by-default allowlist; is_private REQUIRED for any above-public reply;
    # grounding ENFORCE; per-user rate limit; metadata-only ledger; access-change refusal.
```
`GatewayCore.handle` is the one place all the safety logic lives; adapters are
thin and dumb. The existing `TelegramGateway` becomes a `TelegramAdapter`
feeding `GatewayCore`.

### Adapters (each new, each stdlib-only)
```python
gateway/telegram_adapter.py   # refactor of today's gateway, long-poll
gateway/slack_adapter.py      # See P4-T2 feasibility note — delivery mechanism decided at
                               # phase opening; implemented under whichever option is chosen.
gateway/email_adapter.py      # IMAP poll + SMTP send, stdlib imaplib/smtplib
gateway/http_adapter.py       # a localhost-only http.server surface + minimal MCP-shaped endpoint
```
- **Per-surface privacy guarantee (replaces H3's hardcoded check):** each
  adapter sets `is_private` truthfully. Telegram: `chat.type=="private" and
  chat.id==user_id`. Slack: a DM channel (`im`) with the resolving user.
  Email: the From is an allowlisted single address and the reply goes only to
  it (never reply-all, never a list). HTTP: localhost-bound, single-operator.
  `GatewayCore` refuses any above-`public` reply when `is_private` is false.

### Config (config.py, migrated via P1-T3)
```jsonc
"gateway": {
  "telegram": { ... },                         // unchanged
  "slack":    {"enabled":false,"token_env":"...","signing_secret_env":"...",
               "allowlist":{...},"max_sensitivity":"internal"},
  "email":    {"enabled":false,"imap_host":"...","smtp_host":"...",
               "user_env":"...","pass_env":"...","allowlist":{...},
               "max_sensitivity":"internal","poll_seconds":60},
  "http":     {"enabled":false,"bind":"127.0.0.1","port":8765,
               "token_env":"...","max_sensitivity":"internal"}
}
```

### serve.py
`_build_gateway` becomes `_build_gateways` returning a list of (adapter, core)
pairs; the serve loop polls each enabled adapter between ticks. Push-capable
adapters (http) run their own listener thread; poll-capable adapters
(telegram/email) are polled. One `GatewayCore` per (surface, instance).

## Tasks

- **P4-T1 — extract GatewayCore.** Refactor today's `TelegramGateway` logic
  into `gateway/core.py` (`InboundMessage`/`OutboundReply`/`GatewayCore`) +
  `telegram_adapter.py`. Behavior identical; all existing `test_telegram.py`
  pass through the new structure. *Acceptance:* zero behavior change; the
  private-chat, allowlist, ledger, rate-limit, refusal tests pass unchanged.
  *Tests:* refactor `test_telegram.py`; add `test_gateway_core.py`. *Deps:* P1.

- **P4-T2 — Slack adapter.** `slack_adapter.py`: verify Slack request
  signatures (HMAC, stdlib `hmac`), resolve the user, DM-only `is_private`,
  send via `chat.postMessage` (urllib).

  **Phase-opening feasibility checkpoint (decide before coding):** Slack event
  delivery requires one of two approaches, each with trade-offs:

  - **Option A — Socket Mode (preferred if an optional websocket dependency is
    acceptable).** Slack's Socket Mode delivers events over a persistent
    WebSocket without requiring a public-facing HTTP endpoint. Implement using
    an OPTIONAL third-party `websockets` library; per I1 (graceful-degradation
    clause), the Slack adapter is disabled/skipped when the library is absent
    and degrades cleanly (no hard import at the module level). This is the
    approach that fits I1's "optional lib" pattern.

  - **Option B — Events API via documented tunnel/reverse-proxy.** The Slack
    Events API posts to a public HTTPS URL. The local oracle does not have one
    by default. This option is viable only if the operator deploys a
    reverse-proxy or tunnel (e.g. ngrok) and the requirement is explicitly
    documented. The adapter's `poll()` becomes a no-op (Slack pushes to the
    http_adapter endpoint via P4-T4); the HTTP adapter must then route
    Slack-signed payloads to `slack_adapter`.

  The "Events API over the local HTTP server via urllib ws-less long-poll" is
  not a viable approach: Slack's Events API is push-only (no long-poll), and
  urllib does not provide WebSocket support. This framing must not appear in
  the implementation. Record the chosen option and any dependency decision in
  the phase-opening stress-pass notes before coding begins.

  *Acceptance:* a signed DM from an allowlisted user round-trips through
  GatewayCore; an unsigned/!DM/unknown request is refused; signature mismatch
  rejected; if Option A, adapter is cleanly disabled when the optional dep is
  absent. *Tests:* `test_slack_adapter.py` (fake HTTP / fake socket).
  *Deps:* P4-T1; Option A also requires choosing the optional dep before P4-T5.

- **P4-T3 — email adapter.** `email_adapter.py`: IMAP poll (stdlib `imaplib`),
  parse From, allowlist-resolve, reply via SMTP to the single sender only;
  `is_private` true iff From is a single allowlisted address (never lists/cc).
  *Acceptance:* an allowlisted sender's mail produces a single-recipient reply;
  a list/cc'd mail is refused above public; unknown sender ignored. *Tests:*
  `test_email_adapter.py` (fake imap/smtp objects). *Deps:* P4-T1.

- **P4-T4 — local HTTP/MCP surface.** `http_adapter.py`: a `http.server` bound
  to `127.0.0.1` (refuse any non-loopback bind, I4), bearer-token auth from
  `token_env`, a JSON `POST /ask {text}` → grounded reply, and a minimal
  MCP-shaped tool endpoint so other local agents can consult the oracle.
  *Acceptance:* localhost request with token → grounded reply; missing/bad
  token → 401; non-loopback bind refused at startup. *Tests:*
  `test_http_adapter.py`. *Deps:* P4-T1.

- **P4-T5 — serve multi-gateway.** `_build_gateways`; poll loop drives all
  enabled poll adapters; push adapters get a listener thread; clean shutdown of
  all. *Acceptance:* `serve --once` with telegram+email enabled polls both;
  http listener starts/stops cleanly; one core per (surface,instance). *Tests:*
  extend `test_scheduler`/new `test_serve_gateways.py`. *Deps:* P4-T1..T4.

- **P4-T6 — streaming/typing affordances (where cheap).** Optional per-adapter
  "thinking" indicator (Telegram `sendChatAction`, Slack typing) emitted before
  a long turn; never required, degrades silently. *Acceptance:* indicator
  emitted then real reply; absence never blocks. *Tests:* adapter unit tests.
  *Deps:* P4-T2/T3.

- **P4-T7 — SECURITY.md + doctor.** Guarantees: "above-public replies require
  an adapter-asserted private 1:1 channel", "every surface deny-by-default",
  "http binds loopback only". Doctor validates each enabled surface
  (token/creds resolvable, allowlist non-empty, bind loopback). *Acceptance:*
  `verify_enforcers()` empty; doctor flags a misconfigured surface. *Deps:*
  P4-T1..T5, P1-T1.

## Security invariants for this phase

- `GatewayCore` is the ONLY decision point; adapters never decide
  authorization, ceiling, or grounding. An adapter bug can drop a message but
  cannot widen access.
- `is_private == false` ⇒ reply capped at `public` regardless of allowlist
  ceiling (the multi-recipient leak class from H3, generalized).
- Email never reply-alls; HTTP never binds non-loopback; Slack always verifies
  the signature before any processing.
- All credentials via `*_env` names (config secret-guard from S1 applies);
  adapter subprocesses (none expected) would scrub env if added.
- Access-change refusal and metadata-only ledger are enforced in core, so every
  surface inherits them.

## Stress pass (before coding)

Per adapter: spoofing the identity (Slack signature replay, email From
forging, Telegram is covered), the "private" assertion being game-able (Slack
multi-person DM/`mpim`, email Bcc), the HTTP surface reachable off-box via an
SSRF/rebind. Append findings; the privacy guarantee per surface must survive
them.

## Definition of done

- [ ] `GatewayCore` extracted; Telegram refactored with zero behavior change.
- [ ] Slack, email, HTTP/MCP adapters; each with a truthful `is_private` and
      its own stress-tested privacy guarantee.
- [ ] `serve` drives all enabled surfaces; clean shutdown.
- [ ] Above-public replies impossible on non-private channels (tested per
      surface).
- [ ] SECURITY.md guarantees added; doctor validates each surface.
- [ ] `make check` green; CI green.
