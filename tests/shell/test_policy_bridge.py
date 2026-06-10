"""Tests for agentloop/policy_bridge.py (SPEC S3 / S10, S1 remediation)."""
from __future__ import annotations

import pytest

from oracle_agent.agentloop import policy_bridge as pb


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:11434/v1", "local_agent"),
    ("http://localhost:8080/v1", "local_agent"),
    ("http://[::1]:8080/v1", "local_agent"),
    ("https://api.openai.com/v1", "external"),
    ("http://127.0.0.2:8080/v1", "local_agent"),     # 127/8 is all loopback, on-box
    ("http://169.254.169.254/v1", "external"),       # cloud metadata, NOT loopback
    ("not a url", "external"),
    ("", "external"),
])
def test_environment_classification(url, expected):
    assert pb.environment_for(url) == expected


def test_userinfo_host_does_not_fool_classifier():
    # urlsplit hostname of http://127.0.0.1@evil.com is "evil.com"
    assert pb.environment_for("http://127.0.0.1@evil.com/v1") == "external"


def test_localhost_subdomain_is_external():
    assert pb.environment_for("http://localhost.evil.com/v1") == "external"


# S1 remediation: DNS resolution is no longer performed.
# A hostname that would resolve to 127.0.0.1 via DNS must be classified external.
def test_rebinding_shaped_hostname_is_external():
    """A hostname other than 'localhost' is always external, no DNS consulted."""
    # These are names that an attacker could configure to resolve to 127.0.0.1.
    assert pb.environment_for("http://localhost.attacker.example/v1") == "external"
    assert pb.environment_for("http://myapp.local/v1") == "external"
    assert pb.environment_for("http://loopback.internal/v1") == "external"


def test_only_exact_localhost_name_is_loopback():
    """Only the exact string 'localhost' (case-insensitive handled by urlsplit)
    qualifies as a loopback name; all other names are external regardless of
    whether they would resolve to loopback addresses."""
    assert pb.environment_for("http://LOCALHOST/v1") == "local_agent"  # urlsplit lowercases
    assert pb.environment_for("http://localhost./v1") == "external"    # trailing dot = different name


def test_ipv6_loopback_forms():
    """Both bracketed and bare ::1 are local_agent."""
    assert pb.environment_for("http://[::1]/v1") == "local_agent"
    # Bare ::1 is not a valid HTTP URL host (no brackets), urlsplit hostname returns empty
    # -> external. This is correct: a properly formed IPv6 URL must use brackets.
    assert pb.environment_for("http://::1/v1") == "external"


def test_min_sensitivity():
    assert pb.min_sensitivity("internal", "public") == "public"
    assert pb.min_sensitivity("confidential", "secret") == "confidential"


def test_ceiling_external_is_public(spawned_root):
    ceiling = pb.max_sensitivity_for(spawned_root, "external")
    assert ceiling == "public"


def test_ceiling_local_agent_is_internal(spawned_root):
    # allow-minimized (confidential+) is NOT a grant -> caps at internal (H2)
    ceiling = pb.max_sensitivity_for(spawned_root, "local_agent")
    assert ceiling == "internal"


def test_ceiling_fails_closed_to_public_on_error(tmp_path):
    # No oracle root here -> policy check errors -> public.
    ceiling = pb.max_sensitivity_for(tmp_path, "external")
    assert ceiling == "public"


def test_ceiling_with_injected_policy_check(spawned_root):
    # allow-minimized must NOT raise the ceiling.
    def fake(label, env):
        return {"public": "allow", "internal": "allow",
                "confidential": "allow-minimized"}.get(label, "deny")
    ceiling = pb.max_sensitivity_for(spawned_root, "local_agent", policy_check=fake)
    assert ceiling == "internal"


def test_sensitivity_order_from_root(spawned_root):
    order = pb.sensitivity_order(spawned_root)
    assert order[0] == "public"
    assert "secret" in order


# ---------------------------------------------------------------------------
# S1: validate_sensitivity_label — override validation
# ---------------------------------------------------------------------------

def test_validate_sensitivity_label_known_labels():
    """Known labels pass through unchanged."""
    for label in pb.CANONICAL_ORDER:
        assert pb.validate_sensitivity_label(label) == label


def test_validate_sensitivity_label_unknown_raises():
    """Unknown labels raise ValueError with actionable text."""
    with pytest.raises(ValueError, match="Unknown sensitivity label"):
        pb.validate_sensitivity_label("Public")   # wrong case
    with pytest.raises(ValueError, match="Unknown sensitivity label"):
        pb.validate_sensitivity_label("top-secret")  # not in order
    with pytest.raises(ValueError, match="Unknown sensitivity label"):
        pb.validate_sensitivity_label("")


def test_validate_sensitivity_label_custom_order():
    """Validation uses the supplied order, not just CANONICAL_ORDER."""
    custom = ["low", "med", "high"]
    assert pb.validate_sensitivity_label("med", custom) == "med"
    with pytest.raises(ValueError):
        pb.validate_sensitivity_label("public", custom)  # not in custom order


# ---------------------------------------------------------------------------
# S1: builder ceiling_override validation (HANDOFF surface: builder is owned
# by S1; cli.py / gateway are not owned here, so we test via builder directly)
# ---------------------------------------------------------------------------

def test_builder_rejects_unknown_ceiling_override(spawned_root, tmp_path):
    """build_loop must raise ValueError for an unknown ceiling_override label."""
    import json
    from oracle_agent.agentloop.builder import build_loop

    cfg = {
        "provider": {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "test-model",
            "api_key_env": "",
            "max_tokens": 256,
        },
        "chat": {"max_iterations": 2, "tool_result_max_chars": 1000,
                 "history_max_chars": 10000},
        "instances": {},
        "ingest_roots": [],
    }
    with pytest.raises(ValueError, match="Unknown sensitivity label"):
        build_loop(cfg, spawned_root, surface="chat",
                   ceiling_override="Public")   # wrong case


def test_builder_accepts_valid_ceiling_override(spawned_root):
    """build_loop accepts a valid ceiling_override without raising."""
    from oracle_agent.agentloop.builder import build_loop

    cfg = {
        "provider": {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "test-model",
            "api_key_env": "",
            "max_tokens": 256,
        },
        "chat": {"max_iterations": 2, "tool_result_max_chars": 1000,
                 "history_max_chars": 10000},
        "instances": {},
        "ingest_roots": [],
    }
    # "public" is a valid label; should not raise
    loop = build_loop(cfg, spawned_root, surface="chat",
                      ceiling_override="public")
    assert loop is not None
