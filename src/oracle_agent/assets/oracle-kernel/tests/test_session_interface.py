#!/usr/bin/env python3
"""Tests for session_interface.py.

The session interface is a stateless UX/control-plane gate. These tests prove
the default User interface, admin approval prompting, optional legacy switch
resolution, interface/capability blocking, CLI dispatch, and the
non-authentication boundary with policy.py.
"""
from __future__ import annotations

import pytest

import oracle_cli
import policy
import session_interface


def test_new_session_defaults_to_user(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    assert session_interface.default_interface(root) == "user"
    data = session_interface.contract(root)
    assert data["default"] == "user"
    assert data["startup_prompt"] is False
    assert data["reset_policy"] == "every_new_session"


def test_contract_exposes_goal_clarity_policy(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    policy = session_interface.contract(root)["goal_clarity_policy"]

    assert policy["version"] == "goal-clarity-policy/v1"
    assert policy["default_behavior"] == "proportional_dialectic_before_execution"
    assert policy["dialectic_method"]["question_style"] == "one_at_a_time"
    assert policy["dialectic_method"]["include_recommended_answer"] is True
    assert policy["dialectic_method"]["resolve_dependencies"] == "one_branch_at_a_time"
    assert policy["dialectic_method"]["inspect_available_material_first"] is True
    assert "executing_tools" in policy["applies_to"]
    assert "compute_cost" in policy["proportionality_axes"]
    assert policy["levels"]["quick_low_compute"]["clarity_threshold"] == "low"
    assert policy["levels"]["quick_low_compute"]["dialectic_default"] == "none_or_one_turn"
    assert policy["levels"]["bounded_standard"]["clarity_threshold"] == "medium"
    assert policy["levels"]["extended_high_compute"]["clarity_threshold"] == "high"
    assert (
        policy["levels"]["extended_high_compute"]["dialectic_default"]
        == "explicit_specing_until_clear"
    )
    assert "large_or_multi_file_change" in policy["escalation_triggers"]
    assert "request_is_trivial" in policy["do_not_over_ask_when"]
    assert policy["proceed_with_assumptions_allowed"] is True


def test_resolve_text_does_not_require_slash_commands(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    result = session_interface.resolve_text(root, "/admin change connector policy")
    assert result["interface"] == "user"
    assert result["switched"] is False
    assert result["text"] == "/admin change connector policy"


def test_legacy_switch_commands_still_work_when_configured(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    cfg = root / "oracle.yml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "  reset_policy: every_new_session\n",
            (
                "  reset_policy: every_new_session\n"
                "  switch_commands:\n"
                "    admin: \"/admin\"\n"
                "    user: \"/user\"\n"
            ),
        ),
        encoding="utf-8",
    )

    admin = session_interface.resolve_text(root, "/admin change connector policy")
    assert admin["interface"] == "admin"
    assert admin["switched"] is True
    assert admin["text"] == "change connector policy"

    embedded = session_interface.resolve_text(root, "please /admin change policy")
    assert embedded["interface"] == "user"
    assert embedded["switched"] is False

    near_miss = session_interface.resolve_text(root, "/administer change policy")
    assert near_miss["interface"] == "user"
    assert near_miss["switched"] is False


def test_gate_blocks_user_control_plane_and_allows_business_capability(
    tmp_path,
    minimal_oracle,
):
    root = minimal_oracle(tmp_path)

    denied = session_interface.gate(root, "user", "change_architecture")
    assert denied["allowed"] is False
    assert denied["reason"] == "blocked_by_session_interface"
    assert denied["admin_prompt"] == session_interface.DEFAULT_ADMIN_PROMPT
    assert denied["redirect"] == session_interface.DEFAULT_ADMIN_PROMPT
    assert denied["requires_admin_approval"] is True
    assert denied["next_interface_on_approval"] == "admin"
    assert denied["role_gate_still_required"] == "policy.require_role"

    allowed = session_interface.gate(root, "user", "ask_questions")
    assert allowed["allowed"] is True
    assert allowed["reason"] == "allowed_by_session_interface"
    assert allowed["requires_admin_approval"] is False


def test_admin_interface_is_not_authentication(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)

    admin_interface = session_interface.gate(root, "admin", "change_architecture")
    assert admin_interface["allowed"] is True
    assert admin_interface["role_gate_still_required"] == "policy.require_role"

    with pytest.raises(PermissionError):
        policy.require_role("user1", "user", "change_architecture", root=root)


def test_oracle_cli_dispatches_session_default_and_gate(
    tmp_path,
    minimal_oracle,
    capsys,
):
    root = minimal_oracle(tmp_path)

    rc = oracle_cli.main(["session", "--root", str(root), "default"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "user"

    rc = oracle_cli.main([
        "session",
        "--root",
        str(root),
        "gate",
        "--interface",
        "user",
        "--capability",
        "change_architecture",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert session_interface.DEFAULT_ADMIN_PROMPT in captured.err
