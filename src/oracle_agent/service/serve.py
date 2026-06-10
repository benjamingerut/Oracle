"""service/serve.py -- the daemon: tick loops + poll the gateway (SPEC S6).

One process, one job: every ``tick_seconds`` run each instance's harness pass
(autonomy-gated, A5) and, between ticks, poll the messaging gateway when
enabled. A single ``serve.lock`` prevents two daemons; per-root flocks (in
scheduler) prevent chat/serve write races.

Config is read once at startup; registry changes need a restart (A11).

Stdlib only.
"""
from __future__ import annotations

import signal
import time
from pathlib import Path

from .. import config
from . import scheduler


def _log(line: str) -> None:
    try:
        path = config.logs_dir() / "serve.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except OSError:
        pass


def _build_gateway(cfg: dict):
    """Construct the Telegram gateway if enabled, else None."""
    tg = ((cfg.get("gateway") or {}).get("telegram") or {})
    if not tg.get("enabled"):
        return None
    token = config.resolve_secret(tg.get("token_env") or "")
    if not token:
        _log("gateway: telegram enabled but token unresolved; skipping")
        return None
    from ..agentloop.builder import build_loop
    from ..gateway.telegram import TelegramAPI, TelegramGateway

    instances = config.instance_roots(cfg)
    gw_ceiling = tg.get("max_sensitivity", "internal")
    holder: dict = {}

    def loop_factory(user_id, instance, root):
        gw = holder.get("gw")
        gate = (lambda uid=str(user_id): gw.allow_write(uid)) if gw else None
        return build_loop(cfg, root, surface="gateway",
                          ceiling_override=gw_ceiling,
                          write_actor=f"gateway_user:{user_id}",
                          write_gate=gate)

    gateway = TelegramGateway(TelegramAPI(token), cfg, instances, loop_factory,
                              logger=_log)
    holder["gw"] = gateway
    return gateway


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
            for r in scheduler.tick_all(instances):
                _log(f"tick {r.instance}: rc={r.rc} skipped={r.skipped} {r.output[:200]}")
            if gateway is not None:
                n = gateway.poll_once()
                _log(f"gateway poll: handled {n}")
            return 0

        last_tick = 0.0
        while not stop["flag"]:
            now = time.time()
            if now - last_tick >= tick_seconds:
                for r in scheduler.tick_all(instances):
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
