"""gateway/slack.py -- Slack messaging surface (Phase 4, P4-T2).

**DECIDED at phase opening (P4S-13/14): Option A -- Socket Mode**, via an
OPTIONAL third-party websocket library. Per I1's graceful-degradation clause the
adapter is disabled/skipped when the library is absent -- there is NO
module-level import of the optional dep (P4S-14). The websocket connection is an
INJECTED object (:class:`SlackTransport` protocol), so ALL Slack security
guarantees (allowlist via the core, ``im``-only privacy, payload parsing,
graceful dep-absent disable) are enforced by **dep-free tests** over a fake
transport -- no Slack guarantee hangs off a ``skipif(websockets)`` test.

The :class:`SlackAdapter` owns ONLY adapter-row responsibilities (P4S-3):

  * wire parsing of Socket Mode envelopes -> normalized :class:`InboundMessage`;
  * identity extraction (the ``U…`` member id is the allowlist key, P4S-17);
  * the ``is_private`` assertion -- ``channel_type == "im"`` ONLY. ``mpim``,
    group DMs, and channels are NOT ``im`` and are **dropped at the adapter**
    (no InboundMessage, no reply, no LLM call) -- the same H3 discipline as
    telegram (P4S-5/13 matrix);
  * reply chunking (Slack ~40k);
  * the post-authorization "typing" indicator hook (P4-T6/P4S-19): the core
    signals authorization via the ``on_authorized`` callback, and ONLY then does
    the adapter emit a typing affordance -- never on a denied update.

Socket Mode carries NO request signatures, so none are claimed (P4S-13). The
adapter authenticates by holding the app-level token used to open the socket;
inbound message identity is the ``user`` field, allowlist-resolved by the core.

Stdlib only (the optional websocket dep is injected, never imported here).
"""
from __future__ import annotations

import time
from typing import Protocol

from .core import GatewayCore, InboundMessage, OutboundReply

# Slack message limit is ~40k chars; chunk well under it (P4S-3 adapter row).
_SLACK_MAX = 39000


# --------------------------------------------------------------------------- #
# Injected transport protocol (P4S-14): the WS connection is an injected object
# so every guarantee is dep-free testable. A real implementation wraps the
# optional ``slack_sdk``/``websockets`` Socket Mode client; the fake in the test
# suite replays scripted envelopes.
# --------------------------------------------------------------------------- #
class SlackTransport(Protocol):
    def events(self):  # -> Iterable[dict]: one batch of Socket Mode envelopes
        ...

    def ack(self, envelope_id: str) -> None:  # Socket Mode envelope ack
        ...

    def post_message(self, channel: str, text: str) -> None:  # chat.postMessage
        ...

    def post_typing(self, channel: str) -> None:  # typing affordance (P4-T6)
        ...


def build_socket_transport(token: str):  # pragma: no cover - needs live dep
    """Construct the live Socket Mode transport (production only, P4S-14).

    The optional dep is imported FUNCTION-LOCALLY and try/except-guarded; this
    function is reached ONLY after :func:`transport_available` returns True (the
    serve wiring checks first). With the dep absent it raises a clear error
    rather than importing at module scope -- so ``slack`` imports cleanly when
    the dep is missing (the clean-absence test) and every security guarantee is
    enforced over the INJECTED fake transport in the dep-free tests.
    """
    try:
        import websockets  # noqa: F401  # optional-dep, function-local (P4S-14)
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "slack Socket Mode requires the optional websocket dependency; "
            "it is absent, so Slack is disabled") from exc
    # A live transport wires ``websockets`` to Slack's Socket Mode WSS URL
    # (obtained via apps.connections.open with the app-level token). The concrete
    # wiring is intentionally out of the dep-free test path; everything that
    # MATTERS for security (parse / im-only / allowlist / typing) is tested over
    # the injected fake transport. Left as a NotImplemented seam for the operator
    # who opts into the optional dep.
    raise NotImplementedError(
        "live Slack Socket Mode transport wiring is operator-provided; inject a "
        "SlackTransport instead (see SlackAdapter)")


