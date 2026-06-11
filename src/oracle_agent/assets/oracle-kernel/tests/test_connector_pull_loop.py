#!/usr/bin/env python3
"""Tests for the builtin ``connector-pull`` loop (P7-T9).

Load-bearing guarantees exercised here (per the Phase-7 spec + its stress
table):

  * The scheduled pull runs ONLY through the autonomy gate as the canonical
    ``connector-pull`` loop id, which is NEVER a level-1 deterministic loop
    (P7S-20): an explicit ``allowed_loops`` entry is required.
  * Per-path autonomy-OFF behavior (P7S-21, A5-consistent): a DIRECT
    ``harness.run_once`` pass with autonomy OFF logs intended/denied action
    events and performs ZERO network calls and ZERO bytes (authorize-before-
    probe, P7S-18) -- the loop's connector pull never runs. (The shell-scheduler
    skip path lives in tests/shell/test_scheduler.py.)
  * With the connector + ``connector-pull`` allowlisted within caps, a
    pull+ingest runs: every landed file appears as a source record with
    connector provenance and the manifest sensitivity floor is honored into
    ingest-time re-classification (P7S-16/P7S-19).
  * An ``skipped_out_of_scope`` row is an EXPECTED outcome -- never a
    failure_event, never a demotion signal (P7S-12).
  * The connector cadence grammar (hourly|daily|weekly|<N>h|<N>d, default daily)
    parses exactly and falls back to daily on anything else (P7S-24).

Self-contained: a toy RemoteConnector subclass is registered into the runtime
registry; no real network. Depends on loops.py + connectors + harness +
ingest_pipeline + actions + the floor + the shared ``minimal_oracle`` fixture.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import actions  # noqa: E402
import connectors  # noqa: E402
import harness  # noqa: E402
import loops  # noqa: E402
from connectors import base as connbase  # noqa: E402
from connectors.remote import RemoteConnector, RemoteItem, load_cursor  # noqa: E402


# --------------------------------------------------------------------------- #
# a toy RemoteConnector + helpers (no network)
# --------------------------------------------------------------------------- #
class ToyConnector(RemoteConnector):
    access_mode = "api"
    scope_allowlist_keys = ("folder_ids",)
    download_host_suffixes = ("toy.example.com",)

    def __init__(self, manifest, items=None, bodies=None):
        super().__init__(manifest)
        self._items = items or []
        self._bodies = bodies or {}

    def list_items(self, ctx):
        for it in self._items:
            yield it

    def fetch_item(self, ctx, item):
        import tempfile

        stage_dir = Path(tempfile.mkdtemp(prefix="oracle-toy-"))
        stage = stage_dir / "body"
        body = self._bodies.get(item.item_id, b"plain unmarked business content")
        fd = os.open(str(stage), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:  # safe_paths-internal: private temp stage
            f.write(body)
        return stage


@pytest.fixture(autouse=True)
def _restore_registry():
    """Snapshot + restore the connector registry around each test so a toy
    registration never leaks into the next test."""
    reg = dict(connectors.REGISTRY)
    sysf = dict(connectors.SYSTEM_FACTORIES)
    yield
    connectors.REGISTRY.clear()
    connectors.REGISTRY.update(reg)
    connectors.SYSTEM_FACTORIES.clear()
    connectors.SYSTEM_FACTORIES.update(sysf)


def _write_manifest(root: Path, *, cid="toy", folder_ids=("FID1",),
                    default_sensitivity="internal", cadence=None, max_files=5) -> dict:
    mdir = root / "Connectors" / cid
    mdir.mkdir(parents=True, exist_ok=True)
    fid_block = "  folder_ids:\n" + "".join(f"    - {f}\n" for f in folder_ids)
    cad_line = f"  cadence: {cadence}\n" if cadence else ""
    mf_line = f"  max_files: {max_files}\n" if max_files else ""
    text = f"""\
