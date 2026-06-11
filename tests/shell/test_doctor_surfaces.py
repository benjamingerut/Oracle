"""Per-surface gateway doctor matrix + briefing-target refusal (Phase 4 / P4-T7/T8).

The shell doctor validates each ENABLED gateway surface against the pinned
matrix (P4S-20) and refuses any briefing delivery target that does not resolve
to an allowlisted private identity (P4S-15). Doctor stays read-only.

These tests live in their OWN file (not test_cli.py / test_doctor_connectors.py)
to avoid colliding with the concurrent agents that own those files. They drive
the surface-check helpers directly with a fresh ``Report`` (so they need no
spawned root and no live network), plus a couple of end-to-end ``doctor.run``
assertions. The matrix replicates the adapters' own startup gates read-only
(email.py's authserv/dmarc cap, http.py's validate_bind).
"""
from __future__ import annotations

import json

import pytest

from oracle_agent import config, doctor
from oracle_agent.doctor import Report


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _rows(rep: Report, needle: str) -> list[tuple[str, str, str]]:
    return [(lvl, msg, fix) for (lvl, msg, fix) in rep.rows if needle in msg]


def _levels(rep: Report, needle: str) -> set[str]:
    return {lvl for (lvl, msg, _) in rep.rows if needle in msg}


# --------------------------------------------------------------------------- #
# telegram surface
# --------------------------------------------------------------------------- #
def test_telegram_disabled_emits_no_row():
    rep = Report()
    doctor._check_telegram_surface(rep, {"enabled": False})
    assert _rows(rep, "telegram") == []


def test_telegram_token_unresolved_fails(profile):
    rep = Report()
    doctor._check_telegram_surface(
        rep, {"enabled": True, "token_env": "ORACLE_TELEGRAM_TOKEN_MISSING",
              "allowlist": {"123": {}}})
    assert "fail" in _levels(rep, "telegram")


def test_telegram_empty_allowlist_warns(profile, monkeypatch):
    monkeypatch.setenv("ORACLE_TG_TOK", "real-token")
    rep = Report()
    doctor._check_telegram_surface(
        rep, {"enabled": True, "token_env": "ORACLE_TG_TOK", "allowlist": {}})
    assert "warn" in _levels(rep, "telegram")


def test_telegram_healthy_ok(profile, monkeypatch):
    monkeypatch.setenv("ORACLE_TG_TOK", "real-token")
    rep = Report()
    doctor._check_telegram_surface(
        rep, {"enabled": True, "token_env": "ORACLE_TG_TOK",
              "allowlist": {"123": {}}})
    assert _levels(rep, "telegram") == {"ok"}


# --------------------------------------------------------------------------- #
# slack surface
# --------------------------------------------------------------------------- #
def test_slack_token_unresolved_fails(profile):
    rep = Report()
    doctor._check_slack_surface(
        rep, {"enabled": True, "token_env": "ORACLE_SLACK_TOKEN_MISSING",
              "allowlist": {"U1": {}}})
    assert "fail" in _levels(rep, "slack")


def test_slack_empty_allowlist_warns(profile, monkeypatch):
    monkeypatch.setenv("ORACLE_SLACK_TOK", "xoxb-real")
    rep = Report()
    doctor._check_slack_surface(
        rep, {"enabled": True, "token_env": "ORACLE_SLACK_TOK", "allowlist": {}})
    rows = _rows(rep, "slack")
    assert rows and rows[0][0] == "warn"
    assert "allowlist empty" in rows[0][1]


def test_slack_websocket_lib_absent_warns_disabled(profile, monkeypatch):
    monkeypatch.setenv("ORACLE_SLACK_TOK", "xoxb-real")
    monkeypatch.setattr(doctor, "_websocket_lib_present", lambda: False)
    rep = Report()
    doctor._check_slack_surface(
        rep, {"enabled": True, "token_env": "ORACLE_SLACK_TOK",
              "allowlist": {"U1": {}}})
    rows = _rows(rep, "slack")
    assert rows and rows[0][0] == "warn"
    assert "websocket lib absent — disabled" in rows[0][1]


def test_slack_healthy_ok_when_dep_present(profile, monkeypatch):
    monkeypatch.setenv("ORACLE_SLACK_TOK", "xoxb-real")
    monkeypatch.setattr(doctor, "_websocket_lib_present", lambda: True)
    rep = Report()
    doctor._check_slack_surface(
        rep, {"enabled": True, "token_env": "ORACLE_SLACK_TOK",
              "allowlist": {"U1": {}}})
    assert _levels(rep, "slack") == {"ok"}


# --------------------------------------------------------------------------- #
# email surface
# --------------------------------------------------------------------------- #
def _email_cfg(**over) -> dict:
    base = {
        "enabled": True,
        "user_env": "ORACLE_EMAIL_USER",
        "pass_env": "ORACLE_EMAIL_PASS",
        "allowlist": {"ceo@co.com": {}},
        "imap_host": "imap.co.com",
        "smtp_host": "smtp.co.com",
        "authserv_id": None,
        "dedicated_mailbox_ack": True,
    }
    base.update(over)
    return base


