"""Tests for service/briefer.py -- scheduled briefing delivery (Phase 4, P4-T8).

Covers every P4S-15 pin:
  * registry-driven detection (watches _STANDING/.registry.jsonl for new
    ``leadership-brief`` rows; NO cadence cloning);
  * exactly-once across restarts (persisted ``(instance, surface, drop_id)``
    state; a second pass / a fresh state object delivers nothing new);
  * corruption => NO send + doctor flag (fail closed);
  * push targets must resolve to allowlisted PRIVATE identities (group id /
    unlisted chat / list address refused; deny-by-default with no target);
  * document-level ceiling re-check per surface (above => withhold WHOLE brief);
  * pinned ``briefing_delivery`` ledger row (metadata only).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from oracle_agent.service import briefer
from oracle_agent.service.briefer import (
    DeliveryState,
    deliver,
    new_briefs,
    resolve_target,
    run_once,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_registry(root: Path, rows: list[dict]) -> None:
    reg = root / "Workproduct.nosync" / "_STANDING" / ".registry.jsonl"
    reg.parent.mkdir(parents=True, exist_ok=True)
    with open(reg, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _brief_row(drop_id="STD-20260611-001", sensitivity="internal",
               name="2026-06-11_leadership-brief.md"):
    return {
        "drop_id": drop_id,
        "kind": "leadership-brief",
        "sensitivity": sensitivity,
        "artifact_name": name,
        "canonical_location": f"Workproduct.nosync/_STANDING/{name}",
    }


def _cfg(*, tg_allow=None, em_allow=None, targets=None,
         tg_max="internal", em_max="public"):
    return {
        "gateway": {
            "telegram": {"allowlist": tg_allow or {}, "max_sensitivity": tg_max},
            "email": {"allowlist": em_allow or {}, "max_sensitivity": em_max},
        },
        "briefings": {"main": {"targets": targets or []}} if targets else {},
    }


def _seed_brief_artifact(root: Path, name="2026-06-11_leadership-brief.md",
                         body="# Leadership Brief\n\nthe brief body\n"):
    p = root / "Workproduct.nosync" / "_STANDING" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class _RecordingSender:
    def __init__(self):
        self.sent = []

    def __call__(self, target, text):
        self.sent.append((target, text))


# --------------------------------------------------------------------------- #
# Target resolution (provably private; P4S-15)
# --------------------------------------------------------------------------- #
def test_telegram_target_resolves_when_allowlisted():
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}})
    assert resolve_target(cfg, {"surface": "telegram", "user_id": "12345"}) == \
        ("telegram", "12345")


def test_telegram_target_refused_when_not_allowlisted():
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}})
    # A group id / unlisted chat is NOT in the allowlist => refused.
    assert resolve_target(cfg, {"surface": "telegram", "user_id": "-100999"}) is None


def test_email_target_resolves_when_allowlisted():
    cfg = _cfg(em_allow={"ceo@co.com": {"instance": "main"}})
    assert resolve_target(cfg, {"surface": "email", "address": "CEO@co.com"}) == \
        ("email", "ceo@co.com")


def test_email_list_address_refused():
    cfg = _cfg(em_allow={"ceo@co.com": {"instance": "main"}})
    assert resolve_target(cfg, {"surface": "email", "address": "all@co.com"}) is None


def test_unknown_surface_refused():
    assert resolve_target(_cfg(), {"surface": "carrier-pigeon", "user_id": "x"}) is None


# --------------------------------------------------------------------------- #
# Registry-driven detection + deny-by-default (P4S-15)
# --------------------------------------------------------------------------- #
def test_new_briefs_empty_when_no_target(tmp_path):
    root = tmp_path / "root"
    _write_registry(root, [_brief_row()])
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}})  # no briefings block
    state = DeliveryState(tmp_path / "state.json")
    assert new_briefs(cfg, {"main": root}, state) == []


def test_new_briefs_detects_brief_for_configured_target(tmp_path):
    root = tmp_path / "root"
    _write_registry(root, [_brief_row(drop_id="STD-1")])
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}},
               targets=[{"surface": "telegram", "user_id": "12345"}])
    state = DeliveryState(tmp_path / "state.json")
    deliveries = new_briefs(cfg, {"main": root}, state)
    assert len(deliveries) == 1
    assert deliveries[0].drop_id == "STD-1"
    assert deliveries[0].surface == "telegram"
    assert deliveries[0].target == "12345"


def test_non_brief_registry_rows_ignored(tmp_path):
    root = tmp_path / "root"
    _write_registry(root, [
        {"drop_id": "X", "kind": "contradiction-digest", "sensitivity": "internal"},
        _brief_row(drop_id="STD-1"),
    ])
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}},
               targets=[{"surface": "telegram", "user_id": "12345"}])
    state = DeliveryState(tmp_path / "state.json")
    deliveries = new_briefs(cfg, {"main": root}, state)
    assert [d.drop_id for d in deliveries] == ["STD-1"]


# --------------------------------------------------------------------------- #
# Exactly-once across restarts (P4S-15)
# --------------------------------------------------------------------------- #
def test_delivers_exactly_once_then_nothing(tmp_path):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    _write_registry(root, [_brief_row(drop_id="STD-1")])
    _seed_brief_artifact(root)
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}},
               targets=[{"surface": "telegram", "user_id": "12345"}])
    statefile = tmp_path / "state.json"
    sender = _RecordingSender()

    state = DeliveryState(statefile)
    n1 = run_once(cfg, {"main": root}, {"telegram": sender}, state)
    assert n1 == 1
    assert len(sender.sent) == 1

    # Second pass, SAME process: nothing new.
    n2 = run_once(cfg, {"main": root}, {"telegram": sender}, state)
    assert n2 == 0
    assert len(sender.sent) == 1


def test_exactly_once_survives_restart(tmp_path):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    _write_registry(root, [_brief_row(drop_id="STD-1")])
    _seed_brief_artifact(root)
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}},
               targets=[{"surface": "telegram", "user_id": "12345"}])
    statefile = tmp_path / "state.json"
    sender = _RecordingSender()

    # First daemon lifetime.
    run_once(cfg, {"main": root}, {"telegram": sender}, DeliveryState(statefile))
    assert len(sender.sent) == 1

    # Fresh DeliveryState object (simulates a daemon restart): loads persisted
    # state, delivers nothing new.
    run_once(cfg, {"main": root}, {"telegram": sender}, DeliveryState(statefile))
    assert len(sender.sent) == 1, "restart must not re-deliver an already-sent brief"


# --------------------------------------------------------------------------- #
# Corruption => no send + doctor flag (fail closed; P4S-15)
# --------------------------------------------------------------------------- #
def test_corrupt_state_refuses_all_delivery(tmp_path):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    _write_registry(root, [_brief_row(drop_id="STD-1")])
    _seed_brief_artifact(root)
    statefile = tmp_path / "state.json"
    statefile.write_text("{ this is not valid json", encoding="utf-8")
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}},
               targets=[{"surface": "telegram", "user_id": "12345"}])
    sender = _RecordingSender()

    state = DeliveryState(statefile)
    n = run_once(cfg, {"main": root}, {"telegram": sender}, state)
    assert n == 0
    assert sender.sent == []
    assert state.corrupt is True, "corruption must set the doctor flag"


def test_corrupt_state_malformed_entry_fails_closed(tmp_path):
    statefile = tmp_path / "state.json"
    statefile.write_text(json.dumps({"delivered": [["a", "b"]]}), encoding="utf-8")
    state = DeliveryState(statefile)
    state.load()
    assert state.corrupt is True


# --------------------------------------------------------------------------- #
# Document-level ceiling re-check per surface (P4S-15 / SH-057)
# --------------------------------------------------------------------------- #
def test_above_ceiling_brief_withheld_entirely(tmp_path):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    # confidential brief; telegram surface is capped at internal => withhold.
    _write_registry(root, [_brief_row(drop_id="STD-1", sensitivity="confidential")])
    _seed_brief_artifact(root)
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}}, tg_max="internal",
               targets=[{"surface": "telegram", "user_id": "12345"}])
    sender = _RecordingSender()
    state = DeliveryState(tmp_path / "state.json")
    n = run_once(cfg, {"main": root}, {"telegram": sender}, state)
    assert n == 0
    assert sender.sent == [], "above-ceiling brief must be withheld entirely"


def test_email_push_is_public_capped(tmp_path):
    """A PUSH to email cannot verify DMARC, so it is public-capped (P4S-10/15)."""
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    # internal brief; email push surface is hard-capped at public => withhold.
    _write_registry(root, [_brief_row(drop_id="STD-1", sensitivity="internal")])
    _seed_brief_artifact(root)
    cfg = _cfg(em_allow={"ceo@co.com": {"instance": "main"}}, em_max="internal",
               targets=[{"surface": "email", "address": "ceo@co.com"}])
    sender = _RecordingSender()
    state = DeliveryState(tmp_path / "state.json")
    n = run_once(cfg, {"main": root}, {"email": sender}, state)
    assert n == 0, "email push is public-capped; internal brief withheld"


def test_public_brief_delivered_to_email(tmp_path):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    _write_registry(root, [_brief_row(drop_id="STD-1", sensitivity="public")])
    _seed_brief_artifact(root)
    cfg = _cfg(em_allow={"ceo@co.com": {"instance": "main"}},
               targets=[{"surface": "email", "address": "ceo@co.com"}])
    sender = _RecordingSender()
    state = DeliveryState(tmp_path / "state.json")
    n = run_once(cfg, {"main": root}, {"email": sender}, state)
    assert n == 1
    assert sender.sent[0][0] == "ceo@co.com"


# --------------------------------------------------------------------------- #
# Pinned briefing_delivery ledger row (metadata only; P4S-15)
# --------------------------------------------------------------------------- #
def test_delivery_ledger_row_pinned(tmp_path):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    _write_registry(root, [_brief_row(drop_id="STD-7", sensitivity="public")])
    _seed_brief_artifact(root)
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}}, tg_max="internal",
               targets=[{"surface": "telegram", "user_id": "12345"}])
    sender = _RecordingSender()
    state = DeliveryState(tmp_path / "state.json")
    run_once(cfg, {"main": root}, {"telegram": sender}, state)

    ledger = root / "Meta.nosync" / "ledgers" / "gateway_event.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    delivery_rows = [r for r in rows if r.get("kind") == "briefing_delivery"]
    assert len(delivery_rows) == 1
    row = delivery_rows[0]
    assert set(row) == {"kind", "surface", "target", "drop_id", "sensitivity", "ts"}
    assert row["surface"] == "telegram"
    assert row["target"] == "12345"
    assert row["drop_id"] == "STD-7"
    assert row["sensitivity"] == "public"


# --------------------------------------------------------------------------- #
# deliver() returns False when no sender for the surface
# --------------------------------------------------------------------------- #
def test_deliver_no_sender_returns_false(tmp_path):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    _write_registry(root, [_brief_row(drop_id="STD-1", sensitivity="public")])
    _seed_brief_artifact(root)
    cfg = _cfg(tg_allow={"12345": {"instance": "main"}},
               targets=[{"surface": "telegram", "user_id": "12345"}])
    state = DeliveryState(tmp_path / "state.json")
    deliveries = new_briefs(cfg, {"main": root}, state)
    assert deliveries
    # No sender registered for telegram => refused, not crashed.
    assert deliver(cfg, deliveries[0], {}, state) is False
