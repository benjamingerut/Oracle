"""Tests for agentloop/policy_bridge.py (SPEC S3 / S10)."""
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