def _email_creds(monkeypatch):
    monkeypatch.setenv("ORACLE_EMAIL_USER", "oracle@co.com")
    monkeypatch.setenv("ORACLE_EMAIL_PASS", "app-password")


def test_email_creds_unresolved_fails(profile):
    rep = Report()
    doctor._check_email_surface(rep, _email_cfg())
    assert "fail" in _levels(rep, "email")


def test_email_empty_allowlist_warns(profile, monkeypatch):
    _email_creds(monkeypatch)
    rep = Report()
    doctor._check_email_surface(rep, _email_cfg(allowlist={}))
    rows = _rows(rep, "email")
    assert rows and rows[0][0] == "warn"
    assert "allowlist empty" in rows[0][1]


def test_email_missing_tls_host_fails(profile, monkeypatch):
    _email_creds(monkeypatch)
    rep = Report()
    doctor._check_email_surface(rep, _email_cfg(smtp_host=""))
    rows = _rows(rep, "TLS host")
    assert rows and rows[0][0] == "fail"


def test_email_no_dedicated_mailbox_ack_warns(profile, monkeypatch):
    _email_creds(monkeypatch)
    rep = Report()
    doctor._check_email_surface(rep, _email_cfg(dedicated_mailbox_ack=False,
                                                authserv_id="mx.co.com"))
    rows = _rows(rep, "dedicated-mailbox")
    assert rows and rows[0][0] == "warn"


def test_email_authserv_unset_warns_public_capped(profile, monkeypatch):
    """authserv_id unset => '[warn] email capped at public' (P4S-10), regardless
    of what max_sensitivity the config names."""
    _email_creds(monkeypatch)
    rep = Report()
    doctor._check_email_surface(
        rep, _email_cfg(authserv_id=None, max_sensitivity="internal"))
    rows = _rows(rep, "capped at public")
    assert rows and rows[0][0] == "warn"


def test_email_authserv_set_ok(profile, monkeypatch):
    _email_creds(monkeypatch)
    rep = Report()
    doctor._check_email_surface(rep, _email_cfg(authserv_id="mx.co.com"))
    # No public-cap warning when authserv_id is set; the surface row is OK.
    assert _rows(rep, "capped at public") == []
    assert "ok" in _levels(rep, "email enabled")


# --------------------------------------------------------------------------- #
# http surface (allowlist non-empty does NOT apply — identity is the token)
# --------------------------------------------------------------------------- #
def _http_cfg(**over) -> dict:
    base = {
        "enabled": True,
        "bind": "127.0.0.1",
        "port": 8765,
        "token_env": "ORACLE_HTTP_TOKEN",
        "principal": "http-operator",
    }
    base.update(over)
    return base


def test_http_token_unresolved_fails(profile):
    rep = Report()
    doctor._check_http_surface(rep, _http_cfg(token_env="ORACLE_HTTP_TOKEN_MISSING"))
    rows = _rows(rep, "http")
    assert rows and rows[0][0] == "fail"
    assert "refuses to start" in rows[0][1]


def test_http_hostname_bind_refused(profile, monkeypatch):
    monkeypatch.setenv("ORACLE_HTTP_TOKEN", "secret-token")
    rep = Report()
    doctor._check_http_surface(rep, _http_cfg(bind="localhost"))
    rows = _rows(rep, "literal loopback IP")
    assert rows and rows[0][0] == "fail"


def test_http_zero_address_bind_refused(profile, monkeypatch):
    monkeypatch.setenv("ORACLE_HTTP_TOKEN", "secret-token")
    rep = Report()
    doctor._check_http_surface(rep, _http_cfg(bind="0.0.0.0"))
    rows = _rows(rep, "literal loopback IP")
    assert rows and rows[0][0] == "fail"


def test_http_bad_port_fails(profile, monkeypatch):
    monkeypatch.setenv("ORACLE_HTTP_TOKEN", "secret-token")
    rep = Report()
    doctor._check_http_surface(rep, _http_cfg(port=99999))
    rows = _rows(rep, "out of range")
    assert rows and rows[0][0] == "fail"


def test_http_no_allowlist_check_applies(profile, monkeypatch):
    """HTTP's identity is the TOKEN, not an allowlist (P4S-20): a healthy HTTP
    surface with NO allowlist key is OK, never a 'allowlist empty' warning."""
    monkeypatch.setenv("ORACLE_HTTP_TOKEN", "secret-token")
    rep = Report()
    doctor._check_http_surface(rep, _http_cfg())  # no allowlist key at all
    assert _levels(rep, "http enabled") == {"ok"}
    assert _rows(rep, "allowlist") == []


# --------------------------------------------------------------------------- #
# briefings — every target must resolve to an allowlisted private identity
# --------------------------------------------------------------------------- #
def _cfg_with_allowlists() -> dict:
    return {
        "gateway": {
            "telegram": {"allowlist": {"12345": {"role": "user"}}},
            "email": {"allowlist": {"ceo@co.com": {"role": "user"}}},
        },
    }


