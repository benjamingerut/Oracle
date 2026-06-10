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

import contextlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

_TELEGRAM_MAX = 4000
_ACCESS_RE = ("allowlist", "add me", "give me access", "authorize me",
              "approve the pairing", "approve my access", "let me in",
              "grant me access")
_ACCESS_REFUSAL = (
    "I can't change access from chat. Access is managed only on the host "
    "machine by the operator."
)

# Backoff parameters for consecutive poll failures (SPEC S2 #4).
_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 60.0

# LRU loop cache capacity (SPEC S2 #6).
_LOOP_CACHE_SIZE = 64


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


# --------------------------------------------------------------------------- #
# No-op root lock (used by tests / fallback)
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def _noop_lock(name: str):  # noqa: ARG001
    yield


def _scheduler_root_lock(name: str):
    """Real lock: delegates to scheduler.root_lock (lazy import).

    Imported lazily so tests that inject a no-op avoid needing a profile dir.
    """
    from ..service.scheduler import root_lock as _rl
    return _rl(name)


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


class TelegramGateway:
    def __init__(self, api, cfg: dict, instances: dict[str, Path], loop_factory,
                 *, clock=time.time, sleep=time.sleep, logger=None,
                 profile_dir=None, root_lock_factory=None):
        self.api = api
        self.cfg = cfg
        self.instances = instances
        self.loop_factory = loop_factory     # (user_id, instance, root) -> AgentLoop
        self.clock = clock
        self._sleep = sleep
        self.logger = logger or (lambda *a: None)
        self._profile_dir = profile_dir      # injected for tests; None -> config
        # ``root_lock_factory(name)`` returns a context manager (SPEC S2 #1).
        # Tests inject _noop_lock to avoid a real profile dir.
        self._root_lock_factory = (
            root_lock_factory if root_lock_factory is not None
            else _scheduler_root_lock
        )
        self._offset = 0
        self._offset_file: Path | None = None
        self._loops: OrderedDict[tuple[str, str], object] = OrderedDict()
        self._write_times: dict[str, list[float]] = {}
        self._fail_streak = 0               # consecutive poll failures
        self._offset_loaded = False

    # -- offset persistence ------------------------------------------------- #
    def _offset_path(self) -> Path | None:
        """Return the path for the persisted offset file, or None if unavailable."""
        if self._offset_file is not None:
            return self._offset_file
        # Use a stable slug that does not embed the raw secret.
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
        """Load persisted offset from disk; corruption falls back to 0."""
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

    def _save_offset(self) -> None:
        """Atomically persist current offset to disk."""
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
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            self.logger(f"gateway: offset save failed: {type(exc).__name__}")

    # -- polling ------------------------------------------------------------ #
    @property
    def _tg(self) -> dict:
        return ((self.cfg.get("gateway") or {}).get("telegram") or {})

    def poll_once(self) -> int:
        """Fetch + handle one batch of updates. Returns count handled (served).

        Failure path: exponential backoff (base 2s, cap 60s), then return 0.
        Success path: reset failure streak, no extra sleep.
        Normal empty long-poll: treated as success (no extra sleep).
        """
        self._load_offset()
        try:
            updates = self.api.get_updates(self._offset, timeout=20)
        except Exception as exc:
            self._fail_streak += 1
            delay = min(_BACKOFF_BASE * (2 ** (self._fail_streak - 1)), _BACKOFF_CAP)
            self.logger(f"gateway: getUpdates failed ({type(exc).__name__}); "
                        f"backoff {delay:.0f}s (streak={self._fail_streak})")
            self._sleep(delay)
            return 0

        # Success: reset streak.
        self._fail_streak = 0

        handled = 0
        for upd in updates:
            try:
                uid = int(upd.get("update_id", 0))
            except (TypeError, ValueError):
                uid = 0
            self._offset = max(self._offset, uid + 1)
            try:
                if self._handle(upd):
                    handled += 1
            except Exception as exc:
                # Per-update isolation: log, never kill the daemon.
                self.logger(f"gateway: unhandled exception in _handle: "
                            f"{type(exc).__name__}: {exc}")
                # Best-effort notify sender of internal error.
                try:
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    cid = (msg.get("chat") or {}).get("id")
                    if cid is not None:
                        self._reply(cid, "Sorry — I hit an internal error.")
                except Exception:
                    pass

        self._save_offset()
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
        if (user_id is None or chat.get("type") != "private"
                or chat.get("id") != user_id):
            self.logger(
                f"gateway: ignored non-private/from-less update (uid={user_id})")
            return False
        if not isinstance(text, str) or not text.strip():
            return False

        # Allowlist lookup with malformed-entry guard (SPEC S2 #3).
        raw_allowlist = self._tg.get("allowlist") or {}
        raw_entry = raw_allowlist.get(str(user_id))
        if not isinstance(raw_entry, dict):
            if raw_entry is not None:
                self.logger(
                    f"gateway: allowlist entry for {user_id} is malformed "
                    f"(type={type(raw_entry).__name__}); denying")
            else:
                self.logger(f"gateway: denied unknown sender {user_id}")
            return False  # deny-by-default, no reply

        entry = raw_entry
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
            # SPEC S2 #1: every gateway turn holds the root lock.
            with self._root_lock_factory(instance):
                result = loop.run_turn(text)
        except Exception as exc:
            self.logger(
                f"gateway: turn failed for {user_id}: {type(exc).__name__}")
            self._reply(chat["id"], "Sorry — I hit an error handling that.")
            return True

        self._reply(chat["id"], result.text)
        self._ledger(root, user_id, chat["id"], text, result)
        return True

    def _loop_for(self, user_id: str, instance: str, root: Path):
        key = (user_id, instance)
        loop = self._loops.get(key)
        if loop is not None:
            # LRU: move accessed entry to the most-recently-used end.
            self._loops.move_to_end(key)
            return loop
        loop = self.loop_factory(user_id, instance, root)
        # Evict the least-recently-used (first) entry when at capacity.
        if len(self._loops) >= _LOOP_CACHE_SIZE:
            self._loops.popitem(last=False)
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
