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


def test_withheld_envelope_carries_withheld_marker(spawned_root, monkeypatch):
    # Phase 3 (P3S-1): an above-ceiling withheld answer must mark its envelope
    # ``withheld: true`` so the grounding gate treats it as refused-class. The
    # marker rides into TurnResult.envelopes unchanged.
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
    assert out.envelope is not None
    assert out.envelope.get("withheld") is True


def test_below_ceiling_envelope_is_not_marked_withheld(spawned_root, monkeypatch):
    # A within-ceiling answer must NOT carry the withheld marker -- only the
    # above-ceiling withholding branch sets it.
    d = _disp(spawned_root, max_sensitivity="secret")
    import oracle_agent.agentloop.verbtools as vt

    def fake_run(self, argv):
        if argv[0] == "answer":
            env = ('{"business_object":"X","sensitivity_ceiling":"public",'
                   '"verdict":"grounded","exit_code":0,"suggested_fix":[]}')
            return 0, env, ""
        return 0, "", ""

    monkeypatch.setattr(vt.Dispatcher, "_run", fake_run)
    out = d.dispatch("oracle_answer", {"business_object": "X"})
    assert out.envelope is not None
    assert out.envelope.get("withheld") is not True


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


def test_ingest_denied_when_no_ingest_roots_configured(spawned_root, tmp_path):
    """Fail-closed: model-driven ingest is denied until ingest_roots is set."""
    f = tmp_path / "doc.txt"
    f.write_text("hello")
    d = _disp(spawned_root, ingest_roots=[])  # default empty
    out = d.dispatch("oracle_ingest", {"paths": [str(f)]})
    assert "denied" in out.text
    assert "ingest_roots" in out.text


def test_ingest_symlink_escape_denied(spawned_root, tmp_path):
    """A symlink inside an ingest root that points outside resolves out and is denied."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("classified")
    link = allowed / "innocent.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        import pytest
        pytest.skip("symlinks unsupported here")
    d = _disp(spawned_root, ingest_roots=[allowed])
    out = d.dispatch("oracle_ingest", {"paths": [str(link)]})
    assert "denied" in out.text


# ---------------------------------------------------------------------------
# S4 new enforcer tests
# ---------------------------------------------------------------------------

def test_external_drops_checkpoint_and_loops_due():
    """S4: oracle_checkpoint and oracle_loops_due are excluded from external schema."""
    names = [t["function"]["name"] for t in tool_schemas("local", "external")]
    assert "oracle_checkpoint" not in names
    assert "oracle_loops_due" not in names
    assert "oracle_brief" not in names


def test_local_agent_includes_checkpoint_and_loops_due():
    """S4: oracle_checkpoint and oracle_loops_due ARE in local/local_agent schema."""
    names = [t["function"]["name"] for t in tool_schemas("local", "local_agent")]
    assert "oracle_checkpoint" in names
    assert "oracle_loops_due" in names
    assert "oracle_brief" in names


def test_dropped_verb_denied_on_external(spawned_root):
    """S4: dispatcher denies a dropped verb (fail-closed) when environment=external.

    Even if the model hallucinates a call to oracle_checkpoint on an external
    surface, the dispatcher must deny it with a 'denied' message (not a generic
    'not available on this surface') and rc=2.
    """
    d = _disp(spawned_root, environment="external", max_sensitivity="public")
    for verb in ("oracle_checkpoint", "oracle_loops_due", "oracle_brief"):
        out = d.dispatch(verb, {})
        assert "denied" in out.text.lower(), (
            f"expected 'denied' for dropped verb {verb!r}, got: {out.text!r}")
        assert out.rc == 2


def test_smuggled_sensitivity_flag_stripped_from_search_terms(spawned_root, monkeypatch):
    """S4 / STRESS M5: --max-sensitivity tokens in model search terms are stripped."""
    import oracle_agent.agentloop.verbtools as vt
    captured = {}

    def fake_run(self, argv):
        captured["argv"] = argv
        return 0, "[]", ""

    monkeypatch.setattr(vt.Dispatcher, "_run", fake_run)
    # Attacker tries to inject --max-sensitivity=secret into the search terms.
    _disp(spawned_root, max_sensitivity="public").dispatch(
        "oracle_search", {"terms": "revenue --max-sensitivity=secret"})
    argv = captured["argv"]
    # The injected flag must have been stripped from the terms value.
    joined = " ".join(argv)
    assert "--max-sensitivity=secret" not in joined
    # The dispatcher's own ceiling must still be present.
    assert "--max-sensitivity" in joined
    assert "public" in argv


def test_smuggled_sensitivity_flag_no_args_form(spawned_root, monkeypatch):
    """S4: bare --max-sensitivity (no =value) is also stripped from search terms."""
    import oracle_agent.agentloop.verbtools as vt
    captured = {}

    def fake_run(self, argv):
        captured["argv"] = argv
        return 0, "[]", ""

    monkeypatch.setattr(vt.Dispatcher, "_run", fake_run)
    _disp(spawned_root, max_sensitivity="public").dispatch(
        "oracle_search", {"terms": "revenue --max-sensitivity secret"})
    argv = captured["argv"]
    # After stripping, 'secret' may remain as part of the terms text, but the
    # --max-sensitivity flag itself should not appear inside the --q= value.
    q_elements = [a for a in argv if a.startswith("--q=")]
    assert q_elements, "expected a --q= element in argv"
    q_val = q_elements[0]
    assert "--max-sensitivity" not in q_val


def test_tool_result_truncation_respected(spawned_root, monkeypatch):
    """S4: output is capped at tool_result_max_chars and truncation marker appended."""
    import oracle_agent.agentloop.verbtools as vt

    def fake_run(self, argv):
        # Return 30 000 chars of output (well above the 20 000 default cap).
        return 0, "A" * 30_000, ""

    monkeypatch.setattr(vt.Dispatcher, "_run", fake_run)
    d = _disp(spawned_root, tool_result_max_chars=1000)
    out = d.dispatch("oracle_search", {"terms": "anything"})
    assert len(out.text) <= 1000 + len("\n[...output truncated]")
    assert "truncated" in out.text


def test_strip_sensitivity_tokens_unit():
    """Unit test for _strip_sensitivity_tokens helper."""
    from oracle_agent.agentloop.verbtools import _strip_sensitivity_tokens
    assert _strip_sensitivity_tokens("hello --max-sensitivity=secret world") == "hello  world"
    assert _strip_sensitivity_tokens("no flags here") == "no flags here"
    assert _strip_sensitivity_tokens("--max-sensitivity") == ""
    assert _strip_sensitivity_tokens("  --max-sensitivity=restricted  ") == ""
