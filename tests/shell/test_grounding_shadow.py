"""Tests for the P3-T7 shadow-mode measurement machinery.

Covers (per docs/roadmap/PHASE-3-forced-grounding.md, P3-T7 / P3S-10):

  * Capture fires ONLY when (a) policy is OBSERVE, (b) surface is local, (c) the
    operator consent flag is on. Each line is one flagged claim-unit record
    (claim text + verdict + object_guess + turn timing) written append-only 0600.
  * The gateway path NEVER writes the file -- structurally the capture call site
    is on the local-OBSERVE branch only, and a gateway-built loop (ENFORCE,
    consent forced off) never writes even in a hypothetical OBSERVE config.
  * The budget-evaluation report computes the spec's FP + latency metrics from
    the captured file and makes the go/no-go recommendation, stating the
    >= 50-turns / >= 7-days window requirement is unmet when it is.

Stdlib only; uses the session-scoped real spawned root (conftest) so the
server-side known_objects() enumeration runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from oracle_agent.agentloop.loop import (
    AgentLoop,
    GroundingPolicy,
    SHADOW_FILENAME,
    _shadow_path,
)
from oracle_agent.llm.client import ChatResponse, ToolCall


# --- reuse the agentloop test doubles ---------------------------------------
from oracle_agent.agentloop.verbtools import ToolOutcome


class FakeClient:
    def __init__(self, script):
        self.script = script
        self.i = 0

    def chat(self, messages, tools=None, **kw):
        resp = self.script[self.i]
        self.i += 1
        return resp


class FakeDispatcher:
    def __init__(self, surface="local", environment="local_agent",
                 outcomes=None, root=None):
        self.surface = surface
        self.environment = environment
        self.outcomes = outcomes or {}
        self.root = root

    def dispatch(self, name, args):
        return self.outcomes.get(name, ToolOutcome("[ok]", rc=0))


_REV_OBJ = "Revenue / invoices"
_REV_CLAIM = "Revenue / invoices was $1M last quarter."


def _env(obj=_REV_OBJ, *, exit_code=0, verdict="grounded", withheld=None):
    e = {"business_object": obj, "exit_code": exit_code, "verdict": verdict}
    if withheld is not None:
        e["withheld"] = withheld
    return e


def _loop(script, disp, *, grounding=GroundingPolicy.OBSERVE, shadow_consent=False,
          **kw):
    return AgentLoop(FakeClient(script), disp, "SYS", grounding=grounding,
                     shadow_consent=shadow_consent,
                     retry_kwargs={"sleep": lambda *_: None}, **kw)


def _read_shadow(profile: Path) -> list[dict]:
    p = profile / SHADOW_FILENAME
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ===========================================================================
# Capture binding semantics
# ===========================================================================

class TestCaptureBinding:
    def test_local_observe_with_consent_captures_flagged_unit(
        self, profile, spawned_root
    ):
        """OBSERVE + local + consent: a flagged unbacked claim lands in the file."""
        disp = FakeDispatcher(surface="local", root=spawned_root)
        loop = _loop([ChatResponse(content=_REV_CLAIM)], disp,
                     grounding=GroundingPolicy.OBSERVE, shadow_consent=True)
        res = loop.run_turn("revenue?")
        # OBSERVE never alters prose.
        assert _REV_CLAIM in res.text

        rows = _read_shadow(profile)
        assert len(rows) == 1, f"expected one flagged-unit row, got {rows}"
        row = rows[0]
        assert row["claim"] == _REV_CLAIM
        assert row["object_guess"] == _REV_OBJ
        assert row["verdict"] == "unbacked"      # no envelope this turn
        assert row["surface"] == "local"
        # Turn timing metadata is present.
        assert "added_seconds" in row and isinstance(row["added_seconds"], (int, float))
        assert "iterations" in row and "repairs" in row
        assert "ts" in row

    def test_consent_off_writes_nothing(self, profile, spawned_root):
        """No consent -> no capture, even on local OBSERVE with a flagged claim."""
        disp = FakeDispatcher(surface="local", root=spawned_root)
        loop = _loop([ChatResponse(content=_REV_CLAIM)], disp,
                     grounding=GroundingPolicy.OBSERVE, shadow_consent=False)
        loop.run_turn("revenue?")
        assert not (profile / SHADOW_FILENAME).exists()
        assert _read_shadow(profile) == []

    def test_non_local_surface_never_writes(self, profile, spawned_root):
        """A non-local surface NEVER writes the shadow file even with consent set
        AND policy forced to OBSERVE (the structural double-layer, P3S-10)."""
        disp = FakeDispatcher(surface="gateway", root=spawned_root)
        # Hypothetical: a gateway-surfaced loop that is (wrongly) OBSERVE with
        # consent on. The capture must STILL refuse because the surface is not
        # local. (The builder never constructs this; this asserts the inner guard.)
        loop = _loop([ChatResponse(content=_REV_CLAIM)], disp,
                     grounding=GroundingPolicy.OBSERVE, shadow_consent=True)
        loop.run_turn("revenue?")
        assert not (profile / SHADOW_FILENAME).exists()

    def test_no_flagged_units_writes_nothing(self, profile, spawned_root):
        """A purely conversational draft flags nothing -> no shadow line."""
        disp = FakeDispatcher(surface="local", root=spawned_root)
        loop = _loop([ChatResponse(content="Sure, happy to help.")], disp,
                     grounding=GroundingPolicy.OBSERVE, shadow_consent=True)
        loop.run_turn("hi")
        assert _read_shadow(profile) == []

    def test_backed_claim_not_captured(self, profile, spawned_root):
        """A claim backed by a grounded envelope is NOT flagged -> not captured."""
        script = [
            ChatResponse(content=None, tool_calls=[
                ToolCall("a1", "oracle_answer",
                         '{"business_object":"Revenue / invoices"}')]),
            ChatResponse(content=_REV_CLAIM),
        ]
        outcomes = {"oracle_answer": ToolOutcome("{}", envelope=_env(), rc=0)}
        disp = FakeDispatcher(surface="local", outcomes=outcomes, root=spawned_root)
        loop = _loop(script, disp, grounding=GroundingPolicy.OBSERVE,
                     shadow_consent=True)
        loop.run_turn("revenue?")
        # Backed -> nothing flagged -> empty shadow.
        assert _read_shadow(profile) == []

    def test_mismatched_records_refused_verdict(self, profile, spawned_root):
        """A claim on a refused envelope is mismatched -> captured with the
        envelope's verdict (refused), not 'unbacked'."""
        script = [
            ChatResponse(content=None, tool_calls=[
                ToolCall("a1", "oracle_answer",
                         '{"business_object":"Revenue / invoices"}')]),
            ChatResponse(content=_REV_CLAIM),
        ]
        refused = _env(exit_code=4, verdict="refused")
        outcomes = {"oracle_answer": ToolOutcome("{}", envelope=refused, rc=0)}
        disp = FakeDispatcher(surface="local", outcomes=outcomes, root=spawned_root)
        loop = _loop(script, disp, grounding=GroundingPolicy.OBSERVE,
                     shadow_consent=True)
        loop.run_turn("revenue?")
        rows = _read_shadow(profile)
        assert len(rows) == 1
        assert rows[0]["verdict"] == "refused"
        assert rows[0]["object_guess"] == _REV_OBJ

    def test_file_is_0600_and_append_only(self, profile, spawned_root):
        """The shadow file is 0600 and accumulates across turns (append-only)."""
        disp = FakeDispatcher(surface="local", root=spawned_root)
        loop = _loop([ChatResponse(content=_REV_CLAIM),
                      ChatResponse(content=_REV_CLAIM)], disp,
                     grounding=GroundingPolicy.OBSERVE, shadow_consent=True)
        loop.run_turn("revenue?")
        loop.run_turn("revenue again?")
        p = profile / SHADOW_FILENAME
        assert p.exists()
        mode = oct(p.stat().st_mode & 0o777)
        assert mode == "0o600", f"shadow file mode {mode}, expected 0o600"
        # Two turns each flag one unit -> two appended lines.
        assert len(_read_shadow(profile)) == 2