id: {cid}
system: {cid}
status: active
access_mode: api
locality: external_only
capture_tier: snapshot
auth:
  method: oauth
  vars:
    - TOY_TOKEN
permissions: read_only
freshness:
  class: api
  last_verified: "2026-01-01"
  expected_decay_days: 7
source:
{fid_block}{mf_line}{cad_line}  default_sensitivity: {default_sensitivity}
"""
    (mdir / f"{cid}.manifest.yaml").write_text(text, encoding="utf-8")
    return connbase.load_manifest(root, cid)


def _register_toy(cid="toy", items=None, bodies=None):
    connectors.register(cid, lambda m: ToyConnector(m, items=items, bodies=bodies), system=cid)


def _write_autonomy(root: Path, *, enabled=True, allowed_loops=None,
                    writable_lanes=None, connectors_list=None,
                    max_files_per_run=10, max_bytes=1_000_000) -> None:
    allowed_loops = allowed_loops or []
    writable_lanes = writable_lanes or []
    connectors_list = connectors_list or []

    def _block(key, items):
        if not items:
            return f"{key}:\n"
        return f"{key}:\n" + "".join(f"  - {i}\n" for i in items)

    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    text = (
        f"enabled: {'true' if enabled else 'false'}\n"
        "level: 0\n"
        + _block("allowed_loops", allowed_loops)
        + _block("writable_lanes", writable_lanes)
        + _block("readonly_connectors", connectors_list)
        + "blast_radius_caps:\n"
        + f"  max_files_per_run: {max_files_per_run}\n"
        + f"  max_bytes: {max_bytes}\n"
        + 'kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"\n'
    )
    (d / "autonomy.yml").write_text(text, encoding="utf-8")


def _write_pull_loop(root: Path, *, cadence="daily", last_run=None) -> None:
    d = root / "Meta.nosync" / "Loops"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "id: connector-pull",
        "type: loop",
        "title: Connector pull",
        "created: 2026-01-01",
        "updated: 2026-01-01",
        "sensitivity: internal",
        "status: active",
        "tags:",
        "  - meta",
        "  - loop",
        f"cadence: {cadence}",
        "runner: builtin:connector-pull",
        f"last_run: {last_run if last_run else 'null'}",
        f"next_review: {last_run if last_run else 'null'}",
        "trigger_conditions:",
        "  - manifest-due connectors exist",
        "---",
        "",
        "# Connector pull",
        "",
    ]
    (d / "loop-connector-pull.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _landed(root: Path, cid: str) -> list[Path]:
    d = root / "Workproduct.nosync" / "_INPUT" / cid
    if not d.exists():
        return []
    return [p for p in d.rglob("*") if p.is_file()]


def _action_event_count(root: Path) -> int:
    p = root / "Meta.nosync" / "ledgers" / "action_event.jsonl"
    if not p.exists():
        return 0
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


# --------------------------------------------------------------------------- #
# cadence grammar (P7S-24)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cadence,expected",
    [
        ("hourly", timedelta(hours=1)),
        ("daily", timedelta(days=1)),
        ("weekly", timedelta(days=7)),
        ("6h", timedelta(hours=6)),
        ("3d", timedelta(days=3)),
        ("12h", timedelta(hours=12)),
        (None, timedelta(days=1)),          # default daily
        ("", timedelta(days=1)),            # default daily
        ("fortnightly", timedelta(days=1)),  # outside grammar -> daily
        ("0h", timedelta(days=1)),           # zero count -> daily (never "never")
        ("garbage", timedelta(days=1)),
    ],
)
def test_connector_cadence_grammar(cadence, expected):
    assert loops.parse_connector_cadence(cadence) == expected


# --------------------------------------------------------------------------- #
# connector-pull is never a level-1 deterministic loop (P7S-20)
# --------------------------------------------------------------------------- #
def test_connector_pull_not_deterministic():
    assert "connector-pull" not in actions.DETERMINISTIC_LOOPS
    # level 1 alone (no explicit allowlist entry) must NOT admit it.
    auto = actions.Autonomy(enabled=True, level=1)
    assert "connector-pull" not in auto.effective_allowed_loops()


# --------------------------------------------------------------------------- #
# DIRECT harness path, autonomy OFF: deny rows, zero network, zero bytes (P7S-21)
# --------------------------------------------------------------------------- #
def test_direct_harness_autonomy_off_zero_network(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    (root / "oracle.yml").exists()  # minimal_oracle ships oracle.yml

    touched = {"listed": False, "fetched": False}

    class _Spy(ToyConnector):
        def list_items(self, ctx):
            touched["listed"] = True
            return iter([])

        def fetch_item(self, ctx, item):  # pragma: no cover - must never run
            touched["fetched"] = True
            raise AssertionError("must not fetch when autonomy is OFF")

    connectors.register("toy", lambda m: _Spy(m, items=[RemoteItem("A", "a.txt", "2026-06-01", -1, {})]), system="toy")
    _write_manifest(root)
    _write_pull_loop(root)
    _write_autonomy(root, enabled=False)  # OFF

    report = harness.run_once(root, now=datetime(2026, 6, 10))
    assert report["autonomy_enabled"] is False
    assert "connector-pull" in report["due"]
    outcome = next(o for o in report["outcomes"] if o["loop_id"] == "connector-pull")
    # The loop run is BLOCKED at the gate -- the connector pull never runs.
    assert outcome["status"] == "blocked"
    assert outcome["verdict"] == actions.RESULT_DENY
    # Zero network: neither list_items nor fetch_item was reached.
    assert touched["listed"] is False
    assert touched["fetched"] is False
    # Zero bytes landed.
    assert _landed(root, "toy") == []
    # The deny WAS logged as an action_event (intended/deny row exists).
    assert _action_event_count(root) >= 1


# --------------------------------------------------------------------------- #
# allowlisted within caps: pull + ingest, provenance, floor honored
# --------------------------------------------------------------------------- #
def test_allowlisted_pull_ingests_with_provenance(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    items = [RemoteItem("A", "a.txt", "2026-06-01", -1, {})]
    bodies = {"A": b"ordinary quarterly business content"}
    connectors.register("toy", lambda m: ToyConnector(m, items=items, bodies=bodies), system="toy")
    _write_manifest(root, default_sensitivity="confidential", max_files=5)
    _write_pull_loop(root)
    _write_autonomy(
        root, enabled=True, allowed_loops=["connector-pull"],
        writable_lanes=["_INPUT"], connectors_list=["toy"],
        max_files_per_run=10, max_bytes=1_000_000,
    )

    # The per-connector pull is ALWAYS gated and grants here (allowlisted). The
    # OUTER loop gate is exercised by the harness path test above; here we read
    # the inner pull result via a direct loops.run (gate=False on the loop layer
    # only -- the connector pull layer still authorizes under connector-pull).
    res = loops.run(root, "connector-pull", now=datetime(2026, 6, 10),
                    headless=True, gate=False)
    inner = res["outcome"]
    assert res["status"] == "ok", res
    assert inner["kind"] == "builtin:connector-pull"
    assert inner["pulled"] == 1
    assert inner["ingested"] == 1
    assert inner["gate_denied"] == 0

    # A file landed in _INPUT/toy/.
    landed = _landed(root, "toy")
    assert len(landed) == 1

    # A source record was persisted carrying connector provenance.
    sources = list((root / "Memory.nosync" / "Sources").glob("*.md"))
    sources = [p for p in sources if not p.name.startswith("_")]
    assert sources, "a source record should be persisted"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in sources)
    assert "toy" in blob  # connector tag in provenance
    # The manifest floor (confidential) is honored at ingest re-classification.
    assert "confidential" in blob


# --------------------------------------------------------------------------- #
# skipped_out_of_scope is never a failure / demotion signal (P7S-12)
# --------------------------------------------------------------------------- #
def test_out_of_scope_skip_does_not_fail_or_demote(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # One in-scope item + one item the adapter marks out_of_scope (an API
    # returning a share/delta/link outside the allowlist -- expected, rc 0).
    out_item = RemoteItem("OUT", "shared.txt", "2026-06-01", -1,
                          {"out_of_scope": True, "scope_reason": "outside folder allowlist"})
    in_item = RemoteItem("IN", "doc.txt", "2026-06-01", -1, {})
    connectors.register(
        "toy",
        lambda m: ToyConnector(m, items=[out_item, in_item], bodies={"IN": b"plain content"}),
        system="toy",
    )
    _write_manifest(root, max_files=5)
    _write_pull_loop(root)
    _write_autonomy(
        root, enabled=True, allowed_loops=["connector-pull"],
        writable_lanes=["_INPUT"], connectors_list=["toy"],
        max_files_per_run=10, max_bytes=1_000_000,
    )

    res = loops.run(root, "connector-pull", now=datetime(2026, 6, 10),
                    headless=True, gate=False)
    inner = res["outcome"]
    # The out-of-scope item is skipped (expected), the in-scope item pulled.
    assert inner["pulled"] == 1
    assert inner["skipped"] >= 1
    assert inner["refused"] == 0
    # The loop is OK (not fail) -> no failure_event, no demotion fuel.
    assert res["status"] == "ok"
    assert inner["status"] == "ok"

    # And via the harness: the demotion sweep finds nothing to demote (an
    # outsider sharing files at an allowlisted folder cannot demote autonomy).
    report = harness.run_once(root, now=datetime(2026, 6, 11))
    assert report.get("demotion") in (None, {}) or not report.get("demotion", {}).get("demoted")


# --------------------------------------------------------------------------- #
# manifest-due gating from the cursor (P7S-23: freshness-from-cursor)
# --------------------------------------------------------------------------- #
def test_not_due_connector_is_skipped(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    items = [RemoteItem("A", "a.txt", "2026-06-01", -1, {})]
    fetched = {"n": 0}

    class _CountingToy(ToyConnector):
        def fetch_item(self, ctx, item):  # pragma: no cover - should not run
            fetched["n"] += 1
            return super().fetch_item(ctx, item)

    connectors.register("toy", lambda m: _CountingToy(m, items=items, bodies={"A": b"x"}), system="toy")
    manifest = _write_manifest(root, cadence="weekly", max_files=5)
    _write_pull_loop(root)
    _write_autonomy(
        root, enabled=True, allowed_loops=["connector-pull"],
        writable_lanes=["_INPUT"], connectors_list=["toy"],
    )

    # Write a fresh cursor: a pull succeeded yesterday, cadence is weekly -> NOT due.
    from connectors.remote import save_cursor
    yesterday = (datetime(2026, 6, 10) - timedelta(days=1)).isoformat(timespec="seconds")
    save_cursor(root, "toy", {"last_success_ts": yesterday})

    res = loops.run(root, "connector-pull", now=datetime(2026, 6, 10),
                    headless=True, gate=False)
    inner = res["outcome"]
    assert inner["due_connectors"] == 0
    assert fetched["n"] == 0
    assert _landed(root, "toy") == []


def test_is_due_when_cursor_stale(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    manifest = _write_manifest(root, cadence="daily")
    from connectors.remote import save_cursor

    # Last success a week ago, daily cadence -> due.
    old = (datetime(2026, 6, 10) - timedelta(days=7)).isoformat(timespec="seconds")
    save_cursor(root, "toy", {"last_success_ts": old})
    assert loops._connector_is_due(connectors, root, "toy", manifest, datetime(2026, 6, 10)) is True

    # Never pulled (no cursor) -> due.
    save_cursor(root, "toy2", {})
    assert loops._connector_is_due(connectors, root, "toy2", {"source": {"cadence": "daily"}},
                                   datetime(2026, 6, 10)) is True
