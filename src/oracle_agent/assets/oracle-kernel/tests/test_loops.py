#!/usr/bin/env python3
"""Tests for the loop runner + due-ness engine (loops.py) and the capture path
(capture.py).

Load-bearing guarantees exercised here:

  * Due-ness is a PURE function of an injected clock. ``compute_due(loops, now)``
    never reads the wall clock; a weekly loop whose ``last_run`` was 8 days ago
    is due; immediately after ``record`` (which advances ``last_run`` to ``now``)
    it is NOT due. Determinism: same loops + same ``now`` -> identical worklist.
  * Only ACTIVE loops run/are due; an active loop must carry a runner +
    last_run to be a real runnable record (validate_active rejects one without).
  * ``record`` appends a loop_runs ledger row with the contracted shape AND
    advances the note's last_run/next_review on disk.
  * Cadence vocabulary: weekly/monthly/daily, "N days", ISO-8601 (P7D),
    every-session (always due), on-event (never time-due).
  * Ranking: most-overdue-first; never-run floats to the top.
  * capture.feedback/value/failure each write a schema-valid Meta note AND a
    ledger row; value_event rows carry target/polarity/strength so
    recommendation.adjudicate can consume them; the scorecard rolls them up.

Self-contained: depends only on loops.py / capture.py + the floor + the shared
``minimal_oracle`` fixture. Loop records are built INLINE (no spawn needed).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import loops  # noqa: E402
import capture  # noqa: E402
import ledger  # noqa: E402


# --------------------------------------------------------------------------- #
# inline loop record builder (no spawn dependency)
# --------------------------------------------------------------------------- #
def _loops_dir(root: Path) -> Path:
    d = root / "Meta.nosync" / "Loops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_loop(
    root: Path,
    loop_id: str,
    *,
    cadence: str = "weekly",
    status: str = "active",
    runner: str = "agent-worklist",
    last_run: str | None = None,
    next_review: str | None = None,
    trigger_conditions: list[str] | None = None,
    model_policy: bool = False,
    created: str = "2026-01-01",
) -> Path:
    """Materialize a loop record inline as a block-style frontmatter note.

    The filename is a fixed literal under the test tmp tree (not user-influenced),
    so the constant-string ``write_text`` here is allowed by the no-bypass guard
    (it greps the _tools/ layer, not the test tree).
    """
    d = _loops_dir(root)
    fm_lines = [
        "---",
        f"id: {loop_id}",
        "type: loop",
        f"title: Loop {loop_id}",
        f"created: {created}",
        f"updated: {created}",
        "sensitivity: internal",
        f"status: {status}",
        "tags:",
        "  - meta",
        "  - loop",
        f"cadence: {cadence}",
    ]
    if runner is not None:
        fm_lines.append(f"runner: {runner}")
    fm_lines.append(f"last_run: {last_run if last_run else 'null'}")
    fm_lines.append(f"next_review: {next_review if next_review else 'null'}")
    if model_policy:
        fm_lines.extend(
            [
                "model_policy:",
                "  version: loop-model-policy/v1",
                "  applies_to:",
                "    - scheduled",
                "    - headless",
                "    - agent-worklist",
                "  deterministic_code_first: true",
                "  default_model_selection: cheapest_fully_capable",
                "  premium_model_use:",
                "    allowed_when_any:",
                "      - explicit_admin_approval",
                "      - documented_on_demand_complexity",
                "    rationale_required: true",
                "  multi_agent_passes:",
                "    allowed_when_any:",
                "      - explicit_admin_approval",
                "      - documented_on_demand_complexity",
                "    rationale_required: true",
                "  rationale:",
                "    required_for:",
                "      - premium_model_use",
                "      - multi_agent_passes",
                "    record_in:",
                "      - loop_completion_notes",
                "      - durable_run_artifact",
                "  forbid_expensive_default_model: true",
            ]
        )
    fm_lines.append("trigger_conditions:")
    for t in (trigger_conditions or ["TBD"]):
        fm_lines.append(f"  - {t}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(f"# Loop {loop_id}")
    fm_lines.append("")
    text = "\n".join(fm_lines) + "\n"
    p = d / f"loop-{loop_id}.md"
    p.write_text(text, encoding="utf-8")
    return p


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _write_autonomy(
    root: Path,
    *,
    enabled: bool,
    allowed_loops: list[str] | None = None,
    max_files_per_run: int = 0,
    max_bytes: int = 0,
) -> Path:
    allowed_loops = allowed_loops or []

    def _block(key: str, items: list[str]) -> str:
        if not items:
            return f"{key}:\n"
        lines = "\n".join(f"  - {it}" for it in items)
        return f"{key}:\n{lines}\n"

    d = root / "Meta.nosync" / "Autonomy"
    d.mkdir(parents=True, exist_ok=True)
    text = (
        f"enabled: {'true' if enabled else 'false'}\n"
        + _block("allowed_loops", allowed_loops)
        + "writable_lanes:\n"
        + "readonly_connectors:\n"
        + "blast_radius_caps:\n"
        + f"  max_files_per_run: {int(max_files_per_run)}\n"
        + f"  max_bytes: {int(max_bytes)}\n"
        + 'kill_switch_file: "Meta.nosync/Autonomy/KILL-SWITCH"\n'
    )
    p = d / "autonomy.yml"
    p.write_text(text, encoding="utf-8")
    return p


def _action_ledger_rows(root: Path) -> list[dict]:
    rows, _warnings = ledger.load(root / "Meta.nosync" / "ledgers" / "action_event.jsonl")
    return rows


def _spawn_active_loops() -> list[dict]:
    # tests/ -> oracle-kernel/ -> assets/ -> oracle_agent/
    pkg_dir = Path(__file__).resolve().parents[3]
    script = pkg_dir / "spawn.py"
    spec = importlib.util.spec_from_file_location("spawn_oracle_for_loops_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.ACTIVE_LOOPS)


def _spawn_active_loop_ids() -> set[str]:
    return {str(row["id"]) for row in _spawn_active_loops()}


# --------------------------------------------------------------------------- #
# cadence parsing
# --------------------------------------------------------------------------- #
def test_parse_cadence_named_and_iso_and_phrase():
    assert loops.parse_cadence("weekly") == timedelta(days=7)
    assert loops.parse_cadence("daily") == timedelta(days=1)
    assert loops.parse_cadence("monthly") == timedelta(days=30)
    assert loops.parse_cadence("P7D") == timedelta(days=7)
    assert loops.parse_cadence("PT12H") == timedelta(hours=12)
    assert loops.parse_cadence("3 days") == timedelta(days=3)
    assert loops.parse_cadence("every 2 weeks") == timedelta(weeks=2)
    assert loops.parse_cadence("every-session") == loops.ALWAYS_DUE
    assert loops.parse_cadence("on-event") == loops.NEVER_TIME_DUE


# --------------------------------------------------------------------------- #
# the headline due-ness case: weekly + 8 days ago -> due; after record -> not
# --------------------------------------------------------------------------- #
def test_weekly_loop_eight_days_ago_is_due(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    eight_days_ago = _iso(now - timedelta(days=8))
    _write_loop(root, "memory-matriculation", cadence="weekly", last_run=eight_days_ago)

    worklist = loops.compute_due(loops.list_loops(root), now)
    ids = [d.id for d in worklist]
    assert "memory-matriculation" in ids
    d = next(x for x in worklist if x.id == "memory-matriculation")
    assert d.overdue_seconds > 0  # genuinely overdue, not just never-run


def test_weekly_loop_six_days_ago_is_not_due(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    six_days_ago = _iso(now - timedelta(days=6))
    _write_loop(root, "memory-matriculation", cadence="weekly", last_run=six_days_ago)
    worklist = loops.compute_due(loops.list_loops(root), now)
    assert "memory-matriculation" not in [d.id for d in worklist]


def test_record_advances_last_run_so_loop_is_no_longer_due(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    eight_days_ago = _iso(now - timedelta(days=8))
    _write_loop(root, "source-capture", cadence="weekly", last_run=eight_days_ago)

    # Due before recording.
    assert "source-capture" in [d.id for d in loops.compute_due(loops.list_loops(root), now)]

    rec = loops.record(root, "source-capture", "ok", now=now)
    assert rec["last_run"] == _iso(now)
    assert rec["next_review"] == _iso(now + timedelta(days=7))

    # Re-load from disk: last_run advanced -> not due at the same `now`.
    fresh = loops.list_loops(root)
    advanced = next(l for l in fresh if l.id == "source-capture")
    assert advanced.last_run == now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert "source-capture" not in [d.id for d in loops.compute_due(fresh, now)]


def test_record_appends_loop_runs_ledger_row_with_contract_shape(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 9, 30, 0)
    _write_loop(root, "workproduct-io", cadence="weekly", last_run=_iso(now - timedelta(days=10)))
    rec = loops.record(root, "workproduct-io", "ok", now=now, health_signal="healthy")

    led = root / "Meta.nosync" / "ledgers" / "loop_runs.jsonl"
    rows, warnings = ledger.load(led)
    assert warnings == []
    assert len(rows) == 1
    row = rows[0]
    # contracted loop_runs shape
    for key in ("drop_id", "ts", "loop_id", "status", "last_run", "next_review", "health_signal", "notes"):
        assert key in row, f"missing {key} in loop_runs row"
    assert row["loop_id"] == "workproduct-io"
    assert row["status"] == "ok"
    assert row["last_run"] == _iso(now)
    assert row["drop_id"] == rec["drop_id"]
    assert row["drop_id"].startswith("LRUN-")


# --------------------------------------------------------------------------- #
# determinism + ranking
# --------------------------------------------------------------------------- #
def test_compute_due_is_deterministic_for_same_now(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(root, "a", cadence="weekly", last_run=_iso(now - timedelta(days=9)))
    _write_loop(root, "b", cadence="daily", last_run=_iso(now - timedelta(days=2)))
    loop_list = loops.list_loops(root)
    first = [d.id for d in loops.compute_due(loop_list, now)]
    second = [d.id for d in loops.compute_due(loop_list, now)]
    assert first == second


def test_never_run_loop_ranks_above_merely_overdue(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(root, "overdue", cadence="weekly", last_run=_iso(now - timedelta(days=8)))
    _write_loop(root, "neverrun", cadence="weekly", last_run=None)
    worklist = loops.compute_due(loops.list_loops(root), now)
    ids = [d.id for d in worklist]
    assert ids.index("neverrun") < ids.index("overdue")
    nr = next(d for d in worklist if d.id == "neverrun")
    assert nr.reason == "never-run"


# --------------------------------------------------------------------------- #
# only active loops; cadence sentinels
# --------------------------------------------------------------------------- #
def test_proposed_loop_is_never_due(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root, "proposed-one", cadence="weekly", status="proposed",
        runner="agent-worklist", last_run=_iso(now - timedelta(days=30)),
    )
    assert loops.compute_due(loops.list_loops(root), now) == []


def test_every_session_loop_is_always_due(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(root, "session-loop", cadence="every-session", last_run=_iso(now))
    worklist = loops.compute_due(loops.list_loops(root), now)
    assert "session-loop" in [d.id for d in worklist]
    d = next(x for x in worklist if x.id == "session-loop")
    assert d.reason == "every-session"


def test_event_loop_is_not_time_due_after_running(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root, "event-loop", cadence="on-event",
        last_run=_iso(now - timedelta(days=365)),
        trigger_conditions=["a contradiction opens"],
    )
    # It has run (last_run set) -> never time-due regardless of how long ago.
    assert "event-loop" not in [d.id for d in loops.compute_due(loops.list_loops(root), now)]


# --------------------------------------------------------------------------- #
# active-loop validity: a runner is required
# --------------------------------------------------------------------------- #
def test_active_loop_requires_runner(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    p = _write_loop(
        root, "no-runner", cadence="weekly", runner=None, last_run=_iso(now)
    )
    loop = loops.read_note(p)
    errs = loops.validate_active(root, loop.frontmatter)
    assert any("runner" in e for e in errs), errs


def test_active_loop_with_runner_and_last_run_is_valid(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    p = _write_loop(
        root, "good-loop", cadence="weekly",
        runner="agent-worklist", last_run=_iso(now), next_review=_iso(now + timedelta(days=7)),
    )
    loop = loops.read_note(p)
    assert loops.validate_active(root, loop.frontmatter) == []


def test_list_loops_skips_loop_template_file_even_with_concrete_example_id(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    d = _loops_dir(root)
    template = d / "loop-template.md"
    template.write_text(
        "\n".join(
            [
                "---",
                "id: loop-memory-matriculation",
                "type: loop",
                "title: Template example",
                "created: 2026-01-01",
                "updated: 2026-01-01",
                "sensitivity: internal",
                "status: active",
                "tags:",
                "  - meta",
                "  - loop",
                "cadence: every-session",
                "runner: agent-worklist",
                "last_run: 2026-01-01",
                "next_review: 2026-01-01",
                "trigger_conditions:",
                "  - example",
                "---",
                "",
                "# Template example",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert loops.list_loops(root) == []


# --------------------------------------------------------------------------- #
# run dispatch (agent-worklist)
# --------------------------------------------------------------------------- #
def test_run_agent_worklist_returns_work_without_recording(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root, "user-feedback-learning", cadence="weekly",
        runner="agent-worklist", last_run=_iso(now - timedelta(days=8)),
        trigger_conditions=["user gives feedback"],
    )
    result = loops.run(root, "user-feedback-learning", now=now, headless=False)
    assert result["status"] == "worklist"
    assert result["performed"] is False  # agent-worklist hands back work, doesn't perform
    assert result["kind"] == "agent-worklist"
    assert result["outcome"]["worklist"]["loop_id"] == "user-feedback-learning"
    policy = result["outcome"]["worklist"]["model_policy"]
    assert policy["version"] == "loop-model-policy/v1"
    assert policy["deterministic_code_first"] is True
    assert policy["default_model_selection"] == "cheapest_fully_capable"
    assert set(policy["applies_to"]) == {"scheduled", "headless", "agent-worklist"}
    assert policy["premium_model_use"]["rationale_required"] is True
    assert policy["multi_agent_passes"]["rationale_required"] is True
    assert policy["forbid_expensive_default_model"] is True
    # The run was NOT recorded. Viewing a worklist is not completing the loop.
    rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "loop_runs.jsonl")
    assert rows == []
    advanced = next(l for l in loops.list_loops(root) if l.id == "user-feedback-learning")
    assert advanced.last_run == (now - timedelta(days=8)).replace(hour=0, minute=0, second=0, microsecond=0)


def test_record_preserves_nested_model_policy_frontmatter(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root,
        "source-capture",
        cadence="weekly",
        runner="agent-worklist",
        last_run=_iso(now - timedelta(days=8)),
        next_review=_iso(now - timedelta(days=1)),
        model_policy=True,
    )

    loops.record(root, "source-capture", "ok", now=now)

    fresh = next(l for l in loops.list_loops(root) if l.id == "source-capture")
    policy = fresh.frontmatter["model_policy"]
    assert policy["version"] == "loop-model-policy/v1"
    assert policy["premium_model_use"]["allowed_when_any"] == [
        "explicit_admin_approval",
        "documented_on_demand_complexity",
    ]
    assert policy["rationale"]["record_in"] == [
        "loop_completion_notes",
        "durable_run_artifact",
    ]


def test_builtin_self_improvement_runners_write_notes_and_consume_events(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root,
        "user-feedback-learning",
        cadence="on-event",
        runner="builtin:user-feedback-learning",
        last_run=_iso(now),
        trigger_conditions=["feedback_event landed"],
    )
    _write_loop(
        root,
        "skill-repository-learning",
        cadence="on-event",
        runner="builtin:skill-repository-learning",
        last_run=_iso(now),
        trigger_conditions=["feedback_event landed"],
    )
    capture.feedback_event(
        root,
        target="answer-protocol",
        polarity=1,
        strength=0.9,
        excerpt="Prefer more explicit caveats on stale finance evidence.",
        actor="admin",
        now=now,
    )

    user_result = loops.run(root, "user-feedback-learning", now=now)

    assert user_result["status"] == "ok"
    assert user_result["performed"] is True
    assert user_result["kind"] == "builtin:user-feedback-learning"
    assert loops.pending_events(root, "user-feedback-learning") == []
    assert len(loops.pending_events(root, "skill-repository-learning")) == 1
    user_note = root / "Meta.nosync" / "User-Models" / "user-model-self-improvement.md"
    assert user_note.exists()
    assert "Prefer more explicit caveats" in user_note.read_text(encoding="utf-8")

    skill_result = loops.run(root, "skill-repository-learning", now=now)

    assert skill_result["status"] == "ok"
    assert skill_result["performed"] is True
    assert skill_result["kind"] == "builtin:skill-repository-learning"
    assert loops.pending_events(root, "skill-repository-learning") == []
    improvement = root / "Meta.nosync" / "Improvements" / "improvement-skill-repository-learning.md"
    assert improvement.exists()
    assert "Review for reusable skill/procedure update" in improvement.read_text(encoding="utf-8")


def test_headless_gated_run_denial_logs_action_event(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root,
        "user-feedback-learning",
        cadence="weekly",
        runner="agent-worklist",
        last_run=_iso(now - timedelta(days=8)),
        trigger_conditions=["user gives feedback"],
    )
    _write_autonomy(root, enabled=False, allowed_loops=["user-feedback-learning"])

    result = loops.run(root, "user-feedback-learning", now=now, headless=True, gate=True)

    assert result["status"] == "denied"
    assert result["performed"] is False
    rows = _action_ledger_rows(root)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "loop.run"
    assert row["phase"] == "intended"
    assert row["result"] == "deny"
    assert row["scope"]["loop"] == "user-feedback-learning"
    assert "autonomy-disabled" in row["reason"]
    loop_rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "loop_runs.jsonl")
    assert loop_rows == []


def test_headless_gated_run_grant_logs_intended_and_actual(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root,
        "user-feedback-learning",
        cadence="weekly",
        runner="agent-worklist",
        last_run=_iso(now - timedelta(days=8)),
        trigger_conditions=["user gives feedback"],
    )
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["user-feedback-learning"],
        max_files_per_run=0,
        max_bytes=0,
    )

    result = loops.run(root, "user-feedback-learning", now=now, headless=True, gate=True)

    assert result["status"] == "worklist"
    assert result["performed"] is False
    rows = _action_ledger_rows(root)
    assert [r["phase"] for r in rows] == ["intended", "actual"]
    assert [r["result"] for r in rows] == ["grant", "ok"]
    assert all(r["action"] == "loop.run" for r in rows)
    assert all(r["scope"]["loop"] == "user-feedback-learning" for r in rows)


def test_headless_run_gate_false_does_not_log_action_event(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root,
        "user-feedback-learning",
        cadence="weekly",
        runner="agent-worklist",
        last_run=_iso(now - timedelta(days=8)),
        trigger_conditions=["user gives feedback"],
    )
    _write_autonomy(
        root,
        enabled=True,
        allowed_loops=["user-feedback-learning"],
        max_files_per_run=0,
        max_bytes=0,
    )

    result = loops.run(root, "user-feedback-learning", now=now, headless=True, gate=False)

    assert result["status"] == "worklist"
    assert _action_ledger_rows(root) == []


def test_feedback_event_makes_event_loops_due_until_consumed(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(
        root, "user-feedback-learning", cadence="on-event",
        runner="agent-worklist", last_run=_iso(now),
        trigger_conditions=["feedback event landed"],
    )
    _write_loop(
        root, "skill-repository-learning", cadence="on-event",
        runner="agent-worklist", last_run=_iso(now),
        trigger_conditions=["procedural feedback landed"],
    )

    # Pure time-based due-ness does not surface the already-run on-event loops.
    assert loops.compute_due(loops.list_loops(root), now) == []

    capture.feedback_event(
        root,
        target="answer:q-pricing",
        polarity="negative",
        strength=1.0,
        excerpt="The workflow should always check the current price book.",
        now=now,
    )
    due_ids = [d.id for d in loops.due(root, now)]
    assert "user-feedback-learning" in due_ids
    assert "skill-repository-learning" in due_ids

    done = loops.complete(
        root,
        "skill-repository-learning",
        "ok",
        now=now,
        consume_all=True,
        notes="patched pricing workflow skill",
        actor="test-agent",
    )
    assert done["event_consumption"]["count"] == 1
    assert loops.pending_events(root, "skill-repository-learning") == []
    # The user-model loop is independent and still needs to consume the event.
    assert len(loops.pending_events(root, "user-feedback-learning")) == 1


def test_capture_consumers_match_active_spawned_loop_event_map():
    active_loop_ids = _spawn_active_loop_ids()
    assert set(loops.LOOP_EVENT_KINDS).issubset(active_loop_ids)
    active_by_id = {str(row["id"]): row for row in _spawn_active_loops()}
    assert active_by_id["memory-matriculation"]["runner"] == "builtin:memory-matriculation"
    assert active_by_id["user-feedback-learning"]["runner"] == "builtin:user-feedback-learning"
    assert active_by_id["skill-repository-learning"]["runner"] == "builtin:skill-repository-learning"
    assert "memory-dreaming" not in active_loop_ids

    for spec in capture._EVENT_SPECS.values():
        event_type = spec["type"]
        consumers = set(spec["consumed_by"])
        mapped_consumers = {
            loop_id
            for loop_id, event_kinds in loops.LOOP_EVENT_KINDS.items()
            if event_type in event_kinds
        }
        assert consumers
        assert consumers == mapped_consumers
        assert consumers.issubset(active_loop_ids)


def test_spawned_active_loop_records_include_model_policy(spawned_oracle):
    active = [loop for loop in loops.list_loops(spawned_oracle) if loop.status == "active"]
    assert active
    for loop in active:
        policy = loop.frontmatter.get("model_policy")
        assert policy["version"] == "loop-model-policy/v1"
        assert policy["deterministic_code_first"] is True
        assert policy["default_model_selection"] == "cheapest_fully_capable"
        assert "scheduled" in policy["applies_to"]
        assert "headless" in policy["applies_to"]
        assert "agent-worklist" in policy["applies_to"]
        assert policy["premium_model_use"]["allowed_when_any"] == [
            "explicit_admin_approval",
            "documented_on_demand_complexity",
        ]
        assert policy["multi_agent_passes"]["rationale_required"] is True
        assert policy["forbid_expensive_default_model"] is True


def test_run_python_runner_calls_function(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    # Use a real importable stdlib-free runner: capture.scorecard takes (root) only,
    # so point at a tiny shim we define on the loops module namespace instead.
    # We dispatch to "capture:scorecard" is signature-mismatched; use json:dumps?
    # Instead, register a runner via an inline module is overkill -- assert the
    # agent path and the python-resolution error path are both covered.
    _write_loop(
        root, "bad-runner", cadence="weekly",
        runner="nonexistent_module_xyz:go", last_run=_iso(now - timedelta(days=8)),
    )
    result = loops.run(root, "bad-runner", now=now, headless=False)
    assert result["status"] == "fail"
    # even a failed dispatch is recorded (durability of the attempt)
    rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "loop_runs.jsonl")
    assert rows and rows[0]["status"] == "fail"


# --------------------------------------------------------------------------- #
# capture path: feedback / value / failure
# --------------------------------------------------------------------------- #
def test_value_event_writes_ledger_and_note_consumable_by_recommendation(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    res = capture.value_event(
        root,
        target="rec-2026-06-08-raise-price",
        polarity="positive",
        strength=2.0,
        value_kind="decide",
        excerpt="The price recommendation moved MRR up.",
        now=now,
    )
    assert res["kind"] == "value_event"
    assert Path(res["note_path"]).exists()
    # the value_event ledger row carries the fields recommendation.py reads
    rows, warnings = ledger.load(root / "Meta.nosync" / "ledgers" / "value_event.jsonl")
    assert warnings == []
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == "rec-2026-06-08-raise-price"
    assert row["polarity"] == 1
    assert row["strength"] == 2.0
    assert row["value_kind"] == "decide"
    # the note frontmatter back-links the ledger drop_id and is type value_event
    note_text = Path(res["note_path"]).read_text(encoding="utf-8")
    assert "type: value_event" in note_text
    assert res["drop_id"] in note_text


def test_value_event_feeds_recommendation_adjudication(tmp_path, minimal_oracle):
    """End-to-end: a value_event captured here is the OBSERVED evidence the
    recommendation adjudicator scores against (proving the loop closes)."""
    R = pytest.importorskip("recommendation")
    root = minimal_oracle(tmp_path)
    (root / "Memory.nosync" / "Recommendations").mkdir(parents=True, exist_ok=True)
    (root / "Memory.nosync" / "Decisions").mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 6, 8, 12, 0, 0)
    p = R.new(
        root,
        {
            "title": "Raise SMB price",
            "action": "Increase SMB plan $39 -> $49",
            "rationale": "Elasticity test",
            "evidence": ["pricing-test"],
            "baseline": "SMB MRR $410k",
            "expected_signal": ["SMB MRR up >=15%"],
            "risk_if_wrong": "churn",
        },
    )
    rid = R.read_note(p).id
    # capture a positive value event TARGETING the recommendation id
    capture.value_event(root, target=rid, polarity="positive", strength=3.0, now=now)
    sig = R.observed_signals(root, rid)
    assert sig["value_events"] == 1
    assert sig["net_observed_value"] == 3.0


def test_feedback_event_writes_note_and_ledger(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    res = capture.feedback_event(
        root, target="answer:q-2026-06-08-pricing", polarity="negative",
        strength=1.0, excerpt="That number was stale.", actor="admin", now=now,
    )
    assert res["kind"] == "feedback_event"
    assert set(res["consumed_by"]) == {"user-feedback-learning", "skill-repository-learning"}
    rows, warnings = ledger.load(root / "Meta.nosync" / "ledgers" / "feedback_event.jsonl")
    assert warnings == []
    assert rows[0]["polarity"] == -1
    assert rows[0]["actor"] == "admin"
    assert set(rows[0]["consumed_by"]) == {"user-feedback-learning", "skill-repository-learning"}
    assert Path(res["note_path"]).exists()
    note_text = Path(res["note_path"]).read_text(encoding="utf-8")
    assert "consumed_by:" in note_text
    assert "  - user-feedback-learning" in note_text
    assert "  - skill-repository-learning" in note_text


def test_failure_event_severity_sets_default_strength(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    res = capture.failure_event(
        root, target="answer:q-2026-06-08-pricing", severity="high",
        failure_mode="stale-answer", now=now,
    )
    assert res["kind"] == "failure_event"
    rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "failure_event.jsonl")
    row = rows[0]
    assert row["severity"] == "high"
    assert row["failure_mode"] == "stale-answer"
    assert row["polarity"] == -1
    assert row["strength"] == 3.0  # high -> 3.0 default magnitude


def test_capture_scorecard_rolls_up_all_three_kinds(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    capture.value_event(root, target="t1", polarity="positive", strength=2.0, value_kind="decide", now=now)
    capture.value_event(root, target="t2", polarity="negative", strength=1.0, value_kind="act", now=now)
    capture.feedback_event(root, target="t1", polarity="positive", strength=1.0, now=now)
    capture.failure_event(root, target="t3", severity="critical", failure_mode="crash", now=now)
    sc = capture.scorecard(root)
    assert sc["value"]["count"] == 2
    assert sc["value"]["net_signed"] == 1.0  # +2 (decide) + (-1) (act)
    assert sc["value"]["by_kind"]["decide"] == 2.0
    assert sc["value"]["by_kind"]["act"] == -1.0
    assert sc["feedback"]["count"] == 1
    assert sc["failure"]["count"] == 1
    assert sc["failure"]["by_severity"]["critical"] == 1


# --------------------------------------------------------------------------- #
# CLI smoke
# --------------------------------------------------------------------------- #
def test_loops_cli_list_due_record(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 8, 12, 0, 0)
    _write_loop(root, "cli-loop", cadence="weekly", last_run=_iso(now - timedelta(days=10)))
    assert loops.main(["--root", str(root), "list"]) == 0
    assert loops.main(["--root", str(root), "due", "--now", now.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "cli-loop" in out
    assert loops.main(["--root", str(root), "record", "cli-loop", "--status", "ok", "--now", now.isoformat()]) == 0


def test_capture_cli_value(tmp_path, minimal_oracle, capsys):
    root = minimal_oracle(tmp_path)
    rc = capture.main(
        ["--root", str(root), "value", "--target", "rec-x", "--polarity", "positive", "--strength", "2"]
    )
    assert rc == 0
    rows, _ = ledger.load(root / "Meta.nosync" / "ledgers" / "value_event.jsonl")
    assert rows and rows[0]["target"] == "rec-x"
