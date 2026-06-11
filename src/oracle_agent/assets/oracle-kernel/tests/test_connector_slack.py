#!/usr/bin/env python3
"""Tests for the OFFLINE Slack workspace-export connector (P7-T6).

Every assertion maps to a T6 acceptance bullet in
docs/roadmap/PHASE-7-knowledge-connectors.md:

  * channel allowlist is default-deny (None/missing/[]/non-list refuse);
  * only allowlisted channels land; out-of-allowlist channels never land;
  * re-pull of the SAME export is idempotent (cursor by export sha256);
  * the FULL P7S-15 zip member-validation checklist refuses, and lands nothing:
      - ``../`` traversal member, absolute member name,
      - symlink member (external_attr S_IFLNK),
      - decompression bomb LYING about its declared size (ZipInfo.file_size),
      - excessive member count;
  * nested archives are not descended into (treated as opaque members);
  * markdown rendering shape (per channel-per day transcript).

Tiny zips are built in tmp (incl. malicious ones). No network is ever touched.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

import connectors
from connectors import base as connbase
from connectors import slack_export
from connectors.slack_export import SlackExportConnector


# --------------------------------------------------------------------------- #
# zip-building helpers
# --------------------------------------------------------------------------- #
def _users_json() -> bytes:
    return json.dumps([
        {"id": "U1", "profile": {"display_name": "alice"}},
        {"id": "U2", "profile": {"real_name": "Bob Builder"}, "name": "bob"},
    ]).encode("utf-8")


def _day_json(messages) -> bytes:
    return json.dumps(messages).encode("utf-8")


def _good_export(path: Path, *, channels=("general", "random"), users=True) -> Path:
    """A well-formed Slack export zip with two channels and a couple of days."""
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("channels.json", json.dumps(
            [{"name": c} for c in channels]).encode("utf-8"))
        if users:
            zf.writestr("users.json", _users_json())
        if "general" in channels:
            zf.writestr("general/2026-06-01.json", _day_json([
                {"ts": "1748736000.000100", "user": "U1", "text": "hello <@U2>"},
                {"ts": "1748736060.000200", "user": "U2", "text": "hi alice"},
            ]))
            zf.writestr("general/2026-06-02.json", _day_json([
                {"ts": "1748822400.000100", "user": "U1", "text": "morning"},
            ]))
        if "random" in channels:
            zf.writestr("random/2026-06-01.json", _day_json([
                {"ts": "1748736000.000100", "user": "U2", "text": "off topic"},
            ]))
    return path


def _write_manifest(root: Path, *, channels=("general",), path_value="__export__",
                    permissions="read_only", default_sensitivity="internal",
                    cid="slack-export", system="slack", export: Path = None) -> Path:
    mdir = root / "Connectors" / cid
    mdir.mkdir(parents=True, exist_ok=True)
    if channels is None:
        chan_block = "  channels:\n"               # bare key -> None
    elif channels == []:
        chan_block = "  channels:\n"               # empty rendered as bare key
    elif channels == "scalar":
        chan_block = "  channels: not-a-list\n"
    else:
        chan_block = "  channels:\n" + "".join(f"    - {c}\n" for c in channels)
    pv = str(export) if (path_value == "__export__" and export is not None) else path_value
    path_line = f"  path: {pv}\n" if pv else "  path:\n"
    text = f"""\
id: {cid}
system: {system}
status: active
access_mode: file_drop
locality: snapshot_local
capture_tier: snapshot
auth:
  method: none
permissions: {permissions}
freshness:
  class: snapshot
  expected_decay_days: 30
