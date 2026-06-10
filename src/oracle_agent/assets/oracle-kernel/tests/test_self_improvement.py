#!/usr/bin/env python3
"""Tests for the self-improvement meta-architecture (spec: docs/specs/
oracle-self-improvement-meta-architecture-spec.md, Phases A-D).

Load-bearing guarantees exercised here:

  * Phase A -- the oracle measures itself and closes its loops with ZERO admin
    intervention: a cited scorecard is generated (FR-A1/B2), an applied
    improvement is adjudicated from observed ledgers (FR-A2), a repeatedly
    failing loop is auto-paused and an aged critical signal tops the Review
    Inbox (FR-A3), the user model accumulates structured preference counters
    (FR-A4), and confirmed findings past budget surface (FR-A5).
  * A builtin runner that returns a worklist is NOT recorded as a (failed)
    run -- recording it as 'fail' would teach meta-health that a healthy loop
    is degraded.
  * Phase B -- `./oracle answer` logs a metadata-only answer_event (FR-B1);
    a regressing scorecard makes architecture-retrospective due immediately
    (FR-B3).
  * Phase C -- promotion is refused without an evidence-cited proposal and
    granted with one (FR-C2); a critical failure demotes automatically and
    fail-closed (FR-C3); level presets admit deterministic loops at level 1
    and deny dream sessions below level 2 (FR-C1); the kill switch still wins.
  * Phase D -- a dream session is denied below level 2, runs at level 2 only
    via the gate, and records a metadata-only dream_session row (FR-D1/D2);
    `policy.require_role` denies control-plane capabilities to the dream
    actor's user role.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import actions  # noqa: E402
import capture  # noqa: E402
import harness  # noqa: E402
import improvements  # noqa: E402
import ledger  # noqa: E402
import loops  # noqa: E402
import meta_health  # noqa: E402
import oracle_lint  # noqa: E402
import review_queue  # noqa: E402
import scorecard  # noqa: E402
import synthesis  # noqa: E402

NOW = datetime(2026, 6, 10, 12, 0, 0)


# --------------------------------------------------------------------------- #
# inline note/loop builders (fixed-literal test paths; no spawn dependency)
# --------------------------------------------------------------------------- #
def _write_loop(root: Path, loop_id: str, *, cadence: str = "weekly",
                status: str = "active", runner: str = "agent-worklist",
                last_run: str = "2026-06-01") -> Path:
    d = root / "Meta.nosync" / "Loops"
    d.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            "---",
            f"id: {loop_id}",
            "type: loop",
            f"title: Loop {loop_id}",
            "created: 2026-01-01",
            "updated: 2026-01-01",
            "sensitivity: internal",
            f"status: {status}",
            "tags:",
            "  - meta",
            "  - loop",
            f"cadence: {cadence}",
            f"runner: {runner}",
            f"last_run: {last_run}",
            f"next_review: {last_run}",
            "trigger_conditions:",
            "  - TBD",
            "---",
            "",
            f"# Loop {loop_id}",
            "",
        ]
    )
    p = d / f"loop-{loop_id}.md"
    p.write_text(text, encoding="utf-8")
    return p


def _write_improvement(root: Path, imp_id: str, *, status: str = "applied",
                       applied: str = "2026-06-01", verify: str = "auto",
                       signal_event: str = "value_event",
                       target: str = "leadership-brief",
                       polarity: str = "positive", min_count: int = 1,
                       within_days: int = 30, created: str = "2026-05-01",
                       include_signal: bool = True) -> Path:
    d = root / "Meta.nosync" / "Improvements"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"id: {imp_id}",
        "type: improvement",
        f"title: Improvement {imp_id}",
        f"created: \"{created}\"",
        f"updated: \"{applied}\"",
        "sensitivity: internal",
        f"status: {status}",
        "tags:",
        "  - meta",
        "  - improvement",
        f"applied: \"{applied}\"",
        f"verify: {verify}",
    ]
    if include_signal:
        lines.extend(
            [
                "expected_signal:",
                f"  event: {signal_event}",
                f"  target: {target}",
                f"  polarity: {polarity}",
                f"  min_count: {min_count}",
                f"  within_days: {within_days}",
            ]
        )
    lines.extend(["---", "", f"# Improvement {imp_id}", ""])
    p = d / f"{imp_id}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_finding(root: Path, fid: str, *, status: str = "confirmed",
                   updated: str = "2026-01-01") -> Path:
    d = root / "Memory.nosync" / "Findings"
    d.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            "---",
            f"id: {fid}",
            "type: finding",
            f"title: Finding {fid}",
            f"created: \"{updated}\"",
            f"updated: \"{updated}\"",
            "sensitivity: internal",
            f"status: {status}",
            "tags:",
            "  - finding",
            "business_object: Revenue",
            "evidence:",
            "  - SRC-0001",
            "disconfirmers:",
            "  - none known",
            "---",
            "",
            "claim",
            "",
        ]
    )
    p = d / f"{fid}.md"
    p.write_text(text, encoding="utf-8")
    return p


def _autonomy_yml(root: Path, *, enabled: bool = True, level: int = 1,
                  max_files: int = 50, max_bytes: int = 10_000_000,
                  dream_command: str = "") -> Path:
    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        f"enabled: {'true' if enabled else 'false'}",
        f"level: {level}",
        "allowed_loops:",
        "writable_lanes:",
        "readonly_connectors:",
        "blast_radius_caps:",
        f"  max_files_per_run: {max_files}",
        f"  max_bytes: {max_bytes}",
        'kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"',
        "dream:",
        (f'  command: "{dream_command}"' if dream_command else "  command:"),
        "  max_minutes: 1",
        "  max_inbox_items: 5",
    ]
    p = d / "autonomy.yml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Phase A -- scorecard (FR-A1 / FR-B2)
# --------------------------------------------------------------------------- #
def test_scorecard_generates_cited_note_and_kpis(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    v = capture.value_event(root, target="brief", polarity="+", strength=2.0,
                            value_kind="decide", now=NOW - timedelta(days=3))
    capture.feedback_event(root, target="brief", polarity="+", now=NOW - timedelta(days=2))
    capture.failure_event(root, target="answer", severity="medium",
                          failure_mode="stale-answer", now=NOW - timedelta(days=2))
    capture.failure_event(root, target="answer", severity="medium",
                          failure_mode="stale-answer", now=NOW - timedelta(days=1))

    res = scorecard.generate(root, now=NOW)
    assert Path(res["note_path"]).exists()
    kpis = res["kpis"]
    assert kpis["value"]["net_signed"] == 2.0
    assert kpis["value"]["by_kind"]["decide"] == 2.0
    assert kpis["failures"]["count"] == 2
    # A failure mode seen twice in one window is an improvement that didn't close.
    assert kpis["failures"]["recurring_modes"] == ["stale-answer"]
    # Scores cite their evidence by drop_id.
    note_text = Path(res["note_path"]).read_text(encoding="utf-8")
    assert v["drop_id"] in note_text
    assert res["trend"] == "baseline"
    assert scorecard.latest_trend(root) == "baseline"


def test_scorecard_trend_regression_detected(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    capture.value_event(root, target="brief", polarity="+", strength=5.0,
                        now=NOW - timedelta(days=40))
    scorecard.generate(root, now=NOW - timedelta(days=30))
    # Next window: only damage.
    capture.failure_event(root, target="answer", severity="high",
                          now=NOW - timedelta(days=5))
    res = scorecard.generate(root, now=NOW)
    assert res["trend"] == "regressing"
    assert scorecard.latest_trend(root) == "regressing"


# --------------------------------------------------------------------------- #
# Phase A -- improvement lifecycle (FR-A2)
# --------------------------------------------------------------------------- #
def test_applied_improvement_verified_from_observed_events(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_improvement(root, "IMP-good", applied="2026-06-01")
    capture.value_event(root, target="leadership-brief", polarity="+",
                        now=datetime(2026, 6, 5))
    results = improvements.adjudicate_all(root, now=NOW)
    by_id = {r["id"]: r for r in results}
    assert by_id["IMP-good"]["verdict"] == "verified"
    assert by_id["IMP-good"]["changed"] is True
    fm = improvements.load_all(root)[0]["fm"]
    assert fm["status"] == "verified"
    assert fm["adjudication"]["verdict"] == "verified"
    assert fm["adjudication"]["matched"]  # cited drop_ids


def test_absence_predicate_regresses_on_match(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_improvement(root, "IMP-norepeat", applied="2026-06-01",
                       signal_event="failure_event", target="ingest",
                       min_count=0, within_days=30)
    capture.failure_event(root, target="ingest", severity="medium",
                          failure_mode="crash", now=datetime(2026, 6, 7))
    results = improvements.adjudicate_all(root, now=NOW)
    assert results[0]["verdict"] == "regressed"


def test_stale_proposed_improvement_surfaces_in_inbox(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_improvement(root, "IMP-stalled", status="proposed",
                       created="2026-05-01", include_signal=False)
    items = review_queue.build_queue(root, datetime(2026, 6, 10, tzinfo=None))
    kinds = {i["kind"] for i in items}
    assert "stale-improvement" in kinds


def test_lint_fails_unverifiable_applied_improvement(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_improvement(root, "IMP-theatre", status="applied", include_signal=False)
    violations = oracle_lint.lint(root)
    codes = {v.code for v in violations}
    assert "improvement-unverifiable" in codes
    # A manual stamp is honest and passes.
    _write_improvement(root, "IMP-theatre", status="applied", verify="manual",
                       include_signal=False)
    violations = oracle_lint.lint(root)
    assert "improvement-unverifiable" not in {v.code for v in violations}


# --------------------------------------------------------------------------- #
# Phase A -- meta-health (FR-A3)
# --------------------------------------------------------------------------- #
def test_meta_health_pauses_loop_after_three_consecutive_fails(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_loop(root, "broken-loop", runner="nosuchmodule:nofn")
    for i in range(3):
        loops.record(root, "broken-loop", "fail", now=NOW - timedelta(days=3 - i))
    result = meta_health.run_meta_health_loop(root, now=NOW)
    assert any(p["loop_id"] == "broken-loop" and p["paused"] for p in result["paused"])
    paused = [l for l in loops.list_loops(root) if l.id == "broken-loop"][0]
    assert paused.status == "paused"
    assert paused.get("paused_reason")
    # The pause captured a failure_event and the inbox surfaces the paused loop.
    items = review_queue.build_queue(root)
    assert "paused-loop" in {i["kind"] for i in items}
    # A paused loop is no longer due.
    assert "broken-loop" not in [d.id for d in loops.due(root, NOW)]


def test_no_captured_signal_ages_silently(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    capture.failure_event(root, target="answer", severity="critical",
                          failure_mode="wrong-grounding",
                          now=NOW - timedelta(days=10))
    aged = meta_health.aged_signals(root, now=NOW)
    assert len(aged) == 1 and aged[0]["critical"] is True
    items = review_queue.build_queue(root, NOW)
    top_kinds = [i["kind"] for i in items if i["rank"] == 0]
    assert "aged-signal" in top_kinds  # urgent: tops the inbox


def test_builtin_worklist_run_is_not_recorded_as_fail(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_loop(root, "insight-synthesis", runner="builtin:insight-synthesis",
                last_run="2026-05-01")
    for i in range(3):
        _write_finding(root, f"F-{i}", status="needs_review", updated="2026-06-01")
    res = loops.run(root, "insight-synthesis", now=NOW)
    assert res["status"] == "worklist"
    rows, _w = ledger.load(root / "Meta.nosync" / "ledgers" / "loop_runs.jsonl")
    assert rows == []  # the run is not finished; nothing recorded
    # ... so meta-health cannot mistake a healthy worklist loop for degraded.
    assert meta_health.degraded_loops(root) == []


# --------------------------------------------------------------------------- #
# Phase A -- structured user model (FR-A4) + staleness sweep (FR-A5)
# --------------------------------------------------------------------------- #
def test_user_feedback_learning_accumulates_structured_preferences(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_loop(root, "user-feedback-learning", cadence="on-event",
                runner="builtin:user-feedback-learning")
    capture.feedback_event(root, target="brief", polarity="+", now=NOW)
    capture.value_event(root, target="brief", polarity="+", strength=2.0,
                        value_kind="decide", now=NOW)
    capture.failure_event(root, target="answer", severity="low",
                          failure_mode="stale-answer", now=NOW)
    res = loops.run(root, "user-feedback-learning", now=NOW)
    assert res["status"] == "ok" and res["outcome"]["processed"] == 3
    prefs = loops.user_model_signals(root)
    assert prefs["signal_counts"]["positive"] == 2
    assert prefs["signal_counts"]["negative"] == 1  # failure is a negative signal
    assert prefs["value_by_kind"]["decide"] == 2.0
    assert prefs["failure_modes"]["stale-answer"] == 1
    assert len(prefs["last_evidence"]) == 3
    # Counters accumulate across runs (recency window via last_evidence cap).
    capture.feedback_event(root, target="brief", polarity="+", now=NOW + timedelta(hours=1))
    loops.run(root, "user-feedback-learning", now=NOW + timedelta(hours=2))
    prefs = loops.user_model_signals(root)
    assert prefs["signal_counts"]["positive"] == 3


def test_staleness_sweep_surfaces_fossilized_confirmed_finding(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_finding(root, "F-old", status="confirmed", updated="2026-01-01")
    _write_finding(root, "F-new", status="confirmed", updated="2026-06-01")
    res = synthesis.run_staleness_sweep(root, now=NOW)
    assert res["status"] == "worklist"
    refs = [i["finding"] for i in res["worklist"]["items"]]
    assert any("F-old" in r for r in refs)
    assert not any("F-new" in r for r in refs)


# --------------------------------------------------------------------------- #
# Phase B -- answer telemetry (FR-B1) + retrospective trigger (FR-B3)
# --------------------------------------------------------------------------- #
def test_answer_cli_logs_metadata_only_answer_event(tmp_path, minimal_oracle):
    import answer_protocol

    root = minimal_oracle(tmp_path)
    rc = answer_protocol.main(
        ["--root", str(root), "answer", "--object", "Revenue", "--format", "json"]
    )
    assert rc in (0, 2, 3, 4)
    rows, _w = ledger.load(root / "Meta.nosync" / "ledgers" / "answer_event.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "answer_event"
    assert row["business_object"] == "Revenue"
    assert row["exit_code"] == rc
    assert "claim" not in row and "text" not in row  # metadata only


def test_regressing_scorecard_makes_retrospective_due_immediately(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_loop(root, "architecture-retrospective", cadence="quarterly",
                runner="builtin:architecture-retrospective", last_run="2026-06-01")
    assert "architecture-retrospective" not in [d.id for d in loops.due(root, NOW)]
    capture.value_event(root, target="x", polarity="+", strength=5.0,
                        now=NOW - timedelta(days=40))
    scorecard.generate(root, now=NOW - timedelta(days=30))
    capture.failure_event(root, target="x", severity="high", now=NOW - timedelta(days=5))
    scorecard.generate(root, now=NOW - timedelta(days=1))
    assert scorecard.latest_trend(root) == "regressing"
    due = {d.id: d for d in loops.due(root, NOW)}
    assert "architecture-retrospective" in due
    assert due["architecture-retrospective"].reason.startswith("regression-trigger")


# --------------------------------------------------------------------------- #
# Phase C -- the graduated ladder (FR-C1..C3)
# --------------------------------------------------------------------------- #
def test_level1_admits_deterministic_loops_only(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _autonomy_yml(root, level=1)
    granted = actions.authorize("loop:meta-health", {"loop": "meta-health", "files": 1}, root=root)
    assert granted["result"] == "grant"
    denied = actions.authorize("loop:custom", {"loop": "custom-loop", "files": 1}, root=root)
    assert denied["result"] == "deny"
    dream = actions.authorize("dream.session", {"files": 1}, root=root)
    assert dream["result"] == "deny" and "level 2" in dream["reason"]


def test_promotion_refused_without_proposal_then_granted_with_one(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _autonomy_yml(root, level=1)
    with pytest.raises(actions.ActionDenied):
        actions.promote(root, actor="Test Admin", role="admin")
    res = actions.propose_promotion(root, to_level=2, reason="test", actor="meta-health")
    assert res["proposed"] is True
    # Dedupe: a second proposal for the same level is refused.
    assert actions.propose_promotion(root, to_level=2)["proposed"] is False
    out = actions.promote(root, actor="Test Admin", role="admin")
    assert out["level"] == 2
    assert actions.Autonomy.load(root).level == 2
    # And the user role can never promote (enable_autonomy is admin-only).
    actions.propose_promotion(root, to_level=3, reason="t", actor="meta-health")
    with pytest.raises(PermissionError):
        actions.promote(root, actor="someone", role="user")


def test_critical_failure_demotes_automatically(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _autonomy_yml(root, level=2)
    res = capture.failure_event(root, target="dream", severity="critical",
                                failure_mode="containment", now=NOW)
    assert res["demotion"] and res["demotion"]["demoted"] is True
    assert actions.Autonomy.load(root).level == 1
    # The demotion is visible (urgent) in the Review Inbox.
    items = review_queue.build_queue(root)
    autonomy_items = [i for i in items if i["kind"] == "autonomy"]
    assert any("DEMOTED" in i["title"] for i in autonomy_items)
    # One step per trigger set: the sweep does not demote again on old evidence.
    assert actions.enforce_demotion_policy(root, now=NOW) is None


def test_kill_switch_wins_at_every_level(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _autonomy_yml(root, level=3)
    (root / "Meta.nosync" / "Autonomy" / "KILL-SWITCH").write_text("", encoding="utf-8")
    decision = actions.authorize("loop:meta-health", {"loop": "meta-health"}, root=root)
    assert decision["result"] == "deny"
    assert decision["reason"] == "kill-switch-engaged"


def test_promotion_readiness_requires_clean_evidence(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _autonomy_yml(root, level=0)
    assert actions.promotion_readiness(root, now=NOW)["ready"] is False
    capture.value_event(root, target="x", polarity="+", now=NOW - timedelta(days=40))
    scorecard.generate(root, now=NOW - timedelta(days=30))
    capture.value_event(root, target="x", polarity="+", now=NOW - timedelta(days=10))
    scorecard.generate(root, now=NOW - timedelta(days=1))
    ready = actions.promotion_readiness(root, now=NOW)
    assert ready["ready"] is True and ready["to_level"] == 1
    assert ready["evidence"]  # cites the scorecards
    # A critical failure in the window kills readiness.
    capture.failure_event(root, target="x", severity="critical", now=NOW)
    assert actions.promotion_readiness(root, now=NOW)["ready"] is False


# --------------------------------------------------------------------------- #
# Phase D -- dream sessions (FR-D1/D2)
# --------------------------------------------------------------------------- #
def test_dream_denied_below_level_2(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _autonomy_yml(root, level=1, dream_command="true")
    report = harness.run_dream(root, now=NOW)
    assert report["status"] == "blocked"
    assert "level 2" in report["reason"]
    rows, _w = ledger.load(root / "Meta.nosync" / "ledgers" / "dream_session.jsonl")
    assert rows == []


def test_dream_runs_at_level_2_and_records_metadata_row(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    cmd = f"{sys.executable} -c \"import sys; sys.stdin.read()\""
    _autonomy_yml(root, level=2, dream_command=cmd)
    report = harness.run_dream(root, now=NOW)
    assert report["status"] == "ok"
    rows, _w = ledger.load(root / "Meta.nosync" / "ledgers" / "dream_session.jsonl")
    assert len(rows) == 1
    assert rows[0]["kind"] == "dream_session"
    assert rows[0]["actor"] == "system:dream"
    assert rows[0]["result"] == "ok"
    # The action gate logged intended + actual phases for the session.
    act_rows, _w = ledger.load(root / "Meta.nosync" / "ledgers" / "action_event.jsonl")
    phases = {r["phase"] for r in act_rows if r["action"] == "dream.session"}
    assert phases == {"intended", "actual"}


def test_dream_unconfigured_refuses_clearly(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _autonomy_yml(root, level=2, dream_command="")
    report = harness.run_dream(root, now=NOW)
    assert report["status"] == "unconfigured"


def test_dream_actor_user_role_cannot_touch_control_plane(tmp_path, minimal_oracle):
    import policy

    root = minimal_oracle(tmp_path)
    with pytest.raises(PermissionError):
        policy.require_role("system:dream", "user", "change_architecture", root=root)


# --------------------------------------------------------------------------- #
# spawn integration -- the 12 active loops are real and runnable
# --------------------------------------------------------------------------- #
def test_spawned_oracle_ships_twelve_active_self_tuning_loops(spawned_oracle):
    import setup_audit

    recs = {l.id: l for l in loops.list_loops(spawned_oracle)}
    for lid in setup_audit.ACTIVE_LOOP_IDS:
        assert lid in recs, f"missing active loop {lid}"
        assert recs[lid].status == "active"
        assert recs[lid].runner
    assert len(setup_audit.ACTIVE_LOOP_IDS) == 12
    # The new builtins actually dispatch on a fresh root (ok or worklist).
    for lid in ("value-scorecard", "improvement-lifecycle", "meta-health",
                "stale-finding-refresh"):
        res = loops.run(spawned_oracle, lid, now=NOW)
        assert res["status"] in ("ok", "worklist"), (lid, res)
    retro = loops.run(spawned_oracle, "architecture-retrospective", now=NOW)
    assert retro["status"] == "worklist"
    assert "dossier" in retro["outcome"]["worklist"]