# ===========================================================================
# Gateway enforcer: a gateway-built loop never writes the shadow file
# ===========================================================================

class TestGatewayNeverWrites:
    def test_builder_gateway_loop_has_no_shadow_consent(self, profile, spawned_root):
        """A loop built for surface='gateway' has shadow_consent forced False AND
        is ENFORCE -- it can never write grounding_shadow.jsonl (P3S-10/11)."""
        from oracle_agent import config
        from oracle_agent.agentloop.builder import build_loop

        cfg = config.load_config()
        # Even if config (wrongly) had grounding_shadow on, the builder forces it
        # off for the gateway. Set it on to prove the guard.
        cfg.setdefault("chat", {})["grounding_shadow"] = True

        loop = build_loop(cfg, spawned_root, surface="gateway")
        assert loop.grounding is GroundingPolicy.ENFORCE
        assert loop.shadow_consent is False

    def test_gateway_enforce_run_writes_nothing(self, profile, spawned_root):
        """A gateway loop (ENFORCE) running a turn with an unbacked claim never
        touches the shadow file -- the capture call site is OBSERVE-only."""
        disp = FakeDispatcher(surface="gateway", root=spawned_root)
        # ENFORCE with consent forced off (as the builder would set it). The
        # stubborn ungrounded claim gets redacted; nothing is captured.
        loop = _loop([ChatResponse(content=_REV_CLAIM) for _ in range(3)], disp,
                     grounding=GroundingPolicy.ENFORCE, shadow_consent=False,
                     max_repair=2)
        res = loop.run_turn("revenue?")
        assert "claim(s) withheld" in res.text  # redacted on the gateway
        assert not (profile / SHADOW_FILENAME).exists()

    def test_shadow_path_under_profile_dir(self, profile):
        """The shadow file resolves under profile_dir()."""
        p = _shadow_path()
        assert p.name == SHADOW_FILENAME
        assert p.parent == profile or str(p).startswith(str(profile))