def test_briefing_no_block_emits_nothing():
    rep = Report()
    doctor._check_briefings(rep, {"briefings": {}})
    assert _rows(rep, "briefings") == []


def test_briefing_allowlisted_telegram_target_ok():
    cfg = _cfg_with_allowlists()
    cfg["briefings"] = {"main": {"targets": [
        {"surface": "telegram", "user_id": "12345"}]}}
    rep = Report()
    doctor._check_briefings(rep, cfg)
    assert "ok" in _levels(rep, "briefings[main]")
    assert not rep.worst_is_fail()


def test_briefing_allowlisted_email_target_ok():
    cfg = _cfg_with_allowlists()
    cfg["briefings"] = {"main": {"targets": [
        {"surface": "email", "address": "CEO@co.com"}]}}  # case-insensitive
    rep = Report()
    doctor._check_briefings(rep, cfg)
    assert "ok" in _levels(rep, "briefings[main]")


def test_briefing_target_not_allowlisted_is_refused():
    """A briefing target that is NOT in the surface allowlist (a group id, an
    unlisted chat, a list address) is refused (SH-084 enforcer, P4S-15)."""
    cfg = _cfg_with_allowlists()
    cfg["briefings"] = {"main": {"targets": [
        {"surface": "telegram", "user_id": "-100999"}]}}  # group id, not listed
    rep = Report()
    doctor._check_briefings(rep, cfg)
    assert rep.worst_is_fail()
    rows = _rows(rep, "briefings[main]")
    assert any(lvl == "fail" and "refused" in msg for lvl, msg, _ in rows)


def test_briefing_unlisted_email_address_refused():
    cfg = _cfg_with_allowlists()
    cfg["briefings"] = {"main": {"targets": [
        {"surface": "email", "address": "list@co.com"}]}}
    rep = Report()
    doctor._check_briefings(rep, cfg)
    assert rep.worst_is_fail()


def test_briefing_unknown_surface_refused():
    cfg = _cfg_with_allowlists()
    cfg["briefings"] = {"main": {"targets": [
        {"surface": "slack", "user_id": "U1"}]}}
    rep = Report()
    doctor._check_briefings(rep, cfg)
    assert rep.worst_is_fail()


def test_briefing_empty_targets_warns():
    cfg = _cfg_with_allowlists()
    cfg["briefings"] = {"main": {"targets": []}}
    rep = Report()
    doctor._check_briefings(rep, cfg)
    rows = _rows(rep, "no delivery targets")
    assert rows and rows[0][0] == "warn"


# --------------------------------------------------------------------------- #
# briefing delivery-state file (fail closed on corruption)
# --------------------------------------------------------------------------- #
def test_briefing_state_corrupt_fails(profile):
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "briefing_delivery_state.json").write_text("{not json", encoding="utf-8")
    rep = Report()
    doctor._check_briefing_state(rep)
    rows = _rows(rep, "delivery-state file")
    assert rows and rows[0][0] == "fail"


def test_briefing_state_missing_is_ok(profile):
    rep = Report()
    doctor._check_briefing_state(rep)
    assert _rows(rep, "delivery-state file") == []


def test_briefing_state_valid_object_ok(profile):
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "briefing_delivery_state.json").write_text(
        json.dumps({"main:telegram:abc123": True}), encoding="utf-8")
    rep = Report()
    doctor._check_briefing_state(rep)
    assert _rows(rep, "delivery-state file") == []


# --------------------------------------------------------------------------- #
# end-to-end: doctor.run wires the surface + briefing checks in
# --------------------------------------------------------------------------- #
def test_doctor_run_flags_refused_briefing_target(profile, spawned_root):
    """doctor.run surfaces a refused briefing target as a FAIL (integration)."""
    cfg = config.load_config()
    cfg = config.register_instance(cfg, "main", spawned_root)
    cfg["gateway"]["telegram"]["allowlist"] = {"12345": {"role": "user"}}
    cfg["briefings"] = {"main": {"targets": [
        {"surface": "telegram", "user_id": "99999"}]}}  # not allowlisted
    config.save_config(cfg)
    rep = doctor.run("main")
    assert rep.worst_is_fail(), rep.render()
    assert any("refused" in msg for _, msg, _ in rep.rows if "briefings" in msg)


def test_doctor_run_http_hostname_bind_fails(profile, spawned_root, monkeypatch):
    """doctor.run flags a non-literal-loopback HTTP bind as a FAIL (integration)."""
    monkeypatch.setenv("ORACLE_HTTP_TOKEN", "secret-token")
    cfg = config.load_config()
    cfg = config.register_instance(cfg, "main", spawned_root)
    cfg["gateway"]["http"]["enabled"] = True
    cfg["gateway"]["http"]["bind"] = "localhost"
    cfg["gateway"]["http"]["token_env"] = "ORACLE_HTTP_TOKEN"
    config.save_config(cfg)
    rep = doctor.run("main")
    assert any(lvl == "fail" and "loopback" in msg
               for lvl, msg, _ in rep.rows), rep.render()
