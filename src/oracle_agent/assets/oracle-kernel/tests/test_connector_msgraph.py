#!/usr/bin/env python3
"""Tests for the Microsoft Graph connector (P7-T3).

Every assertion maps to a T3 acceptance bullet in
docs/roadmap/PHASE-7-knowledge-connectors.md:

  * delta response items outside the allowlisted site/drive are skipped
    out-of-scope per item;
  * the delta link survives a restart (persisted in the cursor, resumed next
    pull);
  * a 410 Gone on a delta link triggers reset + full resync -- one ledger-visible
    result note, not a crash, not a silent skip;
  * a rotated refresh token in a token response lands in .env.nosync atomically
    at 0o600 and is used on the next refresh;
  * throttling (429 + Retry-After) backs off bounded;
  * the site/drive allowlist is default-deny (None/missing/[]/non-list refuse);
  * /content downloads go through http_download (the 302 enumerated-host hop).

The connector is driven entirely against FAKE ``http_json`` / ``http_download``
seams -- no socket is ever opened. The base ``RemoteConnector.pull`` template is
exercised unchanged.
"""
from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from connectors import base as connbase
from connectors import remote
from connectors import msgraph
from connectors.remote import RemoteItem
from connectors.msgraph import MicrosoftGraphConnector


# --------------------------------------------------------------------------- #
# manifest + ctx helpers
# --------------------------------------------------------------------------- #
def _write_manifest(root: Path, *, sites=None, drives=None, permissions="read_only",
                    default_sensitivity="internal", cid="msgraph", system="msgraph",
                    max_files=None, max_bytes=None,
                    client_id_var="MSGRAPH_CLIENT_ID",
                    refresh_var="MSGRAPH_REFRESH_TOKEN") -> Path:
    mdir = root / "Connectors" / cid
    mdir.mkdir(parents=True, exist_ok=True)

    def _list_block(key, vals):
        if vals is None:
            return f"  {key}:\n"            # bare key -> None
        if vals == "scalar":
            return f"  {key}: not-a-list\n"
        if vals == []:
            return f"  {key}:\n"
        return f"  {key}:\n" + "".join(f"    - {v}\n" for v in vals)

    src_lines = ""
    if sites is not None or sites == []:
        src_lines += _list_block("sites", sites)
    if drives is not None or drives == []:
        src_lines += _list_block("drives", drives)
    if max_files:
        src_lines += f"  max_files: {max_files}\n"
    if max_bytes:
        src_lines += f"  max_bytes: {max_bytes}\n"
    src_lines += f"  default_sensitivity: {default_sensitivity}\n"

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
    - {client_id_var}
    - {refresh_var}
permissions: {permissions}
freshness:
  class: api
  last_verified: "2026-01-01"
  expected_decay_days: 7
