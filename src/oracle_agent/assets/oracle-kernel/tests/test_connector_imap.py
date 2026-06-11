#!/usr/bin/env python3
"""Tests for the IMAP mailbox connector (P7-T5).

Every assertion maps to a T5 acceptance bullet in
docs/roadmap/PHASE-7-knowledge-connectors.md:

  * the pull is READ-ONLY -- the connector opens folders with EXAMINE and never
    issues a writable SELECT / STORE / EXPUNGE (verified by recording every IMAP
    verb the scripted fake server receives);
  * it honors the folder allowlist (default-deny -- None/missing/[]/non-list all
    refuse) and the UID cursor (incremental: only UIDs above the high-water
    mark);
  * it resets cleanly on a UIDVALIDITY change (full re-pull, logged note);
  * it refuses an unverified / plain connection (IMAP4_SSL + a default-verifying
    ssl context ONLY -- the connector never touches plain IMAP4 or STARTTLS);
  * the default sensitivity floor is ``confidential``;
  * auth is username + app password resolved via resolve_auth env-var NAMES.

No network is opened: ``imaplib.IMAP4_SSL`` is monkeypatched with a scripted
fake server, and ``ssl.create_default_context`` is observed (not bypassed).
"""
from __future__ import annotations

import os
import ssl
from datetime import datetime
from pathlib import Path

import pytest

import connectors
from connectors import base as connbase
from connectors import imap_mailbox
from connectors.imap_mailbox import ImapMailboxConnector


# --------------------------------------------------------------------------- #
# a scripted fake IMAP4_SSL server (no sockets)
# --------------------------------------------------------------------------- #
class FakeIMAP:
    """A scripted in-memory IMAP4_SSL stand-in.

    ``folders`` maps a folder name -> (uidvalidity, {uid: rfc822_bytes}). Every
    verb the connector issues is appended to ``calls`` so a test can assert the
    pull was READ-ONLY (EXAMINE only; no SELECT/STORE/EXPUNGE).

    Instances are created via the ``factory`` classmethod so a test can capture
    the connection construction args (host, ssl_context) and the single shared
    instance the connector uses.
    """

    instances: list = []
    raise_on_construct = None  # set to an Exception type to simulate TLS refusal

    def __init__(self, host=None, ssl_context=None, **kw):
        self.host = host
        self.ssl_context = ssl_context
        self.calls: list = []
        self.logged_in = False
        self.selected = None
        self.folders: dict = {}
        FakeIMAP.instances.append(self)

    # construction is monkeypatched in; this records intent + can refuse
    @classmethod
    def factory(cls, host=None, ssl_context=None, **kw):
        if cls.raise_on_construct is not None:
            raise cls.raise_on_construct("TLS handshake / verification failed")
        inst = cls.current
        inst.host = host
        inst.ssl_context = ssl_context
        inst.calls.append(("__init__", host, ssl_context))
        return inst

    # -- imaplib surface the connector touches ------------------------------- #
    def login(self, user, password):
        self.calls.append(("login", user, password))
        self.logged_in = True
        return ("OK", [b"LOGIN completed"])

    def logout(self):
        self.calls.append(("logout",))
        return ("BYE", [b"logout"])

    def select(self, *a, **k):  # MUST NOT be called -- writable select
        self.calls.append(("SELECT",) + a)
        return ("OK", [b"1"])

    def store(self, *a, **k):  # MUST NOT be called -- mutates flags
        self.calls.append(("STORE",) + a)
        return ("OK", [])

    def expunge(self, *a, **k):  # MUST NOT be called
        self.calls.append(("EXPUNGE",) + a)
        return ("OK", [])

    def examine(self, folder, *a, **k):
        self.calls.append(("EXAMINE", folder))
        self.selected = folder
        uidvalidity, _msgs = self.folders.get(self._unquote(folder), ("0", {}))
        self._cur_uidvalidity = str(uidvalidity)
        return ("OK", [b"EXAMINE completed"])

    def response(self, code):
        if code == "UIDVALIDITY":
            return ("OK", [str(getattr(self, "_cur_uidvalidity", "0")).encode()])
        return ("OK", [None])

    def uid(self, command, *args):
        cmd = command.upper()
        self.calls.append(("UID", cmd) + tuple(a for a in args if a is not None))
        folder = self._unquote(self.selected)
        uidvalidity, msgs = self.folders.get(folder, ("0", {}))
        if cmd == "SEARCH":
            # ignore the SINCE filter in the fake -- return all uids in folder
            uids = " ".join(str(u) for u in sorted(msgs)).encode()
            return ("OK", [uids])
        if cmd == "FETCH":
            uid = int(args[0])
            spec = args[1] if len(args) > 1 else ""
            if "RFC822.SIZE" in spec:
                body = msgs.get(uid, b"")
                line = f"{uid} (UID {uid} RFC822.SIZE {len(body)})".encode()
                return ("OK", [line])
            if "RFC822" in spec:
                body = msgs.get(uid, b"")
                return ("OK", [(f"{uid} (UID {uid} RFC822 {{{len(body)}}})".encode(), body)])
        return ("OK", [None])

    @staticmethod
    def _unquote(folder) -> str:
        f = str(folder)
        if len(f) >= 2 and f[0] == '"' and f[-1] == '"':
            return f[1:-1]
        return f


