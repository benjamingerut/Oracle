"""gateway/core.py -- the transport-agnostic gateway engine (Phase 4, P4-T1).

``GatewayCore`` owns everything that must be identical across every messaging
surface (telegram/slack/email/http): allowlist resolution + deny-by-default,
the ``is_private`` privacy rule, forced grounding (ENFORCE) + wall clock,
the per-surface ceiling, write rate limiting, the per-user repair cap, the
metadata-only ledger row (with P3 repair telemetry), access-change refusal,
the LRU loop cache, per-message exception isolation, and the per-turn root
flock. Adapters only translate their platform's wire format to/from a
normalized :class:`InboundMessage` / :class:`OutboundReply` and assert
``is_private`` truthfully (P4S-3 responsibility table).

The ceiling and the write-gate are injected by **core itself** into a pinned
``loop_builder`` signature (P4S-2): an adapter bug or a serve-wiring slip can
drop a message but can never widen access. Write provenance is surface-
namespaced (``gateway_user:<surface>:<id>``, P4S-17).

Stdlib only.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Access-change refusal (DESIGN D7 / STRESS-I4). Shared across surfaces.
_ACCESS_RE = ("allowlist", "add me", "give me access", "authorize me",
              "approve the pairing", "approve my access", "let me in",
              "grant me access")
_ACCESS_REFUSAL = (
    "I can't change access from chat. Access is managed only on the host "
    "machine by the operator."
)

# LRU loop cache capacity (SPEC S2 #6); one cache per core.
_LOOP_CACHE_SIZE = 64

# Public-cap label used when a reply lands on a non-private channel (P4S-5).
_PUBLIC = "public"


# --------------------------------------------------------------------------- #
# Normalized wire types (P4S-1/5)
# --------------------------------------------------------------------------- #
@dataclass
class InboundMessage:
    surface: str               # TRANSPORT name: "telegram" | "slack" | "email" | "http"
    user_id: str               # platform-native, verified by the adapter
    channel_id: str            # where a reply goes
    text: str
    is_private: bool           # adapter asserts the delivery target is 1:1 to user_id
    meta: dict = field(default_factory=dict)  # SCALAR metadata only (P4S-5)


@dataclass
class OutboundReply:
    channel_id: str
    text: str


class GatewayAdapter(Protocol):
    surface: str

    def poll(self) -> list[InboundMessage]: ...        # or push; see below

    def send(self, reply: OutboundReply) -> None: ...  # adapter owns chunking

    def commit(self) -> None: ...                      # persist cursor AFTER batch

    def supports_push(self) -> bool: ...


# --------------------------------------------------------------------------- #
# No-op root lock (used by tests / fallback)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _noop_lock(name: str):  # noqa: ARG001
    yield


def _scheduler_root_lock(name: str):
    """Real lock: delegates to scheduler.root_lock (lazy import)."""
    from ..service.scheduler import root_lock as _rl
    return _rl(name)


# --------------------------------------------------------------------------- #
# GatewayCore -- the shared decision point (P4S-2/3)
# --------------------------------------------------------------------------- #
class GatewayCore:
    """The ONLY authorization/ceiling/grounding/rate-limit decision point.

    ``loop_builder`` carries the pinned signature
    ``loop_builder(user_id, instance, root, *, ceiling_override, write_actor,
    write_gate)`` (in production a thin shim over ``builder.build_loop`` with
    ``surface="gateway"`` hard-coded). Core injects all three keyword args
    itself -- no adapter or serve wiring can substitute any of them (P4S-2).
    """

    def __init__(self, surface_cfg: dict, surface: str,
                 instances: dict[str, Path], loop_builder,
                 *, clock=time.time, logger=None, root_lock_factory=None):
        self.surface_cfg = surface_cfg or {}
        self.surface = surface
        self.instances = instances
        self.loop_builder = loop_builder
        self.clock = clock
        self.logger = logger or (lambda *a: None)
        self._root_lock_factory = (
            root_lock_factory if root_lock_factory is not None
            else _scheduler_root_lock
        )
        self._loops: OrderedDict[tuple[str, str], object] = OrderedDict()
        self._write_times: dict[str, list[float]] = {}
        self._repair_times: dict[str, list[float]] = {}

    # -- public API --------------------------------------------------------- #
    def handle(self, msg: InboundMessage) -> OutboundReply | None:
        """Authorize, run, ledger one inbound message.

        Returns an :class:`OutboundReply` (the adapter sends it) or ``None``
        when nothing is to be said (deny-by-default silence). Per-message
        exception isolation is the driver's job around the batch; the core's
        own turn-failure path returns a best-effort error reply rather than
        raising. Authorization failures are silent (no reply, no LLM call).
        """
        user_id = str(msg.user_id)

        # Privacy rule (core decides; adapter asserts). is_private == false ⇒
        # any above-public reply is impossible; the per-surface drop happens in
        # the adapter (telegram/slack), email serves capped (P4S-5).
        if not isinstance(msg.text, str) or not msg.text.strip():
            return None

        # Allowlist lookup with malformed-entry guard (deny-by-default, P4S-3).
        raw_allowlist = self.surface_cfg.get("allowlist") or {}
        raw_entry = raw_allowlist.get(user_id)
        if not isinstance(raw_entry, dict):
            if raw_entry is not None:
                self.logger(
                    f"gateway[{self.surface}]: allowlist entry for {user_id} is "
                    f"malformed (type={type(raw_entry).__name__}); denying")
            else:
                self.logger(
                    f"gateway[{self.surface}]: denied unknown sender {user_id}")
            return None  # deny-by-default, no reply

        instance = raw_entry.get("instance")
        root = self.instances.get(instance)
        if root is None:
            return OutboundReply(msg.channel_id,
                                 f"Instance '{instance}' is not available.")

        if any(kw in msg.text.lower() for kw in _ACCESS_RE):
            return OutboundReply(msg.channel_id, _ACCESS_REFUSAL)

        loop = self._loop_for(user_id, instance, root, msg.is_private)

        # Per-user repair cap (P3S-3): refuse before any model call when over.
        if not self._allow_repairs(user_id):
            return OutboundReply(
                msg.channel_id,
                "You've hit the hourly limit for grounding-repair turns. "
                "Please try again later.")

        started = self.clock()
        try:
            # Every gateway turn holds the root lock (SPEC S2 #1).
            with self._root_lock_factory(instance):
                result = loop.run_turn(msg.text)
        except Exception as exc:
            self.logger(
                f"gateway[{self.surface}]: turn failed for {user_id}: "
                f"{type(exc).__name__}")
            return OutboundReply(msg.channel_id,
                                 "Sorry — I hit an error handling that.")

        added_seconds = max(0.0, self.clock() - started)
        self._record_repairs(user_id, int(getattr(result, "repairs", 0) or 0))
        self._ledger(root, msg, result, added_seconds)
        return OutboundReply(msg.channel_id, result.text)

    # -- loop cache (LRU, capacity 64; P4S-3) ------------------------------- #
    def _loop_for(self, user_id: str, instance: str, root: Path,
                  is_private: bool):
        # The core-owned ceiling: per-surface max_sensitivity, hard-capped at
        # ``public`` when the channel is not a private 1:1 (P4S-5). Cache key
        # carries the effective ceiling so a private and a (hypothetical) non-
        # private turn for the same user never share a loop built at the wrong
        # cap.
        ceiling = self._ceiling_for(is_private)
        key = (user_id, instance, ceiling)
        loop = self._loops.get(key)
        if loop is not None:
            self._loops.move_to_end(key)  # LRU promote
            return loop
        gate = self._write_gate_for(user_id)
        actor = f"gateway_user:{self.surface}:{user_id}"
        loop = self.loop_builder(
            user_id, instance, root,
            ceiling_override=ceiling,
            write_actor=actor,
            write_gate=gate,
        )
        if len(self._loops) >= _LOOP_CACHE_SIZE:
            self._loops.popitem(last=False)  # evict LRU
        self._loops[key] = loop
        return loop

    def _ceiling_for(self, is_private: bool) -> str:
        """The core-owned ceiling for this surface, public-capped if non-private."""
        ceiling = self.surface_cfg.get("max_sensitivity", "internal")
        if not is_private:
            return _PUBLIC
        return ceiling

    def _write_gate_for(self, user_id: str):
        uid = str(user_id)
        return lambda u=uid: self.allow_write(u)

    # -- ledger (metadata only, whitelisted fields; P4S-5) ------------------ #
    def _ledger(self, root: Path, msg: InboundMessage, result,
                added_seconds: float) -> None:
        """Append the pinned ``gateway_turn`` row. ``meta``/raw never serialized."""
        verdicts = [e.get("verdict") for e in (getattr(result, "envelopes", None) or [])]
        row = {
            "kind": "gateway_turn",
            "surface": self.surface,
            "user_id": str(msg.user_id),
            "channel_id": str(msg.channel_id),
            "chars_in": len(msg.text),
            "chars_out": len(result.text),
            "verdicts": verdicts,
            "grounding": getattr(result, "grounding", None),
            "repairs": int(getattr(result, "repairs", 0) or 0),
            "redacted": int(getattr(result, "redacted_count", 0) or 0),
            "withheld": bool(getattr(result, "withheld", False)),
            "added_seconds": round(float(added_seconds), 3),
            "ts": _iso(self.clock()),
        }
        path = Path(root) / "Meta.nosync" / "ledgers" / "gateway_event.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _append_jsonl(path, row)
        except OSError as exc:
            self.logger(
                f"gateway[{self.surface}]: ledger append failed: "
                f"{type(exc).__name__}")

    # -- repair cap (P3S-3, optional) --------------------------------------- #
    def _repair_cap(self) -> int | None:
        raw = self.surface_cfg.get("per_user_repairs_per_hour")
        if raw is None:
            return None
        try:
            cap = int(raw)
        except (TypeError, ValueError):
            return None
        return cap if cap > 0 else None

    def _allow_repairs(self, user_id: str) -> bool:
        cap = self._repair_cap()
        if cap is None:
            return True
        now = self.clock()
        window = [t for t in self._repair_times.get(user_id, []) if now - t < 3600]
        self._repair_times[user_id] = window
        return len(window) < cap

    def _record_repairs(self, user_id: str, n: int) -> None:
        if n <= 0 or self._repair_cap() is None:
            return
        now = self.clock()
        window = [t for t in self._repair_times.get(user_id, []) if now - t < 3600]
        window.extend([now] * n)
        self._repair_times[user_id] = window

    # -- write rate limiting (M4); bound into the loop via write_gate ------- #
    def allow_write(self, user_id: str) -> bool:
        cap = int(self.surface_cfg.get("per_user_writes_per_hour", 20))
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
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).isoformat()