source:
{src_lines}"""
    mf = mdir / f"{cid}.manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    return mf


def _write_env(root: Path, **kv) -> None:
    lines = "\n".join(f"{k}={v}" for k, v in kv.items()) + "\n"
    (root / ".env.nosync").write_text(lines, encoding="utf-8")


def _ctx(root, manifest, **kw):
    return connbase.ConnectorContext(root, manifest, **kw)


def _landed(root: Path, cid="msgraph") -> list:
    d = root / "Workproduct.nosync" / "_INPUT" / cid
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*") if p.is_file())


def _resync_ledger_rows(root: Path) -> int:
    """Count the delta-link-410 resync notes in the failure-event ledger."""
    led = root / "Meta.nosync" / "ledgers" / "failure_event.jsonl"
    if not led.exists():
        return 0
    import json as _json
    n = 0
    for line in led.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = _json.loads(line)
        except ValueError:
            continue
        if row.get("failure_mode") == "delta-link-410-resync":
            n += 1
    return n


# --------------------------------------------------------------------------- #
# fake Graph: a programmable http_json + http_download seam
# --------------------------------------------------------------------------- #
class FakeGraph:
    """Programmable fake for remote.http_json / remote.http_download.

    ``json_routes`` maps a URL substring -> a response dict OR a list of
    responses (consumed in order) OR a callable(url, body, headers) -> dict.
    A response may be an Exception instance/class to raise (simulating 410/429).
    ``downloads`` maps a URL substring -> bytes to write to the stage.
    """

    def __init__(self):
        self.json_routes = {}
        self.downloads = {}
        self.token_responses = []      # consumed in order for the /token URL
        self.json_calls = []
        self.download_calls = []

    # -- the patched http_json -------------------------------------------------
    def http_json(self, method, url, *, headers=None, body=None, timeout=30, _max_retries=3):
        self.json_calls.append((method, url, body))
        if "/oauth2/v2.0/token" in url:
            if not self.token_responses:
                return {"access_token": "AT-default"}
            r = self.token_responses.pop(0)
            return _maybe_raise(r)
        for frag, resp in self.json_routes.items():
            if frag in url:
                if isinstance(resp, list):
                    r = resp.pop(0) if resp else {}
                elif callable(resp):
                    r = resp(url, body, headers)
                else:
                    r = resp
                return _maybe_raise(r)
        return {}

    # -- the patched http_download --------------------------------------------
    def http_download(self, url, dest_stage, *, headers=None, max_bytes,
                      allowed_host_suffixes=None, timeout=60):
        self.download_calls.append((url, dict(headers or {}), allowed_host_suffixes))
        body = b"file body bytes"
        for frag, data in self.downloads.items():
            if frag in url:
                body = data
                break
        dest = Path(dest_stage)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        return dest


def _maybe_raise(r):
    if isinstance(r, BaseException):
        raise r
    if isinstance(r, type) and issubclass(r, BaseException):
        raise r()
    return r


@pytest.fixture
def patched(monkeypatch):
    fake = FakeGraph()
    # Patch the names the msgraph module bound at import time.
    monkeypatch.setattr(msgraph, "http_json", fake.http_json)
    monkeypatch.setattr(msgraph, "http_download", fake.http_download)
    return fake


# --------------------------------------------------------------------------- #
# default-deny allowlist (sites/drives)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sites,drives", [
    (None, None),          # both bare -> None
    ([], []),              # both empty
    ("scalar", None),      # non-list
])
def test_allowlist_default_deny(tmp_path, minimal_oracle, patched, sites, drives):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, sites=sites, drives=drives)
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    # scalar fails schema; load with validate off so the PULL-time gate refuses.
    mf = connbase.load_manifest(root, "msgraph", validate=(sites != "scalar"))
    conn = MicrosoftGraphConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))
    assert _landed(root) == []
    # Default-deny refuses BEFORE any Graph call.
    assert fake_no_graph_calls(patched)


def fake_no_graph_calls(fake) -> bool:
    return all("/oauth2/v2.0/token" in u for (_m, u, _b) in fake.json_calls)


# --------------------------------------------------------------------------- #
# in-scope pull + per-item out-of-scope skip
# --------------------------------------------------------------------------- #
def _delta_page(items, *, delta_link="https://graph.microsoft.com/v1.0/drives/D1/root/delta?token=NEW"):
    return {"value": items, "@odata.deltaLink": delta_link}


def _file_entry(item_id, name, drive_id="D1", parent_drive=None, download_url=None, size=10):
    e = {
        "id": item_id,
        "name": name,
        "size": size,
        "lastModifiedDateTime": "2026-06-01T00:00:00Z",
        "file": {"mimeType": "text/plain"},
        "parentReference": {"driveId": parent_drive or drive_id},
        "webUrl": f"https://contoso.sharepoint.com/{name}?guest=1",
    }
    if download_url:
        e["@microsoft.graph.downloadUrl"] = download_url
    return e


def test_in_scope_pull_and_out_of_scope_skip(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")

    in_scope = _file_entry("ITEM-A", "alpha.txt", drive_id="D1",
                           download_url="https://contoso.sharepoint.com/dl/alpha?token=PRESIGNED")
    out_scope = _file_entry("ITEM-B", "beta.txt", drive_id="D1", parent_drive="OTHER-DRIVE")
    patched.json_routes["/drives/D1/root/delta"] = _delta_page([in_scope, out_scope])
    patched.downloads["/dl/alpha"] = b"alpha contents"

    conn = MicrosoftGraphConnector(mf)
    results = conn.pull(_ctx(root, mf))
    by_id = {r["item_id"]: r["action"] for r in results}
    assert by_id["ITEM-A"] == "ingested"
    assert by_id["ITEM-B"] == "skipped_out_of_scope"
    assert len(_landed(root)) == 1


def test_presigned_download_used_without_authorization(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")
    e = _file_entry("ITEM-A", "a.txt", download_url="https://contoso.sharepoint.com/dl/a?token=PRE")
    patched.json_routes["/drives/D1/root/delta"] = _delta_page([e])

    conn = MicrosoftGraphConnector(mf)
    conn.pull(_ctx(root, mf))
    # The pre-signed download is used; no Authorization header on that hop, and
    # the enumerated host suffixes are handed to the core.
    assert patched.download_calls, "expected a download"
    url, headers, suffixes = patched.download_calls[0]
    assert "/dl/a" in url
    assert "Authorization" not in headers
    assert "sharepoint.com" in suffixes


def test_content_download_uses_bearer_when_no_presigned_url(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")
    e = _file_entry("ITEM-A", "a.txt", download_url=None)  # no pre-signed URL
    patched.json_routes["/drives/D1/root/delta"] = _delta_page([e])

    conn = MicrosoftGraphConnector(mf)
    conn.pull(_ctx(root, mf))
    url, headers, suffixes = patched.download_calls[0]
    # Falls back to /content with the bearer header (the core follows the 302).
    assert "/content" in url
    assert headers.get("Authorization", "").startswith("Bearer ")
    assert "sharepoint.com" in suffixes


# --------------------------------------------------------------------------- #
# delta link survives restart
# --------------------------------------------------------------------------- #
def test_delta_link_persisted_and_resumed(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")

    link = "https://graph.microsoft.com/v1.0/drives/D1/root/delta?token=AFTER1"
    e1 = _file_entry("ITEM-A", "a.txt", download_url="https://contoso.sharepoint.com/dl/a?t=1")
    patched.json_routes["/drives/D1/root/delta"] = _delta_page([e1], delta_link=link)

    conn = MicrosoftGraphConnector(mf)
    conn.pull(_ctx(root, mf))

    cur = remote.load_cursor(root, "msgraph")
    assert cur.get("delta_links", {}).get("D1") == link

    # A SECOND pull (new connector instance = a restart) resumes from the saved
    # delta link, not the full delta endpoint.
    seen_urls = []

    def _capture(url, body, headers):
        seen_urls.append(url)
        return _delta_page([], delta_link=link)

    patched.json_routes["/drives/D1/root/delta"] = _capture
    conn2 = MicrosoftGraphConnector(mf)
    conn2.pull(_ctx(root, mf))
    # The first delta request of the restart used the SAVED link (carries
    # token=AFTER1), proving the incremental resume.
    assert any("token=AFTER1" in u for u in seen_urls)


# --------------------------------------------------------------------------- #
# 410 Gone -> reset + full resync + one ledger-visible note
# --------------------------------------------------------------------------- #
def test_410_triggers_reset_and_full_resync(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")

    # Seed a stale delta link in the cursor.
    stale = "https://graph.microsoft.com/v1.0/drives/D1/root/delta?token=STALE"
    remote.save_cursor(root, "msgraph", {"delta_links": {"D1": stale}})

    new_link = "https://graph.microsoft.com/v1.0/drives/D1/root/delta?token=FRESH"
    fresh_item = _file_entry("ITEM-A", "a.txt",
                             download_url="https://contoso.sharepoint.com/dl/a?t=1")

    calls = {"n": 0}

    def _route(url, body, headers):
        calls["n"] += 1
        # First call uses the STALE saved link -> 410 Gone.
        if "token=STALE" in url:
            raise connbase.ConnectorError("http_json GET HTTP 410: deltaLink expired")
        # The reset restarts from the FULL delta endpoint -> a fresh page.
        return _delta_page([fresh_item], delta_link=new_link)

    patched.json_routes["/drives/D1/root/delta"] = _route

    conn = MicrosoftGraphConnector(mf)
    results = conn.pull(_ctx(root, mf))

    actions = [r["action"] for r in results]
    # The fresh item is ingested after the full resync -- not a crash, not a
    # silent skip.
    assert "ingested" in actions
    assert len(_landed(root)) == 1
    # The cursor reset to the FRESH link after the full resync, and records the
    # resync (ledger-visible via the failure-event note below).
    cur = remote.load_cursor(root, "msgraph")
    assert cur.get("delta_links", {}).get("D1") == new_link
    assert cur.get("last_resync_ts")
    assert "D1" in (cur.get("last_resync_drives") or [])
    # The resync is recorded as ONE ledger row (the "ledger-visible note").
    assert _resync_ledger_rows(root) == 1


def test_410_resync_is_one_note(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")
    stale = "https://graph.microsoft.com/v1.0/drives/D1/root/delta?token=STALE"
    remote.save_cursor(root, "msgraph", {"delta_links": {"D1": stale}})

    def _route(url, body, headers):
        if "token=STALE" in url:
            raise connbase.ConnectorError("http_json GET HTTP 410: Gone")
        return _delta_page([], delta_link="https://graph.microsoft.com/v1.0/drives/D1/root/delta?token=F")

    patched.json_routes["/drives/D1/root/delta"] = _route
    conn = MicrosoftGraphConnector(mf)
    conn.pull(_ctx(root, mf))
    # Exactly ONE ledger-visible resync note for the single 410.
    assert _resync_ledger_rows(root) == 1


# --------------------------------------------------------------------------- #
# rotated refresh token persistence (P7S-2)
# --------------------------------------------------------------------------- #
def test_rotated_refresh_token_persisted_atomically_0600(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-OLD")
    mf = connbase.load_manifest(root, "msgraph")

    # The token endpoint hands back a NEW refresh token (Microsoft rotation).
    patched.token_responses = [{"access_token": "AT-1", "refresh_token": "rt-NEW-ROTATED"}]
    patched.json_routes["/drives/D1/root/delta"] = _delta_page([])

    conn = MicrosoftGraphConnector(mf)
    conn.pull(_ctx(root, mf))

    env = remote._load_env_nosync(root)
    assert env.get("MSGRAPH_REFRESH_TOKEN") == "rt-NEW-ROTATED"
    # The client id is preserved by the upsert.
    assert env.get("MSGRAPH_CLIENT_ID") == "cid"
    # 0o600 on the secret store.
    mode = stat.S_IMODE(os.stat(root / ".env.nosync").st_mode)
    assert mode == 0o600


def test_rotated_token_used_on_next_refresh(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-OLD")
    mf = connbase.load_manifest(root, "msgraph")

    sent_refresh_tokens = []

    # Drive two pulls through a custom http_json that captures each refresh_token
    # sent and rotates rt-OLD -> rt-NEW on the first refresh.
    def _custom_http_json(method, url, *, headers=None, body=None, timeout=30, _max_retries=3):
        if "/oauth2/v2.0/token" in url:
            import urllib.parse as up
            params = dict(up.parse_qsl(body))
            sent_refresh_tokens.append(params.get("refresh_token"))
            if params.get("refresh_token") == "rt-OLD":
                return {"access_token": "AT-1", "refresh_token": "rt-NEW"}
            return {"access_token": "AT-2"}
        if "/drives/D1/root/delta" in url:
            return _delta_page([])
        return {}

    import pytest as _pt
    monkey = _pt.MonkeyPatch()
    monkey.setattr(msgraph, "http_json", _custom_http_json)
    try:
        conn = MicrosoftGraphConnector(mf)
        conn.pull(_ctx(root, mf))   # uses rt-OLD, rotates to rt-NEW
        conn2 = MicrosoftGraphConnector(mf)
        conn2.pull(_ctx(root, mf))  # must now use rt-NEW
    finally:
        monkey.undo()

    assert sent_refresh_tokens[0] == "rt-OLD"
    assert sent_refresh_tokens[-1] == "rt-NEW"


# --------------------------------------------------------------------------- #
# 429 + Retry-After bounded backoff (exercised at the http_json primitive)
# --------------------------------------------------------------------------- #
def test_429_retry_after_backoff_bounded(monkeypatch):
    """The shared http_json primitive backs off on 429 honoring Retry-After and
    gives up after a bounded number of attempts (msgraph relies on this; P7S-8)."""
    import urllib.error

    sleeps = []
    monkeypatch.setattr(remote.time, "sleep", lambda s: sleeps.append(s))

    class _Opener:
        def __init__(self):
            self.calls = 0

        def open(self, req, timeout=None):
            self.calls += 1
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests",
                hdrs={"Retry-After": "2"}, fp=io.BytesIO(b""))

    monkeypatch.setattr(remote.urllib.request, "build_opener", lambda *a: _Opener())
    with pytest.raises(connbase.ConnectorError):
        remote.http_json("GET", "https://graph.microsoft.com/v1.0/drives/D1/root/delta")
    # Bounded: a finite number of backoff sleeps, each honoring Retry-After=2.
    assert sleeps, "expected at least one backoff sleep"
    assert all(s == 2 for s in sleeps)
    assert len(sleeps) <= 4  # _max_retries=3 -> at most 3-4 sleeps, never unbounded


def test_429_then_success_recovers(monkeypatch):
    import urllib.error

    monkeypatch.setattr(remote.time, "sleep", lambda s: None)

    class _Resp:
        def __init__(self, data):
            self._d = data

        def getcode(self):
            return 200

        def read(self):
            return self._d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def __init__(self):
            self.calls = 0

        def open(self, req, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many", hdrs={"Retry-After": "1"}, fp=io.BytesIO(b""))
            return _Resp(b'{"value": []}')

    monkeypatch.setattr(remote.urllib.request, "build_opener", lambda *a: _Opener())
    out = remote.http_json("GET", "https://graph.microsoft.com/v1.0/x")
    assert out == {"value": []}


# --------------------------------------------------------------------------- #
# read-only / pull-is-final / no-direct-urllib
# --------------------------------------------------------------------------- #
def test_read_write_manifest_refused(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"], permissions="read_write")
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph", validate=False)
    conn = MicrosoftGraphConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))


def test_msgraph_does_not_override_pull():
    assert "pull" not in MicrosoftGraphConnector.__dict__, (
        "RemoteConnector.pull is FINAL; msgraph must not override it"
    )


# --------------------------------------------------------------------------- #
# health: broken on unresolved auth vars / empty scope
# --------------------------------------------------------------------------- #
def test_health_broken_on_unresolved_auth(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    # No .env.nosync -> auth vars unresolved.
    mf = connbase.load_manifest(root, "msgraph")
    conn = MicrosoftGraphConnector(mf)
    rep = conn.health(_ctx(root, mf))
    assert rep["status"] == "broken"


def test_health_broken_on_empty_scope(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=None, sites=None)
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")
    conn = MicrosoftGraphConnector(mf)
    rep = conn.health(_ctx(root, mf))
    assert rep["status"] == "broken"


def test_health_healthy_when_configured(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")
    conn = MicrosoftGraphConnector(mf)
    rep = conn.health(_ctx(root, mf))
    assert rep["status"] in ("healthy", "degraded")


# --------------------------------------------------------------------------- #
# sites -> drive resolution (SharePoint site's default library)
# --------------------------------------------------------------------------- #
def test_site_resolves_to_drive_and_pulls(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, sites=["SITE-1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")

    # /sites/SITE-1/drive resolves to drive id D1.
    patched.json_routes["/sites/SITE-1/drive"] = {"id": "D1"}
    e = _file_entry("ITEM-A", "a.txt", drive_id="D1",
                    download_url="https://contoso.sharepoint.com/dl/a?t=1")
    patched.json_routes["/drives/D1/root/delta"] = _delta_page([e])

    conn = MicrosoftGraphConnector(mf)
    results = conn.pull(_ctx(root, mf))
    assert any(r["action"] == "ingested" for r in results)
    assert len(_landed(root)) == 1


# --------------------------------------------------------------------------- #
# redaction: a pre-signed download URL never leaks into results
# --------------------------------------------------------------------------- #
def test_presigned_url_not_in_results(tmp_path, minimal_oracle, patched):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, drives=["D1"])
    _write_env(root, MSGRAPH_CLIENT_ID="cid", MSGRAPH_REFRESH_TOKEN="rt-1")
    mf = connbase.load_manifest(root, "msgraph")
    e = _file_entry("ITEM-A", "a.txt",
                    download_url="https://contoso.sharepoint.com/dl/a?token=SUPERSECRET123456")
    patched.json_routes["/drives/D1/root/delta"] = _delta_page([e])

    import json as _json
    conn = MicrosoftGraphConnector(mf)
    results = conn.pull(_ctx(root, mf))
    blob = _json.dumps(results)
    assert "SUPERSECRET123456" not in blob
    assert "guest=1" not in blob  # webUrl query string redacted too
