#!/usr/bin/env python3
"""Tests for the autonomy chokepoint (actions.py) and headless harness (harness.py).

These prove the four safety invariants the manifest names for this unit:

  * ``enabled: false`` (the default a fresh spawn ships) DENIES every action.
  * the KILL-SWITCH, when present, DENIES even when autonomy is otherwise
    enabled -- it is checked FIRST, before the allowlist.
  * an allowlisted action within the blast-radius caps is PERMITTED and logs
    BOTH the ``intended`` and the ``actual`` action_event phases.
  * an action over the blast-radius caps is DENIED.

Plus the harness contract: it computes due loops and runs each ONLY through the
autonomy gate, so with autonomy off it performs ZERO side effects regardless of
how many loops are due.

Each test builds its own minimal oracle (via the shared ``minimal_oracle``
fixture) and writes a block-style ``autonomy.yml`` inline. The harness tests
tolerate an unavailable ``loops.py`` module.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import actions  # noqa: E402  (conftest puts _tools on sys.path)
import harness  # noqa: E402
import ledger  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers: write a block-style autonomy.yml + kill switch
# --------------------------------------------------------------------------- #
def _autonomy_dir(root: Path) -> Path:
    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_autonomy(
    root: Path,
    *,
    enabled: bool,
    allowed_loops=None,
    writable_lanes=None,
    readonly_connectors=None,
    max_files_per_run: int = 0,
    max_bytes: int = 0,
    kill_switch_file: str = "Meta.nosync/Autonomy/KILL-SWITCH",
) -> Path:
    """Author a BLOCK-STYLE autonomy.yml (the only style the oracle_yaml loader
    accepts). Empty lists are written as a bare ``key:`` (parses to None), never
    as ``[]``.
    """
    allowed_loops = allowed_loops or []
    writable_lanes = writable_lanes or []
    readonly_connectors = readonly_connectors or []

    def _block(key: str, items) -> str:
        if not items:
            return f"{key}:\n"
        lines = "\n".join(f"  - {it}" for it in items)
        return f"{key}:\n{lines}\n"

    text = (
        f"enabled: {'true' if enabled else 'false'}\n"
        + _block("allowed_loops", allowed_loops)
        + _block("writable_lanes", writable_lanes)
        + _block("readonly_connectors", readonly_connectors)
        + "blast_radius_caps:\n"
        + f"  max_files_per_run: {int(max_files_per_run)}\n"
        + f"  max_bytes: {int(max_bytes)}\n"
        + f'kill_switch_file: "{kill_switch_file}"\n'
    )
    d = _autonomy_dir(root)
    (d / "autonomy.yml").write_text(text, encoding="utf-8")
    return d / "autonomy.yml"


def _engage_kill_switch(root: Path) -> Path:
    d = _autonomy_dir(root)
    ks = d / "KILL-SWITCH"
    ks.write_text("halt\n", encoding="utf-8")
    return ks


def _action_ledger_rows(root: Path):
    path = root / "Meta.nosync" / "ledgers" / "action_event.jsonl"
    rows, _ = ledger.load(path)
    return rows


# --------------------------------------------------------------------------- #
# autonomy.yml parsing + default-off
# --------------------------------------------------------------------------- #
def test_autonomy_yaml_is_parseable_block_style(tmp_path, minimal_oracle):
    """The inline autonomy.yml round-trips through the safe-subset loader."""
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["connector-health"],
        writable_lanes=["01_Finance"],
        readonly_connectors=["localfolder"],
        max_files_per_run=5,
        max_bytes=1024,
    )
    a = actions.Autonomy.load(root)
    assert a.enabled is True
    assert a.allowed_loops == ["connector-health"]
    assert a.writable_lanes == ["01_Finance"]
    assert a.readonly_connectors == ["localfolder"]
    assert a.max_files_per_run == 5
    assert a.max_bytes == 1024
    assert a.kill_switch_file == "Meta.nosync/Autonomy/KILL-SWITCH"


def test_missing_config_is_off(tmp_path, minimal_oracle):
    """No autonomy.yml at all => autonomy OFF, empty allowlists."""
    root = minimal_oracle(tmp_path)
    a = actions.Autonomy.load(root)
    assert a.enabled is False
    assert a.allowed_loops == []
    assert a.source == "missing-config"


def test_empty_lists_parse_as_empty(tmp_path, minimal_oracle):
    """A bare 'key:' (empty block list) parses to an empty Python list."""
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=False)
    a = actions.Autonomy.load(root)
    assert a.allowed_loops == []
    assert a.writable_lanes == []
    assert a.readonly_connectors == []
    assert a.max_files_per_run == 0


def test_unparseable_config_fails_closed(tmp_path, minimal_oracle):
    """A malformed autonomy.yml must NEVER enable autonomy (fail closed)."""
    root = minimal_oracle(tmp_path)
    d = _autonomy_dir(root)
    # Flow-style mapping is outside the safe subset -> loader raises -> OFF.
    (d / "autonomy.yml").write_text("enabled: {true}\n", encoding="utf-8")
    a = actions.Autonomy.load(root)
    assert a.enabled is False
    assert a.source == "unparseable-config"


# --------------------------------------------------------------------------- #
# INVARIANT 1: enabled:false denies everything
# --------------------------------------------------------------------------- #
def test_disabled_denies_action(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=False, allowed_loops=["lp"], max_files_per_run=99)
    decision = actions.authorize("do-thing", {"loop": "lp", "files": 1}, root=root)
    assert decision["result"] == "deny"
    assert "autonomy-disabled" in decision["reason"]


def test_disabled_guard_raises_and_logs_intended_only(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=False)
    with pytest.raises(actions.ActionDenied):
        actions.guard("do-thing", {"loop": "lp"}, root=root)
    rows = _action_ledger_rows(root)
    assert len(rows) == 1
    assert rows[0]["phase"] == "intended"
    assert rows[0]["result"] == "deny"


# --------------------------------------------------------------------------- #
# INVARIANT 2: kill-switch denies even when enabled (checked FIRST)
# --------------------------------------------------------------------------- #
def test_kill_switch_denies_even_when_enabled(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["lp"],
        writable_lanes=["01_Finance"],
        max_files_per_run=100,
        max_bytes=10_000,
    )
    _engage_kill_switch(root)
    assert actions.kill_switch_engaged(root) is True
    decision = actions.authorize(
        "do-thing", {"loop": "lp", "lanes": ["01_Finance"], "files": 1}, root=root
    )
    assert decision["result"] == "deny"
    assert decision["reason"] == "kill-switch-engaged"


def test_kill_switch_is_checked_before_allowlist(tmp_path, minimal_oracle):
    """Even a scope that would FAIL the allowlist still reports the kill-switch
    reason first -- proving kill-switch precedence."""
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=True, allowed_loops=["only-this"], max_files_per_run=1)
    _engage_kill_switch(root)
    decision = actions.authorize(
        "do-thing", {"loop": "NOT-ALLOWLISTED", "files": 9999}, root=root
    )
    assert decision["result"] == "deny"
    assert decision["reason"] == "kill-switch-engaged"


def test_removing_kill_switch_resumes(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root, enabled=True, allowed_loops=["lp"], max_files_per_run=5, max_bytes=999
    )
    ks = _engage_kill_switch(root)
    assert actions.authorize("x", {"loop": "lp"}, root=root)["result"] == "deny"
    ks.unlink()
    assert actions.authorize("x", {"loop": "lp", "files": 1}, root=root)["result"] == "grant"


# --------------------------------------------------------------------------- #
# INVARIANT 3: allowlisted within caps is permitted + logs intended AND actual
# --------------------------------------------------------------------------- #
def test_allowed_within_caps_is_permitted(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["connector-health"],
        writable_lanes=["01_Finance"],
        readonly_connectors=["localfolder"],
        max_files_per_run=5,
        max_bytes=4096,
    )
    decision = actions.authorize(
        "pull",
        {
            "loop": "connector-health",
            "lanes": ["01_Finance"],
            "connectors": ["localfolder"],
            "files": 3,
            "bytes": 2048,
        },
        root=root,
    )
    assert decision["result"] == "grant"
    assert decision["reason"] == "allowlisted-within-caps"


def test_with_action_logs_intended_and_actual(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["lp"],
        writable_lanes=["01_Finance"],
        max_files_per_run=10,
        max_bytes=10_000,
    )
    ran = {"flag": False}
    with actions.with_action(
        "emit", {"loop": "lp", "lanes": ["01_Finance"], "files": 2, "bytes": 100},
        root=root,
    ):
        ran["flag"] = True
    assert ran["flag"] is True

    rows = _action_ledger_rows(root)
    phases = [r["phase"] for r in rows]
    assert "intended" in phases
    assert "actual" in phases
    intended = [r for r in rows if r["phase"] == "intended"][0]
    actual = [r for r in rows if r["phase"] == "actual"][0]
    assert intended["result"] == "grant"
    assert actual["result"] == "ok"
    # the ledger carries the contracted action_event shape
    for r in rows:
        assert set(("drop_id", "ts", "action", "scope", "phase", "caps", "result")).issubset(r)


def test_with_action_logs_failure_phase(tmp_path, minimal_oracle):
    """If the gated body raises, the actual-phase row records result 'fail'."""
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=True, allowed_loops=["lp"], max_files_per_run=5)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with actions.with_action("emit", {"loop": "lp", "files": 1}, root=root):
            raise Boom("explode")

    rows = _action_ledger_rows(root)
    actual = [r for r in rows if r["phase"] == "actual"]
    assert actual and actual[0]["result"] == "fail"
    assert "Boom" in actual[0]["reason"]


def test_with_action_denied_body_never_runs(tmp_path, minimal_oracle):
    """A denied gate raises BEFORE the body, so no side effect occurs."""
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=False)
    body_ran = {"flag": False}
    with pytest.raises(actions.ActionDenied):
        with actions.with_action("emit", {"loop": "lp"}, root=root):
            body_ran["flag"] = True  # must never execute
    assert body_ran["flag"] is False
    # only the intended (deny) row exists -- no actual phase.
    rows = _action_ledger_rows(root)
    assert all(r["phase"] == "intended" for r in rows)


# --------------------------------------------------------------------------- #
# INVARIANT 4: over-cap is denied
# --------------------------------------------------------------------------- #
def test_over_file_cap_denied(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root, enabled=True, allowed_loops=["lp"], max_files_per_run=2, max_bytes=10_000
    )
    decision = actions.authorize("x", {"loop": "lp", "files": 3}, root=root)
    assert decision["result"] == "deny"
    assert "max_files_per_run" in decision["reason"]


def test_over_byte_cap_denied(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root, enabled=True, allowed_loops=["lp"], max_files_per_run=10, max_bytes=512
    )
    decision = actions.authorize(
        "x", {"loop": "lp", "files": 1, "bytes": 4096}, root=root
    )
    assert decision["result"] == "deny"
    assert "max_bytes" in decision["reason"]


def test_non_allowlisted_loop_denied(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=True, allowed_loops=["allowed"], max_files_per_run=99)
    decision = actions.authorize("x", {"loop": "other", "files": 1}, root=root)
    assert decision["result"] == "deny"
    assert "not in allowed_loops" in decision["reason"]


def test_non_allowlisted_lane_denied(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["lp"],
        writable_lanes=["01_Finance"],
        max_files_per_run=99,
        max_bytes=99_999,
    )
    decision = actions.authorize(
        "x", {"loop": "lp", "lanes": ["04_Operations"], "files": 1}, root=root
    )
    assert decision["result"] == "deny"
    assert "not in writable_lanes" in decision["reason"]


def test_non_allowlisted_connector_denied(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["lp"],
        readonly_connectors=["localfolder"],
        max_files_per_run=99,
        max_bytes=99_999,
    )
    decision = actions.authorize(
        "x", {"loop": "lp", "connectors": ["secret_api"], "files": 1}, root=root
    )
    assert decision["result"] == "deny"
    assert "not in readonly_connectors" in decision["reason"]


# --------------------------------------------------------------------------- #
# status / inspection
# --------------------------------------------------------------------------- #
def test_status_reports_posture(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root, enabled=True, allowed_loops=["lp"], max_files_per_run=3, max_bytes=10
    )
    st = actions.status(root)
    assert st["enabled"] is True
    assert st["kill_switch_engaged"] is False
    assert st["allowed_loops"] == ["lp"]
    assert st["blast_radius_caps"]["max_files_per_run"] == 3


# --------------------------------------------------------------------------- #
# HARNESS: headless pass runs loops ONLY through the gate
# --------------------------------------------------------------------------- #
def test_harness_kill_switch_short_circuits(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=True, allowed_loops=["lp"], max_files_per_run=99)
    _engage_kill_switch(root)
    report = harness.run_once(root)
    assert report["kill_switch_engaged"] is True
    assert report["outcomes"] == []


def test_harness_autonomy_off_runs_no_side_effects(tmp_path, minimal_oracle):
    """With autonomy off, even a due loop is BLOCKED at the gate -- the harness
    performs no side effects. We inject a fake loops module so the test does not
    depend on the real loops.py."""
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=False)

    fake = _FakeLoops(
        records=[{"id": "lp-1", "runner": "agent-worklist"}],
        due_ids=["lp-1"],
    )
    report = _run_harness_with_fake(root, fake)
    assert report["due"] == ["lp-1"]
    # gate denied => blocked, never ran => the fake's runner was not invoked
    assert fake.run_calls == []
    blocked = [o for o in report["outcomes"] if o["status"] == "blocked"]
    assert blocked and blocked[0]["verdict"] == "deny"


def test_harness_enabled_allowlisted_loop_runs(tmp_path, minimal_oracle):
    """With autonomy on AND the loop allowlisted within caps, the harness
    dispatches the runner through the gate and records intended+actual."""
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["lp-1"],
        max_files_per_run=5,
        max_bytes=10_000,
    )
    fake = _FakeLoops(
        records=[{"id": "lp-1", "runner": "loops:run"}],
        due_ids=["lp-1"],
    )
    report = _run_harness_with_fake(root, fake)
    ran = [o for o in report["outcomes"] if o["ran"]]
    assert ran and ran[0]["loop_id"] == "lp-1"
    assert fake.run_calls == ["lp-1"]
    # the gate logged both phases for the granted run
    rows = _action_ledger_rows(root)
    phases = {r["phase"] for r in rows if r["action"] == "loop:lp-1"}
    assert "intended" in phases and "actual" in phases


def test_harness_dry_run_does_not_dispatch(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_autonomy(
        root, enabled=True, allowed_loops=["lp-1"], max_files_per_run=5, max_bytes=10
    )
    fake = _FakeLoops(
        records=[{"id": "lp-1", "runner": "loops:run"}], due_ids=["lp-1"]
    )
    report = _run_harness_with_fake(root, fake, dry_run=True)
    assert report["dry_run"] is True
    assert report["model_policy"]["version"] == "test-loop-model-policy"
    assert fake.run_calls == []  # dry-run never dispatches
    assert report["outcomes"][0]["status"] == "dry-run"
    assert report["outcomes"][0]["verdict"] == "grant"
    # dry-run logs NOTHING to the action ledger
    assert _action_ledger_rows(root) == []


def test_harness_missing_loops_module_is_clean(tmp_path, minimal_oracle, monkeypatch):
    """If the loops module cannot be imported, the harness no-ops cleanly."""
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=True, allowed_loops=["lp"], max_files_per_run=5)
    monkeypatch.setattr(harness, "_import_loops", lambda: None)
    report = harness.run_once(root)
    assert report["due"] == []
    assert report["outcomes"] == []
    assert "loops module unavailable" in report.get("reason", "")


# --------------------------------------------------------------------------- #
# test doubles + harness injection helper
# --------------------------------------------------------------------------- #
class _FakeLoops:
    """A minimal stand-in for the loops module the harness lazily imports.

    Implements just the accessors the harness probes: ``load_loops`` (records),
    ``compute_due`` (the due worklist), ``run`` (dispatch), and ``record``.
    Records every ``run`` call so a test can assert the runner was (or was not)
    invoked.
    """

    def __init__(self, records, due_ids):
        self._records = records
        self._due_ids = set(due_ids)
        self.run_calls: list[str] = []
        self.record_calls: list[str] = []

    def list_loops(self, root):  # mirrors the real loops.list_loops(root)
        return list(self._records)

    def compute_due(self, loops, now=None):
        return [r for r in loops if r.get("id") in self._due_ids]

    def loop_model_policy(self):
        return {"version": "test-loop-model-policy"}

    def run(self, root, loop_id, *, now=None, headless=False, gate=True):
        # Mirrors the real loops.run(root, loop_id, *, headless, gate). The
        # harness invokes it with gate=False (it has already gated via
        # with_action), so the fake never re-gates -- it just records the call.
        self.run_calls.append(loop_id)
        return {"status": "ok", "loop_id": loop_id}

    def record(self, root, loop_id, status, *, now=None, health_signal=None,
               notes=None, next_review=None):
        self.record_calls.append(loop_id)
        return {"loop_id": loop_id, "status": status}


def _run_harness_with_fake(root, fake, *, dry_run=False):
    """Run one harness pass with ``_import_loops`` patched to return ``fake``.

    Uses a tiny manual patch (no monkeypatch fixture dependency) so the helper
    is reusable from any test.
    """
    original = harness._import_loops
    harness._import_loops = lambda: fake
    try:
        return harness.run_once(root, dry_run=dry_run)
    finally:
        harness._import_loops = original


# --------------------------------------------------------------------------- #
# CLI smoke
# --------------------------------------------------------------------------- #
def test_actions_cli_status(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=False)
    rc = actions.main(["--root", str(root), "status"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is False


def test_actions_cli_authorize_denies_when_off(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=False)
    rc = actions.main(
        ["--root", str(root), "authorize", "--action", "x", "--loop", "lp"]
    )
    assert rc == 2  # non-zero on deny
    out = json.loads(capsys.readouterr().out)
    assert out["result"] == "deny"


def test_actions_cli_kill_reports_engaged(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _write_autonomy(root, enabled=True, allowed_loops=["lp"], max_files_per_run=1)
    _engage_kill_switch(root)
    rc = actions.main(["--root", str(root), "kill"])
    assert rc == 0  # engaged -> exit 0 per CLI contract
    assert "ENGAGED" in capsys.readouterr().out


def test_harness_cli_requires_oracle_yml(tmp_path, capsys):
    empty = tmp_path / "not_an_oracle"
    empty.mkdir()
    rc = harness.main(["--root", str(empty), "--once"])
    assert rc == 2
    assert "no oracle.yml" in capsys.readouterr().err