# ===========================================================================
# Config consent key
# ===========================================================================

class TestConsentConfigKey:
    def test_grounding_shadow_default_off(self):
        from oracle_agent.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["chat"]["grounding_shadow"] is False

    def test_grounding_shadow_not_a_security_key(self):
        """The shadow consent is telemetry, NOT a security key (a migration may
        freely turn it off; default-off is the safe direction, P3S-10/11)."""
        from oracle_agent.config import SECURITY_KEYS
        assert "chat.grounding_shadow" not in SECURITY_KEYS
        # The grounding DEFAULT, by contrast, IS a security key (P3S-11).
        assert "chat.grounding_default" in SECURITY_KEYS

    def test_builder_local_consent_threaded(self, profile, spawned_root):
        """Local build with grounding_shadow on yields shadow_consent True."""
        from oracle_agent import config
        from oracle_agent.agentloop.builder import build_loop

        cfg = config.load_config()
        cfg.setdefault("chat", {})["grounding_shadow"] = True
        loop = build_loop(cfg, spawned_root, surface="local")
        assert loop.shadow_consent is True

    def test_builder_local_consent_off_by_default(self, profile, spawned_root):
        from oracle_agent import config
        from oracle_agent.agentloop.builder import build_loop

        cfg = config.load_config()
        loop = build_loop(cfg, spawned_root, surface="local")
        assert loop.shadow_consent is False


# ===========================================================================
# Budget-evaluation report
# ===========================================================================

