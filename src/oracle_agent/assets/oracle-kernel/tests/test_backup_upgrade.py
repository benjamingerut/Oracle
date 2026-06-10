#!/usr/bin/env python3
"""Tests for backup.py + upgrade.py + migrations + standing_deliverables.py.

These cover the S12 unit guarantees:

  * backup -> restore round-trip is hash-EQUAL on a minimal oracle, and
    populates ``last_verified_restore`` in BACKUP-RECOVERY.md;
  * secret-tier bytes (``.env.nosync``) are NEVER written into a backup;
  * migrations are discovered + applied in ascending ``NNNN`` order and the
    baseline migration idempotently stamps ``kernel.tools_version``;
  * upgrade swaps ONLY ``_tools`` (data/doctrine untouched), hash-verifies the
    bundle, REFUSES a bundle whose manifest lists a non-tool file, and REFUSES a
    headless / unapproved apply;
  * standing deliverables route every claim through the answer protocol and DROP
    any claim returning exit 4 (no authority).

They depend only on this unit's modules plus the floor (safe_paths, ledger,
oracle_yaml, answer_protocol, truth_map, artifact_io) and the conftest
``minimal_oracle`` helper, so they pass in isolation while siblings build.
"""
from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

import pytest

import backup
import upgrade
import migrations


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _seed_content(root: Path) -> None:
    """Drop some representative tier-0/tier-1 content plus a secret to exclude."""
    # tier 0: a memory note.
    src = root / "Memory.nosync" / "Sources"
    src.mkdir(parents=True, exist_ok=True)
    (src / "src-001.md").write_text(
        "---\nid: src-001\ntype: source\n---\nbody bytes here\n", encoding="utf-8"
    )
    # tier 1: a workproduct artifact.
    created = root / "Workproduct.nosync" / "01_Finance" / "created"
    created.mkdir(parents=True, exist_ok=True)
    (created / "2026-01-01_note.md").write_text("artifact body\n", encoding="utf-8")
    # tier 0: a control-plane doc.
    (root / "GOVERNANCE.md").write_text("# Governance\nrules\n", encoding="utf-8")
    skill_dir = root / "AgentResources.nosync" / "Skills" / "pricing-review"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: pricing-review\n"
        "description: Review pricing evidence.\n"
        "status: active\n"
        "sensitivity: internal\n"
        "provenance: agent\n"
        "created: \"2026-01-01\"\n"
        "updated: \"2026-01-01\"\n"
        "tags:\n"
        "  - skill\n"
        "---\n\n"
        "# Pricing Review\n\nCheck current evidence.\n",
        encoding="utf-8",
    )
    # a SECRET that must never be backed up in plaintext.
    (root / ".env.nosync").write_text(
        "API_TOKEN=sk_live_super_secret_value\n", encoding="utf-8"
    )


