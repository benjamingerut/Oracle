"""Tests for service/serve.py multiplexing (Phase 4, P4-T5).

The multiplexing discipline (P4S-18, the P1S-13 class):
  * per-adapter exception isolation -- one adapter raising never skips the
    others or the tick;
  * non-blocking backoff -- a backed-off adapter (``next_poll_not_before`` in
    the future) is skipped WITHOUT sleeping, so it never delays the others;
  * NO ``sleep()`` anywhere in a poll path;
  * push adapters (http) get their own listener thread, started/stopped cleanly;
  * one ``GatewayCore`` per (surface, instance-set);
  * the briefer is driven between ticks and rides the same adapters/ceilings.

These drive the real ``_PollDriver`` / ``_build_brief_senders`` / ``serve(once)``
wiring with injected fakes -- no live network, no real sockets.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from oracle_agent.service import serve
from oracle_agent.gateway.core import InboundMessage, OutboundReply


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeCore:
    def __init__(self):
        self.handled = []
        self.authorized = []

    def handle(self, msg, *, on_authorized=None):
        self.handled.append(msg)
        if on_authorized is not None:
            on_authorized()
            self.authorized.append(msg)
        return OutboundReply(msg.channel_id, "ok")


class FakeAdapter:
    """A poll-shaped fake adapter (telegram/email/slack-shaped)."""

    def __init__(self, surface, items=None, *, fail_fetch=False,
                 next_poll_not_before=0.0, typing=True):
        self.surface = surface
        self._items = list(items or [])
        self.fail_fetch = fail_fetch
        self.next_poll_not_before = next_poll_not_before
        self.sent = []
        self.committed = 0
        self.typing_calls = []
        self._typing = typing

    def fetch(self):
        if self.fail_fetch:
            raise OSError("boom")
        items, self._items = self._items, []
        return items

    def parse(self, item):
        if item.get("drop"):
            return None
        return InboundMessage(self.surface, "u1", "c1", item["text"], True, {})

    def typing_cb(self, channel_id):
        def _emit():
            if self._typing:
                self.typing_calls.append(channel_id)
        return _emit

    def send(self, reply):
        self.sent.append((reply.channel_id, reply.text))

    def commit(self):
        self.committed += 1


class RaisingAdapter(FakeAdapter):
    def fetch(self):
        raise RuntimeError("adapter A exploded")


# --------------------------------------------------------------------------- #
# _PollDriver: happy path + typing seam
# --------------------------------------------------------------------------- #
def test_poll_driver_round_trips_and_commits():
    adapter = FakeAdapter("telegram", [{"text": "hi"}])
    core = FakeCore()
    d = serve._PollDriver(adapter, core, clock=lambda: 0.0)
    handled = d.poll_once()
    assert handled == 1
    assert adapter.sent == [("c1", "ok")]
    assert adapter.committed == 1
    # Typing seam fired (post-authorization).
    assert adapter.typing_calls == ["c1"]
    assert core.authorized == core.handled


def test_poll_driver_skips_dropped_items():
    adapter = FakeAdapter("telegram", [{"text": "x", "drop": True}])
    core = FakeCore()
    d = serve._PollDriver(adapter, core, clock=lambda: 0.0)
    assert d.poll_once() == 0
    assert core.handled == []


# --------------------------------------------------------------------------- #
# Non-blocking backoff: a backed-off adapter is skipped without sleeping
# --------------------------------------------------------------------------- #
def test_backed_off_adapter_skipped_nonblocking():
    adapter = FakeAdapter("slack", [{"text": "hi"}], next_poll_not_before=100.0)
    core = FakeCore()
    # clock < next_poll_not_before => skip entirely (no fetch, no sleep).
    d = serve._PollDriver(adapter, core, clock=lambda: 50.0)
    assert d.poll_once() == 0
    assert core.handled == []
    assert adapter.committed == 0


def test_backoff_window_elapsed_polls_again():
    adapter = FakeAdapter("slack", [{"text": "hi"}], next_poll_not_before=100.0)
    core = FakeCore()
    d = serve._PollDriver(adapter, core, clock=lambda: 150.0)
    assert d.poll_once() == 1


# --------------------------------------------------------------------------- #
# Per-adapter isolation: A raising never skips B or the tick (P4S-18)
# --------------------------------------------------------------------------- #
def test_one_adapter_raising_does_not_skip_others(tmp_path, monkeypatch):
    """serve(once) with adapter A raising still polls B and still ticks."""
    a = RaisingAdapter("telegram")
    b = FakeAdapter("slack", [{"text": "hi"}])
    core_a, core_b = FakeCore(), FakeCore()
    driver_a = serve._PollDriver(a, core_a, clock=lambda: 0.0)
    driver_b = serve._PollDriver(b, core_b, clock=lambda: 0.0)

    ticked = {"n": 0}

    def fake_tick_all(instances, logger=None):
        ticked["n"] += 1
        return []

    monkeypatch.setattr(serve.scheduler, "tick_all", fake_tick_all)
    monkeypatch.setattr(serve.scheduler, "acquire_serve_lock",
                        lambda: _FakeLock())
    monkeypatch.setattr(serve, "_build_gateways",
                        lambda cfg, instances: ([driver_a, driver_b], []))
    monkeypatch.setattr(serve.config, "instance_roots", lambda cfg: {})

    rc = serve.serve({"serve": {"tick_seconds": 300}}, once=True)
    assert rc == 0
    # Adapter B was polled despite A raising; the tick fired.
    assert b.sent == [("c1", "ok")]
    assert ticked["n"] == 1


# --------------------------------------------------------------------------- #
# Briefer runs between ticks, riding the same adapters (P4-T8 wiring)
# --------------------------------------------------------------------------- #
def test_brief_senders_reuse_adapter_send():
    tg = FakeAdapter("telegram")
    slack = FakeAdapter("slack")
    drivers = [serve._PollDriver(tg, FakeCore(), clock=lambda: 0.0),
               serve._PollDriver(slack, FakeCore(), clock=lambda: 0.0)]
    senders = serve._build_brief_senders({}, drivers, [])
    assert set(senders) == {"telegram", "slack"}
    senders["telegram"]("12345", "brief body")
    assert tg.sent == [("12345", "brief body")]


def test_serve_once_runs_briefer(tmp_path, monkeypatch):
    """serve(once) invokes the briefer pass between/after the tick."""
    calls = {"n": 0}

    def fake_run_briefer(cfg, instances, poll_drivers, push_adapters):
        calls["n"] += 1

    monkeypatch.setattr(serve.scheduler, "tick_all",
                        lambda instances, logger=None: [])
    monkeypatch.setattr(serve.scheduler, "acquire_serve_lock",
                        lambda: _FakeLock())
    monkeypatch.setattr(serve, "_build_gateways",
                        lambda cfg, instances: ([], []))
    monkeypatch.setattr(serve, "_run_briefer", fake_run_briefer)
    monkeypatch.setattr(serve.config, "instance_roots", lambda cfg: {})

    serve.serve({"serve": {"tick_seconds": 300}}, once=True)
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# Push adapter (http) listener started + stopped cleanly (P4S-9)
# --------------------------------------------------------------------------- #
class FakePushAdapter:
    surface = "http"

    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def send(self, reply):
        pass


def test_push_adapter_started_and_stopped(monkeypatch):
    push = FakePushAdapter()
    monkeypatch.setattr(serve.scheduler, "tick_all",
                        lambda instances, logger=None: [])
    monkeypatch.setattr(serve.scheduler, "acquire_serve_lock",
                        lambda: _FakeLock())
    monkeypatch.setattr(serve, "_build_gateways",
                        lambda cfg, instances: ([], [push]))
    monkeypatch.setattr(serve, "_run_briefer",
                        lambda *a, **k: None)
    monkeypatch.setattr(serve.config, "instance_roots", lambda cfg: {})

    serve.serve({"serve": {"tick_seconds": 300}}, once=True)
    assert push.started is True
    assert push.stopped is True, "listener must be stopped cleanly (P4S-9)"


# --------------------------------------------------------------------------- #
# Real HTTP listener start/stop is clean (P4S-9) -- exercises the real adapter
# --------------------------------------------------------------------------- #
def test_real_http_listener_starts_and_stops():
    from oracle_agent.gateway.http import HTTPAdapter

    class _Core:
        def handle(self, msg, *, on_authorized=None):
            return None

    adapter = HTTPAdapter(
        {"bind": "127.0.0.1", "port": 0, "principal": "p"},
        _Core(), token="tok")
    adapter.start()
    try:
        assert adapter._thread is not None
        assert adapter._thread.is_alive()
    finally:
        adapter.stop()
    assert adapter._server is None
    # The listener thread is joined on stop.
    assert not any(t.name == "oracle-http" and t.is_alive()
                   for t in threading.enumerate())


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _FakeLock:
    def close(self):
        pass
