"""gateway/telegram.py -- Telegram messaging surface (SPEC S7; Phase 4 P4-T1).

Phase 4 refactor: the transport-agnostic decision engine now lives in
``gateway/core.py`` (:class:`GatewayCore`). This module is the Telegram
*adapter* (:class:`TelegramAdapter`) plus a thin compatibility shell
(:class:`TelegramGateway`) that composes an adapter + a core and preserves the
exact behavior the daemon and the test suite already depend on.

Every safety property is still enforced -- but in :class:`GatewayCore`, so it
is identical across surfaces:

  * Deny-by-default: only allowlisted user IDs are served; unknown senders are
    ignored entirely (logged id only, no reply, no LLM call).
  * Private-chat-only (STRESS H3): the ADAPTER drops any update that is not a
    private chat where chat.id == from.id (groups/channels/forwarded/anonymous/
    from-less). No InboundMessage is produced, so the core never sees it -- the
    H3/SH-015 discipline is preserved byte-for-byte (P4S-5 matrix; telegram
    drops non-private entirely).
  * Gateway (reduced) tool surface; control-plane never crosses the gateway.
  * Writes tagged with surface-namespaced provenance (``gateway_user:telegram:
    <id>``, P4S-17) and rate-limited (STRESS M4) -- in core.
  * Metadata-only ledger row per handled turn -- never message bodies -- in core.
  * Access-change requests get a fixed refusal -- in core.

Carve-out from "zero behavior change" (P4S-18): poll-failure backoff is now
NON-BLOCKING. Instead of an in-line ``sleep`` inside the poll path (which froze
every other adapter, the briefer, and the tick), the adapter records a
``next_poll_not_before`` timestamp the driver consults. ``poll_once`` still
records the same backoff *delay* for observability (and for the legacy
``sleep`` hook the test suite injects) but never blocks the loop.

Stdlib only.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .core import (
    GatewayCore,
    InboundMessage,
    OutboundReply,
    _LOOP_CACHE_SIZE,  # re-exported for tests (SPEC S2 #6)
    _noop_lock,        # re-exported for tests / fallback
    _scheduler_root_lock,
)

_TELEGRAM_MAX = 4000

# Backoff parameters for consecutive poll failures (SPEC S2 #4).
_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 60.0


# --------------------------------------------------------------------------- #
# No-redirect opener (SPEC S2 #7)
# --------------------------------------------------------------------------- #
def _no_redirect_opener():
    """Build a urllib opener that refuses HTTP redirects."""
    class _NoRedirect(urllib.request.HTTPErrorProcessor):
        def http_response(self, req, resp):
            if resp.status in (301, 302, 303, 307, 308):
                raise urllib.error.URLError(
                    f"redirect refused: {resp.status} -> "
                    f"{resp.headers.get('Location')}"
                )
            return resp
        https_response = http_response

    return urllib.request.build_opener(_NoRedirect)


_OPENER = _no_redirect_opener()


class TelegramAPI:
    """Thin urllib wrapper over the Telegram Bot API (injectable base for tests)."""

    def __init__(self, token: str, base: str = "https://api.telegram.org",
                 timeout: float = 35.0):
        self._token = token
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _url(self, method: str) -> str:
        return f"{self.base}/bot{self._token}/{method}"

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        data = urllib.parse.urlencode({"offset": offset, "timeout": timeout}).encode()
        req = urllib.request.Request(self._url("getUpdates"), data=data, method="POST")
        with _OPENER.open(req, timeout=self.timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        return obj.get("result", []) if obj.get("ok") else []

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text, _TELEGRAM_MAX):
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode()
            req = urllib.request.Request(self._url("sendMessage"), data=data, method="POST")
            with _OPENER.open(req, timeout=self.timeout):
                pass

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Emit a "typing" affordance (P4-T6). Best-effort; the caller swallows."""
        data = urllib.parse.urlencode({"chat_id": chat_id, "action": action}).encode()
        req = urllib.request.Request(self._url("sendChatAction"), data=data, method="POST")
        with _OPENER.open(req, timeout=self.timeout):
            pass