def transport_available() -> bool:
    """Report whether the optional websocket dep is importable (P4S-14).

    The import is FUNCTION-LOCAL and try/except-guarded: importing this module
    never pulls the optional dep, so ``slack_adapter`` imports cleanly when the
    dep is absent (the clean-absence test). ``serve`` / ``doctor`` consult this
    to disable the surface gracefully (and to emit the "[warn] slack configured
    but websocket lib absent" doctor line).
    """
    try:
        import websockets  # noqa: F401  # optional-dep, function-local (P4S-14)

        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# SlackAdapter (P4S-3 adapter rows)
# --------------------------------------------------------------------------- #
class SlackAdapter:
    """Translate Slack Socket Mode envelopes to/from the normalized gateway types.

    ``transport`` is an injected :class:`SlackTransport`. ``core`` is the shared
    :class:`~oracle_agent.gateway.core.GatewayCore` (the ONLY decision point).
    The adapter never decides authorization, ceiling, grounding, or rate limits
    -- it only parses, asserts ``is_private`` (``im``-only), sends, and emits the
    post-authorization typing affordance.
    """

    surface = "slack"

    def __init__(self, transport, core: GatewayCore, *, clock=time.time,
                 logger=None, typing: bool = True):
        self.transport = transport
        self.core = core
        self.clock = clock
        self.logger = logger or (lambda *a: None)
        self.typing = typing
        # Non-blocking backoff state (driver consults this; P4S-18). Slack
        # Socket Mode is event-driven, but a transport read failure arms it so a
        # black-holed socket never starves the other adapters or the tick.
        self.next_poll_not_before = 0.0
        self._fail_streak = 0
        self._last_delay = 0.0

    # -- protocol ----------------------------------------------------------- #
    def supports_push(self) -> bool:
        # Socket Mode is push-shaped, but the driver polls ``events()`` between
        # ticks (a bounded, non-blocking read), so it rides the poll path with a
        # poll budget (P4S-18). Reported as poll-capable for the serve driver.
        return False

    def commit(self) -> None:
        # No durable cursor: Socket Mode acks each envelope inline (at-least-once
        # is the platform's redelivery contract, P4S-4). Nothing to persist.
        return None

    # -- polling ------------------------------------------------------------ #
    def fetch(self) -> list[dict]:
        """Fetch one batch of raw Socket Mode envelopes; arm backoff on failure.

        On a transport failure returns ``[]`` and sets ``next_poll_not_before``
        so the driver skips this adapter until the window elapses -- NO sleep
        (P4S-18).
        """
        self._last_delay = 0.0
        try:
            envelopes = list(self.transport.events())
        except Exception as exc:
            self._fail_streak += 1
            delay = min(2.0 * (2 ** (self._fail_streak - 1)), 60.0)
            self._last_delay = delay
            self.next_poll_not_before = self.clock() + delay
            self.logger(f"gateway[slack]: events() failed ({type(exc).__name__}); "
                        f"backoff {delay:.0f}s (streak={self._fail_streak})")
            return []
        self._fail_streak = 0
        self.next_poll_not_before = 0.0
        return envelopes

    def parse(self, envelope: dict) -> InboundMessage | None:
        """Parse one Socket Mode envelope into an InboundMessage, or drop it.

        Drops (returns ``None``, no reply, no LLM call) any envelope that is not
        a user ``message`` event in an ``im`` channel from a real user. ``mpim``,
        group DMs, channels, bot/edit/subtype messages, and from-less events are
        all dropped at the adapter (P4S-5/13 ``im``-only matrix).
        """
        if not isinstance(envelope, dict):
            return None
        # Socket Mode wraps the Events API payload under ``payload.event``.
        etype = envelope.get("type")
        if etype not in (None, "events_api", "event_callback"):
            # slack_command / interactive / hello / disconnect -> not a message.
            return None
        payload = envelope.get("payload") or envelope
        event = payload.get("event") or payload
        if not isinstance(event, dict):
            return None
        if event.get("type") != "message":
            return None
        # A message with a subtype (bot_message, message_changed, channel_join,
        # …) is NOT a direct user message; drop it (no edits, no bots).
        if event.get("subtype"):
            self.logger("gateway[slack]: dropped message with subtype "
                        f"{event.get('subtype')!r}")
            return None
        if event.get("bot_id"):
            self.logger("gateway[slack]: dropped bot message")
            return None

        # --- im-ONLY privacy assertion (P4S-13) ---------------------------- #
        # channel_type must be exactly "im". mpim / group / channel are dropped
        # at the adapter -- no InboundMessage is produced (same H3 discipline as
        # telegram). This is the truthful is_private for this surface.
        channel_type = event.get("channel_type")
        if channel_type != "im":
            self.logger("gateway[slack]: dropped non-im channel_type "
                        f"{channel_type!r} (mpim/group/channel)")
            return None

        user_id = event.get("user")
        channel_id = event.get("channel")
        text = event.get("text")
        if not user_id or not channel_id:
            self.logger("gateway[slack]: dropped from-less/channel-less message")
            return None
        if not isinstance(text, str) or not text.strip():
            return None

        return InboundMessage(
            surface="slack",
            user_id=str(user_id),       # the U… member id (P4S-17 allowlist key)
            channel_id=str(channel_id),
            text=text,
            is_private=True,            # im-only got us here
            meta={
                "team": str(payload.get("team_id", "") or ""),
                "event_ts": str(event.get("ts", "") or ""),
            },
        )

    def ack(self, envelope: dict) -> None:
        """Ack a Socket Mode envelope (platform redelivery contract, P4S-4)."""
        env_id = envelope.get("envelope_id") if isinstance(envelope, dict) else None
        if not env_id:
            return
        try:
            self.transport.ack(str(env_id))
        except Exception as exc:
            self.logger(f"gateway[slack]: ack failed: {type(exc).__name__}")

    # -- typing affordance (P4-T6 / P4S-19) --------------------------------- #
    def _typing_cb(self, channel_id: str):
        """Return a callback the CORE invokes after authorizing the message.

        The indicator is emitted ONLY when the core calls this back, i.e. AFTER
        allowlist + privacy pass (P4S-19). A denied update never authorizes, so
        no typing event is emitted -- the silent-deny discipline (SH-017) is not
        turned into a presence oracle. Best-effort: any failure degrades
        silently and never blocks the turn.
        """
        def _emit():
            if not self.typing:
                return
            try:
                self.transport.post_typing(str(channel_id))
            except Exception as exc:
                self.logger(f"gateway[slack]: typing failed (ignored): "
                            f"{type(exc).__name__}")
        return _emit

    # -- sending (chunked; Slack ~40k) -------------------------------------- #
    def send(self, reply: OutboundReply) -> None:
        """Send a reply (adapter owns chunking; Slack ~40k)."""
        try:
            for chunk in _chunks(reply.text, _SLACK_MAX):
                self.transport.post_message(str(reply.channel_id), chunk)
        except Exception as exc:
            self.logger(f"gateway[slack]: send failed: {type(exc).__name__}")

    def error_reply(self, channel_id) -> None:
        """Best-effort error notice (per-update isolation; adapter sends)."""
        try:
            self.transport.post_message(str(channel_id),
                                        "Sorry — I hit an internal error.")
        except Exception:
            pass

    # -- driver entrypoint -------------------------------------------------- #
    def handle_envelope(self, envelope: dict) -> int:
        """Ack + parse + (authorize via core) + reply for one envelope.

        Returns 1 when a turn was served, else 0. The typing affordance fires
        through the core's ``on_authorized`` callback so it is emitted ONLY
        after authorization (P4S-19). Per-envelope exception isolation is the
        driver's job; this method's own send/typing failures degrade silently.
        """
        self.ack(envelope)
        msg = self.parse(envelope)
        if msg is None:
            return 0
        reply = self.core.handle(msg, on_authorized=self._typing_cb(msg.channel_id))
        if reply is None:
            return 0
        self.send(reply)
        return 1


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _chunks(text: str, n: int):
    if not text:
        yield ""
        return
    for i in range(0, len(text), n):
        yield text[i:i + n]
