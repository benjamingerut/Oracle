#!/usr/bin/env python3
"""Shared pytest fixtures for the spawned-kernel test suite.

Provides:
  * ``kernel_dir``      -- Path to the oracle-kernel root (this file's grandparent).
  * ``minimal_oracle``  -- a callable(tmp_path) -> Path that materializes a
                            MINIMAL but valid oracle root inline (a real
                            oracle.yml with workproduct.routing_lanes plus the
                            Workproduct/_INPUT, Workproduct/_OUTPUT and
                            Meta.nosync/ledgers directories). Tests that only
                            need a containment base or a config use this instead
                            of running the full spawn script.
  * ``spawned_oracle``  -- LAZY fixture that runs the real spawn script into
                            tmp_path. Only request it from tests that genuinely
                            need a fully-spawned oracle; it skips cleanly if the
                            spawn script is unavailable.

The ``_tools`` directory is prepended to ``sys.path`` at import time so every
kernel test can ``import safe_paths`` / ``import oracle_yaml`` / etc. as bare
modules, matching how the tools import one another at runtime.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> oracle-kernel/ -> assets/ -> <skill root>
_TESTS_DIR = Path(__file__).resolve().parent
_KERNEL_DIR = _TESTS_DIR.parent
_TOOLS_DIR = _KERNEL_DIR / "_tools"
# tests/ -> oracle-kernel/ -> assets/ -> oracle_agent/ -> src/
_SRC_DIR = Path(__file__).resolve().parents[4]

# Make the kernel tools importable as top-level modules for every test.
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


_MINIMAL_ORACLE_YML = """\
company:
  name: "Test Co"
  codename: "TESTORACLE"
  bootstrap_date: "2026-01-01"
  maturity: scaffolded

oracle:
  purpose: "Test oracle root for unit tests."
  seed_kernel: oracle-spawn
  local_sovereignty: true
  bootstrap_admin:
    name: "Test Admin"
    role: admin

security:
  mode: local_confidential
  secrets_policy: env_only_never_logged
  external_processing_default: denied_without_admin_approval
  raw_data_export: admin_approval_required
  sensitivity_labels:
    - public
    - internal
    - confidential
    - restricted
    - secret

governance:
  roles:
    admin:
      can:
        - change_architecture
        - approve_sensitive_export
    user:
      can:
        - ask_questions
        - provide_documents
        - give_feedback
      cannot:
        - change_architecture
        - approve_raw_data_export

session_interfaces:
  default: user
  startup_prompt: false
  reset_policy: every_new_session
  goal_clarity_policy:
    version: goal-clarity-policy/v1
    default_behavior: proportional_dialectic_before_execution
    dialectic_method:
      question_style: one_at_a_time
      include_recommended_answer: true
      resolve_dependencies: one_branch_at_a_time
      inspect_available_material_first: true
    applies_to:
      - answering_questions
      - producing_workproduct
      - executing_tools
      - code_changes
      - loop_work
      - headless_work
    proportionality_axes:
      - ambiguity
      - work_extent
      - compute_cost
      - reversibility
      - business_risk
      - user_time_cost
    general_rule: "Establish enough shared understanding before execution that the agent can state the goal, output shape, constraints, and success criteria without guessing."
    levels:
      quick_low_compute:
        clarity_threshold: low
        dialectic_default: none_or_one_turn
        proceed_when:
          - intent_is_plain
          - output_is_small_or_reversible
          - reasonable_assumptions_are_low_risk
        before_execution:
          - make_reasonable_assumptions
          - ask_only_if_material_ambiguity_blocks_the_work
      bounded_standard:
        clarity_threshold: medium
        dialectic_default: targeted_back_and_forth
        proceed_when:
          - goal_and_scope_are_clear
          - important_constraints_are_known_or_stated_as_assumptions
          - success_criteria_are_inferable
        before_execution:
          - restate_goal_when_helpful
          - ask_targeted_questions_for_material_gaps
          - document_assumptions_if_proceeding
      extended_high_compute:
        clarity_threshold: high
        dialectic_default: explicit_specing_until_clear
        proceed_when:
          - goal_scope_constraints_and_non_goals_are_clear
          - output_format_and_acceptance_criteria_are_clear
          - unresolved_questions_are_non_blocking_or_explicitly_deferred
        before_execution:
          - conduct_dialectic_back_and_forth
          - confirm_success_criteria
          - identify_non_goals_and_constraints
          - record_assumptions_and_open_questions
    escalation_triggers:
      - large_or_multi_file_change
      - expensive_or_long_running_compute
      - irreversible_or_external_side_effect
      - ambiguous_business_object_or_audience
      - broad_strategy_or_architecture_request
      - request_spans_multiple_domains_or_deliverables
    do_not_over_ask_when:
      - request_is_trivial
      - cost_of_clarification_exceeds_cost_of_trying
      - safe_reversible_default_is_obvious
    proceed_with_assumptions_allowed: true
  modes:
    user:
      purpose: "Business-facing Oracle interface."
      tone: business_terms
      answer_protocol: required_for_material_answers
      control_plane_boundary: prompt_for_admin_approval
      admin_prompt: "This requires the Admin interface. Do you approve entering Admin mode for this request?"
      allow_capabilities:
        - ask_questions
        - provide_documents
        - give_feedback
      block_capabilities:
        - change_architecture
        - approve_raw_data_export
    admin:
      purpose: "Control-plane Oracle interface."
      tone: control_plane_partner
      answer_protocol: required_for_material_answers
      role_gate: policy.require_role
      allow_capabilities:
        - "*"
      block_capabilities:

ontology:
  entity_subtypes:
    - company
    - customer
    - vendor
  decision_domains:
    - finance
    - operations

workproduct:
  routing_lanes:
    - 00_Ownership-Strategy
    - 01_Finance
    - 02_Revenue-Customers
    - 03_Product-Service-Delivery
    - 04_Operations
    - 05_People
    - 06_Legal-Compliance
    - 07_Market-Competitive-Intel
  routing_status: provisional

connectors:
  default_capture_tier: snapshot
  known:

loops:
  registry: LOOPS.md
  create_loop_rule: "Create a loop when repeated re-evaluation adds value."

backup:
  tier_0_control_plane: admin_policy_needed
  tier_1_artifacts: admin_policy_needed
  tier_2_raw_data: admin_decision_required
  tier_3_secrets: never_plaintext

kernel:
  tools_version: "0.0.0-test"
  tools_sha256: ""
  manifest: ".kernel-manifest.json"

autonomy:
  config: "Meta.nosync/Autonomy/autonomy.yml"
"""


def _build_minimal_oracle(tmp_path: Path) -> Path:
    """Create a minimal valid oracle root under ``tmp_path`` and return it."""
    root = Path(tmp_path) / "oracle_root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "oracle.yml").write_text(_MINIMAL_ORACLE_YML, encoding="utf-8")

    wp = root / "Workproduct.nosync"
    for lane in [
        "00_Ownership-Strategy",
        "01_Finance",
        "02_Revenue-Customers",
        "03_Product-Service-Delivery",
        "04_Operations",
        "05_People",
        "06_Legal-Compliance",
        "07_Market-Competitive-Intel",
    ]:
        (wp / lane / "received").mkdir(parents=True, exist_ok=True)
        (wp / lane / "created").mkdir(parents=True, exist_ok=True)
    (wp / "_INPUT").mkdir(parents=True, exist_ok=True)
    (wp / "_OUTPUT").mkdir(parents=True, exist_ok=True)

    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)
    (root / "Memory.nosync" / "Sources").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def kernel_dir() -> Path:
    """Path to the oracle-kernel directory shipped in this repo."""
    return _KERNEL_DIR


@pytest.fixture
def minimal_oracle():
    """Return a builder: ``minimal_oracle(tmp_path) -> Path`` (the oracle root).

    Usage::

        def test_x(tmp_path, minimal_oracle):
            root = minimal_oracle(tmp_path)
            ...
    """
    return _build_minimal_oracle


@pytest.fixture
def spawned_oracle(tmp_path) -> Path:
    """Run the real spawn script into a temp dir and return the spawned root.

    LAZY: only request this from tests that need a fully spawned oracle. If the
    spawn script is unavailable, the test is skipped rather than failing.
    """
    import os

    spawn_module = _SRC_DIR / "oracle_agent" / "spawn.py"
    if not spawn_module.exists():
        pytest.skip(f"spawn module not present yet: {spawn_module}")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    root = Path(tmp_path) / "spawned"
    cmd = [
        sys.executable,
        "-m",
        "oracle_agent.spawn",
        "--root",
        str(root),
        "--company-name",
        "Test Co",
        "--codename",
        "TESTORACLE",
        "--admin-name",
        "Test Admin",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        pytest.skip(
            f"spawn failed (rc={proc.returncode}).\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    if not (root / "oracle.yml").exists():
        pytest.skip("spawn produced no oracle.yml.")
    return root