# --------------------------------------------------------------------------- #
# TelegramAdapter -- wire parsing, identity, is_private assertion, chunking,
# cursor persistence, non-blocking backoff (P4S-3 adapter rows)
# --------------------------------------------------------------------------- #
class TelegramAdapter:
    """Translate Telegram updates to/from the normalized gateway types.

    Owns ONLY adapter-row responsibilities (P4S-3): wire parsing, identity
    extraction, the ``is_private`` assertion (and the H3 drop), reply chunking
    (Telegram 4000), offset cursor persistence (``commit()``), and the non-
    blocking failure-backoff state (``next_poll_not_before``).
    """

    surface = "telegram"

    def __init__(self, api, *, clock=time.time, logger=None, profile_dir=None,
                 typing: bool = True):
        self.api = api
        self.clock = clock
        self.logger = logger or (lambda *a: None)
        self._profile_dir = profile_dir
        self.typing = typing
        self._offset = 0
        self._offset_file: Path | None = None
        self._offset_loaded = False
        self._offset_dirty = False
        self._fail_streak = 0
        # Non-blocking backoff (P4S-18): the driver consults this before polling.
        self.next_poll_not_before = 0.0

    # -- offset persistence (cursor; adapter owns it) ----------------------- #
    def _offset_path(self) -> Path | None:
        if self._offset_file is not None:
            return self._offset_file
        # Grandfathered slug for telegram (P4S-20): the existing
        # ``telegram_offset_default.json`` name is preserved.
        slug = "default"
        if self._profile_dir is not None:
            base = Path(self._profile_dir)
        else:
            try:
                from .. import config as _cfg
                base = _cfg.profile_dir()
            except Exception:
                return None
        self._offset_file = base / f"telegram_offset_{slug}.json"
        return self._offset_file

    def _load_offset(self) -> None:
        if self._offset_loaded:
            return
        self._offset_loaded = True
        p = self._offset_path()
        if p is None or not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            val = int(data["offset"])
            if val >= 0:
                self._offset = val
        except Exception as exc:
            self.logger(f"gateway: offset file corrupt ({type(exc).__name__}); "
                        "starting from 0 (some updates may replay)")

    def commit(self) -> None:
        """Atomically persist the current offset AFTER the batch is handled (P4S-4)."""
        if not self._offset_dirty:
            return
        p = self._offset_path()
        if p is None:
            return
        payload = json.dumps({"offset": self._offset}) + "\n"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent),
                                       prefix=".tgoff-", suffix="~")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, p)
                self._offset_dirty = False
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            self.logger(f"gateway: offset save failed: {type(exc).__name__}")

    # -- polling ------------------------------------------------------------ #
    def supports_push(self) -> bool:
        return False

    def fetch(self) -> list[dict]:
        """Fetch one batch of raw updates; on failure arm non-blocking backoff.

        Returns the raw update list (possibly empty). On a transport failure
        returns ``[]`` and sets ``next_poll_not_before`` so the driver skips
        this adapter until the backoff window elapses -- NO ``sleep`` (P4S-18).
        Also returns the computed backoff ``delay`` via ``self._last_delay`` for
        observability.
        """
        self._load_offset()
        self._last_delay = 0.0
        try:
            updates = self.api.get_updates(self._offset, timeout=20)
        except Exception as exc:
            self._fail_streak += 1
            delay = min(_BACKOFF_BASE * (2 ** (self._fail_streak - 1)), _BACKOFF_CAP)
            self._last_delay = delay
            self.next_poll_not_before = self.clock() + delay
            self.logger(f"gateway: getUpdates failed ({type(exc).__name__}); "
                        f"backoff {delay:.0f}s (streak={self._fail_streak})")
            return []
        self._fail_streak = 0
        self.next_poll_not_before = 0.0
        return updates

    def parse(self, upd: dict) -> InboundMessage | None:
        """Parse one raw update into an InboundMessage, or drop it (H3)."""
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            return None
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        text = msg.get("text")
        user_id = frm.get("id")

        # H3 (P4S-5 telegram row): private chat only, sender == chat, sender
        # present. Dropped at the ADAPTER -- no InboundMessage, no reply, no LLM
        # call. SH-015/SH-016 preserved byte-for-byte.
        if (user_id is None or chat.get("type") != "private"
                or chat.get("id") != user_id):
            self.logger(
                f"gateway: ignored non-private/from-less update (uid={user_id})")
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        return InboundMessage(
            surface="telegram",
            user_id=str(user_id),
            channel_id=str(chat["id"]),
            text=text,
            is_private=True,
            meta={"update_id": int(upd.get("update_id", 0) or 0)},
        )

    def advance(self, upd: dict) -> None:
        """Advance the offset past a raw update (cursor sequencing, P4S-4)."""
        try:
            uid = int(upd.get("update_id", 0))
        except (TypeError, ValueError):
            uid = 0
        new = max(self._offset, uid + 1)
        if new != self._offset:
            self._offset = new
            self._offset_dirty = True

    def typing_cb(self, channel_id):
        """Return a callback the CORE invokes AFTER authorizing the message.

        The "typing" affordance (P4-T6) is emitted only when the core calls this
        back, i.e. after allowlist + privacy pass (P4S-19). A denied update never
        authorizes, so no ``sendChatAction`` is emitted -- the silent-deny
        discipline (SH-017) stays a silent deny, not a presence oracle.
        Best-effort: absence/failure never blocks the reply.
        """
        def _emit():
            if not self.typing:
                return
            try:
                self.api.send_chat_action(int(channel_id), "typing")
            except Exception as exc:
                self.logger(f"gateway: typing failed (ignored): "
                            f"{type(exc).__name__}")
        return _emit

    def send(self, reply: OutboundReply) -> None:
        """Send a reply (adapter owns chunking; Telegram 4000)."""
        try:
            self.api.send_message(int(reply.channel_id), reply.text)
        except Exception as exc:
            self.logger(f"gateway: send failed: {type(exc).__name__}")

    def error_reply(self, channel_id) -> None:
        """Best-effort error notice (per-update isolation; adapter sends)."""
        try:
            self.api.send_message(int(channel_id), "Sorry — I hit an internal error.")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# TelegramGateway -- thin compat shell (adapter + core)