def _install_fake(monkeypatch, fake: FakeIMAP):
    FakeIMAP.current = fake
    FakeIMAP.raise_on_construct = None
    monkeypatch.setattr(imap_mailbox.imaplib, "IMAP4_SSL", FakeIMAP.factory)


# --------------------------------------------------------------------------- #
# manifest + helpers
# --------------------------------------------------------------------------- #
def _write_manifest(root: Path, *, folders=("INBOX",), host="imap.example.com",
                    permissions="read_only", default_sensitivity="confidential",
                    since_days=30, cid="imap-mailbox") -> Path:
    mdir = root / "Connectors" / cid
    mdir.mkdir(parents=True, exist_ok=True)
    if folders is None:
        folders_block = "  folders:\n"           # bare key -> None
    elif folders == []:
        folders_block = "  folders:\n"
    elif folders == "scalar":
        folders_block = "  folders: not-a-list\n"
    else:
        folders_block = "  folders:\n" + "".join(f"    - {f}\n" for f in folders)
    host_line = f"  host: {host}\n" if host is not None else "  host:\n"
    text = f"""\
id: {cid}
system: imap
status: active
access_mode: api
locality: external_only
capture_tier: snapshot
auth:
  method: app_password
  vars:
    - IMAP_USERNAME
    - IMAP_APP_PASSWORD
permissions: {permissions}
freshness:
  class: api
  last_verified: "2026-01-01"
  expected_decay_days: 7
source:
{host_line}{folders_block}  since_days: {since_days}
  default_sensitivity: {default_sensitivity}
"""
    mf = mdir / f"{cid}.manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    return mf


def _ctx(root, manifest, **kw):
    return connbase.ConnectorContext(root, manifest, **kw)


def _set_auth_env(monkeypatch, user="alice@example.com", pw="app-pass-1234"):
    monkeypatch.setenv("IMAP_USERNAME", user)
    monkeypatch.setenv("IMAP_APP_PASSWORD", pw)


def _msg(subject="Hi", body="plain business note") -> bytes:
    return (
        f"From: bob@example.com\r\n"
        f"To: alice@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


def _landed(root: Path, cid: str = "imap-mailbox") -> list:
    d = root / "Workproduct.nosync" / "_INPUT" / cid
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# read-only: EXAMINE only, never SELECT / STORE / EXPUNGE
# --------------------------------------------------------------------------- #
def test_pull_is_read_only_examine_only(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _mf(root, monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")
    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {1: _msg("a"), 2: _msg("b")})}
    _install_fake(monkeypatch, fake)

    conn = ImapMailboxConnector(mf)
    results = conn.pull(_ctx(root, mf))

    verbs = {c[0] for c in fake.calls}
    examine_verbs = {c[1] for c in fake.calls if c[0] == "UID"}
    assert "EXAMINE" in verbs
    assert "SELECT" not in verbs            # never a writable select
    assert "STORE" not in verbs             # flags never mutated
    assert "EXPUNGE" not in verbs
    assert examine_verbs <= {"SEARCH", "FETCH"}   # only read verbs over UID
    assert any(r["action"] == "ingested" for r in results)
    assert len(_landed(root)) == 2


def test_landed_files_are_eml_rfc822(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _mf(root, monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")
    body = _msg("subjectX", "the message body text")
    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {7: body})}
    _install_fake(monkeypatch, fake)

    conn = ImapMailboxConnector(mf)
    conn.pull(_ctx(root, mf))
    landed = _landed(root)
    assert len(landed) == 1
    assert landed[0].suffix == ".eml"
    assert landed[0].read_bytes() == body   # full raw RFC822, attachments inside


# --------------------------------------------------------------------------- #
# folder allowlist (default-deny)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("folders", [None, [], "scalar", "__missing__"])
def test_empty_folder_allowlist_refuses(tmp_path, minimal_oracle, monkeypatch, folders):
    root = minimal_oracle(tmp_path)
    _set_auth_env(monkeypatch)
    if folders == "__missing__":
        mdir = root / "Connectors" / "imap-mailbox"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "imap-mailbox.manifest.yaml").write_text(
            "id: imap-mailbox\nsystem: imap\nstatus: active\naccess_mode: api\n"
            "locality: external_only\ncapture_tier: snapshot\n"
            "auth:\n  method: app_password\n  vars:\n    - IMAP_USERNAME\n    - IMAP_APP_PASSWORD\n"
            "permissions: read_only\nfreshness:\n  class: api\n"
            '  last_verified: "2026-01-01"\n  expected_decay_days: 7\n'
            "source:\n  host: imap.example.com\n  default_sensitivity: confidential\n",
            encoding="utf-8",
        )
        mf = connbase.load_manifest(root, "imap-mailbox")
    else:
        _write_manifest(root, folders=folders)
        mf = connbase.load_manifest(root, "imap-mailbox", validate=(folders != "scalar"))

    fake = FakeIMAP()
    _install_fake(monkeypatch, fake)
    conn = ImapMailboxConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))
    # default-deny refuses BEFORE any IMAP connection is opened.
    assert all(c[0] != "login" for c in fake.calls)
    assert _landed(root) == []


