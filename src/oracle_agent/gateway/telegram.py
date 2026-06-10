"""gateway/telegram.py -- Telegram messaging surface (SPEC S7).

Remote users chat with an oracle over Telegram. Every safety property is
enforced in code:

  * Deny-by-default: only allowlisted user IDs are served; unknown senders are
    ignored entirely (logged id only, no reply, no LLM call).
  * Private-chat-only (STRESS H3): serve only when chat.type == "private" AND
    chat.id == from.id. Groups/channels/forwarded/anonymous/from-less updates
    are never served -- an answer can never leak to a non-allowlisted member.
  * Gateway (reduced) tool surface; control-plane never crosses the gateway.
  * Writes (remember/capture) tagged with gateway provenance and rate-limited
    (STRESS M4).
  * Metadata-only ledger row per handled turn -- never message bodies.
  * Access-change requests get a fixed refusal; there is no tool to change
    access, so the boundary is structural (DESIGN D7).

Stdlib only.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

_TELEGRAM_MAX = 4000
_ACCESS_RE = ("allowlist", "add me", "give me access", "authorize me",
              "approve the pairing", "approve my access", "let me in",
              "grant me access")
_ACCESS_REFUSAL = (
    "I can't change access from chat. Access is managed only on the host "
    "machine by the operator."
)


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
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        return obj.get("result", []) if obj.get("ok") else []

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text, _TELEGRAM_MAX):
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode()
            req = urllib.request.Request(self._url("sendMessage"), data=data, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass


class TelegramGateway:
    def __init__(self, api, cfg: dict, instances: dict[str, Path], loop_factory,
                 *, clock=time.time, logger=None):
        self.api = api
        self.cfg = cfg
        self.instances = instances
        self.loop_factory = loop_factory     # (user_id, instance, root) -> AgentLoop
        self.clock = clock
        self.logger = logger or (lambda *a: None)
        self._offset = 0
        self._loops: dict[tuple[str, str], object] = {}
        self._write_times: dict[str, list[float]] = {}

    @property
    def _tg(self) -> dict:
        return ((self.cfg.get("gateway") or {}).get("telegram") or {})

    def poll_once(self) -> int:
        """Fetch + handle one batch of updates. Returns count handled (served)."""
        try:
            updates = self.api.get_updates(self._offset, timeout=20)
        except Exception as exc:
            self.logger(f"gateway: getUpdates failed: {type(exc).__name__}")
            return 0
        handled = 0
        for upd in updates:
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
            if self._handle(upd):
                handled += 1
        return handled

    # -- handling ----------------------------------------------------------- #
    def _handle(self, upd: dict) -> bool:
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            return False
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        text = msg.get("text")
        user_id = frm.get("id")

        # H3: private chat only, sender == chat, sender present.
        if user_id is None or chat.get("type") != "private" or chat.get("id") != user_id:
            self.logger(f"gateway: ignored non-private/from-less update (uid={user_id})")
            return False
        if not isinstance(text, str) or not text.strip():
            return False

        entry = (self._tg.get("allowlist") or {}).get(str(user_id))
        if not entry:
            self.logger(f"gateway: denied unknown sender {user_id}")
            return False  # deny-by-default, no reply

        instance = entry.get("instance")
        root = self.instances.get(instance)
        if root is None:
            self._reply(chat["id"], f"Instance '{instance}' is not available.")
            return False

        if any(kw in text.lower() for kw in _ACCESS_RE):
            self._reply(chat["id"], _ACCESS_REFUSAL)
            return True

        loop = self._loop_for(str(user_id), instance, root)
        try:
            result = loop.run_turn(text)
        except Exception as exc:
            self.logger(f"gateway: turn failed for {user_id}: {type(exc).__name__}")
            self._reply(chat["id"], "Sorry — I hit an error handling that.")
            return True

        self._reply(chat["id"], result.text)
        self._ledger(root, user_id, chat["id"], text, result)
        return True

    def _loop_for(self, user_id: str, instance: str, root: Path):
        key = (user_id, instance)
        loop = self._loops.get(key)
        if loop is None:
            loop = self.loop_factory(user_id, instance, root)
            # simple LRU cap (L3)
            if len(self._loops) >= 64:
                self._loops.pop(next(iter(self._loops)))
            self._loops[key] = loop
        return loop

    def _reply(self, chat_id: int, text: str) -> None:
        try:
            self.api.send_message(chat_id, text)
        except Exception as exc:
            self.logger(f"gateway: send failed: {type(exc).__name__}")

    def _ledger(self, root: Path, user_id, chat_id, text_in, result) -> None:
        """Append a metadata-only gateway_turn row (never message bodies)."""
        verdicts = [e.get("verdict") for e in (result.envelopes or [])]
        row = {
            "kind": "gateway_turn", "platform": "telegram",
            "user_id": str(user_id), "chat_id": str(chat_id),
            "chars_in": len(text_in), "chars_out": len(result.text),
            "verdicts": verdicts, "ts": _iso(self.clock()),
        }
        path = Path(root) / "Meta.nosync" / "ledgers" / "gateway_event.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _append_jsonl(path, row)
        except OSError as exc:
            self.logger(f"gateway: ledger append failed: {type(exc).__name__}")

    # -- write rate limiting (M4), used by the loop_factory's dispatcher ---- #
    def allow_write(self, user_id: str) -> bool:
        cap = int(self._tg.get("per_user_writes_per_hour", 20))
        now = self.clock()
        window = [t for t in self._write_times.get(user_id, []) if now - t < 3600]
        if len(window) >= cap:
            self._write_times[user_id] = window
            return False
        window.append(now)
        self._write_times[user_id] = window
        return True


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _chunks(text: str, n: int):
    if not text:
        yield ""
        return
    for i in range(0, len(text), n):
        yield text[i:i + n]


def _append_jsonl(path: Path, row: dict) -> None:
    import fcntl
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _iso(epoch: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()
