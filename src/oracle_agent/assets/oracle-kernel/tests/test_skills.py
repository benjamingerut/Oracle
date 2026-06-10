#!/usr/bin/env python3
"""Tests for managed oracle-local skills."""
from __future__ import annotations

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import ledger  # noqa: E402
import oracle_lint  # noqa: E402
import skills  # noqa: E402


def _skill_events(root: Path):
    rows, warnings = ledger.load(root / "Meta.nosync" / "ledgers" / "skill_event.jsonl")
    assert warnings == []
    return rows


def test_skill_lifecycle_is_ledgered_and_archive_only(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    body = "# Pricing Review\n\n1. Check the current price book.\n2. Cite the source used."
    created = skills.create_skill(
        root,
        "pricing-review",
        description="Review pricing questions against current evidence.",
        body=body,
        tags=["pricing"],
        actor="test-agent",
        reason="workflow corrected by user",
    )
    skill_path = Path(created["path"])
    assert skill_path.exists()
    assert skill_path.relative_to(root).as_posix() == "AgentResources.nosync/Skills/pricing-review/SKILL.md"

    listed = skills.list_skills(root)
    assert listed[0]["name"] == "pricing-review"
    assert "current price book" in skills.view_skill(root, "pricing-review")

    patched = skills.patch_skill(
        root,
        "pricing-review",
        append="3. Record a skill use event when this procedure is invoked.",
        actor="test-agent",
        reason="make usage telemetry explicit",
    )
    assert patched["mode"] == "append"
    skills.record_use(root, "pricing-review", actor="test-agent", reason="used in answer prep")
    archived = skills.archive_skill(root, "pricing-review", actor="test-agent", reason="superseded by broader skill")

    assert not (root / "AgentResources.nosync" / "Skills" / "pricing-review").exists()
    archive_path = Path(archived["archive_path"])
    assert archive_path.exists()
    assert (archive_path / "SKILL.md").exists()

    actions = [row["action"] for row in _skill_events(root)]
    assert actions == ["create", "patch", "use", "archive"]
    assert all("content" not in row and "payload" not in row for row in _skill_events(root))


def test_lint_rejects_malformed_skill_package(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    pkg = root / "AgentResources.nosync" / "Skills" / "bad-skill"
    pkg.mkdir(parents=True)
    (pkg / "SKILL.md").write_text(
        "---\n"
        "name: bad-skill\n"
        "description: Missing lifecycle metadata\n"
        "---\n"
        "\n",
        encoding="utf-8",
    )
    out: list[oracle_lint.Violation] = []
    oracle_lint.check_skills(root, out)
    codes = {v.code for v in out}
    assert "skill-schema" in codes
    assert "skill-body" in codes