class TestReportEvaluation:
    def test_window_unmet_is_stated_and_no_go(self):
        from oracle_agent.grounding_report import evaluate, render

        # A handful of turns over one day -> window unmet.
        shadow = [
            {"ts": "2026-06-01T10:00:00+00:00", "claim": "c1",
             "object_guess": "X", "verdict": "unbacked",
             "iterations": 1, "repairs": 0, "added_seconds": 0.1},
            {"ts": "2026-06-01T10:05:00+00:00", "claim": "c2",
             "object_guess": "X", "verdict": "unbacked",
             "iterations": 1, "repairs": 0, "added_seconds": 0.2},
        ]
        m = evaluate(shadow, [])
        assert m["window_ok"] is False
        assert m["go"] is False
        assert "NO-GO" in m["recommendation"]
        report = render(m)
        assert "WINDOW UNMET" in report
        assert "NO-GO" in report

    def test_fp_rate_computed_from_labels(self):
        from oracle_agent.grounding_report import evaluate

        shadow = [
            {"ts": f"2026-06-0{d}T10:00:00+00:00", "claim": f"c{d}",
             "object_guess": "X", "verdict": "unbacked",
             "iterations": 1, "repairs": 0, "added_seconds": 0.1}
            for d in range(1, 9)
        ]
        # Label exactly one of eight as non-material -> 1/8 = 12.5% FP unit rate.
        labels = [{"claim": "c1", "non_material": True}]
        labels += [{"claim": f"c{d}", "non_material": False} for d in range(2, 9)]
        m = evaluate(shadow, labels)
        assert m["labeled_units"] == 8
        assert m["fp_units"] == 1
        assert abs(m["fp_unit_rate"] - (1 / 8)) < 1e-9
        # 12.5% > 5% budget -> FP unit budget fails.
        assert m["fp_unit_pass"] is False

    def test_unlabeled_blocks_go(self):
        from oracle_agent.grounding_report import evaluate

        # Enough turns/days but no labels at all -> labeling incomplete -> no go.
        shadow = [
            {"ts": f"2026-06-{d:02d}T10:00:00+00:00", "claim": f"c{d}",
             "object_guess": "X", "verdict": "unbacked",
             "iterations": 1, "repairs": 0, "added_seconds": 0.1}
            for d in range(1, 9)
        ]
        m = evaluate(shadow, [])
        assert m["labeling_complete"] is False
        assert m["go"] is False

    def test_latency_p50_budget(self):
        from oracle_agent.grounding_report import evaluate

        # All added_seconds well under 0.5s -> latency passes.
        shadow = [
            {"ts": f"2026-06-0{d}T10:00:00+00:00", "claim": f"c{d}",
             "object_guess": "X", "verdict": "unbacked",
             "iterations": 1, "repairs": 0, "added_seconds": 0.05}
            for d in range(1, 9)
        ]
        m = evaluate(shadow, [])
        assert m["p50_added_seconds"] == 0.05
        assert m["latency_pass"] is True

    def test_go_when_all_budgets_and_window_met(self):
        from oracle_agent.grounding_report import evaluate

        # 50 turns across 10 distinct days, every unit labeled material, fast.
        shadow = []
        for i in range(50):
            day = (i % 10) + 1
            shadow.append({
                "ts": f"2026-06-{day:02d}T{i:02d}:00:00+00:00",
                "claim": f"claim-{i}", "object_guess": "X",
                "verdict": "unbacked", "iterations": 1, "repairs": 0,
                "added_seconds": 0.1,
            })
        labels = [{"claim": f"claim-{i}", "non_material": False} for i in range(50)]
        m = evaluate(shadow, labels)
        assert m["total_turns"] == 50
        assert m["days_spanned"] == 10
        assert m["window_ok"] is True
        assert m["labeling_complete"] is True
        assert m["fp_unit_pass"] and m["fp_turn_pass"] and m["latency_pass"]
        assert m["go"] is True
        assert "GO" in m["recommendation"]

    def test_fp_only_repair_turn_counted(self):
        from oracle_agent.grounding_report import evaluate

        # One turn with a repair where the single flagged unit is non-material:
        # an FP-only repair turn.
        shadow = [
            {"ts": "2026-06-01T10:00:00+00:00", "claim": "fp1",
             "object_guess": "X", "verdict": "unbacked",
             "iterations": 2, "repairs": 1, "added_seconds": 0.9},
        ]
        labels = [{"claim": "fp1", "non_material": True}]
        m = evaluate(shadow, labels)
        assert m["fp_only_repair_turns"] == 1
        assert m["total_turns"] == 1
        assert m["fp_turn_rate"] == 1.0

    def test_cli_no_capture_returns_1(self, profile, capsys):
        from oracle_agent.grounding_report import cmd_grounding_report
        rc = cmd_grounding_report([])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no shadow capture" in err.lower()

    def test_cli_reads_shadow_and_reports(self, profile, capsys):
        from oracle_agent.grounding_report import cmd_grounding_report

        p = profile / SHADOW_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"ts": "2026-06-01T10:00:00+00:00", "claim": "c1",
                        "object_guess": "X", "verdict": "unbacked",
                        "iterations": 1, "repairs": 0, "added_seconds": 0.1}) + "\n",
            encoding="utf-8",
        )
        rc = cmd_grounding_report([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "P3-T7" in out
        assert "RECOMMENDATION" in out
        # Window unmet with only one turn.
        assert "WINDOW UNMET" in out

    def test_cli_json_mode(self, profile, capsys):
        from oracle_agent.grounding_report import cmd_grounding_report

        p = profile / SHADOW_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"ts": "2026-06-01T10:00:00+00:00", "claim": "c1",
                        "object_guess": "X", "verdict": "unbacked",
                        "iterations": 1, "repairs": 0, "added_seconds": 0.1}) + "\n",
            encoding="utf-8",
        )
        rc = cmd_grounding_report(["--json"])
        assert rc == 0
        out = capsys.readouterr().out
        m = json.loads(out)
        assert m["total_turns"] == 1
        assert m["go"] is False

    def test_corrupt_lines_skipped(self, profile):
        from oracle_agent.grounding_report import _read_jsonl

        p = profile / SHADOW_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '{"claim": "ok"}\n{not json\n\n{"claim": "ok2"}\n',
            encoding="utf-8",
        )
        rows = _read_jsonl(p)
        assert len(rows) == 2