def _make_kernel_bundle(tmp_path: Path, tools_files: dict[str, str]) -> Path:
    """Build a fake incoming kernel dir with a _tools/ tree + a manifest.

    ``tools_files`` maps a relpath UNDER the kernel dir (e.g. ``_tools/foo.py``)
    to its text content. The ``.kernel-manifest.json`` is rendered to match.
    """
    kdir = tmp_path / "incoming_kernel"
    files_meta: dict[str, str] = {}
    for rel, content in tools_files.items():
        p = kdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        files_meta[Path(rel).as_posix()] = backup.sha256_file(p)
    manifest = {"tools_version": "2.0.1", "files": files_meta}
    (kdir / ".kernel-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return kdir


# --------------------------------------------------------------------------- #
# backup + restore round-trip
# --------------------------------------------------------------------------- #
def test_backup_run_excludes_secrets(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _seed_content(root)
    dest = tmp_path / "backup_out"
    manifest = backup.run(root, dest, tier="all")

    rels = {f["rel"] for f in manifest["files"]}
    # the seeded artifacts are present...
    assert any(r.endswith("src-001.md") for r in rels)
    assert any(r.endswith("2026-01-01_note.md") for r in rels)
    assert "AgentResources.nosync/Skills/pricing-review/SKILL.md" in rels
    # ...and the secret is NOT (and was counted as excluded).
    assert not any(".env.nosync" in r for r in rels)
    assert manifest["secrets_excluded"] >= 1
    assert not (dest / ".env.nosync").exists()


def test_verified_copy_streams_source_reads(tmp_path, monkeypatch):
    """Regression guard: backup copies must not load the full file at once."""
    src = tmp_path / "large-ish.bin"
    dst = tmp_path / "out" / "large-ish.bin"
    src.write_bytes((b"0123456789abcdef" * 8192) + b"tail")

    real_open = builtins.open

    class BoundedReadOnly:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def read(self, size=-1):
            if size is None or size < 0:
                raise AssertionError("backup source read must be bounded")
            return self._wrapped.read(size)

        def __enter__(self):
            self._wrapped.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._wrapped.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def guarded_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if Path(path) == src and "r" in mode and "b" in mode:
            return BoundedReadOnly(handle)
        return handle

    monkeypatch.setattr(backup, "open", guarded_open, raising=False)

    copied_hash = backup._verified_copy(src, dst)
    assert copied_hash == backup.sha256_file(src)
    assert dst.read_bytes() == src.read_bytes()


def test_backup_restore_round_trip_hash_equal(tmp_path, minimal_oracle):
    """The core guarantee: backup -> restore is hash-equal end to end."""
    root = minimal_oracle(tmp_path)
    _seed_content(root)

    report = backup.verify_restore(root, tier="all")
    assert report["ok"] is True, report
    assert report["backed_up"] == report["restored"]
    assert report["backed_up"] > 0
    assert report["mismatches"] == []
    assert report["missing_after_restore"] == []


def test_verify_restore_populates_last_verified(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _seed_content(root)
    # No backup doc to start with on a minimal oracle.
    report = backup.verify_restore(root, tier="all")
    assert report["ok"] is True

    doc = root / "BACKUP-RECOVERY.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "last_verified_restore:" in text
    assert "hash-verified" in text


def test_verify_restore_detects_corruption(tmp_path, minimal_oracle, monkeypatch):
    """If a file mutates between backup and the live-source diff, ok is False."""
    root = minimal_oracle(tmp_path)
    _seed_content(root)

    real_run = backup.run
    target = root / "Memory.nosync" / "Sources" / "src-001.md"

    def run_then_corrupt(r, dest, *, tier="all"):
        manifest = real_run(r, dest, tier=tier)
        # Corrupt the LIVE source after it was backed up, so the three-way diff
        # (source == backup == restore) must fail.
        target.write_text("TAMPERED\n", encoding="utf-8")
        return manifest

    monkeypatch.setattr(backup, "run", run_then_corrupt)
    report = backup.verify_restore(root, tier="all")
    assert report["ok"] is False
    assert report["mismatches"]


# --------------------------------------------------------------------------- #
# migrations
# --------------------------------------------------------------------------- #
def test_migrations_discovered_in_order():
    found = migrations.discover()
    assert found, "expected at least the baseline migration"
    seqs = [s for s, _ in found]
    assert seqs == sorted(seqs)
    assert seqs[0] == 1
    assert any(name.endswith("kernel_version_stamp") for _, name in found)
    assert any(name.endswith("session_interfaces") for _, name in found)


def _strip_top_level_block(text: str, block_key: str) -> str:
    """Remove one top-level block-style YAML section from ``text``."""
    lines = text.splitlines()
    kept = []
    skipping = False
    target = f"{block_key}:"
    for line in lines:
        if line.strip() == target and (not line or not line[0].isspace()):
            skipping = True
            continue
        if skipping:
            if line and not line[0].isspace():
                skipping = False
            else:
                continue
        kept.append(line)
    return "\n".join(kept) + "\n"


def test_baseline_migration_stamps_version_idempotently(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    cfg = root / "oracle.yml"

    # Remove the kernel.tools_version so the migration has work to do. We blank
    # the version value in place (keeping it within the block-style subset).
    text = cfg.read_text(encoding="utf-8")
    text = text.replace('tools_version: "0.0.0-test"', 'tools_version: ""')
    cfg.write_text(text, encoding="utf-8")

    reports = migrations.apply_all(root)
    assert reports
    first = reports[0]
    assert first["seq"] == 1
    assert first["changed"] is True

    import oracle_yaml

    data = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["kernel"]["tools_version"]  # non-empty now

    # Idempotent: a second pass makes no change.
    reports2 = migrations.apply_all(root)
    assert reports2[0]["changed"] is False


def test_migration_handles_missing_kernel_block(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    cfg = root / "oracle.yml"
    # Strip the entire kernel: block (and its three child lines).
    cfg.write_text(
        _strip_top_level_block(cfg.read_text(encoding="utf-8"), "kernel"),
        encoding="utf-8",
    )

    import oracle_yaml

    before = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "kernel" not in before

    reports = migrations.apply_all(root)
    assert reports[0]["changed"] is True

    after = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert after["kernel"]["tools_version"]
    # Other top-level config survived untouched.
    assert after["company"]["name"] == before["company"]["name"]
    assert after["workproduct"]["routing_lanes"] == before["workproduct"]["routing_lanes"]


def test_session_interfaces_migration_inserts_missing_contract(
    tmp_path,
    minimal_oracle,
):
    root = minimal_oracle(tmp_path)
    cfg = root / "oracle.yml"
    cfg.write_text(
        _strip_top_level_block(cfg.read_text(encoding="utf-8"), "session_interfaces"),
        encoding="utf-8",
    )

    import oracle_yaml

    before = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "session_interfaces" not in before

    reports = migrations.apply_all(root)
    session_report = next(r for r in reports if r["migration"].endswith("session_interfaces"))
    assert session_report["seq"] == 2
    assert session_report["changed"] is True

    after = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert after["session_interfaces"]["default"] == "user"
    assert after["session_interfaces"]["startup_prompt"] is False
    user_mode = after["session_interfaces"]["modes"]["user"]
    assert "switch_commands" not in after["session_interfaces"]
    assert user_mode["control_plane_boundary"] == "prompt_for_admin_approval"
    assert "approve entering Admin mode" in user_mode["admin_prompt"]
    assert "change_architecture" in after["session_interfaces"]["modes"]["user"]["block_capabilities"]
    assert after["session_interfaces"]["modes"]["admin"]["role_gate"] == "policy.require_role"
    assert after["company"]["name"] == before["company"]["name"]

    reports2 = migrations.apply_all(root)
    session_report2 = next(r for r in reports2 if r["migration"].endswith("session_interfaces"))
    assert session_report2["changed"] is False


def test_session_interfaces_migration_updates_legacy_slash_contract(
    tmp_path,
    minimal_oracle,
):
    root = minimal_oracle(tmp_path)
    cfg = root / "oracle.yml"
    text = cfg.read_text(encoding="utf-8")
    text = text.replace(
        "  reset_policy: every_new_session\n",
        (
            "  reset_policy: every_new_session\n"
            "  switch_commands:\n"
            "    admin: \"/admin\"\n"
            "    user: \"/user\"\n"
        ),
    )
    text = text.replace(
        (
            "      control_plane_boundary: prompt_for_admin_approval\n"
            "      admin_prompt: \"This requires the Admin interface. Do you approve entering Admin mode for this request?\"\n"
        ),
        (
            "      control_plane_boundary: redirect_to_admin\n"
            "      redirect: \"That's an Admin-interface request. Type /admin to switch.\"\n"
        ),
    )
    cfg.write_text(text, encoding="utf-8")

    reports = migrations.apply_all(root)
    session_report = next(r for r in reports if r["migration"].endswith("session_interfaces"))
    assert session_report["changed"] is True

    import oracle_yaml

    after = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    session = after["session_interfaces"]
    assert "switch_commands" not in session
    assert session["modes"]["user"]["control_plane_boundary"] == "prompt_for_admin_approval"
    assert "approve entering Admin mode" in session["modes"]["user"]["admin_prompt"]


# --------------------------------------------------------------------------- #
# upgrade: tool-layer only, hash-verified, never headless
# --------------------------------------------------------------------------- #
def _install_baseline_tools(root: Path) -> None:
    """Give the minimal oracle a small _tools tree so upgrade has something to
    diff/replace (the conftest minimal oracle ships no _tools)."""
    t = root / "_tools"
    t.mkdir(parents=True, exist_ok=True)
    (t / "alpha.py").write_text("# alpha old\n", encoding="utf-8")
    (t / "beta.py").write_text("# beta old\n", encoding="utf-8")


def test_upgrade_check_reports_tool_changes(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _install_baseline_tools(root)
    bundle = _make_kernel_bundle(
        tmp_path,
        {
            "_tools/alpha.py": "# alpha new\n",   # changed
            "_tools/beta.py": "# beta old\n",     # unchanged
            "_tools/gamma.py": "# gamma new\n",  # added
        },
    )
    report = upgrade.check(root, bundle)
    assert report["ok"] is True
    assert report["tool_layer_only"] is True
    assert report["hash_verified"] is True
    assert "_tools/alpha.py" in report["changed"]
    assert "_tools/gamma.py" in report["added"]
    assert "_tools/alpha.py" not in report["added"]


def test_upgrade_refuses_non_tool_file_in_manifest(tmp_path, minimal_oracle):
    """A bundle whose manifest lists oracle.yml (sovereign) must be REFUSED."""
    root = minimal_oracle(tmp_path)
    _install_baseline_tools(root)
    bundle = tmp_path / "evil_kernel"
    (bundle / "_tools").mkdir(parents=True, exist_ok=True)
    (bundle / "_tools" / "alpha.py").write_text("# alpha\n", encoding="utf-8")
    # Sneak a sovereign file into the bundle + manifest.
    (bundle / "oracle.yml").write_text("company:\n  name: evil\n", encoding="utf-8")
    files = {
        "_tools/alpha.py": backup.sha256_file(bundle / "_tools" / "alpha.py"),
        "oracle.yml": backup.sha256_file(bundle / "oracle.yml"),
    }
    (bundle / ".kernel-manifest.json").write_text(
        json.dumps({"tools_version": "9", "files": files}), encoding="utf-8"
    )

    report = upgrade.check(root, bundle)
    assert report["ok"] is False
    assert report["refusal"] and "non-tool" in report["refusal"]

    # And apply must raise rather than touch anything.
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.apply(root, bundle, approve="admin")
    # oracle.yml is the REAL minimal one, never the evil bundle's.
    assert "evil" not in (root / "oracle.yml").read_text(encoding="utf-8")


def test_upgrade_refuses_hash_mismatch(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _install_baseline_tools(root)
    bundle = tmp_path / "tampered_kernel"
    (bundle / "_tools").mkdir(parents=True, exist_ok=True)
    f = bundle / "_tools" / "alpha.py"
    f.write_text("# real content\n", encoding="utf-8")
    # Manifest claims a DIFFERENT hash than the file actually has.
    (bundle / ".kernel-manifest.json").write_text(
        json.dumps({"tools_version": "9", "files": {"_tools/alpha.py": "deadbeef" * 8}}),
        encoding="utf-8",
    )
    report = upgrade.check(root, bundle)
    assert report["ok"] is False
    assert "mismatch" in (report["refusal"] or "")


def test_upgrade_refuses_without_approval(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _install_baseline_tools(root)
    bundle = _make_kernel_bundle(tmp_path, {"_tools/alpha.py": "# alpha new\n"})
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.apply(root, bundle, approve=None)
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.apply(root, bundle, approve="   ")


def test_upgrade_refuses_headless(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _install_baseline_tools(root)
    bundle = _make_kernel_bundle(tmp_path, {"_tools/alpha.py": "# alpha new\n"})
    monkeypatch.setenv("ORACLE_HEADLESS", "1")
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.apply(root, bundle, approve="admin")


def test_upgrade_apply_swaps_only_tools(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _install_baseline_tools(root)

    # Snapshot sovereign data/doctrine bytes BEFORE the upgrade.
    oracle_yml_before = (root / "oracle.yml").read_bytes()
    # Seed a memory note + meta marker to prove they survive byte-identical.
    note = root / "Memory.nosync" / "Sources" / "keep.md"
    note.write_text("important institutional memory\n", encoding="utf-8")
    note_before = note.read_bytes()

    bundle = _make_kernel_bundle(
        tmp_path,
        {
            "_tools/alpha.py": "# alpha UPGRADED\n",
            "_tools/beta.py": "# beta old\n",
        },
    )
    report = upgrade.apply(
        root, bundle, approve="admin", skip_tests=True, skip_lint=True
    )
    assert report["ok"] is True
    assert "_tools/alpha.py" in report["swapped"]

    # _tools WAS replaced.
    assert (root / "_tools" / "alpha.py").read_text(encoding="utf-8") == "# alpha UPGRADED\n"
    # sovereign data/doctrine UNTOUCHED (byte-identical).
    assert (root / "oracle.yml").read_bytes() == oracle_yml_before
    assert note.read_bytes() == note_before
    # the old tools were backed up.
    assert report["backup_dir"].startswith("Meta.nosync/tool-backups/")
    backup_alpha = root / report["backup_dir"] / "_tools" / "alpha.py"
    assert backup_alpha.exists()
    assert backup_alpha.read_text(encoding="utf-8") == "# alpha old\n"


def test_upgrade_apply_runs_migrations(tmp_path, minimal_oracle):
    """apply() runs ordered migrations post-swap; the baseline one is included."""
    root = minimal_oracle(tmp_path)
    _install_baseline_tools(root)
    # blank the version so the baseline migration has work.
    cfg = root / "oracle.yml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            'tools_version: "0.0.0-test"', 'tools_version: ""'
        ),
        encoding="utf-8",
    )
    bundle = _make_kernel_bundle(tmp_path, {"_tools/alpha.py": "# alpha new\n"})
    report = upgrade.apply(
        root, bundle, approve="admin", skip_tests=True, skip_lint=True
    )
    assert report["ok"] is True
    assert report["migrations"]
    assert report["migrations"][0]["seq"] == 1

    import oracle_yaml

    data = oracle_yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["kernel"]["tools_version"]


# --------------------------------------------------------------------------- #
# standing deliverables: answer-protocol gating drops exit-4 claims
# --------------------------------------------------------------------------- #
def test_standing_deliverable_drops_unauthorized_claims(tmp_path, minimal_oracle):
    """A contradiction whose object has NO truth-map authority (bootstrap-empty)
    returns exit 4 and must be DROPPED from the digest."""
    import standing_deliverables as sd

    root = minimal_oracle(tmp_path)
    # Seed one open contradiction touching an object with no truth-map row.
    cdir = root / "Memory.nosync" / "Contradictions"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "con-001.md").write_text(
        "---\n"
        "id: con-001\n"
        "type: contradiction\n"
        "title: Revenue figure disagreement\n"
        "business_object: Q3 revenue\n"
        "status: open\n"
        "severity: high\n"
        "---\n"
        "two sources disagree.\n",
        encoding="utf-8",
    )

    doc = sd.build_document(root, "contradiction-digest")
    # The minimal oracle has NO TRUTH-MAP.md, so every object refuses (exit 4)
    # and is dropped -- nothing ships.
    assert doc["dropped"] >= 1
    assert doc["dropped_items"][0]["object"] == "Q3 revenue"
    assert doc["shipped"] == []
    assert "Needs Authority Setup" in doc["body"]
    assert "`Q3 revenue`: no-authority-bootstrap" in doc["body"]


def test_standing_deliverable_emit_lands_in_standing(tmp_path, minimal_oracle):
    import standing_deliverables as sd

    root = minimal_oracle(tmp_path)
    report = sd.emit(root, "freshness-report")
    landed = root / report["path"]
    assert landed.exists()
    assert report["path"].startswith("Workproduct.nosync/_STANDING/")
    # a registry row was written.
    reg = root / "Workproduct.nosync" / "_STANDING" / ".registry.jsonl"
    assert reg.exists()
    import ledger

    rows, _ = ledger.load(reg)
    assert rows and rows[-1]["kind"] == "freshness-report"


def test_unknown_deliverable_kind_rejected(tmp_path, minimal_oracle):
    import standing_deliverables as sd

    root = minimal_oracle(tmp_path)
    with pytest.raises(ValueError):
        sd.build_document(root, "not-a-kind")
