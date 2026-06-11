"""Doctor connector-health rows (P7-T10).

The shell doctor adds per-instance connector rows: each configured connector's
``health`` with a one-line fix, discovered from the REAL manifest layout
(Connectors/<id>/<id>.manifest.yaml). Doctor stays read-only -- it PROBES via
``connector health``; it never pulls.

These tests live in their OWN file (not test_cli.py) to avoid colliding with the
concurrent agent that owns test_cli.py. They use the shared ``spawned_root`` /
``profile`` fixtures (testkit-spawned root) per the phase's shell-test discipline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from oracle_agent import config, doctor


def _register(root: Path, name: str = "main") -> None:
    cfg = config.load_config()
    cfg = config.register_instance(cfg, name, root)
    config.save_config(cfg)


def _write_localfolder_manifest(root: Path, source_path: Path, *, cid: str = "localfolder",
                                permissions: str = "read_only",
                                last_verified: str = "2026-06-01",
                                expected_decay_days: int = 365) -> None:
    """Schema-valid localfolder manifest at the REAL nested layout
    Connectors/<id>/<id>.manifest.yaml."""
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
        f"permissions: {permissions}\n"
        "freshness:\n  class: manual\n"
        f'  last_verified: "{last_verified}"\n'
        f"  expected_decay_days: {expected_decay_days}\n"
        "source:\n"
        f'  path: "{source_path}"\n'
        "  default_sensitivity: internal\n",
        encoding="utf-8",
    )


def _connector_rows(rep: doctor.Report, name: str) -> list[tuple[str, str, str]]:
    return [(lvl, msg, fix) for (lvl, msg, fix) in rep.rows
            if f"connector '" in msg and f"instance '{name}'" in msg]


# --------------------------------------------------------------------------- #
# discovery + healthy / broken rows
# --------------------------------------------------------------------------- #
def test_doctor_reports_healthy_connector(profile, spawned_root, tmp_path):
    _register(spawned_root, "main")
    src = tmp_path / "src_folder"
    src.mkdir()
    (src / "a.txt").write_text("hello world", encoding="utf-8")
    _write_localfolder_manifest(spawned_root, src)
    try:
        rep = doctor.run("main")
    finally:
        # leave the tree clean for any session-scoped reuse of spawned_root
        import shutil
        shutil.rmtree(spawned_root / "Connectors" / "localfolder", ignore_errors=True)

    rows = _connector_rows(rep, "main")
    assert rows, "doctor should emit a connector row"
    assert any(lvl == doctor.OK and "localfolder" in msg and "healthy" in msg
               for lvl, msg, _ in rows), rows


def test_doctor_flags_broken_connector_with_fix(profile, spawned_root, tmp_path):
    """A misconfigured connector (missing source folder) yields a [fail] with a
    fix line -- and the fix carries the egress-honesty caveat (P7S-6)."""
    _register(spawned_root, "main")
    _write_localfolder_manifest(spawned_root, tmp_path / "does_not_exist")
    try:
        rep = doctor.run("main")
    finally:
        import shutil
        shutil.rmtree(spawned_root / "Connectors" / "localfolder", ignore_errors=True)

    rows = _connector_rows(rep, "main")
    broken = [(lvl, msg, fix) for lvl, msg, fix in rows
              if lvl == doctor.FAIL and "localfolder" in msg]
    assert broken, rows
    _, _, fix = broken[0]
    # the egress-honesty note is pinned on the fix line.
    assert "revoke it AT the provider" in fix
    assert rep.worst_is_fail() is True


def test_doctor_read_only_no_pull(profile, spawned_root, tmp_path):
    """Doctor must not pull: nothing lands in _INPUT from running doctor."""
    _register(spawned_root, "main")
    src = tmp_path / "src_folder"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    _write_localfolder_manifest(spawned_root, src)
    input_dir = spawned_root / "Workproduct.nosync" / "_INPUT" / "localfolder"
    try:
        doctor.run("main")
        landed = list(input_dir.rglob("*")) if input_dir.exists() else []
        assert [p for p in landed if p.is_file()] == [], "doctor must not pull bytes"
    finally:
        import shutil
        shutil.rmtree(spawned_root / "Connectors" / "localfolder", ignore_errors=True)


def test_doctor_no_connectors_emits_no_connector_rows(profile, spawned_root):
    """A root with no Connectors/<id>/ dirs emits no connector rows (absence is
    not a warning)."""
    _register(spawned_root, "main")
    # ensure no leftover connector dirs from other tests
    import shutil
    cdir = spawned_root / "Connectors"
    if cdir.is_dir():
        for sub in cdir.iterdir():
            if sub.is_dir() and (sub / f"{sub.name}.manifest.yaml").exists():
                shutil.rmtree(sub, ignore_errors=True)
    rep = doctor.run("main")
    assert _connector_rows(rep, "main") == []


# --------------------------------------------------------------------------- #
# discovery helper (real nested layout, not the old flat glob)
# --------------------------------------------------------------------------- #
def test_known_connector_ids_uses_nested_layout(tmp_path):
    root = tmp_path / "root"
    (root / "Connectors" / "localfolder").mkdir(parents=True)
    (root / "Connectors" / "localfolder" / "localfolder.manifest.yaml").write_text("id: localfolder\n")
    # a stray top-level file must NOT be counted (old broken glob would have)
    (root / "Connectors" / "stray.manifest.yaml").write_text("id: stray\n")
    ids = doctor._known_connector_ids(root)
    assert ids == ["localfolder"]
