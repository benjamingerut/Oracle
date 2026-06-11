"""Tests for gateway/email.py -- the email adapter (Phase 4, P4-T3).

Drives :class:`EmailAdapter` with FAKE imap/smtp objects + a fake GatewayCore,
covering every pin in P4S-10/11/12:

  * layered fail-closed identity (public hard-cap; authserv_id + dmarc=pass to
    unlock internal);
  * reply to the exact header From only (Reply-To IGNORED, never reply-all);
  * reply body never quotes inbound;
  * loop protection (Auto-Submitted / Precedence / own-References);
  * MIME text/plain preferred + HTML stripped + size cap;
  * per-sender hourly turn cap (always on, before any model call);
  * UID/UIDVALIDITY cursor: corruption/reset => start from UIDNEXT; restart
    does not replay handled mail; commit() persists after the batch.
"""
from __future__ import annotations

import email
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from oracle_agent.gateway.core import GatewayCore, InboundMessage, OutboundReply, _noop_lock
from oracle_agent.gateway.email import EmailAdapter


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeIMAP:
    def __init__(self, *, uidvalidity=100, uidnext=1, messages=None):
        # messages: dict[uid] = rfc822 bytes
        self._uidvalidity = uidvalidity
        self._uidnext = uidnext
        self.messages: dict[int, bytes] = dict(messages or {})
        self.logged_out = False

    def select(self):
        return {"uidvalidity": self._uidvalidity, "uidnext": self._uidnext}

    def search_uids_since(self, last_uid):
        return sorted(u for u in self.messages if u > last_uid)

    def fetch_rfc822(self, uid):
        return self.messages.get(uid, b"")

    def logout(self):
        self.logged_out = True


class FakeSMTP:
    def __init__(self):
        self.sent: list[EmailMessage] = []

    def send(self, msg):
        self.sent.append(msg)


OWN = "oracle@co.com"


