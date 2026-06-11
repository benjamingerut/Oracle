#!/usr/bin/env python3
"""Tests for the RemoteConnector safety core (P7-T1).

Every assertion here maps to a T1 acceptance bullet in
docs/roadmap/PHASE-7-knowledge-connectors.md. The HTTP primitives are exercised
against a fake ``urllib`` opener (no real network), and a toy subclass drives
the FINAL ``pull`` template through every gate: scope allowlist, byte cap,
classification, policy, containment, the result vocabulary, redaction, the
cursor, and freshness-from-cursor.

The file is self-contained: conftest puts ``_tools`` on sys.path, builds a
minimal oracle root, and the toy subclass fetches bytes through a fake
``http_download`` so no socket is ever opened.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import connectors
from connectors import base as connbase
from connectors import remote
from connectors.remote import RemoteConnector, RemoteItem, redact, ByteCapExceeded, RedirectRefused


# --------------------------------------------------------------------------- #
# a toy subclass + manifest
# --------------------------------------------------------------------------- #
class ToyConnector(RemoteConnector):
    """Minimal RemoteConnector: items come from ``_items``; bytes come from a
    per-item ``_bodies`` map written to the stage via the real fetch path
    (http_download is monkeypatched per-test where the HTTP edge is exercised).
    """

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
        # Stage the item's body bytes into a private temp file (no network).
        import tempfile
        stage_dir = Path(tempfile.mkdtemp(prefix="oracle-toy-"))
        stage = stage_dir / "body"
        body = self._bodies.get(item.item_id, b"plain unmarked business content")
        # Write through the safe-paths-internal primitive shape (fixed temp path).
        fd = os.open(str(stage), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:  # safe_paths-internal: private temp stage
            f.write(body)
        return stage


def _write_manifest(root: Path, *, folder_ids=("FID1",), permissions="read_only",
                    default_sensitivity="internal", cid="toy", system="toy",
                    max_files=None) -> Path:
    mdir = root / "Connectors" / cid
    mdir.mkdir(parents=True, exist_ok=True)
    if folder_ids is None:
        fid_block = "  folder_ids:\n"          # bare key -> None
    elif folder_ids == []:
        fid_block = "  folder_ids:\n"          # empty list rendered as bare key
    elif folder_ids == "scalar":
        fid_block = "  folder_ids: not-a-list\n"
    else:
        fid_block = "  folder_ids:\n" + "".join(f"    - {f}\n" for f in folder_ids)
    mf_line = f"  max_files: {max_files}\n" if max_files else ""
    text = f"""\
id: {cid}
system: {system}
status: active
access_mode: api
locality: external_only
capture_tier: snapshot
auth:
  method: oauth
  vars:
    - TOY_TOKEN
permissions: {permissions}
freshness:
  class: api
  last_verified: "2026-01-01"
  expected_decay_days: 7