def test_only_allowlisted_folders_pulled(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")
    fake = FakeIMAP()
    fake.folders = {
        "INBOX": ("100", {1: _msg("in")}),
        "Secret": ("200", {1: _msg("secret")}),  # not in the allowlist
    }
    _install_fake(monkeypatch, fake)

    conn = ImapMailboxConnector(mf)
    conn.pull(_ctx(root, mf))
    examined = {c[1] for c in fake.calls if c[0] == "EXAMINE"}
    assert imap_mailbox.ImapMailboxConnector  # sanity
    assert "INBOX" in examined
    assert "Secret" not in examined            # never even opened
    assert len(_landed(root)) == 1


# --------------------------------------------------------------------------- #
# UID cursor: incremental
# --------------------------------------------------------------------------- #
def test_uid_cursor_incremental(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")

    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {1: _msg("m1"), 2: _msg("m2")})}
    _install_fake(monkeypatch, fake)
    conn = ImapMailboxConnector(mf)
    conn.pull(_ctx(root, mf))
    assert len(_landed(root)) == 2

    # A second pull with a new message above the high-water mark pulls ONLY it.
    fake2 = FakeIMAP()
    fake2.folders = {"INBOX": ("100", {1: _msg("m1"), 2: _msg("m2"), 3: _msg("m3")})}
    _install_fake(monkeypatch, fake2)
    conn2 = ImapMailboxConnector(mf)
    r2 = conn2.pull(_ctx(root, mf))
    ingested2 = [r for r in r2 if r["action"] == "ingested"]
    assert len(ingested2) == 1   # only uid 3 was new
    # uid 1 and 2 were not re-fetched.
    fetched_uids = [c[2] for c in fake2.calls if c[0] == "UID" and c[1] == "FETCH"]
    assert "1" not in fetched_uids and "2" not in fetched_uids


# --------------------------------------------------------------------------- #
# UIDVALIDITY change -> reset + full re-pull
# --------------------------------------------------------------------------- #
def test_uidvalidity_change_resets_cursor(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")

    # First pull at UIDVALIDITY 100.
    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {1: _msg("m1"), 2: _msg("m2")})}
    _install_fake(monkeypatch, fake)
    ImapMailboxConnector(mf).pull(_ctx(root, mf))
    assert len(_landed(root)) == 2

    # Server reissues UIDs under a NEW UIDVALIDITY (200). The same UID numbers
    # now mean DIFFERENT messages -> the cursor must reset and re-pull all.
    fake2 = FakeIMAP()
    fake2.folders = {"INBOX": ("200", {1: _msg("new-1"), 2: _msg("new-2")})}
    _install_fake(monkeypatch, fake2)
    r2 = ImapMailboxConnector(mf).pull(_ctx(root, mf))
    ingested2 = [r for r in r2 if r["action"] == "ingested"]
    # Both UIDs re-pulled despite being <= the old high-water mark.
    assert len(ingested2) == 2
    fetched = [c[2] for c in fake2.calls if c[0] == "UID" and c[1] == "FETCH"]
    assert "1" in fetched and "2" in fetched

    # The cursor records the reset and the new UIDVALIDITY.
    from connectors.remote import load_cursor
    cur = load_cursor(root, "imap-mailbox")
    assert cur["folders"]["INBOX"]["uidvalidity"] == "200"
    assert "last_reset_notes" in cur