def _raw(*, frm="ceo@co.com", to=OWN, cc=None, subject="hi", body="what is revenue?",
         auth_results=None, auto_submitted=None, precedence=None, references=None,
         reply_to=None, html=None, content_type="text/plain"):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    if cc:
        m["Cc"] = cc
    if reply_to:
        m["Reply-To"] = reply_to
    m["Subject"] = subject
    if auth_results:
        m["Authentication-Results"] = auth_results
    if auto_submitted:
        m["Auto-Submitted"] = auto_submitted
    if precedence:
        m["Precedence"] = precedence
    if references:
        m["References"] = references
    if html is not None:
        m.set_content(body)
        m.add_alternative(html, subtype="html")
    elif content_type == "text/html":
        m.set_content(body, subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


def _adapter(tmp_path, *, cfg=None, imap=None, smtp=None, clock=None):
    surface_cfg = {
        "allowlist": {"ceo@co.com": {"role": "user", "instance": "main"}},
        "max_sensitivity": "internal",
        "authserv_id": None,
        "per_sender_turns_per_hour": 10,
        "per_user_writes_per_hour": 20,
    }
    surface_cfg.update(cfg or {})
    imap = imap if imap is not None else FakeIMAP()
    smtp = smtp if smtp is not None else FakeSMTP()
    a = EmailAdapter(
        surface_cfg, OWN, imap, smtp,
        clock=clock or (lambda: 1000.0),
        profile_dir=tmp_path,
    )
    return a, imap, smtp


# --------------------------------------------------------------------------- #
# Layered fail-closed identity (P4S-10)
# --------------------------------------------------------------------------- #
def test_no_authserv_id_forces_non_private(tmp_path):
    """With no authserv_id configured, every message is non-private (public cap),
    no matter what config max_sensitivity says."""
    a, *_ = _adapter(tmp_path, cfg={"authserv_id": None, "max_sensitivity": "internal"})
    msg = a.parse(_raw(
        auth_results="mx.co.com; dmarc=pass header.from=co.com"))
    assert msg is not None
    assert msg.is_private is False  # public cap enforced downstream by core


def test_authserv_id_plus_dmarc_pass_unlocks_private(tmp_path):
    a, *_ = _adapter(tmp_path, cfg={"authserv_id": "mx.co.com",
                                    "max_sensitivity": "internal"})
    msg = a.parse(_raw(
        auth_results="mx.co.com; dmarc=pass header.from=co.com"))
    assert msg is not None
    assert msg.is_private is True


def test_authserv_id_set_but_dmarc_missing_caps_public(tmp_path):
    a, *_ = _adapter(tmp_path, cfg={"authserv_id": "mx.co.com"})
    # No Authentication-Results header at all.
    msg = a.parse(_raw(auth_results=None))
    assert msg is not None
    assert msg.is_private is False


def test_wrong_authserv_id_caps_public(tmp_path):
    a, *_ = _adapter(tmp_path, cfg={"authserv_id": "mx.co.com"})
    msg = a.parse(_raw(
        auth_results="attacker.evil.com; dmarc=pass header.from=co.com"))
    assert msg is not None
    assert msg.is_private is False


def test_dmarc_fail_caps_public(tmp_path):
    a, *_ = _adapter(tmp_path, cfg={"authserv_id": "mx.co.com"})
    msg = a.parse(_raw(
        auth_results="mx.co.com; dmarc=fail header.from=co.com"))
    assert msg is not None
    assert msg.is_private is False


def test_spf_pass_unlocks_when_dmarc_absent(tmp_path):
    a, *_ = _adapter(tmp_path, cfg={"authserv_id": "mx.co.com"})
    msg = a.parse(_raw(auth_results="mx.co.com; spf=pass smtp.mailfrom=co.com"))
    assert msg is not None
    assert msg.is_private is True


# --------------------------------------------------------------------------- #
# Recipient targeting -> is_private (P4S-11)
# --------------------------------------------------------------------------- #
def test_cc_present_forces_non_private(tmp_path):
    a, *_ = _adapter(tmp_path, cfg={"authserv_id": "mx.co.com"})
    msg = a.parse(_raw(cc="other@co.com",
                       auth_results="mx.co.com; dmarc=pass"))
    assert msg is not None
    assert msg.is_private is False  # cc'd => served capped at public


def test_extra_to_recipient_forces_non_private(tmp_path):
    a, *_ = _adapter(tmp_path, cfg={"authserv_id": "mx.co.com"})
    msg = a.parse(_raw(to=f"{OWN}, list@co.com",
                       auth_results="mx.co.com; dmarc=pass"))
    assert msg is not None
    assert msg.is_private is False


# --------------------------------------------------------------------------- #
# Reply discipline (P4S-10/11)
# --------------------------------------------------------------------------- #
def test_reply_goes_to_exact_from_not_reply_to(tmp_path):
    a, imap, smtp = _adapter(tmp_path)
    a.send(OutboundReply(channel_id="ceo@co.com", text="the answer"))
    assert len(smtp.sent) == 1
    out = smtp.sent[0]
    assert out["To"] == "ceo@co.com"
    # Reply-To from the inbound is never consulted by send (channel_id is the
    # exact From the parser pinned).


def test_reply_never_quotes_inbound(tmp_path):
    a, imap, smtp = _adapter(tmp_path)
    a.send(OutboundReply(channel_id="ceo@co.com",
                         text="42 dollars of revenue"))
    out = smtp.sent[0]
    body = out.get_content()
    assert "42 dollars of revenue" in body
    # No quoting markers / inbound text in the body.
    assert "what is revenue" not in body
    assert ">" not in body  # no quoted-line markers


def test_reply_sets_auto_submitted(tmp_path):
    a, imap, smtp = _adapter(tmp_path)
    a.send(OutboundReply(channel_id="ceo@co.com", text="ok"))
    assert smtp.sent[0]["Auto-Submitted"] == "auto-replied"


def test_reply_to_header_is_ignored_on_parse(tmp_path):
    """Reply-To on inbound never becomes the channel_id (P4S-10)."""
    a, *_ = _adapter(tmp_path)
    msg = a.parse(_raw(frm="ceo@co.com", reply_to="attacker@evil.com"))
    assert msg is not None
    assert msg.channel_id == "ceo@co.com"
    assert "evil" not in msg.channel_id


# --------------------------------------------------------------------------- #
# Loop protection (P4S-11)
# --------------------------------------------------------------------------- #
def test_auto_submitted_dropped(tmp_path):
    a, *_ = _adapter(tmp_path)
    assert a.parse(_raw(auto_submitted="auto-replied")) is None


def test_auto_submitted_no_is_served(tmp_path):
    a, *_ = _adapter(tmp_path)
    assert a.parse(_raw(auto_submitted="no")) is not None


def test_precedence_bulk_dropped(tmp_path):
    a, *_ = _adapter(tmp_path)
    assert a.parse(_raw(precedence="bulk")) is None


def test_own_message_id_in_references_dropped(tmp_path):
    a, imap, smtp = _adapter(tmp_path)
    a.send(OutboundReply(channel_id="ceo@co.com", text="prior reply"))
    our_mid = smtp.sent[0]["Message-ID"]
    assert a.parse(_raw(references=our_mid)) is None


# --------------------------------------------------------------------------- #
# MIME/HTML + size (P4S-11)
# --------------------------------------------------------------------------- #
def test_plain_preferred_over_html(tmp_path):
    a, *_ = _adapter(tmp_path)
    msg = a.parse(_raw(body="PLAINTEXT BODY",
                       html="<html><body>HTML BODY</body></html>"))
    assert msg is not None
    assert "PLAINTEXT BODY" in msg.text
    assert "HTML BODY" not in msg.text


def test_html_only_is_text_extracted(tmp_path):
    a, *_ = _adapter(tmp_path)
    raw = _raw(body="<html><head><style>x{}</style></head>"
                    "<body><p>Hello <b>world</b></p><script>bad()</script></body></html>",
               content_type="text/html")
    msg = a.parse(raw)
    assert msg is not None
    assert "Hello" in msg.text and "world" in msg.text
    assert "bad()" not in msg.text
    assert "<" not in msg.text


def test_oversize_inbound_dropped(tmp_path):
    a, *_ = _adapter(tmp_path)
    huge = b"X" * (256 * 1024 + 1)
    assert a.parse(huge) is None


# --------------------------------------------------------------------------- #
# Sender identity
# --------------------------------------------------------------------------- #
def test_multiple_from_dropped(tmp_path):
    a, *_ = _adapter(tmp_path)
    assert a.parse(_raw(frm="ceo@co.com, vp@co.com")) is None


def test_sender_lowercased_as_user_id(tmp_path):
    a, *_ = _adapter(tmp_path)
    msg = a.parse(_raw(frm="CEO@Co.Com"))
    assert msg is not None
    assert msg.user_id == "ceo@co.com"


# --------------------------------------------------------------------------- #
# Per-sender hourly turn cap (ALWAYS on; before any model call) (P4S-10)
# --------------------------------------------------------------------------- #
def test_per_sender_cap_refuses_before_inbound(tmp_path):
    a, *_ = _adapter(tmp_path, cfg={"per_sender_turns_per_hour": 2})
    assert a.parse(_raw(body="q1")) is not None
    assert a.parse(_raw(body="q2")) is not None
    # Third within the hour: dropped at the adapter (no InboundMessage).
    assert a.parse(_raw(body="q3")) is None


def test_per_sender_cap_always_on_even_unconfigured(tmp_path):
    """Cap defaults to 10 even when not set; the 11th is dropped."""
    cfg = {"allowlist": {"ceo@co.com": {"role": "user", "instance": "main"}}}
    # remove the cap key entirely
    a = EmailAdapter(cfg, OWN, FakeIMAP(), FakeSMTP(),
                     clock=lambda: 1000.0, profile_dir=tmp_path)
    for i in range(10):
        assert a.parse(_raw(body=f"q{i}")) is not None
    assert a.parse(_raw(body="overflow")) is None


# --------------------------------------------------------------------------- #
# Cursor: UID/UIDVALIDITY, reset-to-UIDNEXT, restart-no-replay (P4S-12)
# --------------------------------------------------------------------------- #
def test_fetch_returns_new_uids(tmp_path):
    msgs = {5: _raw(body="m5"), 6: _raw(body="m6")}
    imap = FakeIMAP(uidvalidity=100, uidnext=7, messages=msgs)
    a, *_ = _adapter(tmp_path, imap=imap)
    out = a.fetch()
    assert [u for u, _ in out] == [5, 6]


def test_commit_persists_cursor_and_restart_no_replay(tmp_path):
    msgs = {5: _raw(body="m5"), 6: _raw(body="m6")}
    imap = FakeIMAP(uidvalidity=100, uidnext=7, messages=msgs)
    a, *_ = _adapter(tmp_path, imap=imap)
    out = a.fetch()
    for uid, _ in out:
        a.advance(uid)
    a.commit()
    cursor_file = tmp_path / "email_cursor_default.json"
    assert cursor_file.exists()
    data = json.loads(cursor_file.read_text())
    assert data["uidvalidity"] == 100
    assert data["last_uid"] == 6

    # Restart with the SAME mailbox: handled mail must not replay.
    imap2 = FakeIMAP(uidvalidity=100, uidnext=7, messages=msgs)
    a2, *_ = _adapter(tmp_path, imap=imap2)
    out2 = a2.fetch()
    assert out2 == []


def test_uidvalidity_change_resets_to_uidnext(tmp_path):
    # First run: cursor at uidvalidity=100, last_uid=6.
    cursor_file = tmp_path / "email_cursor_default.json"
    cursor_file.write_text(json.dumps({"uidvalidity": 100, "last_uid": 6}))
    # Mailbox now has a DIFFERENT uidvalidity (recreated) + old low UIDs.
    imap = FakeIMAP(uidvalidity=999, uidnext=3,
                    messages={1: _raw(body="old1"), 2: _raw(body="old2")})
    a, *_ = _adapter(tmp_path, imap=imap)
    out = a.fetch()
    # Reset to UIDNEXT-1 == 2: nothing strictly newer this pass (no replay).
    assert out == []
    assert a._uidvalidity == 999
    assert a._last_uid == 2


def test_corrupt_cursor_starts_from_uidnext(tmp_path):
    cursor_file = tmp_path / "email_cursor_default.json"
    cursor_file.write_text("{ not valid json !!!")
    imap = FakeIMAP(uidvalidity=100, uidnext=50,
                    messages={1: _raw(body="old"), 49: _raw(body="recent")})
    a, *_ = _adapter(tmp_path, imap=imap)
    out = a.fetch()
    # Corruption => start from current UIDNEXT (49), no unbounded replay.
    assert out == []
    assert a._last_uid == 49


# --------------------------------------------------------------------------- #
# End-to-end through a real GatewayCore (acceptance)
# --------------------------------------------------------------------------- #
class FakeTurn:
    def __init__(self, text="the grounded answer"):
        self.text = text
        self.envelopes = [{"verdict": "grounded"}]
        self.grounding = "enforce"
        self.repairs = 0
        self.redacted_count = 0
        self.withheld = False


class FakeLoop:
    def __init__(self):
        self.seen_ceiling = None

    def run_turn(self, text):
        return FakeTurn()


def _core(tmp_path, surface_cfg, captured):
    root = tmp_path / "root"
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)

    def builder(user_id, instance, r, *, ceiling_override, write_actor, write_gate):
        captured.append({"ceiling_override": ceiling_override,
                         "write_actor": write_actor})
        return FakeLoop()

    return GatewayCore(surface_cfg, "email", {"main": root}, builder,
                       clock=lambda: 1000.0, root_lock_factory=_noop_lock), root


