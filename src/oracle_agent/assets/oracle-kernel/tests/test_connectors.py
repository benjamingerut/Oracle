#!/usr/bin/env python3
"""Tests for the connector runtime + the localfolder reference connector.

These tests are self-contained: they build a minimal oracle root inline (via the
``minimal_oracle`` conftest fixture) plus a temp SOURCE folder, write a real
connector manifest on disk, and exercise the runtime contract end to end. They
depend only on this unit plus the floor (conftest puts ``_tools`` on sys.path):
``intake_classify`` and ``actions`` are optional siblings, so the connector
degrades to its built-in classifier and an un-gated (but still safe) pull when
they are absent.

What is proven here (matches the test plan for this unit):
  * a localfolder pull from a tmp source INGESTS files into _INPUT and CLASSIFIES
    each one's sensitivity, copying NON-DESTRUCTIVELY (source left intact, bytes
    verified by sha256 at the destination);
  * a file whose realpath ESCAPES the configured source root is REFUSED, never
    copied (both the per-file containment guard and a planted symlink escape);
  * health reports the correct state (healthy / degraded / broken) for a
    populated folder, an empty folder, and a missing/misconfigured source.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import connectors
from connectors import base as connbase
from connectors import localfolder


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write_manifest(root: Path, source_path: Path, **overrides) -> Path:
    """Write a schema-valid localfolder manifest under the oracle root.

    Block-style YAML only (the strict oracle_yaml subset): empty values are a
    bare ``key:`` and lists are one ``- item`` per line.
    """
    cid = overrides.get("id", "localfolder")
    status = overrides.get("status", "active")
    permissions = overrides.get("permissions", "read_only")
    decay = overrides.get("expected_decay_days", 7)
    last_verified = overrides.get("last_verified", "2026-01-01")
    default_sensitivity = overrides.get("default_sensitivity", "internal")

    manifest_dir = root / "Connectors" / cid
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / f"{cid}.manifest.yaml"
    text = f"""\
id: {cid}
system: local-filesystem
status: {status}
access_mode: folder
locality: snapshot_local
capture_tier: snapshot
auth:
  method: none
  vars:
permissions: {permissions}
freshness:
  class: manual
  last_verified: "{last_verified}"
  expected_decay_days: {decay}
source:
  path: "{source_path}"
  default_sensitivity: {default_sensitivity}
authoritative_for:
corroborates:
cannot_prove:
forbidden_uses:
biases:
health_check:
  cadence: weekly
  checks:
    - source folder exists and is readable
    - last pull inside freshness SLA
schema_refresh:
  enabled: false
  cadence:
  remote_probe: false