# --------------------------------------------------------------------------- #
class TelegramGateway:
    """Compose a :class:`TelegramAdapter` + :class:`GatewayCore`.

    Preserves the exact public surface the daemon and tests rely on
    (``poll_once``, ``_offset``, ``_loops``, ``allow_write``, ``_fail_streak``,
    ``_loop_for``). The ``loop_factory`` keeps its legacy
    ``(user_id, instance, root)`` shape: the shell wraps it into the pinned
    core ``loop_builder`` signature, so the core still owns ceiling/gate/actor
    injection (P4S-2) while existing call sites stay unchanged.
    """

    def __init__(self, api, cfg: dict, instances: dict[str, Path], loop_factory,
                 *, clock=time.time, sleep=time.sleep, logger=None,
                 profile_dir=None, root_lock_factory=None):
        self.api = api
        self.cfg = cfg
        self.instances = instances
        self.loop_factory = loop_factory     # (user_id, instance, root) -> AgentLoop
        self.clock = clock
        self._sleep = sleep                  # retained for legacy test hook
        self.logger = logger or (lambda *a: None)

        self.adapter = TelegramAdapter(api, clock=clock, logger=self.logger,
                                       profile_dir=profile_dir)

        surface_cfg = ((cfg.get("gateway") or {}).get("telegram") or {})

        # Adapt the legacy loop_factory into the pinned core loop_builder. The
        # core injects ceiling_override/write_actor/write_gate; the legacy
        # factory ignores them (production serve passes the real builder shim,
        # which honors them; tests pass a 3-arg FakeLoop factory).
        def loop_builder(user_id, instance, root, *, ceiling_override,
                         write_actor, write_gate):
            return loop_factory(user_id, instance, root)

        self.core = GatewayCore(
            surface_cfg, "telegram", instances, loop_builder,
            clock=clock, logger=self.logger,
            root_lock_factory=root_lock_factory,
        )

    # -- legacy property mirrors (tests read these directly) ---------------- #
    @property
    def _offset(self) -> int:
        return self.adapter._offset

    @_offset.setter
    def _offset(self, value: int) -> None:
        self.adapter._offset = value

    @property
    def _fail_streak(self) -> int:
        return self.adapter._fail_streak

    @_fail_streak.setter
    def _fail_streak(self, value: int) -> None:
        self.adapter._fail_streak = value

    @property
    def _loops(self):
        return self.core._loops

    @property
    def _tg(self) -> dict:
        return ((self.cfg.get("gateway") or {}).get("telegram") or {})

    # -- driver entrypoint -------------------------------------------------- #
    def poll_once(self) -> int:
        """Fetch + handle one batch. Returns the count of served turns.

        Carve-out (P4S-18): a transport failure no longer blocks. The adapter
        arms ``next_poll_not_before``; for the legacy in-process daemon and the
        existing test hook we still invoke ``self._sleep(delay)`` so the
        observable backoff sequence is unchanged, but the daemon's multi-adapter
        driver (P4-T5) consults ``next_poll_not_before`` and never sleeps.
        """
        before_streak = self.adapter._fail_streak
        updates = self.adapter.fetch()
        if self.adapter._fail_streak > before_streak:
            # Failure path: legacy observable sleep (driver uses non-blocking).
            self._sleep(self.adapter._last_delay)
            return 0

        handled = 0
        for upd in updates:
            self.adapter.advance(upd)
            try:
                msg = self.adapter.parse(upd)
                if msg is None:
                    continue
                reply = self.core.handle(
                    msg, on_authorized=self.adapter.typing_cb(msg.channel_id))
                if reply is not None:
                    self.adapter.send(reply)
                    handled += 1
            except Exception as exc:
                # Per-update isolation: log, best-effort error reply, never die.
                self.logger(f"gateway: unhandled exception in handle: "
                            f"{type(exc).__name__}: {exc}")
                try:
                    raw = upd.get("message") or upd.get("edited_message") or {}
                    cid = (raw.get("chat") or {}).get("id")
                    if cid is not None:
                        self.adapter.error_reply(cid)
                except Exception:
                    pass
        self.adapter.commit()
        return handled

    # -- legacy helpers (tests call these directly) ------------------------- #
    def _loop_for(self, user_id: str, instance: str, root: Path):
        return self.core._loop_for(str(user_id), instance, root, True)

    def allow_write(self, user_id: str) -> bool:
        return self.core.allow_write(str(user_id))

    # -- offset persistence shims (a few tests reach for these) ------------- #
    def _load_offset(self) -> None:
        self.adapter._load_offset()

    def _save_offset(self) -> None:
        self.adapter._offset_dirty = True
        self.adapter.commit()

    # logger flows through to both adapter + core when reassigned in tests.
    @property
    def logger(self):
        return self._logger

    @logger.setter
    def logger(self, fn) -> None:
        self._logger = fn or (lambda *a: None)
        if hasattr(self, "adapter"):
            self.adapter.logger = self._logger
        if hasattr(self, "core"):
            self.core.logger = self._logger


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _chunks(text: str, n: int):
    if not text:
        yield ""
        return
    for i in range(0, len(text), n):
        yield text[i:i + n]
