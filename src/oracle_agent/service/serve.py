"""service/serve.py -- the daemon: tick loops + poll the gateway (SPEC S6).

One process, one job: every ``tick_seconds`` run each instance's harness pass
(autonomy-gated, A5) and, between ticks, poll the messaging gateway when
enabled. A single ``serve.lock`` prevents two daemons; per-root flocks (in
scheduler) prevent chat/serve write races.

Config is read once at startup; registry changes need a restart (A11).

Stdlib only.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from .. import config
from . import scheduler

# Rotate serve.log at 5 MiB; keep one backup (.1).
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP = 1


def _rotate_log(path: Path) -> None:
    """Rotate ``path`` to ``path.1`` when it exceeds ``_LOG_MAX_BYTES``."""
    try:
        if path.exists() and path.stat().st_size >= _LOG_MAX_BYTES:
            backup = Path(str(path) + ".1")
            # Atomic on POSIX; backup is silently overwritten if present.
            os.replace(str(path), str(backup))
    except OSError:
        pass


def _log(line: str) -> None:
    try:
        path = config.logs_dir() / "serve.log"
        _rotate_log(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except OSError:
        pass


def _gateway_loop_builder(cfg: dict):
    """Return the pinned core ``loop_builder`` (P4S-2): a thin shim over
    ``builder.build_loop`` with ``surface="gateway"`` HARD-CODED.

    The transport name never reaches ``build_loop`` (P4S-1): every gateway
    message is served on the literal ``"gateway"`` loop surface. The core
    injects ``ceiling_override``/``write_actor``/``write_gate`` itself; this
    shim merely forwards them -- there is no path by which an adapter or serve
    wiring can substitute any of the four (the holder hack is gone).
    """
    from ..agentloop.builder import build_loop

    def loop_builder(user_id, instance, root, *, ceiling_override,
                     write_actor, write_gate):
        return build_loop(
            cfg, root, surface="gateway",
            ceiling_override=ceiling_override,
            write_actor=write_actor,
            write_gate=write_gate,
        )

    return loop_builder


def _build_gateway(cfg: dict):
    """Construct the Telegram gateway if enabled, else None."""
    tg = ((cfg.get("gateway") or {}).get("telegram") or {})
    if not tg.get("enabled"):
        return None
    token = config.resolve_secret(tg.get("token_env") or "")
    if not token:
        _log("gateway: telegram enabled but token unresolved; skipping")
        return None
    from ..gateway.core import GatewayCore
    from ..gateway.telegram import TelegramAdapter, TelegramAPI

    instances = config.instance_roots(cfg)
    adapter = TelegramAdapter(TelegramAPI(token), logger=_log)
    core = GatewayCore(tg, "telegram", instances, _gateway_loop_builder(cfg),
                       logger=_log)
    return _TelegramDriver(adapter, core, logger=_log)


class _TelegramDriver:
    """Drive one poll-capable telegram adapter through its core (P4S-4).

    ``poll() -> core.handle() -> adapter.send() -> adapter.commit()``. Honors
    the adapter's non-blocking ``next_poll_not_before`` backoff (P4S-18) -- no
    in-line sleep in the poll path.
    """

    def __init__(self, adapter, core, *, logger=None, clock=time.time):
        self.adapter = adapter
        self.core = core
        self.logger = logger or (lambda *a: None)
        self.clock = clock

    def poll_once(self) -> int:
        # Non-blocking backoff: skip until the window elapses (P4S-18).
        if self.clock() < getattr(self.adapter, "next_poll_not_before", 0.0):
            return 0
        updates = self.adapter.fetch()
        handled = 0
        for upd in updates:
            self.adapter.advance(upd)
            try:
                msg = self.adapter.parse(upd)
                if msg is None:
                    continue
                reply = self.core.handle(msg)
                if reply is not None:
                    self.adapter.send(reply)
                    handled += 1
            except Exception as exc:
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


def serve(cfg: dict, *, once: bool = False) -> int:
    """Run the daemon. ``once=True`` does a single tick + single gateway poll."""
    lock = scheduler.acquire_serve_lock()
    if lock is None:
        _log("serve: another daemon holds serve.lock; exiting")
        return 1

    stop = {"flag": False}

    def _handle(signum, _frame):
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass  # not in main thread (tests)

    instances = config.instance_roots(cfg)
    gateway = _build_gateway(cfg)
    tick_seconds = int((cfg.get("serve") or {}).get("tick_seconds", 300))

    try:
        if once:
            for r in scheduler.tick_all(instances, logger=_log):
                _log(f"tick {r.instance}: rc={r.rc} skipped={r.skipped} {r.output[:200]}")
            if gateway is not None:
                n = gateway.poll_once()
                _log(f"gateway poll: handled {n}")
            return 0

        last_tick = 0.0
        while not stop["flag"]:
            now = time.time()
            if now - last_tick >= tick_seconds:
                for r in scheduler.tick_all(instances, logger=_log):
                    _log(f"tick {r.instance}: rc={r.rc} skipped={r.skipped} {r.output[:200]}")
                last_tick = now
            if gateway is not None:
                gateway.poll_once()
            else:
                time.sleep(min(tick_seconds, 5))
        _log("serve: stopped cleanly")
        return 0
    finally:
        try:
            lock.close()
        except Exception:
            pass