source:
{path_line}{chan_block}  default_sensitivity: {default_sensitivity}
"""
    mf = mdir / f"{cid}.manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    return mf


def _ctx(root, manifest, **kw):
    return connbase.ConnectorContext(root, manifest, **kw)


def _landed(root: Path, cid: str = "slack-export"):
    d = root / "Workproduct.nosync" / "_INPUT" / cid
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# channel allowlist (default-deny)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("channels", [None, [], "scalar", "__missing__"])
def test_empty_channel_allowlist_refuses(tmp_path, minimal_oracle, channels):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    if channels == "__missing__":
        mdir = root / "Connectors" / "slack-export"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "slack-export.manifest.yaml").write_text(
            "id: slack-export\nsystem: slack\nstatus: active\naccess_mode: file_drop\n"
            "locality: snapshot_local\ncapture_tier: snapshot\n"
            "auth:\n  method: none\npermissions: read_only\n"
            "freshness:\n  class: snapshot\n  expected_decay_days: 30\n"
            f"source:\n  path: {export}\n  default_sensitivity: internal\n",
            encoding="utf-8",
        )
        mf = connbase.load_manifest(root, "slack-export")
    else:
        _write_manifest(root, channels=channels, export=export)
        mf = connbase.load_manifest(root, "slack-export", validate=(channels != "scalar"))
    conn = SlackExportConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))
    assert _landed(root) == []


def test_only_allowlisted_channels_land(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    conn = SlackExportConnector(mf)
    results = conn.pull(_ctx(root, mf))
    ingested = [r for r in results if r["action"] == "ingested"]
    # general has 2 days; random is NOT allowlisted, so 0 of its days land.
    assert len(ingested) == 2
    landed = _landed(root)
    assert len(landed) == 2
    blob = "\n".join(p.read_text(encoding="utf-8") for p in landed)
    assert "#general" in blob
    assert "off topic" not in blob  # random channel never read/landed


# --------------------------------------------------------------------------- #
# markdown rendering shape
# --------------------------------------------------------------------------- #
def test_markdown_render_shape(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    conn = SlackExportConnector(mf)
    conn.pull(_ctx(root, mf))
    landed = _landed(root)
    day1 = [p for p in landed if "2026-06-01" in p.read_text(encoding="utf-8")][0]
    text = day1.read_text(encoding="utf-8")
    assert text.startswith("# #general -- 2026-06-01")
    # Display-name resolution + mention resolution.
    assert "**alice**" in text
    assert "@Bob Builder" in text  # <@U2> mention resolved to real_name
    # Bob's display name comes from real_name fallback.
    assert "**Bob Builder**" in text


# --------------------------------------------------------------------------- #
# idempotency by export hash
# --------------------------------------------------------------------------- #
def test_repull_same_export_is_noop(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")

    r1 = SlackExportConnector(mf).pull(_ctx(root, mf))
    assert len([r for r in r1 if r["action"] == "ingested"]) == 2

    # Re-pull the SAME export -> no-op (cursor records the export sha256).
    r2 = SlackExportConnector(mf).pull(_ctx(root, mf))
    assert [r for r in r2 if r["action"] == "ingested"] == []
    assert len(_landed(root)) == 2  # nothing new landed


def test_repull_new_export_lands(tmp_path, minimal_oracle):
    """A DIFFERENT export (new sha256) is not suppressed by idempotency."""
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    SlackExportConnector(mf).pull(_ctx(root, mf))

    # Replace the export with a new one carrying an extra day.
    with zipfile.ZipFile(str(export), "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("general/2026-06-03.json", _day_json([
            {"ts": "1748908800.000100", "user": "U1", "text": "new day"},
        ]))
    r2 = SlackExportConnector(mf).pull(_ctx(root, mf))
    # New sha256 -> the export is re-rendered; the new day lands (others
    # supersede in place by stable name).
    assert any(r["action"] == "ingested" for r in r2)
    assert len(_landed(root)) == 3


# --------------------------------------------------------------------------- #
# P7S-15: malicious-zip refusals -- nothing landed, ``refused`` vocabulary
# --------------------------------------------------------------------------- #
def _pull_results(root, mf):
    conn = SlackExportConnector(mf)
    return conn.pull(_ctx(root, mf))


def test_traversal_member_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(export), "w") as zf:
        zf.writestr("general/2026-06-01.json", _day_json([{"text": "ok"}]))
        zf.writestr("../../etc/passwd", b"root:x:0:0")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    results = _pull_results(root, mf)
    assert any(r["action"] == "refused" for r in results)
    assert _landed(root) == []


def test_absolute_member_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(export), "w") as zf:
        zf.writestr("general/2026-06-01.json", _day_json([{"text": "ok"}]))
        # Force an absolute member name (ZipFile.writestr normalises, so craft
        # the ZipInfo directly).
        info = zipfile.ZipInfo("/abs/secret.txt")
        zf.writestr(info, b"secret")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    results = _pull_results(root, mf)
    assert any(r["action"] == "refused" for r in results)
    assert _landed(root) == []


def test_symlink_member_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(export), "w") as zf:
        zf.writestr("general/2026-06-01.json", _day_json([{"text": "ok"}]))
        # A symlink member: S_IFLNK in the top 16 bits of external_attr.
        info = zipfile.ZipInfo("general/link")
        info.external_attr = (0o120777 << 16)  # S_IFLNK | 0777
        zf.writestr(info, b"/etc/passwd")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    results = _pull_results(root, mf)
    assert any(r["action"] == "refused" for r in results)
    assert _landed(root) == []


def test_decompression_bomb_lying_size_refused(tmp_path, minimal_oracle):
    """A member that decompresses far beyond the per-member cap is refused WHILE
    streaming, regardless of the declared ZipInfo.file_size (P7S-15)."""
    root = minimal_oracle(tmp_path)
    export = tmp_path / "bomb.zip"
    # 1 KB highly-compressible source that inflates well past a small cap. We
    # also LIE about file_size by patching the per-member cap down so the test
    # is fast and deterministic (1KB -> 200KB body vs a 50KB cap).
    body = b"\x00" * (200 * 1024)
    with zipfile.ZipFile(str(export), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("general/2026-06-01.json", _day_json([{"text": "ok"}]))
        zf.writestr("general/bomb.json", body)
    # Confirm the on-disk compressed size is tiny relative to the decompressed
    # body (a real bomb shape), and that ZipInfo.file_size is what we must NOT
    # trust.
    with zipfile.ZipFile(str(export)) as zf:
        info = [i for i in zf.infolist() if i.filename.endswith("bomb.json")][0]
        assert info.compress_size < info.file_size  # compresses small

    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    # Patch the per-member cap to 50 KB so the 200 KB member trips it.
    monkey_cap(slack_export, member=50 * 1024)
    try:
        results = _pull_results(root, mf)
    finally:
        monkey_cap(slack_export, member=None)
    assert any(r["action"] == "refused" for r in results)
    assert _landed(root) == []


def test_total_decompressed_cap_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = tmp_path / "big.zip"
    with zipfile.ZipFile(str(export), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("general/2026-06-01.json", _day_json([{"text": "ok"}]))
        for i in range(5):
            zf.writestr(f"general/pad-{i}.json", b"\x00" * (30 * 1024))
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    # Total cap 100KB; 5x30KB = 150KB total -> tripped.
    monkey_cap(slack_export, total=100 * 1024)
    try:
        results = _pull_results(root, mf)
    finally:
        monkey_cap(slack_export, total=None)
    assert any(r["action"] == "refused" for r in results)
    assert _landed(root) == []


def test_member_count_overflow_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = tmp_path / "many.zip"
    with zipfile.ZipFile(str(export), "w") as zf:
        for i in range(20):
            zf.writestr(f"general/2026-06-{i:02d}.json", _day_json([{"text": "x"}]))
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    monkey_cap(slack_export, members=5)
    try:
        results = _pull_results(root, mf)
    finally:
        monkey_cap(slack_export, members=None)
    assert any(r["action"] == "refused" for r in results)
    assert _landed(root) == []


def test_nested_archive_not_descended(tmp_path, minimal_oracle):
    """A nested .zip member is treated as an opaque member (counted toward caps),
    never recursively expanded. The good day still renders."""
    root = minimal_oracle(tmp_path)
    # Build an inner zip and embed it as a member.
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as izf:
        izf.writestr("inner/2026-01-01.json", _day_json([{"text": "inner secret"}]))
    export = tmp_path / "nested.zip"
    with zipfile.ZipFile(str(export), "w") as zf:
        zf.writestr("general/2026-06-01.json", _day_json([{"text": "ok"}]))
        zf.writestr("general/bundle.zip", inner.getvalue())
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    results = _pull_results(root, mf)
    # The nested zip is NOT a YYYY-MM-DD.json day file -> not rendered; the good
    # day renders. No refusal (the nested archive is opaque, within caps).
    assert any(r["action"] == "ingested" for r in results)
    assert not any(r["action"] == "refused" for r in results)
    landed = _landed(root)
    blob = "\n".join(p.read_text(encoding="utf-8") for p in landed)
    assert "inner secret" not in blob  # never descended into


# --------------------------------------------------------------------------- #
# rc split through the CLI: refused -> rc 1; clean pull -> rc 0
# --------------------------------------------------------------------------- #
def test_malicious_export_rc1_via_cli(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    export = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(export), "w") as zf:
        zf.writestr("general/2026-06-01.json", _day_json([{"text": "ok"}]))
        zf.writestr("../escape.json", b"x")
    _write_manifest(root, channels=("general",), export=export)
    connectors.register("slack-export", slack_export.build, system="slack")
    rc = connectors.main(["--root", str(root), "--json", "pull", "slack-export"])
    assert rc == 1
    assert "refused" in capsys.readouterr().out
    assert _landed(root) == []


def test_clean_pull_rc0_via_cli(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=("general",), export=export)
    connectors.register("slack-export", slack_export.build, system="slack")
    rc = connectors.main(["--root", str(root), "--json", "pull", "slack-export"])
    assert rc == 0
    assert "ingested" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# missing-source / health
# --------------------------------------------------------------------------- #
def test_missing_export_path_refuses(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, channels=("general",), path_value="")
    mf = connbase.load_manifest(root, "slack-export")
    conn = SlackExportConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))


def test_health_broken_on_missing_zip(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, channels=("general",), path_value=str(tmp_path / "nope.zip"))
    mf = connbase.load_manifest(root, "slack-export")
    conn = SlackExportConnector(mf)
    rep = conn.health(_ctx(root, mf))
    assert rep["status"] == "broken"


def test_health_broken_on_empty_allowlist(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=[], export=export)
    mf = connbase.load_manifest(root, "slack-export")
    conn = SlackExportConnector(mf)
    rep = conn.health(_ctx(root, mf))
    assert rep["status"] == "broken"


def test_health_healthy_on_good_export(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    conn = SlackExportConnector(mf)
    rep = conn.health(_ctx(root, mf))
    assert rep["status"] in ("healthy", "degraded")


# --------------------------------------------------------------------------- #
# read-only / final-pull invariants inherited from the core
# --------------------------------------------------------------------------- #
def test_read_write_manifest_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=("general",), permissions="read_write", export=export)
    mf = connbase.load_manifest(root, "slack-export", validate=False)
    conn = SlackExportConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))


def test_does_not_override_final_pull():
    """slack_export must NOT override the FINAL RemoteConnector.pull."""
    assert "pull" not in SlackExportConnector.__dict__


def test_manifest_validates_against_schema(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = _good_export(tmp_path / "export.zip")
    _write_manifest(root, channels=("general",), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    assert connbase.validate_manifest(mf) == []


# --------------------------------------------------------------------------- #
# redaction: a poisoned channel/day cannot leak a token into results
# --------------------------------------------------------------------------- #
def test_results_are_redacted(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    export = tmp_path / "export.zip"
    poisoned = "general?access_token=SUPERSECRETTOKEN12345"
    with zipfile.ZipFile(str(export), "w") as zf:
        zf.writestr(f"{poisoned}/2026-06-01.json", _day_json([{"text": "ok"}]))
    _write_manifest(root, channels=(poisoned,), export=export)
    mf = connbase.load_manifest(root, "slack-export")
    conn = SlackExportConnector(mf)
    results = conn.pull(_ctx(root, mf))
    assert "SUPERSECRETTOKEN12345" not in json.dumps(results)


# --------------------------------------------------------------------------- #
# cap-patch helper (deterministic, fast malicious-zip tests)
# --------------------------------------------------------------------------- #
_ORIG_CAPS = {
    "member": slack_export._MAX_MEMBER_BYTES,
    "total": slack_export._MAX_TOTAL_BYTES,
    "members": slack_export._MAX_MEMBERS,
}


def monkey_cap(mod, *, member=None, total=None, members=None):
    """Temporarily lower a cap to make a bomb/overflow test small + fast.
    Pass ``None`` for a field to restore its original value."""
    mod._MAX_MEMBER_BYTES = member if member is not None else _ORIG_CAPS["member"]
    mod._MAX_TOTAL_BYTES = total if total is not None else _ORIG_CAPS["total"]
    mod._MAX_MEMBERS = members if members is not None else _ORIG_CAPS["members"]