source:
{fid_block}{mf_line}  default_sensitivity: {default_sensitivity}
"""
    mf = mdir / f"{cid}.manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    return mf


def _ctx(root, manifest, **kw):
    return connbase.ConnectorContext(root, manifest, **kw)


def _item(item_id, name="doc.txt", body_id=None):
    return RemoteItem(item_id=item_id, name=name, modified="2026-06-01", size=-1, meta={})


# --------------------------------------------------------------------------- #
# scope allowlist (default-deny)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("folder_ids", [None, [], "scalar", "__missing__"])
def test_empty_allowlist_refuses(tmp_path, minimal_oracle, folder_ids):
    root = minimal_oracle(tmp_path)
    if folder_ids == "__missing__":
        # Manifest with NO folder_ids key at all.
        mdir = root / "Connectors" / "toy"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "toy.manifest.yaml").write_text(
            "id: toy\nsystem: toy\nstatus: active\naccess_mode: api\n"
            "locality: external_only\ncapture_tier: snapshot\n"
            "auth:\n  method: oauth\n  vars:\n    - TOY_TOKEN\n"
            "permissions: read_only\nfreshness:\n  class: api\n"
            '  last_verified: "2026-01-01"\n  expected_decay_days: 7\n'
            "source:\n  default_sensitivity: internal\n",
            encoding="utf-8",
        )
        mf = connbase.load_manifest(root, "toy")
    else:
        _write_manifest(root, folder_ids=folder_ids)
        # A non-list scalar is also caught by schema validation at load; load
        # with validate=False so the PULL-time default-deny guard is what
        # refuses (proving _assert_scope_allowlist refuses a non-list too).
        mf = connbase.load_manifest(root, "toy", validate=(folder_ids != "scalar"))
    conn = ToyConnector(mf, items=[_item("A")])
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))
    # Nothing landed.
    assert _landed(root, "toy") == []


# --------------------------------------------------------------------------- #
# schema layer (P7S-13): shape/typo detection only -- emptiness is pull-time
# --------------------------------------------------------------------------- #
def test_schema_tolerates_empty_source_values(tmp_path, minimal_oracle):
    """Empty/None source values VALIDATE at schema/lint time (the shipped
    localfolder template legitimately carries a bare ``path:``); the refusal
    happens at PULL time via the default-deny gate, not at lint."""
    root = minimal_oracle(tmp_path)
    # Bare keys -> None for path AND folder_ids; both must validate.
    mdir = root / "Connectors" / "toy"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "toy.manifest.yaml").write_text(
        "id: toy\nsystem: toy\nstatus: active\naccess_mode: api\n"
        "locality: external_only\ncapture_tier: snapshot\n"
        "auth:\n  method: oauth\n  vars:\n    - TOY_TOKEN\n"
        "permissions: read_only\nfreshness:\n  class: api\n"
        '  last_verified: "2026-01-01"\n  expected_decay_days: 7\n'
        "source:\n  path:\n  folder_ids:\n  default_sensitivity: internal\n",
        encoding="utf-8",
    )
    mf = connbase.load_manifest(root, "toy")  # validate=True: must NOT raise
    assert connbase.validate_manifest(mf) == []
    # ...but the pull-time gate still refuses the empty allowlist.
    conn = ToyConnector(mf, items=[_item("A")])
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))


def test_schema_rejects_typo_allowlist_key(tmp_path, minimal_oracle):
    """A typo'd allowlist key (folder_idz) FAILS schema validation instead of
    silently meaning 'missing' (P7S-13)."""
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mdir = root / "Connectors" / "toy"
    txt = (mdir / "toy.manifest.yaml").read_text(encoding="utf-8")
    txt = txt.replace("  folder_ids:\n", "  folder_idz:\n")
    (mdir / "toy.manifest.yaml").write_text(txt, encoding="utf-8")
    with pytest.raises(connbase.ConnectorError, match="folder_idz"):
        connbase.load_manifest(root, "toy")


def test_populated_allowlist_pulls_only_in_scope(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")
    in_scope = _item("A", "alpha.txt")
    out_scope = RemoteItem("B", "beta.txt", "2026-06-01", -1,
                           {"out_of_scope": True, "scope_reason": "shared from outside"})
    conn = ToyConnector(mf, items=[in_scope, out_scope])
    results = conn.pull(_ctx(root, mf))
    actions = {r["item_id"]: r["action"] for r in results}
    assert actions["A"] == "ingested"
    assert actions["B"] == "skipped_out_of_scope"
    assert len(_landed(root, "toy")) == 1


# --------------------------------------------------------------------------- #
# read-only / no-override
# --------------------------------------------------------------------------- #
def test_read_write_manifest_refused(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, permissions="read_write")
    mf = connbase.load_manifest(root, "toy", validate=False)
    conn = ToyConnector(mf, items=[_item("A")])
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))


def test_pull_is_final_not_overridable():
    """pull is the FINAL template method -- a subclass overriding it fails."""
    overriders = []
    for klass in RemoteConnector.__subclasses__():
        if "pull" in klass.__dict__:
            overriders.append(klass.__name__)
    assert overriders == [], (
        f"RemoteConnector.pull is FINAL; these subclasses override it: {overriders}"
    )


# --------------------------------------------------------------------------- #
# http_json: https-only + no redirects
# --------------------------------------------------------------------------- #
def test_http_json_refuses_http_url():
    with pytest.raises(connbase.ConnectorError):
        remote.http_json("GET", "http://api.example.com/x")


def test_http_json_3xx_raises(monkeypatch):
    import urllib.error

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 302, "Found",
                                         hdrs={"Location": "https://evil.example.com/"}, fp=io.BytesIO(b""))

    monkeypatch.setattr(remote.urllib.request, "build_opener", lambda *a: _Opener())
    with pytest.raises(RedirectRefused):
        remote.http_json("GET", "https://api.example.com/x")


# --------------------------------------------------------------------------- #
# http_download: redirect policy + Authorization strip + byte cap
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        pass


def _http_error(url, code, location=None):
    import urllib.error
    hdrs = {"Location": location} if location else {}
    return urllib.error.HTTPError(url, code, "redirect", hdrs=hdrs, fp=io.BytesIO(b""))


def test_http_download_cross_host_redirect_strips_auth(tmp_path, monkeypatch):
    seen_headers = {}

    class _Opener:
        def __init__(self):
            self.calls = 0

        def open(self, req, timeout=None):
            self.calls += 1
            if self.calls == 1:
                # first host: redirect to the enumerated download host
                raise _http_error(req.full_url, 302, "https://cdn.toy.example.com/blob")
            # second host: capture headers, return the body
            seen_headers.update(req.headers)
            return _FakeResp(b"the bytes")

    monkeypatch.setattr(remote.urllib.request, "build_opener", lambda *a: _Opener())
    stage = tmp_path / "stage"
    out = remote.http_download(
        "https://api.toysource.com/item",
        stage,
        headers={"Authorization": "Bearer abc123", "X-Keep": "1"},
        max_bytes=1024,
        allowed_host_suffixes=("toy.example.com",),
    )
    assert out.read_bytes() == b"the bytes"
    # Authorization stripped on the cross-host hop; other headers kept.
    norm = {k.lower(): v for k, v in seen_headers.items()}
    assert "authorization" not in norm
    assert norm.get("x-keep") == "1"


def test_http_download_non_enumerated_redirect_raises(tmp_path, monkeypatch):
    class _Opener:
        def open(self, req, timeout=None):
            raise _http_error(req.full_url, 302, "https://evil.example.net/blob")

    monkeypatch.setattr(remote.urllib.request, "build_opener", lambda *a: _Opener())
    with pytest.raises(RedirectRefused):
        remote.http_download("https://api.toysource.com/item", tmp_path / "s",
                             headers={}, max_bytes=1024,
                             allowed_host_suffixes=("toy.example.com",))
    assert not (tmp_path / "s").exists()


def test_http_download_aborts_midstream_regardless_of_content_length(tmp_path, monkeypatch):
    big = b"x" * 5000

    class _Opener:
        def open(self, req, timeout=None):
            # Content-Length lies (claims tiny) but the cap is enforced by bytes read.
            return _FakeResp(big)

    monkeypatch.setattr(remote.urllib.request, "build_opener", lambda *a: _Opener())
    stage = tmp_path / "s"
    with pytest.raises(ByteCapExceeded):
        remote.http_download("https://api.toy.example.com/item", stage,
                             headers={}, max_bytes=1000,
                             allowed_host_suffixes=("toy.example.com",))
    # The partial stage is cleaned up.
    assert not stage.exists()


def test_http_download_refuses_http():
    with pytest.raises(connbase.ConnectorError):
        remote.http_download("http://toy.example.com/x", "/tmp/x", headers={}, max_bytes=10)


# --------------------------------------------------------------------------- #
# gate-first: ZERO network on deny
# --------------------------------------------------------------------------- #
def test_gated_pull_zero_network_when_denied(tmp_path, minimal_oracle):
    """authorize-before-network: a gated pull with autonomy OFF performs no
    list_items / fetch_item and lands nothing (P7S-18)."""
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")

    touched = {"listed": False, "fetched": False}

    class _Spy(ToyConnector):
        def list_items(self, ctx):
            touched["listed"] = True
            return iter([])

        def fetch_item(self, ctx, item):
            touched["fetched"] = True
            raise AssertionError("must not fetch on deny")

    conn = _Spy(mf, items=[_item("A")])
    # autonomy OFF by default -> authorize denies before any network.
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf, gated=True))
    assert touched["listed"] is False
    assert touched["fetched"] is False
    assert _landed(root, "toy") == []


def test_gated_pull_kill_switch_zero_network(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")
    # Enable autonomy + allowlist, then engage the kill switch.
    _write_autonomy(root, enabled=True, allowed_loops=["connector-pull"],
                    writable_lanes=["_INPUT"], connectors=["toy"])
    ks = root / "Meta.nosync" / "Autonomy" / "KILL-SWITCH"
    ks.parent.mkdir(parents=True, exist_ok=True)
    ks.write_text("stop", encoding="utf-8")

    class _Spy(ToyConnector):
        def list_items(self, ctx):
            raise AssertionError("kill switch must short-circuit before list")

    conn = _Spy(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf, gated=True))
    assert _landed(root, "toy") == []


def test_gated_pull_runs_when_allowlisted(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # The cap-derived declared scope = the file cap itself (fail-closed); set a
    # small manifest max_files so the declared files (5) fit under the gate cap.
    _write_manifest(root, folder_ids=["FID1"], max_files=5)
    mf = connbase.load_manifest(root, "toy")
    _write_autonomy(root, enabled=True, allowed_loops=["connector-pull"],
                    writable_lanes=["_INPUT"], connectors=["toy"],
                    max_files_per_run=10, max_bytes=1_000_000)
    conn = ToyConnector(mf, items=[_item("A", "a.txt")])
    results = conn.pull(_ctx(root, mf, gated=True, role="admin"))
    assert any(r["action"] == "ingested" for r in results)


def test_connector_pull_not_in_deterministic_loops():
    """connector-pull is never a level-1 deterministic loop (P7S-20)."""
    import actions
    assert "connector-pull" not in actions.DETERMINISTIC_LOOPS


# --------------------------------------------------------------------------- #
# content classification at pull time (not filename)
# --------------------------------------------------------------------------- #
def test_content_classifies_up(tmp_path, minimal_oracle):
    """A file whose CONTENT (not name) carries restricted signals classifies up
    at pull time via classify_file (P7S-16)."""
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"], default_sensitivity="internal")
    mf = connbase.load_manifest(root, "toy")
    # Innocuous filename, restricted content (SSN-shaped).
    item = RemoteItem("A", "harmless-name.txt", "2026-06-01", -1, {})
    bodies = {"A": b"employee ssn 123-45-6789 on file"}
    conn = ToyConnector(mf, items=[item], bodies=bodies)
    results = conn.pull(_ctx(root, mf))
    ing = [r for r in results if r["action"] in ("ingested", "skipped_policy")][0]
    assert ing["sensitivity"] in ("restricted", "secret")


def test_policy_denied_skipped(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")
    import policy
    monkeypatch.setattr(policy, "check_processing", lambda s, e: "deny")
    conn = ToyConnector(mf, items=[_item("A")])
    results = conn.pull(_ctx(root, mf))
    assert results[0]["action"] == "skipped_policy"
    assert _landed(root, "toy") == []


# --------------------------------------------------------------------------- #
# stable landing names: re-pull reuses name; same-name items land separately
# --------------------------------------------------------------------------- #
def test_landing_name_stable_across_repull(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")

    item_v1 = RemoteItem("A", "report.txt", "2026-06-01", -1, {})
    conn = ToyConnector(mf, items=[item_v1], bodies={"A": b"v1 content"})
    r1 = conn.pull(_ctx(root, mf))
    name1 = Path(r1[0]["dst"]).name

    # A re-pull of the SAME item id (changed body) reuses the same landing name.
    item_v2 = RemoteItem("A", "report.txt", "2026-07-01", -1, {})
    conn2 = ToyConnector(mf, items=[item_v2], bodies={"A": b"v2 content changed"})
    r2 = conn2.pull(_ctx(root, mf))
    name2 = Path(r2[0]["dst"]).name
    assert name1 == name2
    landed = _landed(root, "toy")
    assert len(landed) == 1  # superseded, not duplicated
    assert landed[0].read_bytes() == b"v2 content changed"


def test_same_display_name_items_land_separately(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")
    a = RemoteItem("ID-A", "samename.txt", "2026-06-01", -1, {})
    b = RemoteItem("ID-B", "samename.txt", "2026-06-01", -1, {})
    conn = ToyConnector(mf, items=[a, b], bodies={"ID-A": b"aaa", "ID-B": b"bbb"})
    results = conn.pull(_ctx(root, mf))
    names = {Path(r["dst"]).name for r in results if r["action"] == "ingested"}
    assert len(names) == 2  # distinct landing names => no cross-item overwrite
    assert len(_landed(root, "toy")) == 2


def test_landed_paths_under_input(tmp_path, minimal_oracle):
    import safe_paths
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")
    conn = ToyConnector(mf, items=[_item("A", "x.txt")])
    results = conn.pull(_ctx(root, mf))
    dst = Path(results[0]["dst"])
    assert "_INPUT" in dst.parts and "Workproduct.nosync" in dst.parts
    base = Path(os.path.realpath(root / "Workproduct.nosync"))
    assert safe_paths.is_within(base, dst)


# --------------------------------------------------------------------------- #
# vocabulary / rc split: skipped_out_of_scope (rc 0) vs refused (rc 1)
# --------------------------------------------------------------------------- #
def test_out_of_scope_is_rc0_not_failure(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    out_scope = RemoteItem("B", "b.txt", "2026-06-01", -1,
                           {"out_of_scope": True, "scope_reason": "linked share"})
    connectors.register("toy", lambda m: ToyConnector(m, items=[out_scope]), system="toy")
    rc = connectors.main(["--root", str(root), "--json", "pull", "toy"])
    # skipped_out_of_scope is not a refusal -> rc 0.
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped_out_of_scope" in out


def test_containment_refusal_is_rc1(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    # A name that produces an unsafe landing (traversal in the display name).
    bad = RemoteItem("A", "../escape.txt", "2026-06-01", -1, {})
    # Force the dest computation to raise by monkeypatching the landing name to
    # include a traversal segment -- the safe_paths.contain gate refuses it.
    connectors.register("toy", lambda m: _BadDestConnector(m, items=[bad]), system="toy")
    rc = connectors.main(["--root", str(root), "--json", "pull", "toy"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "refused" in out


class _BadDestConnector(ToyConnector):
    def _landing_name(self, sp, item):
        return "../escape.txt"  # forces safe_paths.contain to refuse


# --------------------------------------------------------------------------- #
# redaction: every emitted string is redact()-clean given a poisoned URL
# --------------------------------------------------------------------------- #
def test_redact_strips_query_and_tokens():
    poisoned = "GET https://dl.example.com/file?token=SECRETabc123&sig=deadbeef Bearer ya29.A0ARrdaMxyz"
    out = redact(poisoned)
    assert "SECRETabc123" not in out
    assert "ya29.A0ARrdaMxyz" not in out
    assert "<redacted>" in out


def test_pull_results_are_redacted(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")
    # An item id carrying a pre-signed URL with a token in the query string.
    poisoned_id = "https://dl.example.com/x?access_token=SUPERSECRETTOKEN12345"
    item = RemoteItem(poisoned_id, "doc.txt", "2026-06-01", -1, {})
    conn = ToyConnector(mf, items=[item], bodies={poisoned_id: b"data"})
    results = conn.pull(_ctx(root, mf))
    blob = json.dumps(results)
    assert "SUPERSECRETTOKEN12345" not in blob


# --------------------------------------------------------------------------- #
# cursor: round-trip, torn write, freshness-from-cursor
# --------------------------------------------------------------------------- #
def test_cursor_round_trips(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    remote.save_cursor(root, "toy", {"last_success_ts": "2026-06-01T00:00:00", "n": 3})
    cur = remote.load_cursor(root, "toy")
    assert cur["n"] == 3


def test_torn_cursor_loads_empty_with_warning(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    cpath = root / "Connectors" / "toy"
    cpath.mkdir(parents=True, exist_ok=True)
    (cpath / "state.json").write_text("{not valid json", encoding="utf-8")
    with pytest.warns(UserWarning):
        cur = remote.load_cursor(root, "toy")
    assert cur == {}


def test_freshness_from_cursor(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    mf = connbase.load_manifest(root, "toy")
    conn = ToyConnector(mf, items=[_item("A")])
    # Before any pull: unknown.
    f0 = conn.freshness(_ctx(root, mf, now=datetime(2026, 6, 8)))
    assert f0["verdict"] == "unknown"
    # After a pull the cursor's last_success_ts drives a fresh verdict.
    conn.pull(_ctx(root, mf))
    f1 = conn.freshness(_ctx(root, mf, now=datetime.now()))
    assert f1["verdict"] == "fresh"
    assert f1["last_success_ts"]


def test_running_byte_counter_aborts_pull(tmp_path, minimal_oracle):
    """The cumulative landed-byte counter aborts the pull at max_bytes (runtime
    enforcement, P7S-17)."""
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FID1"])
    # Cap the per-pull bytes via source.max_bytes (direct pull ceiling).
    mdir = root / "Connectors" / "toy"
    txt = (mdir / "toy.manifest.yaml").read_text()
    txt = txt.replace("  default_sensitivity: internal\n",
                      "  max_bytes: 10\n  default_sensitivity: internal\n")
    (mdir / "toy.manifest.yaml").write_text(txt, encoding="utf-8")
    mf = connbase.load_manifest(root, "toy")
    a = RemoteItem("A", "a.txt", "2026-06-01", -1, {})
    b = RemoteItem("B", "b.txt", "2026-06-01", -1, {})
    conn = ToyConnector(mf, items=[a, b], bodies={"A": b"x" * 8, "B": b"y" * 8})
    results = conn.pull(_ctx(root, mf))
    actions_seen = [r["action"] for r in results]
    # First lands (8<=10); second pushes cumulative to 16 > 10 -> failed, abort.
    assert "ingested" in actions_seen
    assert "failed" in actions_seen


# --------------------------------------------------------------------------- #
# registry: id-only + system fallback
# --------------------------------------------------------------------------- #
def test_registry_id_only_and_system_fallback(tmp_path, minimal_oracle):
    # Register the toy class for system "toy".
    connectors.register("toy", ToyConnector, system="toy")
    root = minimal_oracle(tmp_path)
    # A SECOND account: distinct id, same system -> resolves via system fallback.
    _write_manifest(root, cid="toy-finance", system="toy", folder_ids=["FID9"])
    mf = connbase.load_manifest(root, "toy-finance")
    klass = connectors.get_connector_class(mf)
    assert klass is ToyConnector


def test_no_direct_urllib_in_subclass_modules():
    """A connector subclass module must not import urllib directly -- bytes go
    through http_download ONLY (P7S-8). Only remote.py owns urllib."""
    import ast
    cdir = Path(remote.__file__).parent
    offenders = []
    for p in sorted(cdir.glob("*.py")):
        if p.name in ("remote.py", "__init__.py", "base.py"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    if n.name == "urllib" or n.name.startswith("urllib."):
                        offenders.append(f"{p.name}: import {n.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("urllib"):
                    offenders.append(f"{p.name}: from {node.module}")
    assert offenders == [], f"subclass modules must not import urllib: {offenders}"


# --------------------------------------------------------------------------- #
# lint exemption enforcer (P7S-3): EXACTLY <root>/.env.nosync, nothing else
# --------------------------------------------------------------------------- #
def test_lint_exempts_exactly_env_nosync(tmp_path):
    import oracle_lint
    import secret_scan

    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    fake_token = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    # The sanctioned store: exempt.
    (root / ".env.nosync").write_text(f"TOY_TOKEN={fake_token}\n", encoding="utf-8")
    # Same token in any OTHER file (incl. a nested .env.nosync): flagged.
    (root / "sub" / ".env.nosync").write_text(f"X={fake_token}\n", encoding="utf-8")
    (root / "leak.txt").write_text(f"token {fake_token}\n", encoding="utf-8")

    out: list = []
    oracle_lint.check_secrets(root, out)
    flagged = {v.path.replace("\\", "/") for v in out}
    assert ".env.nosync" not in flagged           # the root store is exempt
    assert "sub/.env.nosync" in flagged            # nested is NOT exempt
    assert "leak.txt" in flagged                   # any other file is flagged


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _landed(root: Path, cid: str) -> list[Path]:
    d = root / "Workproduct.nosync" / "_INPUT" / cid
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*") if p.is_file())


def _write_autonomy(root: Path, *, enabled=True, allowed_loops=None,
                    writable_lanes=None, connectors=None,
                    max_files_per_run=10, max_bytes=1_000_000) -> None:
    allowed_loops = allowed_loops or []
    writable_lanes = writable_lanes or []
    connectors = connectors or []

    def _block(key, items):
        if not items:
            return f"{key}:\n"
        return f"{key}:\n" + "".join(f"  - {i}\n" for i in items)

    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    text = (
        f"enabled: {'true' if enabled else 'false'}\n"
        + _block("allowed_loops", allowed_loops)
        + _block("writable_lanes", writable_lanes)
        + _block("readonly_connectors", connectors)
        + "blast_radius_caps:\n"
        + f"  max_files_per_run: {max_files_per_run}\n"
        + f"  max_bytes: {max_bytes}\n"
        + 'kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"\n'
    )
    (d / "autonomy.yml").write_text(text, encoding="utf-8")