# --------------------------------------------------------------------------- #
# TLS: verified IMAP4_SSL ONLY (never plain / unverified)
# --------------------------------------------------------------------------- #
def test_uses_verified_imap4_ssl_default_context(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")

    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {1: _msg("m1")})}
    _install_fake(monkeypatch, fake)

    # Observe ssl.create_default_context is actually used (a default-VERIFYING
    # context: CERT_REQUIRED + hostname checking on).
    real_ctx = ssl.create_default_context
    seen = {}

    def _spy_ctx(*a, **k):
        ctx = real_ctx(*a, **k)
        seen["verify_mode"] = ctx.verify_mode
        seen["check_hostname"] = ctx.check_hostname
        return ctx

    monkeypatch.setattr(imap_mailbox.ssl, "create_default_context", _spy_ctx)
    ImapMailboxConnector(mf).pull(_ctx(root, mf))

    assert seen["verify_mode"] == ssl.CERT_REQUIRED
    assert seen["check_hostname"] is True
    # The connector constructed IMAP4_SSL (never plain IMAP4) with that context.
    assert fake.ssl_context is not None
    assert fake.host == "imap.example.com"


def test_tls_refusal_surfaces_clean_error(tmp_path, minimal_oracle, monkeypatch):
    """A failed TLS handshake / cert verification raises a clean ConnectorError
    (the connector never falls back to plain IMAP4 or STARTTLS)."""
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")

    fake = FakeIMAP()
    FakeIMAP.current = fake
    monkeypatch.setattr(imap_mailbox.imaplib, "IMAP4_SSL", FakeIMAP.factory)
    FakeIMAP.raise_on_construct = ssl.SSLError

    # The connector has no plain-IMAP4 attribute path at all -- assert the only
    # connection primitive it imports is IMAP4_SSL.
    conn = ImapMailboxConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        list(conn.list_items(_ctx(root, mf)))
    FakeIMAP.raise_on_construct = None


def test_module_never_uses_plain_imap4_or_starttls():
    """Source-level guard: the connector must call IMAP4_SSL and create a
    default-verifying ssl context, and must NEVER reference plain IMAP4 or
    STARTTLS."""
    src = Path(imap_mailbox.__file__).read_text(encoding="utf-8")
    assert "IMAP4_SSL" in src
    assert "create_default_context" in src
    # No actual STARTTLS upgrade CALL (a docstring mention explaining the ban is
    # fine; an executed ``.starttls(`` is not).
    assert ".starttls(" not in src.lower()
    # No bare plain-IMAP4 construction (unencrypted).
    assert "imaplib.IMAP4(" not in src
    assert ".IMAP4(" not in src


# --------------------------------------------------------------------------- #
# confidential sensitivity floor
# --------------------------------------------------------------------------- #
def test_confidential_floor(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",), default_sensitivity="confidential")
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")
    fake = FakeIMAP()
    # An utterly innocuous message -- floor must still classify it >= confidential.
    fake.folders = {"INBOX": ("100", {1: _msg("hello", "just saying hi")})}
    _install_fake(monkeypatch, fake)

    conn = ImapMailboxConnector(mf)
    results = conn.pull(_ctx(root, mf))
    ing = [r for r in results if r["action"] == "ingested"][0]
    order = ("public", "internal", "confidential", "restricted", "secret")
    assert order.index(ing["sensitivity"]) >= order.index("confidential")


def test_default_floor_is_confidential_when_unset(tmp_path, minimal_oracle, monkeypatch):
    """Even with no default_sensitivity in the source block the connector's mail
    floor must be confidential (mail is presumptively sensitive)."""
    root = minimal_oracle(tmp_path)
    # Write a manifest WITHOUT default_sensitivity, then assert the connector
    # floors at confidential via its _connector_floor override.
    mdir = root / "Connectors" / "imap-mailbox"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "imap-mailbox.manifest.yaml").write_text(
        "id: imap-mailbox\nsystem: imap\nstatus: active\naccess_mode: api\n"
        "locality: external_only\ncapture_tier: snapshot\n"
        "auth:\n  method: app_password\n  vars:\n    - IMAP_USERNAME\n    - IMAP_APP_PASSWORD\n"
        "permissions: read_only\nfreshness:\n  class: api\n"
        '  last_verified: "2026-01-01"\n  expected_decay_days: 7\n'
        "source:\n  host: imap.example.com\n  folders:\n    - INBOX\n  since_days: 30\n",
        encoding="utf-8",
    )
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")
    conn = ImapMailboxConnector(mf)
    floor = conn._connector_floor(_ctx(root, mf))
    assert floor == "confidential"


