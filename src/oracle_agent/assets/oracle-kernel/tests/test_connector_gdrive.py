#!/usr/bin/env python3
"""Tests for the Google Drive connector (P7-T2).

Every assertion maps to a T2 acceptance bullet in
docs/roadmap/PHASE-7-knowledge-connectors.md. The connector is exercised against
a FAKE Drive API mocked at the safety-core seam: we monkeypatch
``remote.http_json`` (metadata: token refresh, files.list, files.get) and
``remote.http_download`` (bytes: export + alt=media) so NO socket is ever opened
and NO real urllib request fires. Auth resolution is monkeypatched so no
.env.nosync secrets are needed (the unresolved-auth path is tested explicitly).

The connector module is imported DIRECTLY (the orchestrator wires the
connectors/__init__ import afterward; importing the module registers it via its
module-level register() call).

Acceptance coverage:
  * allowlist recursion -> only in-scope (incl. child-folder) files land;
  * a shared/shortcut file outside the allowlist -> skipped_out_of_scope (rc 0),
    never fetched;
  * an oversized native doc -> skipped with a result row, pull still completes;
  * the export matrix (docs/sheets/slides) routes to the right export mime;
  * a moved-in OLD file is caught by the periodic full re-list;
  * cursor (modifiedTime floors + token cache) persists across pulls;
  * the multi-parent rule -> a file under two parents lands once;
  * an expired access token refreshes once then is used;
  * health reports broken on unresolved auth vars.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import connectors
from connectors import base as connbase
from connectors import remote
from connectors import gdrive
from connectors.gdrive import GoogleDriveConnector


# --------------------------------------------------------------------------- #
# manifest builder
# --------------------------------------------------------------------------- #
def _write_manifest(root: Path, *, folder_ids=("FROOT",), permissions="read_only",
                    default_sensitivity="internal", cid="gdrive", system="gdrive",
                    max_bytes=None, max_files=None) -> Path:
    mdir = root / "Connectors" / cid
    mdir.mkdir(parents=True, exist_ok=True)
    if folder_ids is None or folder_ids == []:
        fid_block = "  folder_ids:\n"
    elif folder_ids == "scalar":
        fid_block = "  folder_ids: not-a-list\n"
    else:
        fid_block = "  folder_ids:\n" + "".join(f"    - {f}\n" for f in folder_ids)
    mb = f"  max_bytes: {max_bytes}\n" if max_bytes else ""
    mfl = f"  max_files: {max_files}\n" if max_files else ""
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
    - GDRIVE_CLIENT_ID
    - GDRIVE_CLIENT_SECRET
    - GDRIVE_REFRESH_TOKEN
permissions: {permissions}
freshness:
  class: api
  last_verified: "2026-01-01"
  expected_decay_days: 7
source:
{fid_block}{mb}{mfl}  default_sensitivity: {default_sensitivity}
"""
    mf = mdir / f"{cid}.manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    return mf


def _ctx(root, manifest, **kw):
    return connbase.ConnectorContext(root, manifest, **kw)


def _load(root, cid="gdrive", validate=True):
    return connbase.load_manifest(root, cid, validate=validate)


