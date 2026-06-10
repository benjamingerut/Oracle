#!/usr/bin/env python3
"""Tests for the policy gate (policy.py).

Covers the three binding guarantees of the floor policy chokepoint:

  * check_processing mirrors PROCESSING-MATRIX.md exactly, and stricter-row-wins
    on any uncertainty (unknown sensitivity collapses to the strictest row);
  * gate_export REFUSES confidential/restricted/secret without admin approval
    (writing nothing), and on success appends a metadata-only export_event;
  * require_role reads oracle.yml governance.roles and DENIES a user actor an
    admin-only capability while permitting admin.

Self-contained: depends only on policy.py + the floor (ledger, oracle_yaml) and
the shared ``minimal_oracle`` conftest fixture. The conftest injects _tools on
sys.path so ``import policy`` resolves as a bare module.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import policy  # noqa: E402
import ledger  # noqa: E402


# --------------------------------------------------------------------------- #
# check_processing: full matrix correctness
# --------------------------------------------------------------------------- #
# Expected verdicts mirror PROCESSING-MATRIX.md / policy.PROCESSING_MATRIX.
_EXPECTED = {
    ("public", "local_deterministic"): "allow",
    ("public", "local_agent"): "allow",
    ("public", "external"): "allow",
    ("internal", "local_deterministic"): "allow",
    ("internal", "local_agent"): "allow",
    ("internal", "external"): "deny",
    ("confidential", "local_deterministic"): "allow",
    ("confidential", "local_agent"): "allow-minimized",
    ("confidential", "external"): "deny",
    ("restricted", "local_deterministic"): "allow-minimized",
    ("restricted", "local_agent"): "allow-minimized",
    ("restricted", "external"): "deny",
    ("secret", "local_deterministic"): "allow-minimized",
    ("secret", "local_agent"): "allow-minimized",
    ("secret", "external"): "deny",
}


@pytest.mark.parametrize("key,expected", list(_EXPECTED.items()))
def test_check_processing_full_matrix(key, expected):
    sensitivity, env = key
    assert policy.check_processing(sensitivity, env) == expected


def test_check_processing_only_returns_contract_verdicts():
    valid = {"allow", "allow-minimized", "deny"}
    for sensitivity in policy.SENSITIVITY_ORDER:
        for env in policy.ENVIRONMENTS:
            assert policy.check_processing(sensitivity, env) in valid


def test_external_never_auto_allows_above_public():
    # Only public may be processed externally without an explicit approval gate.
    for sensitivity in ("internal", "confidential", "restricted", "secret"):
        assert policy.check_processing(sensitivity, "external") == "deny"
    assert policy.check_processing("public", "external") == "allow"


# --------------------------------------------------------------------------- #
# stricter-row-wins
# --------------------------------------------------------------------------- #
def test_unknown_sensitivity_collapses_to_strictest_row():
    # An unknown / garbage label must be treated as 'secret' (strictest), never
    # as 'public'. So local_agent => allow-minimized, external => deny.
    assert policy.check_processing("totally-unknown", "local_agent") == "allow-minimized"
    assert policy.check_processing("totally-unknown", "external") == "deny"


def test_none_sensitivity_is_strict():
    assert policy.check_processing(None, "external") == "deny"
    assert policy.check_processing(None, "local_deterministic") == "allow-minimized"


def test_case_insensitive_sensitivity():
    assert policy.check_processing("SECRET", "external") == "deny"
    assert policy.check_processing("Public", "external") == "allow"


def test_unknown_environment_raises():
    with pytest.raises(ValueError):
        policy.check_processing("public", "the-cloud")


# --------------------------------------------------------------------------- #
# gate_export: denial without approval
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sensitivity", ["confidential", "restricted", "secret"])
def test_export_denied_without_approval(sensitivity, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(PermissionError):
        policy.gate_export(sensitivity, approval=None, actor="user1", role="user", root=root)
    # Nothing should have been written to the export ledger.
    led = root / "Meta.nosync" / "ledgers" / "export_event.jsonl"
    rows, _ = ledger.load(led)
    assert rows == []


@pytest.mark.parametrize("placeholder", ["", "  ", "pending", "TBD", "n/a", "changeme"])
def test_export_rejects_placeholder_approval(placeholder, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(PermissionError):
        policy.gate_export(
            "confidential", approval=placeholder, actor="a", role="admin", root=root
        )


@pytest.mark.parametrize("sensitivity", ["public", "internal"])
def test_export_low_sensitivity_needs_no_approval(sensitivity, tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    event = policy.gate_export(
        sensitivity, approval=None, actor="user1", role="user", root=root
    )
    assert event["classification"] == sensitivity
    # A successful low-sensitivity export is still logged.
    led = root / "Meta.nosync" / "ledgers" / "export_event.jsonl"
    rows, _ = ledger.load(led)
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# gate_export: success path emits a metadata-only export_event
# --------------------------------------------------------------------------- #
def test_export_with_approval_emits_event(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    event = policy.gate_export(
        "confidential",
        approval="dir-2026-01-02-admin-approval",
        actor="admin1",
        role="admin",
        root=root,
        destination="_OUTPUT",
        purpose="board pack",
    )
    assert event["classification"] == "confidential"
    assert event["approval"] == "dir-2026-01-02-admin-approval"
    assert event["actor"] == "admin1"
    assert "drop_id" in event and event["drop_id"]

    led = root / "Meta.nosync" / "ledgers" / "export_event.jsonl"
    rows, warnings = ledger.load(led)
    assert warnings == []
    assert len(rows) == 1
    row = rows[0]
    # Contract shape: metadata only, NO payload/content field.
    assert "payload" not in row
    assert "content" not in row
    for field in ("drop_id", "ts", "actor", "role", "classification", "destination", "approval", "purpose"):
        assert field in row, field


def test_export_event_carries_no_payload(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    policy.gate_export(
        "restricted", approval="ok-ref", actor="admin1", role="admin", root=root,
    )
    led = root / "Meta.nosync" / "ledgers" / "export_event.jsonl"
    rows, _ = ledger.load(led)
    forbidden = {"payload", "content", "body", "bytes", "data"}
    for row in rows:
        assert not (forbidden & set(row.keys())), row


# --------------------------------------------------------------------------- #
# require_role: user denied admin caps, admin allowed
# --------------------------------------------------------------------------- #
def test_require_role_denies_user_admin_capability(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # 'change_architecture' is in the user's explicit cannot list.
    with pytest.raises(PermissionError):
        policy.require_role("user1", "user", "change_architecture", root=root)


def test_require_role_denies_user_capability_not_in_can(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # A capability a user simply does not have (default-deny), not in can/cannot.
    with pytest.raises(PermissionError):
        policy.require_role("user1", "user", "approve_sensitive_export", root=root)


def test_require_role_allows_user_permitted_capability(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    # Listed in user's can -> permitted, returns None without raising.
    assert policy.require_role("user1", "user", "ask_questions", root=root) is None
    assert policy.require_role("user1", "user", "give_feedback", root=root) is None


def test_require_role_admin_permitted_admin_capability(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    assert policy.require_role("admin1", "admin", "change_architecture", root=root) is None
    # Admin is the authority root: permitted even a capability not enumerated.
    assert policy.require_role("admin1", "admin", "some_new_capability", root=root) is None


def test_require_role_unknown_role_default_denies(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(PermissionError):
        policy.require_role("ghost", "robot", "change_architecture", root=root)


def test_require_role_empty_args_raise(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    with pytest.raises(PermissionError):
        policy.require_role("a", "", "ask_questions", root=root)
    with pytest.raises(PermissionError):
        policy.require_role("a", "user", "", root=root)


# --------------------------------------------------------------------------- #
# record_redaction
# --------------------------------------------------------------------------- #
def test_record_redaction_appends_event(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    event = policy.record_redaction(
        "admin1",
        reason="GDPR erasure request",
        action="stub_and_remove",
        root=root,
        approved_by="admin1",
        stub_location="Memory.nosync/Sources/redacted-stub.md",
    )
    assert event["reason"] == "GDPR erasure request"
    assert "drop_id" in event and event["drop_id"]
    led = root / "Meta.nosync" / "ledgers" / "redaction_event.jsonl"
    rows, _ = ledger.load(led)
    assert len(rows) == 1
    for field in ("drop_id", "ts", "actor", "reason", "approved_by", "action", "stub_location"):
        assert field in rows[0], field


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_check_prints_verdict(capsys):
    rc = policy.main(["check", "--sensitivity", "secret", "--env", "external"])
    out = capsys.readouterr().out.strip()
    assert out == "deny"
    assert rc == 1  # deny -> nonzero


def test_cli_check_allow_is_zero(capsys):
    rc = policy.main(["check", "--sensitivity", "public", "--env", "local_agent"])
    out = capsys.readouterr().out.strip()
    assert out == "allow"
    assert rc == 0


def test_cli_export_refused_without_approval(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    rc = policy.main([
        "--root", str(root), "export",
        "--sensitivity", "confidential", "--actor", "u", "--role", "user",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err


def test_cli_role_denied(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    rc = policy.main([
        "--root", str(root), "role",
        "--actor", "u", "--role", "user", "--capability", "change_architecture",
    ])
    assert rc == 2
    assert "DENIED" in capsys.readouterr().err


def test_cli_role_granted(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    rc = policy.main([
        "--root", str(root), "role",
        "--actor", "a", "--role", "admin", "--capability", "change_architecture",
    ])
    assert rc == 0
    assert "GRANTED" in capsys.readouterr().out