# --------------------------------------------------------------------------- #
# auth: username + app password via resolve_auth env names
# --------------------------------------------------------------------------- #
def test_auth_resolved_from_env_var_names(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch, user="carol@example.com", pw="secret-app-pw")
    mf = connbase.load_manifest(root, "imap-mailbox")
    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {1: _msg("x")})}
    _install_fake(monkeypatch, fake)

    ImapMailboxConnector(mf).pull(_ctx(root, mf))
    login = [c for c in fake.calls if c[0] == "login"]
    assert login and login[0][1] == "carol@example.com"
    assert login[0][2] == "secret-app-pw"


def test_auth_from_env_nosync_file(tmp_path, minimal_oracle, monkeypatch):
    """resolve_auth reads <root>/.env.nosync when the var is not in os.environ
    (the path a scrubbed kernel subprocess relies on)."""
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    monkeypatch.delenv("IMAP_USERNAME", raising=False)
    monkeypatch.delenv("IMAP_APP_PASSWORD", raising=False)
    (root / ".env.nosync").write_text(
        "IMAP_USERNAME=dave@example.com\nIMAP_APP_PASSWORD=file-app-pw\n",
        encoding="utf-8",
    )
    mf = connbase.load_manifest(root, "imap-mailbox")
    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {1: _msg("x")})}
    _install_fake(monkeypatch, fake)

    ImapMailboxConnector(mf).pull(_ctx(root, mf))
    login = [c for c in fake.calls if c[0] == "login"]
    assert login and login[0][1] == "dave@example.com"


def test_missing_auth_health_broken_with_fixline(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    monkeypatch.delenv("IMAP_USERNAME", raising=False)
    monkeypatch.delenv("IMAP_APP_PASSWORD", raising=False)
    mf = connbase.load_manifest(root, "imap-mailbox")
    conn = ImapMailboxConnector(mf)
    health = conn.health(_ctx(root, mf))
    assert health["status"] == "broken"
    assert any("app password" in n for n in health["notes"])


# --------------------------------------------------------------------------- #
# read-only manifest guard + freshness-from-cursor
# --------------------------------------------------------------------------- #
def test_read_write_manifest_refused(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",), permissions="read_write")
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox", validate=False)
    fake = FakeIMAP()
    _install_fake(monkeypatch, fake)
    conn = ImapMailboxConnector(mf)
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))
    assert all(c[0] != "login" for c in fake.calls)


def test_freshness_from_cursor(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")
    conn = ImapMailboxConnector(mf)
    f0 = conn.freshness(_ctx(root, mf, now=datetime(2026, 6, 8)))
    assert f0["verdict"] == "unknown"

    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {1: _msg("x")})}
    _install_fake(monkeypatch, fake)
    conn.pull(_ctx(root, mf))
    f1 = conn.freshness(_ctx(root, mf, now=datetime.now()))
    assert f1["verdict"] == "fresh"


def test_dry_run_plans_without_landing(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch)
    mf = connbase.load_manifest(root, "imap-mailbox")
    fake = FakeIMAP()
    fake.folders = {"INBOX": ("100", {1: _msg("x"), 2: _msg("y")})}
    _install_fake(monkeypatch, fake)
    conn = ImapMailboxConnector(mf)
    results = conn.pull(_ctx(root, mf, dry_run=True))
    assert all(r["action"] == "planned" for r in results)
    assert _landed(root) == []
    # No RFC822 body fetch happened on a dry-run plan.
    assert not any(c[0] == "UID" and c[1] == "FETCH" and "RFC822)" in str(c) for c in fake.calls)


# --------------------------------------------------------------------------- #
# registry: id-only + system fallback
# --------------------------------------------------------------------------- #
def test_registers_id_only_with_system_fallback(tmp_path, minimal_oracle):
    connectors.register("imap-mailbox", imap_mailbox.build, system="imap")
    root = minimal_oracle(tmp_path)
    # A SECOND account: distinct id, same system -> resolves via system fallback.
    _write_manifest(root, cid="imap-finance")
    # rewrite the id field to match the dir
    mdir = root / "Connectors" / "imap-finance"
    txt = (mdir / "imap-finance.manifest.yaml").read_text()
    txt = txt.replace("id: imap-mailbox", "id: imap-finance")
    (mdir / "imap-finance.manifest.yaml").write_text(txt, encoding="utf-8")
    mf = connbase.load_manifest(root, "imap-finance")
    klass = connectors.get_connector_class(mf)
    assert klass is imap_mailbox.build


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mf(root, monkeypatch):
    """Write the default manifest + set auth env (loads as id 'imap-mailbox')."""
    _write_manifest(root, folders=("INBOX",))
    _set_auth_env(monkeypatch)