# --------------------------------------------------------------------------- #
# a fake Drive API installed at the http_json / http_download seam
# --------------------------------------------------------------------------- #
class FakeDrive:
    """Mock Drive backend. ``children`` maps folder_id -> list of file dicts
    (Drive files.list shape). ``meta`` maps file_id -> a files.get dict.
    ``bytes_for`` maps file_id -> body bytes (export or alt=media)."""

    def __init__(self, children=None, meta=None, bytes_for=None, oversize_ids=(),
                 token_response=None):
        self.children = children or {}
        self.meta = meta or {}
        self.bytes_for = bytes_for or {}
        self.oversize_ids = set(oversize_ids)
        self.token_response = token_response or {"access_token": "AT-1", "expires_in": 3600}
        self.token_refreshes = 0
        self.list_calls = []
        self.export_calls = []
        self.media_calls = []

    # -- http_json seam: token refresh, files.list, files.get -------------- #
    def http_json(self, method, url, *, headers=None, body=None, timeout=30, **kw):
        if url == gdrive._DRIVE_TOKEN_URL:
            self.token_refreshes += 1
            return dict(self.token_response)
        parsed = remote.urllib.parse.urlsplit(url)
        q = dict(remote.urllib.parse.parse_qsl(parsed.query))
        # files.list -> path is exactly /drive/v3/files
        if parsed.path.endswith("/drive/v3/files") and "q" in q:
            self.list_calls.append(q)
            return self._files_list(q)
        # files.get -> /drive/v3/files/<id>
        marker = "/drive/v3/files/"
        if marker in parsed.path:
            fid = parsed.path.split(marker, 1)[1]
            fid = remote.urllib.parse.unquote(fid)
            return dict(self.meta.get(fid, {"id": fid, "parents": []}))
        raise AssertionError(f"unexpected http_json url: {url}")

    def _files_list(self, q):
        # Extract the parent folder id from "'<id>' in parents and ..."
        query = q.get("q", "")
        folder_id = None
        if "' in parents" in query:
            folder_id = query.split("'", 2)[1]
        floor = None
        if "modifiedTime > '" in query:
            floor = query.split("modifiedTime > '", 1)[1].split("'", 1)[0]
        files = list(self.children.get(folder_id, []))
        if floor:
            files = [f for f in files if str(f.get("modifiedTime", "")) > floor]
        return {"files": files}

    # -- http_download seam: export + alt=media ---------------------------- #
    def http_download(self, url, dest_stage, *, headers=None, max_bytes,
                      allowed_host_suffixes=None, timeout=60):
        parsed = remote.urllib.parse.urlsplit(url)
        marker = "/drive/v3/files/"
        tail = parsed.path.split(marker, 1)[1]
        if tail.endswith("/export"):
            fid = remote.urllib.parse.unquote(tail[: -len("/export")])
            self.export_calls.append((fid, dict(remote.urllib.parse.parse_qsl(parsed.query))))
            if fid in self.oversize_ids:
                # Simulate an over-cap export: streaming abort.
                raise remote.ByteCapExceeded(f"export exceeded max_bytes={max_bytes}")
            body = self.bytes_for.get(fid, b"exported content")
        else:
            fid = remote.urllib.parse.unquote(tail)
            self.media_calls.append(fid)
            body = self.bytes_for.get(fid, b"binary content")
        if len(body) > int(max_bytes):
            raise remote.ByteCapExceeded(f"body exceeds max_bytes={max_bytes}")
        dest = Path(dest_stage)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest


def _install(monkeypatch, fake: FakeDrive):
    monkeypatch.setattr(remote, "http_json", fake.http_json)
    monkeypatch.setattr(remote, "http_download", fake.http_download)
    # Auth resolves to fake creds (no .env.nosync needed).
    monkeypatch.setattr(remote, "resolve_auth", lambda root, mf: {
        "GDRIVE_CLIENT_ID": "cid", "GDRIVE_CLIENT_SECRET": "csec",
        "GDRIVE_REFRESH_TOKEN": "rtok",
    })


def _file(fid, name, parents, mime="text/plain", modified="2026-06-01T00:00:00Z", size="10"):
    return {"id": fid, "name": name, "mimeType": mime, "modifiedTime": modified,
            "size": size, "parents": parents}


