#!/usr/bin/env python3
"""Tests for the admin systems dashboard (dashboard.py).

Load-bearing guarantees exercised here:

  * ``build`` is read-only and degrades per-panel: a minimal root (no loops,
    no scorecards, no autonomy config) still renders EVERY registered panel.
  * Every selection toggle the dashboard surfaces carries the exact command
    that flips it (autonomy master + kill switch, per-loop set-status,
    derived-memory engines) and the loop commands round-trip through the real
    ``loops set-status`` CLI.
  * The autonomy panel is fail-closed honest: spawn-default config renders
    OFF; an engaged kill switch outranks everything (state ``attention``).
  * Layout evolution: ``dashboards.nosync/layout.yml`` reorders and hides
    panels; an invalid/missing layout falls back to the full default registry.
  * ``publish`` writes a self-contained HTML render inside dashboards.nosync/
    (contained write) and refuses traversal in the output name.

Self-contained: depends on dashboard.py + the floor + the shared
``minimal_oracle`` fixture. Loop records are built inline (no spawn needed).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import dashboard  # noqa: E402
import loops  # noqa: E402
import oracle_cli  # noqa: E402

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _write_truth_map(root: Path, rows: list[dict]) -> None:
    header = "| Business object | Primary source | Freshness budget | Status |"
    sep = "|---|---|---|---|"
    lines = ["# Truth Map", "", header, sep]
    for r in rows:
        lines.append(
            f"| {r['object']} | {r.get('source', 'TBD')} | "
            f"{r.get('budget', '7d')} | {r.get('status', 'draft')} |"
        )
    (root / "TRUTH-MAP.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_loop(root: Path, loop_id: str, *, status: str = "active",
                cadence: str = "weekly", paused_reason: str = "") -> None:
    d = root / "Meta.nosync" / "Loops"
    d.mkdir(parents=True, exist_ok=True)
    fm = [
        "---",
        f"id: {loop_id}",
        "type: loop",
        f"title: Loop {loop_id}",
        "created: 2026-01-01",
        "updated: 2026-01-01",
        "sensitivity: internal",
        f"status: {status}",
        "tags:",
        "  - loop",
        f"cadence: {cadence}",
        "runner: agent-worklist",
        "last_run: 2026-06-01T00:00:00",
        "next_review: 2026-06-08",
    ]
    if paused_reason:
        fm.append(f"paused_reason: {paused_reason}")
    fm += ["---", "", "Process: test loop.", ""]
    (d / f"loop-{loop_id}.md").write_text("\n".join(fm), encoding="utf-8")


def _ready_root(minimal_oracle, tmp_path) -> Path:
    root = minimal_oracle(tmp_path)
    _write_truth_map(root, [])
    return root


# --------------------------------------------------------------------------- #
# build: every panel renders on a minimal root
# --------------------------------------------------------------------------- #
def test_build_renders_every_registered_panel(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    d = dashboard.build(root, now=NOW)
    keys = [p["key"] for p in d["panels"]]
    assert keys == list(dashboard.PANELS)
    for p in d["panels"]:
        assert p["state"] in ("ok", "warn", "off", "attention")
        assert p["headline"], f"panel {p['key']} has no headline"
    assert d["company"] == "Test Co"
    assert d["codename"] == "TESTORACLE"


def test_build_is_read_only(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    dashboard.build(root, now=NOW)
    after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert before == after


# --------------------------------------------------------------------------- #
# autonomy panel: fail-closed honesty + kill-switch priority
# --------------------------------------------------------------------------- #
def test_autonomy_panel_default_off_with_enable_path(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "autonomy")
    assert panel["state"] == "off"
    assert "OFF" in panel["headline"]
    master = next(c for c in d["controls"] if c["control"] == "autonomy master + level")
    assert "admin autonomy promote" in master["command"]


def test_kill_switch_outranks_enabled(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    auto_dir = root / "Meta.nosync" / "Autonomy"
    auto_dir.mkdir(parents=True, exist_ok=True)
    (auto_dir / "autonomy.yml").write_text(
        "enabled: true\nlevel: 1\n", encoding="utf-8"
    )
    (auto_dir / "KILL-SWITCH").write_text("stop\n", encoding="utf-8")
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "autonomy")
    assert panel["state"] == "attention"
    assert "KILL SWITCH" in panel["headline"]
    kill = next(c for c in d["controls"] if c["control"] == "kill switch")
    assert kill["state"] == "ENGAGED"
    assert kill["command"].startswith("rm ")


# --------------------------------------------------------------------------- #
# loops panel: states + working toggle commands
# --------------------------------------------------------------------------- #
def test_loop_toggles_round_trip_through_cli(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    _write_loop(root, "alpha", status="active")
    _write_loop(root, "beta", status="paused", paused_reason="flaky runner")

    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "loops")
    rows = {r["id"]: r for r in panel["rows"]}
    assert rows["beta"]["state"] == "off"
    assert "flaky runner" in rows["beta"]["note"]

    toggles = {c["control"]: c for c in panel["controls"]}
    # an active loop's toggle pauses; a paused loop's toggle reactivates
    assert "set-status alpha paused" in toggles["loop alpha"]["command"]
    assert "set-status beta active" in toggles["loop beta"]["command"]

    # the printed command is real: run it via the loops CLI and re-render
    rc = loops.main(["--root", str(root), "set-status", "beta", "active"])
    assert rc == 0
    d2 = dashboard.build(root, now=NOW)
    panel2 = next(p for p in d2["panels"] if p["key"] == "loops")
    beta2 = next(r for r in panel2["rows"] if r["id"] == "beta")
    assert beta2["status"] == "active"


# --------------------------------------------------------------------------- #
# attention panel: the consolidated "fix these" surface
# --------------------------------------------------------------------------- #
def test_attention_panel_surfaces_outstanding_issues_with_fixes(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "attention")
    # a minimal root has hygiene debt: backup never proven, manifest missing
    systems = {i["system"] for i in panel["issues"]}
    assert "backup" in systems
    assert "kernel" in systems
    for issue in panel["issues"]:
        assert issue["fix"], f"issue without a remedy: {issue}"
        assert issue["severity"] in ("fix-now", "should-fix")
    assert panel["state"] == "warn"  # should-fix only -> warn, not attention


def test_attention_ranks_fix_now_first_and_names_remedies(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    _write_loop(root, "beta", status="paused", paused_reason="flaky runner")
    auto_dir = root / "Meta.nosync" / "Autonomy"
    auto_dir.mkdir(parents=True, exist_ok=True)
    (auto_dir / "KILL-SWITCH").write_text("stop\n", encoding="utf-8")
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "attention")
    assert panel["state"] == "attention"
    assert panel["issues"][0]["severity"] == "fix-now"
    assert "kill switch" in panel["issues"][0]["issue"]
    sev = [i["severity"] for i in panel["issues"]]
    assert sev == sorted(sev, key=lambda s: s != "fix-now")  # fix-now block first
    beta = [i for i in panel["issues"] if "beta" in i["issue"]]
    # paused loop listed exactly once (inbox paused-loop kind deduped) w/ toggle
    assert len(beta) == 1
    assert "set-status beta active" in beta[0]["fix"]
    text = dashboard.render_md(d)
    assert "## Needs attention" in text


# --------------------------------------------------------------------------- #
# portability panel: machine facts + external couplings + migration readiness
# --------------------------------------------------------------------------- #
def _no_scheduler(monkeypatch, tmp_path):
    """Point the launchd probe at an empty dir so tests never read the real
    ~/Library/LaunchAgents."""
    agents = tmp_path / "launch-agents"
    agents.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dashboard, "_launch_agents_dir", lambda: agents)
    return agents


def test_portability_clean_repo_shows_environment_facts(tmp_path, minimal_oracle, monkeypatch):
    _no_scheduler(monkeypatch, tmp_path)
    root = _ready_root(minimal_oracle, tmp_path)
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "portability")
    assert "relocatable" in panel["headline"]
    env = panel["data"]["environment"]
    assert env["root"] == str(root.resolve())
    assert env["python"]
    # the machine-local root is session-only, never in persistence-safe lines
    assert any(env["root"] in ln for ln in panel["session_lines"])
    assert not any(env["root"] in ln for ln in panel["lines"])
    assert panel["state"] != "attention"


def test_stale_scheduler_root_is_fix_now(tmp_path, minimal_oracle, monkeypatch):
    agents = _no_scheduler(monkeypatch, tmp_path)
    root = _ready_root(minimal_oracle, tmp_path)
    (agents / "com.oracle.testoracle.loops.plist").write_text(
        "<plist><dict><array>\n"
        "    <string>python3</string>\n"
        "    <string>/Users/olduser/old-machine/oracle/_tools/harness.py</string>\n"
        "</array></dict></plist>\n",
        encoding="utf-8",
    )
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "portability")
    assert panel["state"] == "attention"
    assert panel["data"]["scheduler"]["stale_root"] is True
    attention = next(p for p in d["panels"] if p["key"] == "attention")
    sched_issues = [i for i in attention["issues"] if i["system"] == "portability"]
    assert sched_issues and sched_issues[0]["severity"] == "fix-now"
    assert "install_schedule.sh" in sched_issues[0]["fix"]


def test_hardcoded_external_path_in_repo_is_flagged(tmp_path, minimal_oracle, monkeypatch):
    _no_scheduler(monkeypatch, tmp_path)
    root = _ready_root(minimal_oracle, tmp_path)
    (root / "Meta.nosync" / "stale-note.md").write_text(
        "data lives at /Users/olduser/exports/q3.csv\n", encoding="utf-8"
    )
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "portability")
    assert panel["data"]["external_paths"], "lint scan should flag the hardcoded path"
    assert "IN repo" in panel["headline"]
    attention = next(p for p in d["panels"] if p["key"] == "attention")
    assert any("hardcoded external path" in i["issue"] for i in attention["issues"])


def test_missing_host_binary_and_secrets_gap_flagged(tmp_path, minimal_oracle, monkeypatch):
    _no_scheduler(monkeypatch, tmp_path)
    root = _ready_root(minimal_oracle, tmp_path)
    auto_dir = root / "Meta.nosync" / "Autonomy"
    auto_dir.mkdir(parents=True, exist_ok=True)
    (auto_dir / "autonomy.yml").write_text(
        "enabled: false\nlevel: 0\ndream:\n  command: not-a-real-binary-xyz123\n",
        encoding="utf-8",
    )
    conn = root / "Connectors" / "foo"
    conn.mkdir(parents=True, exist_ok=True)
    (conn / "foo.manifest.yaml").write_text(
        "id: foo\nsystem: Foo ERP\nstatus: active\naccess_mode: folder\n"
        "locality: external_only\ncapture_tier: snapshot\npermissions: read_only\n"
        "auth:\n  method: env\n  vars:\n    - FOO_API_TOKEN\n"
        "source:\n  path: /srv/exports/foo\n",
        encoding="utf-8",
    )
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "portability")
    bins = panel["data"]["binaries"]
    assert bins and bins[0]["found"] is False
    assert any(c["id"] == "foo" for c in panel["data"]["connectors"])
    attention = next(p for p in d["panels"] if p["key"] == "attention")
    texts = [i["issue"] for i in attention["issues"]]
    assert any("not-a-real-binary-xyz123" in t for t in texts)
    assert any("FOO_API_TOKEN" in t for t in texts)


def test_published_html_never_embeds_machine_local_paths(tmp_path, minimal_oracle, monkeypatch):
    agents = _no_scheduler(monkeypatch, tmp_path)
    root = _ready_root(minimal_oracle, tmp_path)
    (agents / "com.oracle.testoracle.loops.plist").write_text(
        "<string>/Users/olduser/old-machine/oracle/_tools/harness.py</string>\n",
        encoding="utf-8",
    )
    result = dashboard.publish(root, now=NOW)
    html = Path(result["published"]).read_text(encoding="utf-8")
    # neither this machine's root nor the stale scheduler's old root may be
    # persisted -- the external-path lint scans dashboards.nosync
    assert str(root.resolve()) not in html
    assert "/Users/" not in html


# --------------------------------------------------------------------------- #
# layout overlay: the self-improvement hook
# --------------------------------------------------------------------------- #
def test_layout_yaml_reorders_and_hides(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    dash_dir = root / "dashboards.nosync"
    dash_dir.mkdir(parents=True, exist_ok=True)
    (dash_dir / "layout.yml").write_text(
        "order:\n  - autonomy\n  - loops\nhidden:\n  - backup\n", encoding="utf-8"
    )
    d = dashboard.build(root, now=NOW)
    keys = [p["key"] for p in d["panels"]]
    assert keys[:2] == ["autonomy", "loops"]
    assert "backup" not in keys
    # everything not mentioned still renders (no silent loss of panels)
    assert set(keys) == set(dashboard.PANELS) - {"backup"}
    assert d["layout_source"].endswith("layout.yml")


def test_invalid_layout_falls_back_to_defaults(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    dash_dir = root / "dashboards.nosync"
    dash_dir.mkdir(parents=True, exist_ok=True)
    (dash_dir / "layout.yml").write_text("order: [a, b]\n", encoding="utf-8")  # flow style -> safe_load raises
    layout = dashboard.read_layout(root)
    assert layout["source"] == "default"
    assert layout["order"] == list(dashboard.PANELS)


# --------------------------------------------------------------------------- #
# renderers + publish
# --------------------------------------------------------------------------- #
def test_render_md_contains_systems_table_and_controls(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    _write_loop(root, "alpha", status="active")
    text = dashboard.render_md(dashboard.build(root, now=NOW))
    assert "admin dashboard" in text
    assert "## Systems" in text
    assert "## Controls" in text
    assert "set-status alpha" in text


def test_publish_writes_contained_self_named_html(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    result = dashboard.publish(root, now=NOW)
    out = Path(result["published"])
    assert out.is_file()
    assert out.parent == (root / "dashboards.nosync").resolve()
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "http://" not in html and "https://" not in html  # no external assets
    assert "Test Co" in html


def test_publish_refuses_traversal(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    with pytest.raises(ValueError):
        dashboard.publish(root, out_name="../escape.html")


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_cli_show_json_and_controls(tmp_path, minimal_oracle, capsys):
    root = _ready_root(minimal_oracle, tmp_path)
    assert dashboard.main(["--root", str(root), "show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["key"] for p in data["panels"]] == list(dashboard.PANELS)
    assert dashboard.main(["--root", str(root), "controls"]) == 0
    assert dashboard.main(["--root", str(root), "panels"]) == 0
    capsys.readouterr()


def test_cli_default_subcommand_is_show_via_dispatcher(tmp_path, minimal_oracle, capsys):
    root = _ready_root(minimal_oracle, tmp_path)
    # both the top-level verb and the admin area resolve to the dashboard
    assert oracle_cli.main(["dashboard", "--root", str(root)]) == 0
    assert oracle_cli.main(["admin", "dashboard", "--root", str(root), "panels"]) == 0
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# connectors panel: REAL manifest layout discovery + per-connector health
# (P7-T10 / P7S-28 -- the old top-level glob counted zero)
# --------------------------------------------------------------------------- #
def _write_localfolder_manifest(root: Path, source_path: Path, *, cid: str = "localfolder",
                                last_verified: str = "2026-06-01",
                                expected_decay_days: int = 365) -> None:
    """Write a schema-valid localfolder manifest at the REAL nested layout
    Connectors/<id>/<id>.manifest.yaml (NOT the old flat glob the broken panel
    looked for)."""
    mdir = root / "Connectors" / cid
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{cid}.manifest.yaml").write_text(
        f"id: {cid}\n"
        "system: local-filesystem\n"
        "status: active\n"
        "access_mode: folder\n"
        "locality: snapshot_local\n"
        "capture_tier: snapshot\n"
        "auth:\n  method: none\n  vars:\n"
        "permissions: read_only\n"
        "freshness:\n  class: manual\n"
        f'  last_verified: "{last_verified}"\n'
        f"  expected_decay_days: {expected_decay_days}\n"
        "source:\n"
        f'  path: "{source_path}"\n'
        "  default_sensitivity: internal\n",
        encoding="utf-8",
    )


def test_connectors_panel_discovers_real_layout(tmp_path, minimal_oracle):
    """The panel discovers connectors via _known_connector_ids at the nested
    Connectors/<id>/<id>.manifest.yaml layout -- the old top-level glob saw
    zero (P7S-28)."""
    root = _ready_root(minimal_oracle, tmp_path)
    src = tmp_path / "src_folder"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    _write_localfolder_manifest(root, src)

    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "connectors")
    assert panel["state"] in ("ok", "warn"), panel
    assert "1 installed" in panel["headline"]
    # per-connector health row present + a pull control for it.
    ids = [r["id"] for r in panel.get("rows_connectors", [])]
    assert "localfolder" in ids
    assert any(c["control"] == "connector localfolder" for c in panel["controls"])


def test_connectors_panel_health_and_freshness_from_cursor(tmp_path, minimal_oracle):
    """The row reflects the connector's health() AND last-pull age from the
    cursor (freshness-from-cursor, P7S-23 -- a localfolder reports from its own
    health, a remote from its cursor)."""
    root = _ready_root(minimal_oracle, tmp_path)
    # A missing source folder -> localfolder health is broken.
    _write_localfolder_manifest(root, tmp_path / "does_not_exist")
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "connectors")
    row = next(r for r in panel["rows_connectors"] if r["id"] == "localfolder")
    assert row["status"] == "broken"
    assert panel["state"] == "attention"


def test_connectors_panel_empty_when_none_installed(tmp_path, minimal_oracle):
    root = _ready_root(minimal_oracle, tmp_path)
    d = dashboard.build(root, now=NOW)
    panel = next(p for p in d["panels"] if p["key"] == "connectors")
    assert panel["state"] == "off"
    assert "none installed" in panel["headline"]
    assert panel.get("rows_connectors", []) == []
