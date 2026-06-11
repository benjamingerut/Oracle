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
    injects ``ceiling_override``/``write_actor``/``write_role``/``write_gate``
    itself; this shim merely forwards them -- there is no path by which an
    adapter or serve wiring can substitute any of the five (the holder hack is
    gone). ``write_role`` is the clamped principal role (P5-T2 / P5S-13).
    """
    from ..agentloop.builder import build_loop

    def loop_builder(user_id, instance, root, *, ceiling_override,
                     write_actor, write_role, write_gate):
        return build_loop(
            cfg, root, surface="gateway",
            ceiling_override=ceiling_override,
            write_actor=write_actor,
            write_role=write_role,
            write_gate=write_gate,
        )

    return loop_builder


# --------------------------------------------------------------------------- #
# Generic poll driver (telegram/email/slack share this shape; P4S-4/18)
#
# P4S-2: ``serve._build_gateway`` (and its ``holder`` hack) is DELETED -- the
# multi-surface ``_build_gateways`` below replaces it, and the core injects the
# ceiling/write-gate/actor itself via ``_gateway_loop_builder`` (no serve-side
# substitution path remains).
# --------------------------------------------------------------------------- #
class _PollDriver:
    """Drive one poll-capable adapter through its core (P4S-4).

    ``fetch() -> [items]``, then per item ``parse() -> core.handle() ->
    send()``, then ``commit()``. Honors ``next_poll_not_before`` non-blocking
    backoff (P4S-18) and per-item exception isolation. The ``ack`` hook (Slack
    Socket Mode) and the ``error_cid`` extractor are adapter-shaped; both
    default to no-ops so telegram/email need not provide them.
    """

    def __init__(self, adapter, core, *, logger=None, clock=time.time,
                 error_cid=None):
        self.adapter = adapter
        self.core = core
        self.logger = logger or (lambda *a: None)
        self.clock = clock
        self._error_cid = error_cid or (lambda item: None)

    @property
    def surface(self) -> str:
        return getattr(self.adapter, "surface", "?")

    def poll_once(self) -> int:
        if self.clock() < getattr(self.adapter, "next_poll_not_before", 0.0):
            return 0
        items = self.adapter.fetch()
        handled = 0
        for item in items:
            if hasattr(self.adapter, "ack"):
                try:
                    self.adapter.ack(item)
                except Exception:
                    pass
            if hasattr(self.adapter, "advance"):
                try:
                    self.adapter.advance(item)
                except Exception:
                    pass
            try:
                msg = self.adapter.parse(item)
                if msg is None:
                    continue
                on_auth = None
                if hasattr(self.adapter, "typing_cb"):
                    on_auth = self.adapter.typing_cb(msg.channel_id)
                elif hasattr(self.adapter, "_typing_cb"):
                    on_auth = self.adapter._typing_cb(msg.channel_id)
                reply = self.core.handle(msg, on_authorized=on_auth)
                if reply is not None:
                    self.adapter.send(reply)
                    handled += 1
            except Exception as exc:
                self.logger(f"gateway[{self.surface}]: unhandled exception in "
                            f"handle: {type(exc).__name__}: {exc}")
                try:
                    cid = self._error_cid(item)
                    if cid is not None and hasattr(self.adapter, "error_reply"):
                        self.adapter.error_reply(cid)
                except Exception:
                    pass
        try:
            self.adapter.commit()
        except Exception as exc:
            self.logger(f"gateway[{self.surface}]: commit failed: "
                        f"{type(exc).__name__}")
        return handled


def _telegram_error_cid(upd):
    raw = (upd.get("message") or upd.get("edited_message") or {}) if isinstance(upd, dict) else {}
    return (raw.get("chat") or {}).get("id")


def _slack_error_cid(envelope):
    if not isinstance(envelope, dict):
        return None
    payload = envelope.get("payload") or envelope
    event = (payload.get("event") or payload) if isinstance(payload, dict) else {}
    return event.get("channel") if isinstance(event, dict) else None


def _build_telegram_driver(cfg, instances):
    tg = ((cfg.get("gateway") or {}).get("telegram") or {})
    if not tg.get("enabled"):
        return None
    token = config.resolve_secret(tg.get("token_env") or "")
    if not token:
        _log("gateway: telegram enabled but token unresolved; skipping")
        return None
    from ..gateway.core import GatewayCore
    from ..gateway.telegram import TelegramAdapter, TelegramAPI

    adapter = TelegramAdapter(TelegramAPI(token), logger=_log)
    core = GatewayCore(tg, "telegram", instances, _gateway_loop_builder(cfg),
                       logger=_log)
    return _PollDriver(adapter, core, logger=_log,
                       error_cid=_telegram_error_cid)


def _build_email_driver(cfg, instances):
    em = ((cfg.get("gateway") or {}).get("email") or {})
    if not em.get("enabled"):
        return None
    user = config.resolve_secret(em.get("user_env") or "")
    password = config.resolve_secret(em.get("pass_env") or "")
    imap_host = em.get("imap_host") or ""
    smtp_host = em.get("smtp_host") or ""
    if not (user and password and imap_host and smtp_host):
        _log("gateway: email enabled but creds/hosts unresolved; skipping")
        return None
    from ..gateway.core import GatewayCore
    from ..gateway.email import EmailAdapter, IMAPClient, SMTPClient

    # own-address resolution (HANDOFF): the oracle's own single mailbox address
    # is the authenticated IMAP/SMTP user (the dedicated mailbox, P4S-12).
    own_address = user
    try:
        imap = IMAPClient(imap_host, user, password)
        smtp = SMTPClient(smtp_host, user, password)
    except Exception as exc:
        _log(f"gateway: email transport init failed ({type(exc).__name__}); skipping")
        return None
    adapter = EmailAdapter(em, own_address, imap, smtp,
                           instances=instances, logger=_log)
    core = GatewayCore(em, "email", instances, _gateway_loop_builder(cfg),
                       logger=_log)
    return _PollDriver(adapter, core, logger=_log)


def _build_slack_driver(cfg, instances):
    sl = ((cfg.get("gateway") or {}).get("slack") or {})
    if not sl.get("enabled"):
        return None
    from ..gateway import slack as slack_mod

    # Graceful degradation (I1/P4S-14): no websocket dep => Slack stays disabled
    # (doctor warns). NO module-level import of the optional dep.
    if not slack_mod.transport_available():
        _log("gateway: slack configured but websocket lib absent -- disabled")
        return None
    token = config.resolve_secret(sl.get("token_env") or "")
    if not token:
        _log("gateway: slack enabled but token unresolved; skipping")
        return None
    # NOTE: the live Socket Mode transport is constructed here in production
    # (wrapping the optional dep). With the dep absent we never reach this point.
    try:
        transport = slack_mod.build_socket_transport(token)  # type: ignore[attr-defined]
    except Exception as exc:
        _log(f"gateway: slack transport init failed ({type(exc).__name__}); skipping")
        return None
    from ..gateway.core import GatewayCore

    core = GatewayCore(sl, "slack", instances, _gateway_loop_builder(cfg),
                       logger=_log)
    adapter = slack_mod.SlackAdapter(transport, core, logger=_log)
    return _PollDriver(adapter, core, logger=_log, error_cid=_slack_error_cid)


def _build_http_listener(cfg, instances):
    """Build the push-capable HTTP adapter (its own listener thread; P4S-9)."""
    ht = ((cfg.get("gateway") or {}).get("http") or {})
    if not ht.get("enabled"):
        return None
    token = config.resolve_secret(ht.get("token_env") or "")
    if not token:
        _log("gateway: http enabled but token unresolved; refusing to start")
        return None
    from ..gateway.core import GatewayCore, _noop_lock
    from ..gateway.http import HTTPAdapter

    # HTTP turns take the per-root lock nb=True at the ADAPTER (503 on busy,
    # P4S-9), so the core is composed with a NO-OP root lock (the non-reentrant
    # flock is never taken twice). The adapter is given a non-blocking
    # lock factory that raises BlockingIOError on contention.
    core = GatewayCore(ht, "http", instances, _gateway_loop_builder(cfg),
                       logger=_log, root_lock_factory=_noop_lock)

    def nb_lock(instance):
        return scheduler.root_lock(instance, nb=True)

    try:
        adapter = HTTPAdapter(ht, core, token=token, logger=_log,
                              nb_lock_factory=nb_lock)
    except Exception as exc:
        _log(f"gateway: http adapter refused to start ({type(exc).__name__}: {exc})")
        return None
    return adapter


def _build_gateways(cfg: dict, instances: dict):
    """Return ``(poll_drivers, push_adapters)`` for every enabled surface.

    One :class:`GatewayCore` per (surface, instance-set). Poll-capable adapters
    (telegram/slack/email) are returned as drivers the serve loop polls between
    ticks; push-capable adapters (http) run their own listener thread (P4-T5).
    Per-surface construction is isolated -- one surface failing to build never
    blocks the others.
    """
    poll_drivers = []
    push_adapters = []
    builders = (
        ("telegram", _build_telegram_driver),
        ("email", _build_email_driver),
        ("slack", _build_slack_driver),
    )
    for name, build in builders:
        try:
            d = build(cfg, instances)
            if d is not None:
                poll_drivers.append(d)
        except Exception as exc:
            _log(f"gateway: {name} build failed ({type(exc).__name__}: {exc}); skipping")
    try:
        http = _build_http_listener(cfg, instances)
        if http is not None:
            push_adapters.append(http)
    except Exception as exc:
        _log(f"gateway: http build failed ({type(exc).__name__}: {exc}); skipping")
    return poll_drivers, push_adapters


# --------------------------------------------------------------------------- #
# Briefer wiring (P4-T8): scheduled push rides the same adapters/ceilings.
# --------------------------------------------------------------------------- #
def _build_brief_senders(cfg, poll_drivers, push_adapters):
    """Map surface -> ``send(target, text)`` for briefer delivery (P4S-15).

    Reuses each adapter's send path (and therefore its chunking + transport)
    so a scheduled push gets no privilege an interactive reply would not have.
    """
    from ..gateway.core import OutboundReply

    senders: dict = {}
    by_surface = {}
    for d in poll_drivers:
        by_surface[d.surface] = d.adapter
    for a in push_adapters:
        by_surface[getattr(a, "surface", "?")] = a

    for surface, adapter in by_surface.items():
        if not hasattr(adapter, "send"):
            continue

        def _make(adapter):
            def _send(target, text):
                adapter.send(OutboundReply(channel_id=str(target), text=text))
            return _send

        senders[surface] = _make(adapter)
    return senders


def _run_briefer(cfg, instances, poll_drivers, push_adapters):
    """Detect + deliver new briefs once (between ticks). Best-effort, isolated."""
    if not (cfg.get("briefings") or {}):
        return
    try:
        from . import briefer

        senders = _build_brief_senders(cfg, poll_drivers, push_adapters)
        if not senders:
            return
        state = briefer.DeliveryState(
            briefer.state_path(config.profile_dir()), logger=_log)
        state.load()
        if state.corrupt:
            _log("briefer: delivery-state corrupt; no delivery (doctor flag)")
            return
        n = briefer.run_once(cfg, instances, senders, state, logger=_log)
        if n:
            _log(f"briefer: delivered {n} brief(s)")
    except Exception as exc:
        _log(f"briefer: pass failed ({type(exc).__name__}: {exc})")


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
    poll_drivers, push_adapters = _build_gateways(cfg, instances)
    serve_cfg = cfg.get("serve") or {}
    tick_seconds = int(serve_cfg.get("tick_seconds", 300))
    # P5-T7a / P5S-5: opt-in dream convocation cadence. 0 == OFF (a level-2 root
    # still convenes nothing until the operator sets a cadence). Each convocation
    # is independently autonomy-gated + LOCK_NB-skipped in the scheduler.
    dream_tick_seconds = int(serve_cfg.get("dream_tick_seconds", 0))

    # Push adapters (http) run their own listener thread (P4S-9). Started once;
    # stopped cleanly in the finally block.
    for a in push_adapters:
        try:
            a.start()
        except Exception as exc:
            _log(f"gateway[{getattr(a, 'surface', '?')}]: start failed: "
                 f"{type(exc).__name__}")

    def _poll_all():
        """Poll every enabled poll-adapter once, isolated (P4S-18).

        One adapter raising never skips the others or the tick; a backed-off
        adapter (``next_poll_not_before``) is skipped non-blockingly, not slept
        on. No ``sleep`` anywhere in this path.
        """
        for d in poll_drivers:
            try:
                d.poll_once()
            except Exception as exc:
                _log(f"gateway[{d.surface}]: poll raised {type(exc).__name__}: "
                     f"{exc} (isolated; other adapters + tick continue)")

    try:
        if once:
            for r in scheduler.tick_all(instances, logger=_log):
                _log(f"tick {r.instance}: rc={r.rc} skipped={r.skipped} {r.output[:200]}")
            if dream_tick_seconds > 0:
                for r in scheduler.dream_all(instances, cfg, logger=_log):
                    _log(f"dream {r.instance}: rc={r.rc} skipped={r.skipped} "
                         f"{r.output[:200]}")
            _poll_all()
            _run_briefer(cfg, instances, poll_drivers, push_adapters)
            return 0

        last_tick = 0.0
        last_dream = 0.0
        while not stop["flag"]:
            now = time.time()
            if now - last_tick >= tick_seconds:
                for r in scheduler.tick_all(instances, logger=_log):
                    _log(f"tick {r.instance}: rc={r.rc} skipped={r.skipped} {r.output[:200]}")
                _run_briefer(cfg, instances, poll_drivers, push_adapters)
                last_tick = now
            if dream_tick_seconds > 0 and now - last_dream >= dream_tick_seconds:
                for r in scheduler.dream_all(instances, cfg, logger=_log):
                    _log(f"dream {r.instance}: rc={r.rc} skipped={r.skipped} "
                         f"{r.output[:200]}")
                last_dream = now
            if poll_drivers:
                _poll_all()
            else:
                # No poll adapters: idle in a bounded sleep so SIGTERM is prompt.
                time.sleep(min(tick_seconds, 5))
        _log("serve: stopped cleanly")
        return 0
    finally:
        # Clean shutdown of the HTTP listener thread (P4S-9): shutdown() from the
        # main thread, then join.
        for a in push_adapters:
            try:
                a.stop()
            except Exception as exc:
                _log(f"gateway[{getattr(a, 'surface', '?')}]: stop failed: "
                     f"{type(exc).__name__}")
        try:
            lock.close()
        except Exception:
            pass