def _landed(root, cid="gdrive"):
    d = root / "Workproduct.nosync" / "_INPUT" / cid
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# allowlist recursion: only in-scope files (incl. child folders) land
# --------------------------------------------------------------------------- #
def test_allowlist_recursion_lands_only_in_scope(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    # FROOT contains a file + a child folder FSUB; FSUB contains a nested file.
    # An UNRELATED folder FOUT (not allowlisted) is never queried.
    fake = FakeDrive(children={
        "FROOT": [
            _file("f1", "top.txt", ["FROOT"]),
            _file("FSUB", "subfolder", ["FROOT"], mime=gdrive._GOOGLE_FOLDER),
        ],
        "FSUB": [_file("f2", "nested.txt", ["FSUB"])],
        "FOUT": [_file("f9", "secret.txt", ["FOUT"])],
    }, bytes_for={"f1": b"top body", "f2": b"nested body"})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    results = conn.pull(_ctx(root, mf))
    ingested = {r["item_id"] for r in results if r["action"] == "ingested"}
    assert ingested == {"f1", "f2"}
    assert len(_landed(root)) == 2
    # FOUT was never listed (not reachable from the allowlist).
    assert all("FOUT" not in c.get("q", "") for c in fake.list_calls)


# --------------------------------------------------------------------------- #
# out-of-scope shortcut -> skipped_out_of_scope (rc 0), never fetched
# --------------------------------------------------------------------------- #
def test_out_of_scope_shortcut_skipped_never_fetched(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    # FROOT holds a shortcut whose TARGET lives under FOUT (out of allowlist).
    shortcut = _file("sc1", "link", ["FROOT"], mime=gdrive._GOOGLE_SHORTCUT)
    shortcut["shortcutDetails"] = {"targetId": "tgt", "targetMimeType": "text/plain"}
    fake = FakeDrive(
        children={"FROOT": [shortcut]},
        meta={"tgt": {"id": "tgt", "name": "target.txt", "mimeType": "text/plain",
                      "parents": ["FOUT"]}},
        bytes_for={"tgt": b"should never be fetched"},
    )
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    results = conn.pull(_ctx(root, mf))
    actions = [r["action"] for r in results]
    assert "skipped_out_of_scope" in actions
    assert "ingested" not in actions
    assert fake.media_calls == [] and fake.export_calls == []   # never fetched
    assert _landed(root) == []


def test_in_scope_shortcut_target_is_pulled(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    shortcut = _file("sc1", "link", ["FROOT"], mime=gdrive._GOOGLE_SHORTCUT)
    shortcut["shortcutDetails"] = {"targetId": "tgt", "targetMimeType": "text/plain"}
    fake = FakeDrive(
        children={"FROOT": [shortcut]},
        meta={"tgt": {"id": "tgt", "name": "target.txt", "mimeType": "text/plain",
                      "parents": ["FROOT"], "size": "5"}},
        bytes_for={"tgt": b"hello"},
    )
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    results = conn.pull(_ctx(root, mf))
    ingested = [r for r in results if r["action"] == "ingested"]
    assert len(ingested) == 1 and ingested[0]["item_id"] == "tgt"


# --------------------------------------------------------------------------- #
# export matrix: docs/sheets/slides route to the right export mime
# --------------------------------------------------------------------------- #
def test_export_matrix_routes_correct_mimes(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    fake = FakeDrive(children={"FROOT": [
        _file("d", "doc", ["FROOT"], mime=gdrive._GOOGLE_DOC, size=None),
        _file("s", "sheet", ["FROOT"], mime=gdrive._GOOGLE_SHEET, size=None),
        _file("p", "deck", ["FROOT"], mime=gdrive._GOOGLE_SLIDES, size=None),
    ]}, bytes_for={"d": b"docx", "s": b"a,b,c", "p": b"slide text"})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    results = conn.pull(_ctx(root, mf))
    assert sum(1 for r in results if r["action"] == "ingested") == 3
    # Each export carried the matrix's target mime.
    by_id = {fid: params.get("mimeType") for fid, params in fake.export_calls}
    assert by_id["d"] == gdrive._EXPORT_MATRIX[gdrive._GOOGLE_DOC][0]
    assert by_id["s"] == "text/csv"
    assert by_id["p"] == "text/plain"
    # Landing names carry the matrix suffix.
    names = {p.name for p in _landed(root)}
    assert any(n.endswith(".docx") for n in names)
    assert any(n.endswith(".csv") for n in names)
    assert any(n.endswith(".txt") for n in names)


# --------------------------------------------------------------------------- #
# oversized native doc -> skipped with a result row; pull still completes
# --------------------------------------------------------------------------- #
def test_oversized_native_doc_skipped_with_row(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    fake = FakeDrive(children={"FROOT": [
        _file("big", "huge-doc", ["FROOT"], mime=gdrive._GOOGLE_DOC, size=None),
        _file("ok", "small.txt", ["FROOT"]),
    ]}, bytes_for={"ok": b"fine"}, oversize_ids=["big"])
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    results = conn.pull(_ctx(root, mf))
    by_id = {r["item_id"]: r for r in results}
    # The oversized doc produced a result row (failed/skip), never landed...
    assert "big" in by_id
    assert by_id["big"]["action"] in ("failed", "skipped_policy", "skipped_out_of_scope")
    big_names = {p.name for p in _landed(root)}
    assert not any(n.startswith("") and "huge" in n for n in big_names)
    # ...and the OTHER in-scope file still landed (pull completed; rc not a
    # pull-wide failure).
    assert by_id["ok"]["action"] == "ingested"
    assert any(r["action"] == "ingested" for r in results)


# --------------------------------------------------------------------------- #
# moved-in OLD file caught by the periodic full re-list (P7S-11)
# --------------------------------------------------------------------------- #
def test_moved_in_old_file_caught_by_full_relist(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    # First pull lands a recent file and advances the modifiedTime floor.
    fresh = _file("recent", "recent.txt", ["FROOT"], modified="2026-06-01T00:00:00Z")
    fake = FakeDrive(children={"FROOT": [fresh]}, bytes_for={"recent": b"r"})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    conn.pull(_ctx(root, mf))
    # An OLD file (timestamp BEFORE the floor) is moved into FROOT. An
    # incremental list (modifiedTime > floor) would MISS it. Force a full
    # re-list by setting the counter to the threshold.
    cur = remote.load_cursor(root, "gdrive")
    cur["pulls_since_full_relist"] = gdrive._FULL_RELIST_EVERY - 1
    remote.save_cursor(root, "gdrive", cur)
    old = _file("oldmoved", "old.txt", ["FROOT"], modified="2020-01-01T00:00:00Z")
    fake2 = FakeDrive(children={"FROOT": [fresh, old]},
                      bytes_for={"recent": b"r", "oldmoved": b"o"})
    _install(monkeypatch, fake2)
    conn2 = GoogleDriveConnector(mf)
    results = conn2.pull(_ctx(root, mf))
    ingested = {r["item_id"] for r in results if r["action"] == "ingested"}
    # The full re-list ignored the floor and caught the moved-in old file.
    assert "oldmoved" in ingested
    # The list query for the full re-list carried NO modifiedTime floor.
    assert any("modifiedTime >" not in c.get("q", "") for c in fake2.list_calls)


def test_incremental_list_uses_modifiedtime_floor(tmp_path, minimal_oracle, monkeypatch):
    """A normal (non-full-relist) pull queries with modifiedTime > floor."""
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    f = _file("a", "a.txt", ["FROOT"])
    fake = FakeDrive(children={"FROOT": [f]}, bytes_for={"a": b"a"})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    conn.pull(_ctx(root, mf))  # advances floor
    # Second pull: incremental -> the query carries a modifiedTime floor.
    fake2 = FakeDrive(children={"FROOT": [f]}, bytes_for={"a": b"a"})
    _install(monkeypatch, fake2)
    GoogleDriveConnector(mf).pull(_ctx(root, mf))
    assert any("modifiedTime >" in c.get("q", "") for c in fake2.list_calls)


# --------------------------------------------------------------------------- #
# multi-parent file lands ONCE
# --------------------------------------------------------------------------- #
def test_multi_parent_file_lands_once(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FA", "FB"])
    mf = _load(root)
    # The SAME file id is returned as a child of BOTH allowlisted folders.
    shared = _file("dup", "shared.txt", ["FA", "FB"])
    fake = FakeDrive(children={"FA": [shared], "FB": [shared]},
                     bytes_for={"dup": b"shared body"})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    results = conn.pull(_ctx(root, mf))
    ingested = [r for r in results if r["action"] == "ingested" and r["item_id"] == "dup"]
    assert len(ingested) == 1
    assert len(_landed(root)) == 1


# --------------------------------------------------------------------------- #
# token: cache + single refresh, expired -> refresh once then used
# --------------------------------------------------------------------------- #
def test_token_refreshes_once_and_is_cached(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    fake = FakeDrive(children={"FROOT": [
        _file("a", "a.txt", ["FROOT"]), _file("b", "b.txt", ["FROOT"]),
    ]}, bytes_for={"a": b"a", "b": b"b"})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    conn.pull(_ctx(root, mf))
    # ONE refresh for the whole pull (token cached on the instance + cursor).
    assert fake.token_refreshes == 1
    # The cursor cached the access token + expiry.
    cur = remote.load_cursor(root, "gdrive")
    assert cur.get("access_token") == "AT-1"
    assert cur.get("access_token_expires_at")


def test_expired_token_refreshes_then_used(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    # Pre-seed an EXPIRED cached token in the cursor.
    remote.save_cursor(root, "gdrive", {
        "access_token": "STALE", "access_token_expires_at": 1.0,
    })
    fake = FakeDrive(children={"FROOT": [_file("a", "a.txt", ["FROOT"])]},
                     bytes_for={"a": b"a"},
                     token_response={"access_token": "FRESH", "expires_in": 3600})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    results = conn.pull(_ctx(root, mf))
    assert fake.token_refreshes == 1                 # refreshed once
    assert any(r["action"] == "ingested" for r in results)
    cur = remote.load_cursor(root, "gdrive")
    assert cur.get("access_token") == "FRESH"        # fresh token cached + used


def test_rotated_refresh_token_persisted(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    fake = FakeDrive(children={"FROOT": [_file("a", "a.txt", ["FROOT"])]},
                     bytes_for={"a": b"a"},
                     token_response={"access_token": "AT", "expires_in": 3600,
                                     "refresh_token": "ROTATED-RT"})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    conn.pull(_ctx(root, mf))
    # The rotated refresh token was upserted to .env.nosync via the one
    # sanctioned writer (NOT the cursor, NOT a result row).
    env = (root / ".env.nosync").read_text(encoding="utf-8")
    assert "GDRIVE_REFRESH_TOKEN=ROTATED-RT" in env
    assert oct((root / ".env.nosync").stat().st_mode)[-3:] == "600"


# --------------------------------------------------------------------------- #
# cursor persistence: modifiedTime floors survive across pulls
# --------------------------------------------------------------------------- #
def test_cursor_persists_floors_and_relist_counter(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    fake = FakeDrive(children={"FROOT": [_file("a", "a.txt", ["FROOT"])]},
                     bytes_for={"a": b"a"})
    _install(monkeypatch, fake)
    GoogleDriveConnector(mf).pull(_ctx(root, mf))
    cur = remote.load_cursor(root, "gdrive")
    assert "FROOT" in (cur.get("folder_modified_floors") or {})
    assert isinstance(cur.get("pulls_since_full_relist"), int)
    assert cur.get("last_success_ts")    # the core advanced freshness


# --------------------------------------------------------------------------- #
# health: broken on unresolved auth vars
# --------------------------------------------------------------------------- #
def test_health_broken_on_unresolved_auth(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    # resolve_auth raises (no creds anywhere).
    def _boom(r, m):
        raise connbase.ConnectorError("unresolved auth vars: GDRIVE_CLIENT_ID")
    monkeypatch.setattr(remote, "resolve_auth", _boom)
    conn = GoogleDriveConnector(mf)
    report = conn.health(_ctx(root, mf))
    assert report["status"] == "broken"
    assert any(".env.nosync" in n for n in report["notes"])


def test_health_broken_on_empty_allowlist(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=None)
    mf = _load(root)
    monkeypatch.setattr(remote, "resolve_auth", lambda r, m: {
        "GDRIVE_CLIENT_ID": "x", "GDRIVE_CLIENT_SECRET": "y", "GDRIVE_REFRESH_TOKEN": "z"})
    conn = GoogleDriveConnector(mf)
    report = conn.health(_ctx(root, mf))
    assert report["status"] == "broken"


def test_health_broken_on_read_write(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"], permissions="read_write")
    mf = _load(root, validate=False)
    conn = GoogleDriveConnector(mf)
    report = conn.health(_ctx(root, mf))
    assert report["status"] == "broken"


def test_healthy_when_configured(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    monkeypatch.setattr(remote, "resolve_auth", lambda r, m: {
        "GDRIVE_CLIENT_ID": "x", "GDRIVE_CLIENT_SECRET": "y", "GDRIVE_REFRESH_TOKEN": "z"})
    conn = GoogleDriveConnector(mf)
    report = conn.health(_ctx(root, mf))
    assert report["status"] in ("healthy", "degraded")


# --------------------------------------------------------------------------- #
# scope allowlist default-deny (the core's gate, exercised through gdrive)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("folder_ids", [None, [], "scalar"])
def test_empty_allowlist_refuses_pull(tmp_path, minimal_oracle, monkeypatch, folder_ids):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=folder_ids)
    mf = _load(root, validate=(folder_ids != "scalar"))
    monkeypatch.setattr(remote, "resolve_auth", lambda r, m: {
        "GDRIVE_CLIENT_ID": "x", "GDRIVE_CLIENT_SECRET": "y", "GDRIVE_REFRESH_TOKEN": "z"})
    conn = GoogleDriveConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))
    assert _landed(root) == []


# --------------------------------------------------------------------------- #
# redaction: a poisoned item id never leaks into results
# --------------------------------------------------------------------------- #
def test_results_are_redacted(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folder_ids=["FROOT"])
    mf = _load(root)
    poisoned = "https://dl.example.com/x?access_token=SUPERSECRETTOKEN12345"
    fake = FakeDrive(children={"FROOT": [_file(poisoned, "doc.txt", ["FROOT"])]},
                     bytes_for={poisoned: b"data"})
    _install(monkeypatch, fake)
    conn = GoogleDriveConnector(mf)
    results = conn.pull(_ctx(root, mf))
    assert "SUPERSECRETTOKEN12345" not in json.dumps(results)


# --------------------------------------------------------------------------- #
# registration: id-only + system fallback (P7S-6)
# --------------------------------------------------------------------------- #
def test_registered_id_only_and_system_fallback(tmp_path, minimal_oracle):
    assert connectors.REGISTRY.get("gdrive") is gdrive.build
    assert connectors.SYSTEM_FACTORIES.get("gdrive") is gdrive.build
    root = minimal_oracle(tmp_path)
    # A SECOND account: distinct id, same system -> resolves via system fallback.
    _write_manifest(root, cid="gdrive-finance", system="gdrive", folder_ids=["FX"])
    mf = _load(root, cid="gdrive-finance")
    klass = connectors.get_connector_class(mf)
    assert klass is gdrive.build


def test_pull_is_not_overridden():
    """gdrive must NOT override the FINAL core pull template (P7S-... / T1)."""
    assert "pull" not in GoogleDriveConnector.__dict__