"""
    manifest.write_text(text, encoding="utf-8")
    return manifest


def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "source_folder"
    src.mkdir(parents=True, exist_ok=True)
    (src / "quarterly-report.txt").write_text("revenue numbers here", encoding="utf-8")
    (src / "team-notes.md").write_text("# internal notes\n", encoding="utf-8")
    (src / "confidential-contract.txt").write_text("party A and party B agree", encoding="utf-8")
    return src


# --------------------------------------------------------------------------- #
# manifest loading
# --------------------------------------------------------------------------- #
def test_load_manifest_round_trips(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    manifest = connbase.load_manifest(root, "localfolder")
    assert manifest["id"] == "localfolder"
    assert manifest["access_mode"] == "folder"
    assert manifest["source"]["path"] == str(src)


def test_load_manifest_id_mismatch_raises(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    # Write a manifest whose internal id differs from the requested id.
    _write_manifest(root, src, id="localfolder")
    # Re-point to a directory named differently from the manifest's id.
    other = root / "Connectors" / "other"
    other.mkdir(parents=True, exist_ok=True)
    (other / "other.manifest.yaml").write_text(
        (root / "Connectors" / "localfolder" / "localfolder.manifest.yaml").read_text(),
        encoding="utf-8",
    )
    with pytest.raises(connbase.ConnectorError):
        connbase.load_manifest(root, "other")


def test_get_connector_resolves_localfolder(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    conn = connectors.get_connector(root, "localfolder")
    assert isinstance(conn, localfolder.LocalFolderConnector)
    assert conn.access_mode == "folder"


# --------------------------------------------------------------------------- #
# pull: ingest + classify (non-destructive)
# --------------------------------------------------------------------------- #
def test_pull_ingests_and_classifies(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)

    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest)
    results = conn.pull(ctx)

    ingested = [r for r in results if r["action"] == "ingested"]
    assert len(ingested) == 3, results

    # Every ingested record carries a sensitivity label and a content hash.
    for r in ingested:
        assert r["sensitivity"] in (
            "public", "internal", "confidential", "restricted", "secret"
        )
        assert r["sha256_12"] and len(r["sha256_12"]) == 12
        # The destination is inside the oracle's _INPUT lane.
        dst = Path(r["dst"])
        assert dst.exists(), f"ingested file should exist at destination: {dst}"
        assert "_INPUT" in dst.parts
        assert "Workproduct.nosync" in dst.parts

    # The "confidential-contract" file classified stricter than the default.
    by_src = {Path(r["src"]).name: r for r in ingested}
    assert by_src["confidential-contract.txt"]["sensitivity"] == "confidential"

    # NON-DESTRUCTIVE: the original source files are still present.
    assert (src / "quarterly-report.txt").exists()
    assert (src / "team-notes.md").exists()
    assert (src / "confidential-contract.txt").exists()


def test_pull_dry_run_copies_nothing(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)

    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, dry_run=True)
    results = conn.pull(ctx)

    assert all(r["action"] == "planned" for r in results)
    # _INPUT should hold nothing from this connector.
    input_dir = root / "Workproduct.nosync" / "_INPUT" / "localfolder"
    assert not input_dir.exists() or not any(input_dir.iterdir())


def test_pull_respects_max_files_cap(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, max_files=1)
    results = conn.pull(ctx)
    ingested = [r for r in results if r["action"] == "ingested"]
    assert len(ingested) == 1


def test_pull_refuses_read_write_connector(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src, permissions="read_write")
    conn = connectors.get_connector(root, "localfolder", validate=False)
    ctx = connbase.ConnectorContext(root, conn.manifest)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(ctx)


# --------------------------------------------------------------------------- #
# pull: containment -- a source escaping the configured root is REFUSED
# --------------------------------------------------------------------------- #
def test_within_source_rejects_escape(tmp_path, minimal_oracle):
    """The per-file source-containment guard rejects a path outside the root."""
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    conn = connectors.get_connector(root, "localfolder")

    outside = tmp_path / "outside_secret.txt"
    outside.write_text("not in the source root", encoding="utf-8")

    source_root = Path(os.path.realpath(src))
    # A file genuinely inside the source is accepted.
    assert conn._within_source(source_root, src / "team-notes.md") is True
    # A file outside the configured source root is rejected.
    assert conn._within_source(source_root, outside) is False
    # A traversal that climbs out is rejected.
    assert conn._within_source(source_root, src / ".." / "outside_secret.txt") is False


@pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="platform has no symlink support"
)
def test_pull_does_not_follow_symlink_escape(tmp_path, minimal_oracle):
    """A symlink inside the source pointing OUTSIDE it must not exfiltrate bytes.

    The walk excludes symlinked files, and the containment guard would reject
    any that slipped through, so the secret target is never ingested into
    _INPUT.
    """
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)

    secret = tmp_path / "exfil_target.txt"
    secret.write_text("SECRET CONTENT OUTSIDE THE ROOT", encoding="utf-8")
    link = src / "looks-innocent.txt"
    try:
        os.symlink(str(secret), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("could not create symlink on this platform")

    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest)
    results = conn.pull(ctx)

    # The symlink's real (outside) content never lands in _INPUT.
    input_dir = root / "Workproduct.nosync" / "_INPUT" / "localfolder"
    landed = list(input_dir.rglob("*")) if input_dir.exists() else []
    for f in landed:
        if f.is_file():
            assert "SECRET CONTENT OUTSIDE THE ROOT" not in f.read_text(encoding="utf-8")
    # The real source files (non-symlink) are still ingested.
    ingested = [r for r in results if r["action"] == "ingested"]
    ingested_names = {Path(r["src"]).name for r in ingested}
    assert "team-notes.md" in ingested_names
    assert "looks-innocent.txt" not in ingested_names


def test_dest_for_is_contained(tmp_path, minimal_oracle):
    """Destination computation always lands under the oracle _INPUT lane."""
    import safe_paths

    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    conn = connectors.get_connector(root, "localfolder")

    dest = conn._dest_for(safe_paths, root, src / "quarterly-report.txt")
    base = Path(os.path.realpath(root / "Workproduct.nosync"))
    assert safe_paths.is_within(base, dest)
    assert dest.name.endswith(".txt")
    # Date-prefixed filename.
    assert dest.name[:4].isdigit() and dest.name[4] == "-"


# --------------------------------------------------------------------------- #
# probe + freshness
# --------------------------------------------------------------------------- #
def test_probe_reports_histogram(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest)
    probe = conn.probe(ctx)
    assert probe["items"] == 3
    assert probe["by_suffix"].get(".txt", 0) == 2
    assert probe["by_suffix"].get(".md", 0) == 1
    assert probe["total_bytes"] > 0


def test_freshness_fresh_vs_stale(tmp_path, minimal_oracle):
    from datetime import datetime

    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)

    # last_verified recent, generous decay -> fresh.
    _write_manifest(root, src, last_verified="2026-06-01", expected_decay_days=365)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, now=datetime(2026, 6, 8))
    assert conn.freshness(ctx)["verdict"] == "fresh"

    # last_verified old, tight decay -> stale.
    _write_manifest(root, src, last_verified="2020-01-01", expected_decay_days=7)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, now=datetime(2026, 6, 8))
    assert conn.freshness(ctx)["verdict"] == "stale"


def test_freshness_unknown_when_no_budget(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    # No last_verified at all.
    _write_manifest(root, src, last_verified="", expected_decay_days=7)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest)
    assert conn.freshness(ctx)["verdict"] == "unknown"


# --------------------------------------------------------------------------- #
# health: correct states
# --------------------------------------------------------------------------- #
def test_health_healthy(tmp_path, minimal_oracle):
    from datetime import datetime

    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src, last_verified="2026-06-01", expected_decay_days=365)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, now=datetime(2026, 6, 8))
    report = conn.health(ctx)
    assert report["status"] == "healthy"
    assert report["probe"]["items"] == 3


def test_health_degraded_when_empty(tmp_path, minimal_oracle):
    from datetime import datetime

    root = minimal_oracle(tmp_path)
    src = tmp_path / "empty_source"
    src.mkdir()
    _write_manifest(root, src, last_verified="2026-06-01", expected_decay_days=365)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, now=datetime(2026, 6, 8))
    report = conn.health(ctx)
    assert report["status"] == "degraded"
    assert any("empty" in n for n in report["notes"])


def test_health_degraded_when_stale(tmp_path, minimal_oracle):
    from datetime import datetime

    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src, last_verified="2020-01-01", expected_decay_days=7)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, now=datetime(2026, 6, 8))
    report = conn.health(ctx)
    assert report["status"] == "degraded"


def test_health_broken_when_source_missing(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    missing = tmp_path / "does_not_exist"
    _write_manifest(root, missing)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest)
    report = conn.health(ctx)
    assert report["status"] == "broken"


def test_health_broken_when_read_write(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src, permissions="read_write")
    conn = connectors.get_connector(root, "localfolder", validate=False)
    ctx = connbase.ConnectorContext(root, conn.manifest)
    report = conn.health(ctx)
    assert report["status"] == "broken"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_probe(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    rc = connectors.main(["--root", str(root), "--json", "probe", "localfolder"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"items": 3' in out


def test_cli_pull(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    rc = connectors.main(["--root", str(root), "--json", "pull", "localfolder"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"ingested": 3' in out


def test_cli_health_all(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src, last_verified="2026-06-01", expected_decay_days=365)
    rc = connectors.main(["--root", str(root), "--json", "health"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "localfolder" in out


@pytest.mark.parametrize("cmd", ["pull", "probe", "freshness"])
def test_cli_operational_commands_validate_manifest(tmp_path, minimal_oracle, capsys, cmd):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src, status="not-a-valid-status")

    rc = connectors.main(["--root", str(root), "--json", cmd, "localfolder"])

    assert rc == 2
    captured = capsys.readouterr()
    assert "failed schema validation" in captured.err
    assert "_INPUT" not in captured.out
    assert _pulled_input_files(root) == []


def test_cli_health_all_reports_invalid_manifest_as_broken(
    tmp_path, minimal_oracle, capsys
):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src, status="not-a-valid-status")

    rc = connectors.main(["--root", str(root), "--json", "health"])

    assert rc == 1
    reports = json.loads(capsys.readouterr().out)
    assert len(reports) == 1
    report = reports[0]
    assert report["connector"] == "localfolder"
    assert report["status"] == "broken"
    assert any("failed schema validation" in note for note in report["notes"])


def test_cli_unknown_connector_errors(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    rc = connectors.main(["--root", str(root), "probe", "nonexistent"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "CONNECTOR ERROR" in err


# --------------------------------------------------------------------------- #
# action gate (opt-in; only relevant when actions.py is present)
# --------------------------------------------------------------------------- #
def test_ungated_pull_meta_is_direct(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest)  # gated defaults False
    results, meta = connectors._guarded_pull(conn, ctx)
    assert meta["action_gate"] == "direct"
    assert any(r["action"] == "ingested" for r in results)


def test_pull_denies_unknown_role_before_copy(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, role="robot")

    with pytest.raises(connbase.ConnectorError):
        connectors._guarded_pull(conn, ctx)

    assert _pulled_input_files(root) == []


def test_gated_pull_blocked_when_autonomy_off(tmp_path, minimal_oracle):
    """A GATED pull (headless path) is refused while autonomy is OFF by default.

    This is the safety property: between-sessions pulls cannot run unless the
    admin has explicitly turned autonomy on. If actions.py is not importable,
    the gate is 'unavailable' and the pull runs ungated -- in which case this
    assertion is skipped, never falsely failed.
    """
    actions = connectors._import_actions()
    if actions is None or not hasattr(actions, "with_action"):
        pytest.skip("actions.py unavailable")

    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, gated=True)
    with pytest.raises(connbase.ConnectorError):
        connectors._guarded_pull(conn, ctx)
    # The pull did not run: nothing landed in _INPUT.
    input_dir = root / "Workproduct.nosync" / "_INPUT" / "localfolder"
    assert not input_dir.exists() or not any(input_dir.rglob("*"))


def _write_autonomy(
    root: Path,
    *,
    enabled: bool = True,
    writable_lanes=None,
    max_files_per_run: int = 10,
    max_bytes: int = 100_000,
) -> None:
    writable_lanes = writable_lanes or []

    def _block(key: str, items) -> str:
        if not items:
            return f"{key}:\n"
        return f"{key}:\n" + "".join(f"  - {item}\n" for item in items)

    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    text = (
        f"enabled: {'true' if enabled else 'false'}\n"
        + _block("allowed_loops", ["connector-health"])
        + _block("writable_lanes", writable_lanes)
        + _block("readonly_connectors", ["localfolder"])
        + "blast_radius_caps:\n"
        + f"  max_files_per_run: {max_files_per_run}\n"
        + f"  max_bytes: {max_bytes}\n"
        + 'kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"\n'
    )
    (d / "autonomy.yml").write_text(text, encoding="utf-8")


def _pulled_input_files(root: Path) -> list[Path]:
    input_dir = root / "Workproduct.nosync" / "_INPUT" / "localfolder"
    if not input_dir.exists():
        return []
    return [p for p in input_dir.rglob("*") if p.is_file()]


def test_gated_pull_requires_input_lane_allowlist(tmp_path, minimal_oracle):
    actions = connectors._import_actions()
    if actions is None or not hasattr(actions, "with_action"):
        pytest.skip("actions.py unavailable")

    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    _write_autonomy(root, writable_lanes=["01_Finance"], max_files_per_run=10)

    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, gated=True, role="admin")
    with pytest.raises(connbase.ConnectorError):
        connectors._guarded_pull(conn, ctx)

    assert _pulled_input_files(root) == []


def test_gated_pull_enforces_planned_file_cap(tmp_path, minimal_oracle):
    actions = connectors._import_actions()
    if actions is None or not hasattr(actions, "with_action"):
        pytest.skip("actions.py unavailable")

    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    _write_autonomy(root, writable_lanes=["_INPUT"], max_files_per_run=2)

    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, gated=True, role="admin")
    with pytest.raises(connbase.ConnectorError):
        connectors._guarded_pull(conn, ctx)

    assert _pulled_input_files(root) == []


def test_gated_pull_runs_when_allowlisted_within_caps(tmp_path, minimal_oracle):
    actions = connectors._import_actions()
    if actions is None or not hasattr(actions, "with_action"):
        pytest.skip("actions.py unavailable")

    root = minimal_oracle(tmp_path)
    src = _make_source(tmp_path)
    _write_manifest(root, src)
    _write_autonomy(root, writable_lanes=["_INPUT"], max_files_per_run=5)

    conn = connectors.get_connector(root, "localfolder")
    ctx = connbase.ConnectorContext(root, conn.manifest, gated=True, role="admin")
    results, meta = connectors._guarded_pull(conn, ctx)

    assert meta["action_gate"] == "applied"
    assert len([r for r in results if r["action"] == "ingested"]) == 3