def test_clean_mail_produces_single_recipient_reply(tmp_path):
    surface_cfg = {
        "allowlist": {"ceo@co.com": {"role": "user", "instance": "main"}},
        "max_sensitivity": "internal",
        "authserv_id": "mx.co.com",
        "per_user_writes_per_hour": 20,
    }
    captured = []
    core, root = _core(tmp_path, surface_cfg, captured)
    a, imap, smtp = _adapter(tmp_path, cfg=surface_cfg)
    msg = a.parse(_raw(auth_results="mx.co.com; dmarc=pass"))
    reply = core.handle(msg)
    assert reply is not None
    a.send(reply)
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == "ceo@co.com"
    # Private (dmarc verified) => internal ceiling.
    assert captured[0]["ceiling_override"] == "internal"
    assert captured[0]["write_actor"] == "gateway_user:email:ceo@co.com"


def test_listed_mail_served_at_public(tmp_path):
    surface_cfg = {
        "allowlist": {"ceo@co.com": {"role": "user", "instance": "main"}},
        "max_sensitivity": "internal",
        "authserv_id": "mx.co.com",
        "per_user_writes_per_hour": 20,
    }
    captured = []
    core, root = _core(tmp_path, surface_cfg, captured)
    a, imap, smtp = _adapter(tmp_path, cfg=surface_cfg)
    msg = a.parse(_raw(cc="other@co.com", auth_results="mx.co.com; dmarc=pass"))
    core.handle(msg)
    # Cc'd => non-private => core caps at public.
    assert captured[0]["ceiling_override"] == "public"


def test_unknown_sender_ignored_but_cursor_advances(tmp_path):
    surface_cfg = {
        "allowlist": {"ceo@co.com": {"role": "user", "instance": "main"}},
        "max_sensitivity": "internal",
        "per_user_writes_per_hour": 20,
    }
    captured = []
    core, root = _core(tmp_path, surface_cfg, captured)
    msgs = {5: _raw(frm="stranger@evil.com", body="hi")}
    imap = FakeIMAP(uidvalidity=100, uidnext=6, messages=msgs)
    a, _, smtp = _adapter(tmp_path, cfg=surface_cfg, imap=imap)
    out = a.fetch()
    for uid, raw in out:
        a.advance(uid)
        m = a.parse(raw)
        if m is not None:
            reply = core.handle(m)
            if reply is not None:
                a.send(reply)
    a.commit()
    # Unknown sender: served by core => denied (not in allowlist) => no reply.
    assert smtp.sent == []
    # Cursor still advanced past the handled UID.
    data = json.loads((tmp_path / "email_cursor_default.json").read_text())
    assert data["last_uid"] == 5
