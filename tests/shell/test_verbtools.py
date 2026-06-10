"""Tests for agentloop/verbtools.py (SPEC S4 / S10) against a real spawned root."""
from __future__ import annotations

from pathlib import Path

import pytest

from oracle_agent.agentloop import policy_bridge as pb
from oracle_agent.agentloop.verbtools import Dispatcher, tool_schemas


def _disp(root, **kw):
    defaults = dict(root=root, surface="local", environment="local_agent",
                    max_sensitivity="internal",
                    order=list(pb.CANONICAL_ORDER))
    defaults.update(kw)
    return Dispatcher(**defaults)


def test_local_surface_has_ten_tools():
    names = [t["function"]["name"] for t in tool_schemas("local", "local_agent")]
    assert len(names) == 10
    assert "oracle_ingest" in names


def test_gateway_surface_is_reduced():
    names = [t["function"]["name"] for t in tool_schemas("gateway", "local_agent")]
    assert "oracle_ingest" not in names
    assert "oracle_checkpoint" not in names
    assert "oracle_brief" not in names
    assert set(names) <= {"oracle_status", "oracle_search", "oracle_answer",
                          "oracle_review", "oracle_capture", "oracle_remember"}


def test_external_drops_brief():
    names = [t["function"]["name"] for t in tool_schemas("local", "external")]
    assert "oracle_brief" not in names


def test_no_control_plane_tool_anywhere():
    for surface in ("local", "gateway"):
        for env in ("local_agent", "external"):
            names = [t["function"]["name"] for t in tool_schemas(surface, env)]
            for n in names:
                assert "admin" not in n and "truth" not in n and "upgrade" not in n


def test_status_is_minimized(spawned_root):
    out = _disp(spawned_root).dispatch("oracle_status", {})
    assert "most_urgent" not in out.text
    assert "rung" in out.text


def test_answer_envelope_parsed_and_rc_is_verdict(spawned_root):
    out = _disp(spawned_root).dispatch("oracle_answer", {"business_object": "Nonexistent Object"})
    assert out.envelope is not None
    assert out.envelope["verdict"] == "refused"
    assert out.rc == 4  # verdict, not a failure


def test_answer_above_ceiling_is_withheld(spawned_root, monkeypatch):
    # Force the envelope to claim a high ceiling; dispatcher must withhold.
    d = _disp(spawned_root, max_sensitivity="public")
    import oracle_agent.agentloop.verbtools as vt

    def fake_run(self, argv):
        if argv[0] == "answer":
            env = ('{"business_object":"X","sensitivity_ceiling":"secret",'
                   '"verdict":"grounded","exit_code":0,"suggested_fix":[]}')
            return 0, env, ""
        return 0, "", ""

    monkeypatch.setattr(vt.Dispatcher, "_run", fake_run)
    out = d.dispatch("oracle_answer", {"business_object": "X"})
    assert "withheld" in out.text
    assert out.envelope is not None  # verdict still returned for the footer


def test_search_forces_ceiling(spawned_root, monkeypatch):
    captured = {}
    import oracle_agent.agentloop.verbtools as vt

    def fake_run(self, argv):
        captured["argv"] = argv
        return 0, "[]", ""

    monkeypatch.setattr(vt.Dispatcher, "_run", fake_run)
    _disp(spawned_root, max_sensitivity="public").dispatch(
        "oracle_search", {"terms": "revenue", "k": 5})
    argv = captured["argv"]
    assert argv[-2:] == ["--max-sensitivity", "public"]
    assert "--q=revenue" in argv


def test_ingest_denies_profile_dir(spawned_root, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    secret = profile / ".env"
    secret.write_text("X=y")
    d = _disp(spawned_root, profile_dir=profile, ingest_roots=[tmp_path])
    out = d.dispatch("oracle_ingest", {"paths": [str(secret)]})
    assert "denied" in out.text


def test_ingest_denies_outside_ingest_roots(spawned_root, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    f = other / "doc.txt"
    f.write_text("hello")
    d = _disp(spawned_root, ingest_roots=[allowed])
    out = d.dispatch("oracle_ingest", {"paths": [str(f)]})
    assert "denied" in out.text


def test_ingest_denies_sibling_instance(spawned_root, tmp_path):
    sibling = tmp_path / "sibling_root"
    sibling.mkdir()
    f = sibling / "Memory.nosync"
    f.mkdir()
    note = f / "x.md"
    note.write_text("secret memory")
    d = _disp(spawned_root, ingest_roots=[tmp_path], sibling_roots=[sibling])
    out = d.dispatch("oracle_ingest", {"paths": [str(note)]})
    assert "denied" in out.text


def test_unknown_tool_rejected(spawned_root):
    out = _disp(spawned_root).dispatch("oracle_delete_everything", {})
    assert "not available" in out.text
    assert out.rc == 2


def test_gateway_cannot_ingest(spawned_root, tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hi")
    d = _disp(spawned_root, surface="gateway", ingest_roots=[tmp_path])
    out = d.dispatch("oracle_ingest", {"paths": [str(f)]})
    assert "not available" in out.text


def test_env_is_scrubbed(spawned_root, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "leak-me")
    monkeypatch.setenv("ORACLE_LLM_API_KEY", "leak-me-too")
    import oracle_agent.agentloop.verbtools as vt
    env = vt._scrubbed_env(["ORACLE_LLM_API_KEY"])
    assert "SOME_API_KEY" not in env
    assert "ORACLE_LLM_API_KEY" not in env


def test_write_gate_blocks_remember(spawned_root):
    d = _disp(spawned_root, write_gate=lambda: False)
    out = d.dispatch("oracle_remember", {"user_request": "x", "answer_summary": "y"})
    assert "rate limit" in out.text
    d2 = _disp(spawned_root, write_gate=lambda: False)
    out2 = d2.dispatch("oracle_capture", {"kind": "feedback", "target": "t"})
    assert "rate limit" in out2.text


def test_search_real_subprocess_succeeds(spawned_root):
    """Regression: the built argv must actually parse in the kernel CLI."""
    out = _disp(spawned_root).dispatch("oracle_search", {"terms": "anything at all"})
    assert out.rc == 0, out.text
