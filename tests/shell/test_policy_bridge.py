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


# ---------------------------------------------------------------------------
# C2 (P2S-2): egress veto -- loopback != processing locality
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    """Minimal urllib-opener stand-in. ``handler`` maps url -> body bytes or
    raises if the url is not registered (simulating an unreachable endpoint)."""

    def __init__(self, by_url: dict):
        self._by_url = by_url
        self.opened: list[str] = []

    def open(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        self.opened.append(url)
        if url not in self._by_url:
            raise OSError("connection refused")
        return _FakeResp(self._by_url[url])


def test_egress_veto_cloud_suffix_no_network():
    """A ':cloud' model is vetoed without ANY network call."""
    opener = _FakeOpener({})  # would raise if probed
    reason = pb.egress_veto("http://127.0.0.1:11434/v1",
                            "deepseek-v4-pro:cloud", opener=opener)
    assert reason is not None
    assert ":cloud" in reason or "cloud" in reason.lower()
    assert opener.opened == []  # suffix rule short-circuits the probe


def test_egress_veto_remote_host_in_tags_vetoes():
    """A model whose /api/tags entry carries remote_host is vetoed, naming it."""
    import json
    tags = json.dumps({"models": [
        {"name": "kimi-k2.6:cloud", "remote_host": "ollama.com"},
        {"name": "qwen3.6-32k", "remote_host": ""},
    ]}).encode()
    opener = _FakeOpener({"http://127.0.0.1:11434/api/tags": tags})
    reason = pb.egress_veto("http://127.0.0.1:11434/v1",
                            "kimi-k2.6:cloud", opener=opener)
    assert reason is not None
    # A model that is NOT :cloud but carries remote_host must hit the /api/tags
    # path and be vetoed by the remote_host marker.
    tags2 = json.dumps({"models": [
        {"name": "shadowmodel", "remote_host": "ollama.com"},
    ]}).encode()
    opener2 = _FakeOpener({"http://127.0.0.1:11434/api/tags": tags2})
    reason3 = pb.egress_veto("http://127.0.0.1:11434/v1",
                             "shadowmodel", opener=opener2)
    assert reason3 is not None
    assert "ollama.com" in reason3
    assert opener2.opened == ["http://127.0.0.1:11434/api/tags"]


def test_egress_veto_tags_absent_no_veto():
    """If /api/tags is unreachable, no veto (genuine local vLLM/llama.cpp)."""
    opener = _FakeOpener({})  # nothing registered -> open() raises
    reason = pb.egress_veto("http://127.0.0.1:8000/v1", "local-model",
                            opener=opener)
    assert reason is None
    assert opener.opened == ["http://127.0.0.1:8000/api/tags"]


def test_egress_veto_model_listed_local_no_veto():
    """A model listed with an empty/absent remote_host is NOT vetoed."""
    import json
    tags = json.dumps({"models": [
        {"name": "qwen3.6-32k", "remote_host": ""},
        {"name": "llama3"},
    ]}).encode()
    opener = _FakeOpener({"http://127.0.0.1:11434/api/tags": tags})
    assert pb.egress_veto("http://127.0.0.1:11434/v1", "qwen3.6-32k",
                          opener=opener) is None
    assert pb.egress_veto("http://127.0.0.1:11434/v1", "llama3",
                          opener=opener) is None


def test_egress_veto_model_not_listed_no_veto():
    """A model not present in /api/tags is NOT vetoed (case c)."""
    import json
    tags = json.dumps({"models": [
        {"name": "qwen3.6-32k", "remote_host": "ollama.com"},
    ]}).encode()
    opener = _FakeOpener({"http://127.0.0.1:11434/api/tags": tags})
    assert pb.egress_veto("http://127.0.0.1:11434/v1", "some-other-model",
                          opener=opener) is None


def test_egress_veto_latest_tag_match():
    """A bare model name matches the ':latest'-tagged /api/tags entry."""
    import json
    tags = json.dumps({"models": [
        {"name": "shadow:latest", "remote_host": "ollama.com"},
    ]}).encode()
    opener = _FakeOpener({"http://127.0.0.1:11434/api/tags": tags})
    reason = pb.egress_veto("http://127.0.0.1:11434/v1", "shadow", opener=opener)
    assert reason is not None and "ollama.com" in reason


def test_egress_veto_empty_model_no_veto():
    opener = _FakeOpener({})
    assert pb.egress_veto("http://127.0.0.1:11434/v1", "", opener=opener) is None
    assert opener.opened == []


def _veto_cfg(model: str) -> dict:
    return {
        "provider": {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": model,
            "api_key_env": "",
            "max_tokens": 256,
        },
        "chat": {"max_iterations": 2, "tool_result_max_chars": 1000,
                 "history_max_chars": 10000},
        "instances": {},
        "ingest_roots": [],
    }


def test_egress_veto_in_build_loop_forces_external(spawned_root, capsys):
    """A vetoed loopback model yields an external ceiling + schema in build_loop."""
    from oracle_agent.agentloop.builder import build_loop

    loop = build_loop(_veto_cfg("deepseek-v4-pro:cloud"), spawned_root,
                      surface="chat")
    # Reclassified external: ceiling drops to public, dispatcher env is external.
    assert loop.dispatcher.environment == "external"
    assert loop.dispatcher.max_sensitivity == "public"
    # The LLM client environment is also external.
    assert loop.client.environment == "external"
    # System prompt reflects the external environment.
    assert "`external` model" in loop.system_prompt
    # A stderr warning naming the veto reason was surfaced.
    err = capsys.readouterr().err
    assert "egress veto" in err


def test_no_egress_veto_keeps_local_agent(spawned_root, monkeypatch):
    """A clean local model stays local_agent with the internal ceiling."""
    from oracle_agent.agentloop.builder import build_loop

    # Force egress_veto to return None (clean) regardless of host environment.
    monkeypatch.setattr(pb, "egress_veto", lambda *a, **k: None)
    loop = build_loop(_veto_cfg("qwen3.6-32k"), spawned_root, surface="chat")
    assert loop.dispatcher.environment == "local_agent"
    assert loop.dispatcher.max_sensitivity == "internal"
