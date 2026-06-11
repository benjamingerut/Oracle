"""Tests for the operating-agent curator (P5-T7b).

The curator works the kernel Review Inbox from the LOCAL attended surface through
a FIXED kind->verb mapping (value slots only) -- it NEVER executes a queue item's
free-text ``action`` string. These tests prove the four acceptance properties:

  * a queue item is worked end-to-end with ledger attribution naming the curator;
  * a queue item whose ``action`` smuggles a command is NEVER executed (the
    mapping ignores action text; value slots come from STRUCTURED data only);
  * a control-plane action attempted via the curator path is DENIED;
  * with autonomy below the gate, apply is refused while prepare still works.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from oracle_agent import curator


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _set_autonomy(root: Path, *, enabled: bool, level: int) -> None:
    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "autonomy.yml").write_text(
        f"enabled: {'true' if enabled else 'false'}\n"
        f"level: {level}\n"
        "allowed_loops:\n"
        "writable_lanes:\n"
        "readonly_connectors:\n"
        "blast_radius_caps:\n"
        "  max_files_per_run: 50\n"
        "  max_bytes: 10000000\n"
        'kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"\n'
        "dream:\n"
        "  command:\n"
        "  max_minutes: 30\n"
        "  max_inbox_items: 10\n",
        encoding="utf-8",
    )


def _curator_rows(root: Path) -> list[dict]:
    p = root / "Meta.nosync" / "ledgers" / "curator_event.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# principal (P5S-11: local_user:<id>)
# --------------------------------------------------------------------------- #
def test_local_principal_uses_local_user_form(spawned_root):
    p = curator.local_principal(spawned_root)
    assert p.actor.startswith("local_user:")
    assert p.role == "admin"


# --------------------------------------------------------------------------- #
# the fixed kind->verb mapping (value slots only)
# --------------------------------------------------------------------------- #
def test_control_plane_kinds_are_never_applyable(spawned_root):
    """Truth/authority/autonomy/contradiction kinds are admin-interface-only."""
    p = curator.local_principal(spawned_root)
    for kind in ("contradiction", "promotable-row", "authority-candidate", "autonomy"):
        item = {"kind": kind, "title": "x", "action": "; rm -rf / #", "path": "p"}
        plan = curator.plan_item(spawned_root, item, due_ids=[])
        assert plan.disposition == curator.CONTROL_PLANE
        assert plan.argvs == []
        res = curator.apply_plan(spawned_root, plan, p, autonomy_level=3)
        assert res.applied is False
        assert res.status == "control-plane"


def test_action_text_is_never_executed(spawned_root):
    """The pinned argv comes from STRUCTURED data (due ids), never item text."""
    item = {
        "kind": "unconsumed-events",
        "title": "loop $(rm -rf /) ; echo pwned",
        "action": "rm -rf / --no-preserve-root",
        "detail": "feedback_event",
    }
    plan = curator.plan_item(spawned_root, item, due_ids=["memory-matriculation"])
    assert plan.disposition == "apply"
    assert plan.argvs  # at least one pinned argv
    for argv in plan.argvs:
        joined = " ".join(argv)
        assert "rm" not in joined
        assert "pwned" not in joined
        # the subcommand is pinned; the slot is the STRUCTURED due loop id.
        assert argv[0] == "loops" and argv[1] == "run"
        assert argv[2] == "memory-matriculation"


def test_unmapped_kind_is_default_denied(spawned_root):
    plan = curator.plan_item(spawned_root, {"kind": "brand-new", "title": "t",
                                            "action": "x"}, due_ids=["memory-matriculation"])
    assert plan.disposition == "unmapped"
    p = curator.local_principal(spawned_root)
    res = curator.apply_plan(spawned_root, plan, p, autonomy_level=3)
    assert res.applied is False
    assert res.status == "unmapped"


# --------------------------------------------------------------------------- #
# autonomy gate: prepare always works; apply refused below the gate
# --------------------------------------------------------------------------- #
def test_apply_refused_below_autonomy_gate(spawned_root):
    """Below CURATOR_MIN_LEVEL apply is refused; prepare still produces the plan."""
    p = curator.local_principal(spawned_root)
    item = {"kind": "unconsumed-events", "title": "t", "action": "x"}
    plan = curator.plan_item(spawned_root, item, due_ids=["memory-matriculation"])
    assert plan.disposition == "apply"  # prepare works regardless of autonomy
    res = curator.apply_plan(spawned_root, plan, p, autonomy_level=0)
    assert res.applied is False
    assert res.status == "refused-autonomy"
    # the refusal is ledgered with the resolving principal.
    rows = _curator_rows(spawned_root)
    assert rows and rows[-1]["actor"] == p.actor
    assert rows[-1]["result"] == "refused"


# --------------------------------------------------------------------------- #
# end-to-end apply with ledger attribution
# --------------------------------------------------------------------------- #
def test_item_worked_end_to_end_with_attribution(tmp_path):
    """A loop-backed item is worked end-to-end via the pinned verb + ledgered."""
    from oracle_agent.testkit import spawn_test_root
    root = tmp_path / "root"
    try:
        spawn_test_root(root, name="Curate E2E")
    except RuntimeError as exc:
        pytest.skip(f"spawn failed: {exc}")
    _set_autonomy(root, enabled=True, level=1)  # level-1 admits loop runs

    p = curator.local_principal(root)
    due = curator._due_ids(root)
    assert due, "fresh spawn should have due deterministic loops"
    lid = due[0]
    item = {"kind": "aged-signal", "title": "unconsumed signal", "action": "ignored"}
    plan = curator.plan_item(root, item, due_ids=[lid])
    assert plan.disposition == "apply"
    # keep just the chosen loop's argv for a deterministic assertion.
    plan.argvs = [a for a in plan.argvs if a[2] == lid]

    res = curator.apply_plan(root, plan, p, autonomy_level=1)
    assert res.applied is True
    assert res.status == "applied"

    rows = _curator_rows(root)
    applied = [r for r in rows if r["result"] == "applied"]
    assert applied, "an applied curator_event row should be written"
    last = applied[-1]
    assert last["actor"] == p.actor
    assert last["role"] == "admin"
    assert last["item_kind"] == "aged-signal"
    assert last["verb"].startswith("loops run")
    # the ledgered verb is the PINNED verb, never the item action text.
    assert "ignored" not in last["verb"]


# --------------------------------------------------------------------------- #
# the attended driver (oracle curate body)
# --------------------------------------------------------------------------- #
def test_curate_prepare_only_never_applies(spawned_root, monkeypatch):
    """--prepare-only lists dispositions and never runs a verb."""
    ran = {"called": False}

    def _spy(root, argv, *a, **k):
        ran["called"] = True
        return 0, "{}", ""

    monkeypatch.setattr(curator, "run_verb", _spy)
    # list_queue + _due_ids go through run_verb too; feed them via the spy is
    # awkward, so stub the queue/due directly.
    monkeypatch.setattr(curator, "list_queue",
                        lambda root, limit=0: [{"kind": "unconsumed-events",
                                                "title": "t", "action": "x"}])
    monkeypatch.setattr(curator, "_due_ids", lambda root: ["memory-matriculation"])

    out = io.StringIO()
    rc = curator.curate(spawned_root, stream_out=out, apply=False)
    assert rc == 0
    assert ran["called"] is False  # prepare-only never applied
    assert "unconsumed-events" in out.getvalue()


def test_curate_declined_confirmation_skips_apply(spawned_root, monkeypatch):
    """Answering 'N' at the confirmation prompt skips the apply."""
    applied = {"n": 0}

    def _apply_spy(root, plan, principal, **k):
        applied["n"] += 1
        return curator.ApplyResult(True, "applied")

    monkeypatch.setattr(curator, "apply_plan", _apply_spy)
    monkeypatch.setattr(curator, "list_queue",
                        lambda root, limit=0: [{"kind": "unconsumed-events",
                                                "title": "t", "action": "x"}])
    monkeypatch.setattr(curator, "_due_ids", lambda root: ["memory-matriculation"])
    _set_autonomy(spawned_root, enabled=True, level=1)

    out = io.StringIO()
    rc = curator.curate(spawned_root, stream_in=io.StringIO("n\n"),
                        stream_out=out, apply=True)
    assert rc == 0
    assert applied["n"] == 0  # declined -> never applied
    # restore spawned_root autonomy posture (session-scoped fixture).
    _set_autonomy(spawned_root, enabled=False, level=0)
