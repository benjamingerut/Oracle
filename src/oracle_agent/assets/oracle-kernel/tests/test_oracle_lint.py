#!/usr/bin/env python3
"""Tests for oracle_lint.py -- the schema-validating oracle linter.

These tests are SELF-SUFFICIENT: they build a complete oracle tree inline
(including the JSON schemas the linter validates against, written into a
``schemas/`` directory the test controls and handed to the linter via the
``schemas_dir`` argument) so they pass in isolation given only this module + the
floor primitives (oracle_yaml, schema_check, secret_scan, ledger), with no
dependency on companion units.

POSITIVE: a clean oracle (clean baseline) PASSES every check.
NEGATIVE (each MUST FAIL the gate):
  * a finding asserting a certain $50M with no evidence / claim_tier / disconfirmer
  * an oracle.yml carrying an external ``/Users/...`` path
  * a note missing its ``sensitivity`` frontmatter field
  * an active loop with no ``runner``
Plus: hash-mutated immutable record, an unenforced doctrine guarantee, a
duplicate registry drop_id, a planted secret -- and the known-failures baseline
downgrading a listed violation to a warning.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

import oracle_lint
import ledger
import oracle_yaml
import schema_check


# --------------------------------------------------------------------------- #
# inline schemas (the linter validates against these; we own them in-test)
# --------------------------------------------------------------------------- #
_SENSITIVITY = ["public", "internal", "confidential", "restricted", "secret"]

_ORACLE_YML_SCHEMA = {
    "type": "object",
    "required": ["company", "oracle", "security", "governance", "ontology", "workproduct"],
    "properties": {
        "security": {
            "type": "object",
            "properties": {
                "sensitivity_labels": {"type": "array", "items": {"enum": _SENSITIVITY}},
            },
        },
        "workproduct": {
            "type": "object",
            "required": ["routing_lanes"],
            "properties": {"routing_lanes": {"type": "array", "items": {"type": "string"}}},
        },
    },
}

_NOTE_FRONTMATTER_SCHEMA = {
    "type": "object",
    "required": ["id", "type", "title", "created", "updated", "sensitivity", "status", "tags"],
    "properties": {
        "sensitivity": {"enum": _SENSITIVITY},
        "type": {"type": "string"},
    },
}

_FINDING_SCHEMA = {
    "type": "object",
    "required": ["claim_tier", "confidence", "evidence", "disconfirmer"],
    "properties": {
        "claim_tier": {"enum": ["OBS", "INF", "SPEC", "SPEC-horizon"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_CONTRADICTION_SCHEMA = {
    "type": "object",
    "required": ["status", "claims_in_conflict"],
    "properties": {
        "status": {"enum": ["open", "investigating", "resolved", "accepted_residual", "superseded"]},
        "severity": {"enum": ["low", "medium", "high", "critical"]},
    },
}

_MODEL_SCHEMA = {"type": "object", "required": ["status", "core_claim"]}
_RECOMMENDATION_SCHEMA = {"type": "object", "required": ["action", "rationale"]}

_LOOP_SCHEMA = {
    "type": "object",
    "required": ["cadence", "status"],
    "properties": {"status": {"enum": ["active", "proposed", "retired"]}},
}

_CONNECTOR_SCHEMA = {
    "type": "object",
    "required": ["id", "system", "status"],
    "properties": {
        "status": {"enum": ["planned", "active", "degraded", "broken", "deprecated"]},
    },
}


def _write_schemas(schemas_dir: Path) -> None:
    schemas_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "oracle_yml.schema.json": _ORACLE_YML_SCHEMA,
        "note_frontmatter.schema.json": _NOTE_FRONTMATTER_SCHEMA,
        "finding.schema.json": _FINDING_SCHEMA,
        "contradiction.schema.json": _CONTRADICTION_SCHEMA,
        "model.schema.json": _MODEL_SCHEMA,
        "recommendation.schema.json": _RECOMMENDATION_SCHEMA,
        "loop.schema.json": _LOOP_SCHEMA,
        "connector.schema.json": _CONNECTOR_SCHEMA,
    }
    for name, obj in files.items():
        (schemas_dir / name).write_text(json.dumps(obj, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# clean-oracle content (all block-style YAML; passes every check)
# --------------------------------------------------------------------------- #
_CLEAN_ORACLE_YML = """\
company:
  name: "Test Co"
  codename: "TESTORACLE"
  bootstrap_date: "2026-01-01"
  maturity: scaffolded

oracle:
  purpose: "Clean oracle for lint tests."
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
    user:
      can:
        - ask_questions
      cannot:
        - change_architecture

session_interfaces:
  default: user
  startup_prompt: false
  reset_policy: every_new_session
  modes:
    user:
      purpose: "Business-facing Oracle interface."
      tone: business_terms
      answer_protocol: required_for_material_answers
      control_plane_boundary: prompt_for_admin_approval
      admin_prompt: "This requires the Admin interface. Do you approve entering Admin mode for this request?"
      allow_capabilities:
        - ask_questions
      block_capabilities:
        - change_architecture
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

# A clean finding: grounded claim with tier, in-range confidence, NON-EMPTY
# evidence and disconfirmer.
_CLEAN_FINDING = """\
---
id: F-20260101-001
type: finding
title: "Q4 revenue is in a defensible range"
created: "2026-01-01"
updated: "2026-01-01"
sensitivity: confidential
status: active
tags:
  - revenue
claim_tier: INF
confidence: 0.7
decision_relevance: high
evidence:
  - "accounting export rows 1-40, as_of 2026-01-01"
disconfirmer:
  - "a restated GL would move the figure"
as_of: "2026-01-01"
---

Body: revenue lands in a defensible band given the cited evidence.
"""

_CLEAN_CONTRADICTION = """\
---
id: C-20260101-001
type: contradiction
title: "CRM and accounting disagree on account count"
created: "2026-01-01"
updated: "2026-01-01"
sensitivity: internal
status: open
severity: medium
tags:
  - accounts
claims_in_conflict:
  - "CRM says 412 accounts"
  - "accounting says 398 accounts"
decision_relevance: medium
---

Body: the two systems disagree; do not average.
"""

_CLEAN_LOOP = """\
---
id: L-memory-matriculation
type: loop
title: "memory-matriculation"
created: "2026-01-01"
updated: "2026-01-01"
sensitivity: internal
status: active
tags:
  - loop
cadence: every-session
runner: "agent-worklist"
last_run: "2026-01-01T00:00:00"
next_review: "2026-01-08T00:00:00"
trigger_conditions:
  - "a material session occurred"
---

Body: capture sources/findings/questions each material session.
"""

# Doctrine files: EVERY guarantee line names an enforcer or is 'advisory'.
_CLEAN_DOCTRINE = """\
# Security

Default mode is local_confidential.

## Hard Rules

- Secrets are never logged; presence-only checks are enforced by `oracle lint` (secret_scan).
- External processing is denied without approval -- enforced by `oracle policy check`.
- Raw data exports require admin approval -- enforced by `oracle policy export`.
- Every write is contained under the oracle root -- enforced by safe_paths.
- Calling the answer protocol before a material answer is advisory: the agent obeys it.
"""

_CLEAN_PROCESSING = """\
# Processing Matrix

| Sensitivity | Environment | Decision | Enforcer |
|---|---|---|---|
| confidential | external | denied | `oracle policy check` (policy.py) |
| restricted | external | denied | `oracle policy check` (policy.py) |
| internal | local_agent | allow-minimized | policy.py |
"""

_CLEAN_GOVERNANCE = """\
# Governance

## Roles

- A user cannot change architecture; this is enforced by `oracle policy` (require_role).
- Sensitive exports require admin approval -- enforced by policy.py.
- Actor identity is taken from a flag today; this limitation is advisory and logged.
"""


def _make_note(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def build_clean_oracle(tmp_path: Path) -> tuple[Path, Path]:
    """Materialize a clean oracle that PASSES, plus its schemas dir.

    Returns ``(root, schemas_dir)``.
    """
    root = Path(tmp_path) / "oracle"
    root.mkdir(parents=True, exist_ok=True)
    schemas_dir = Path(tmp_path) / "schemas"
    _write_schemas(schemas_dir)

    (root / "oracle.yml").write_text(_CLEAN_ORACLE_YML, encoding="utf-8")

    # Doctrine.
    (root / "DOCTRINE.md").write_text(_CLEAN_DOCTRINE, encoding="utf-8")
    (root / "PROCESSING-MATRIX.md").write_text(_CLEAN_PROCESSING, encoding="utf-8")
    (root / "GOVERNANCE.md").write_text(_CLEAN_GOVERNANCE, encoding="utf-8")

    # Memory/Meta notes.
    _make_note(root, "Memory.nosync/Findings/2026-01-01_revenue.md", _CLEAN_FINDING)
    _make_note(root, "Memory.nosync/Contradictions/2026-01-01_accounts.md", _CLEAN_CONTRADICTION)
    _make_note(root, "Meta.nosync/Loops/loop-memory-matriculation.md", _CLEAN_LOOP)
    # Context + template files must be ignored by the linter.
    _make_note(root, "Memory.nosync/Findings/_CONTEXT.md", "# Findings\n")
    _make_note(root, "Memory.nosync/Findings/_template.md", "---\ntype: finding\n---\nstub\n")

    # Workproduct skeleton (empty registries are fine).
    (root / "Workproduct.nosync" / "_INPUT").mkdir(parents=True, exist_ok=True)
    (root / "Workproduct.nosync" / "_OUTPUT").mkdir(parents=True, exist_ok=True)
    (root / "Meta.nosync" / "ledgers").mkdir(parents=True, exist_ok=True)

    return root, schemas_dir


# --------------------------------------------------------------------------- #
# POSITIVE
# --------------------------------------------------------------------------- #
def test_clean_oracle_passes(tmp_path):
    root, schemas_dir = build_clean_oracle(tmp_path)
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert report["ok"], "clean oracle should pass; failing:\n" + "\n".join(
        v.render() for v in report["failing"]
    )
    assert report["failing"] == []


def test_clean_oracle_with_empty_baseline_passes(tmp_path):
    root, schemas_dir = build_clean_oracle(tmp_path)
    # The shipped baseline (header + empty) must not introduce false warnings.
    bl = Path(tmp_path) / "known-failures.txt"
    bl.write_text(oracle_lint.BASELINE_HEADER, encoding="utf-8")
    report = oracle_lint.run(root, baseline_path=bl, schemas_dir=schemas_dir)
    assert report["ok"]
    assert report["warnings"] == []


def _write_truth_map(root: Path, *, status: str = "confirmed", source: str = "accounting/ERP") -> None:
    (root / "TRUTH-MAP.md").write_text(
        "\n".join(
            [
                "# Truth Map",
                "",
                "| Business object | Primary source | Freshness budget | Status |",
                "|---|---|---|---|",
                f"| Revenue | {source} | 7d | {status} |",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_confirmed_truth_map_unresolved_authority_fails(tmp_path):
    root, _schemas_dir = build_clean_oracle(tmp_path)
    _write_truth_map(root, status="confirmed", source="accounting/ERP")

    violations = []
    oracle_lint.check_truth_map_authorities(root, violations)

    assert any(v.code == "truth-map-authority-unresolved" for v in violations)


def test_draft_truth_map_unresolved_authority_is_allowed(tmp_path):
    root, _schemas_dir = build_clean_oracle(tmp_path)
    _write_truth_map(root, status="draft", source="accounting/ERP")

    violations = []
    oracle_lint.check_truth_map_authorities(root, violations)

    assert violations == []


def test_confirmed_truth_map_resolves_to_source_record_metadata(tmp_path):
    root, _schemas_dir = build_clean_oracle(tmp_path)
    _write_truth_map(root, status="confirmed", source="accounting/ERP")
    _make_note(
        root,
        "Memory.nosync/Sources/2026-01-01_revenue_source.md",
        textwrap.dedent(
            """\
            ---
            id: SRC-20260101-001
            type: source
            title: "Revenue export"
            created: "2026-01-01"
            updated: "2026-01-01"
            sensitivity: internal
            status: active
            tags:
              - source
            source_system: "accounting/ERP"
            authoritative_for:
              - Revenue
            as_of: "2026-01-01"
            ---

            Revenue evidence.
            """
        ),
    )

    violations = []
    oracle_lint.check_truth_map_authorities(root, violations)

    assert violations == []


def test_confirmed_truth_map_resolves_to_connector_authority(tmp_path):
    root, _schemas_dir = build_clean_oracle(tmp_path)
    _write_truth_map(root, status="confirmed", source="accounting")
    (root / "Connectors" / "accounting").mkdir(parents=True, exist_ok=True)
    (root / "Connectors" / "accounting" / "accounting.manifest.yaml").write_text(
        textwrap.dedent(
            """\
            id: accounting
            system: Accounting system
            status: active
            authoritative_for:
              - Revenue
            """
        ),
        encoding="utf-8",
    )

    violations = []
    oracle_lint.check_truth_map_authorities(root, violations)

    assert violations == []


# --------------------------------------------------------------------------- #
# NEGATIVE -- each must FAIL
# --------------------------------------------------------------------------- #
def _codes(report) -> set[str]:
    return {v.code for v in report["failing"]}


def test_unsupported_50m_finding_fails(tmp_path):
    """A certain $50M finding with no evidence / claim_tier / disconfirmer FAILS."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    bad = textwrap.dedent(
        """\
        ---
        id: F-20260101-099
        type: finding
        title: "Company is worth exactly $50M"
        created: "2026-01-01"
        updated: "2026-01-01"
        sensitivity: confidential
        status: active
        tags:
          - valuation
        evidence:
        disconfirmer:
        ---

        The company is worth exactly $50,000,000. No caveats.
        """
    )
    _make_note(root, "Memory.nosync/Findings/2026-01-01_fifty_million.md", bad)
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert not report["ok"]
    codes = _codes(report)
    # Missing claim_tier, missing/empty evidence, missing/empty disconfirmer, no confidence.
    assert "finding-claim-tier" in codes
    assert "finding-evidence" in codes
    assert "finding-disconfirmer" in codes
    assert "finding-confidence" in codes


def test_oracle_yml_external_path_fails(tmp_path):
    """An oracle.yml carrying an external /Users/... path FAILS."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    poisoned = _CLEAN_ORACLE_YML + '\nexternal_data_root: "/Users/victim/secret-data"\n'
    (root / "oracle.yml").write_text(poisoned, encoding="utf-8")
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert not report["ok"]
    ext = [v for v in report["failing"] if v.code == "external-path" and v.path == "oracle.yml"]
    assert ext, "expected an external-path violation on oracle.yml; got:\n" + "\n".join(
        v.render() for v in report["failing"]
    )


def test_external_path_scan_skips_binary_and_oversized_files(tmp_path):
    """The external-path walker must never read binary or oversized files --
    on a data-heavy oracle that was an effective hang / memory blowup."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    lane = root / "Workproduct.nosync" / "_INPUT"
    # Binary deliverable (NUL bytes) carrying a path-looking byte string.
    (lane / "export.dat").write_bytes(b"\x00\x01" * 128 + b"/Users/victim/leak" + b"\x00" * 64)
    # Oversized text file carrying a real external path.
    big = "filler line of plain text\n" * 250_000  # ~6.5 MB
    (lane / "huge.txt").write_text(big + "/Users/victim/leak\n", encoding="utf-8")
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    ext = [v for v in report["failing"] if v.code == "external-path"]
    assert not ext, "\n".join(v.render() for v in ext)


def _with_scan_exclude(yml: str, *globs: str) -> str:
    block = "security:\n  scan_exclude:\n" + "".join(f'    - "{g}"\n' for g in globs)
    return yml.replace("security:\n", block, 1)


def test_scan_exclude_exempts_declared_subtree(tmp_path):
    """security.scan_exclude globs keep an immutable raw export out of the
    secret/external-path tree walks."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    (root / "oracle.yml").write_text(
        _with_scan_exclude(_CLEAN_ORACLE_YML, "_data.nosync"), encoding="utf-8"
    )
    raw = root / "_data.nosync" / "raw"
    raw.mkdir(parents=True)
    (raw / "export.txt").write_text(
        "token: ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n"
        "path: /Users/somebody/export\n",
        encoding="utf-8",
    )
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    bad = [v for v in report["failing"] if v.code in ("secret", "external-path")]
    assert not bad, "\n".join(v.render() for v in bad)


def test_scan_exclude_cannot_exempt_sovereign_roots(tmp_path):
    """Globs over Memory.nosync/Meta.nosync (and oracle.yml / root *.md) are
    ignored: a credential in authored sovereign content is still caught."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    (root / "oracle.yml").write_text(
        _with_scan_exclude(_CLEAN_ORACLE_YML, "Memory.nosync", "*"), encoding="utf-8"
    )
    leak = root / "Memory.nosync" / "Sources" / "pasted.txt"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text("ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n", encoding="utf-8")
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    secrets = [v for v in report["failing"] if v.code == "secret"]
    assert any("pasted.txt" in v.path for v in secrets), "\n".join(
        v.render() for v in report["failing"]
    )


def test_tool_backups_are_not_linted_as_notes(tmp_path):
    """upgrade.py's timestamped rollbacks under Meta.nosync/tool-backups/ are
    frozen kernel machinery: a README without frontmatter inside one must not
    raise note-frontmatter, and a path-carrying file there must not raise
    external-path (a fresh timestamped key would recur on every upgrade)."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    backup = root / "Meta.nosync" / "tool-backups" / "20260610-110000" / "invariants"
    backup.mkdir(parents=True)
    (backup / "README.md").write_text("# plain README, no frontmatter\n", encoding="utf-8")
    (backup / "notes.txt").write_text("backed up from /Users/someone/dev\n", encoding="utf-8")
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    bad = [
        v
        for v in report["failing"]
        if "tool-backups" in v.path and v.code in ("note-frontmatter", "external-path", "secret")
    ]
    assert not bad, "\n".join(v.render() for v in bad)


def test_note_missing_sensitivity_fails(tmp_path):
    """A Memory/Meta note missing its sensitivity field FAILS."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    no_sens = textwrap.dedent(
        """\
        ---
        id: F-20260101-050
        type: finding
        title: "A grounded claim, but no sensitivity"
        created: "2026-01-01"
        updated: "2026-01-01"
        status: active
        tags:
          - misc
        claim_tier: OBS
        confidence: 0.5
        evidence:
          - "source row 1"
        disconfirmer:
          - "a newer source"
        ---

        Body.
        """
    )
    _make_note(root, "Memory.nosync/Findings/2026-01-01_no_sensitivity.md", no_sens)
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert not report["ok"]
    # The note_frontmatter schema requires 'sensitivity'; report it via note-schema.
    assert any(
        v.path.endswith("2026-01-01_no_sensitivity.md")
        and ("sensitivity" in v.message.lower() or v.code == "note-schema")
        for v in report["failing"]
    ), "expected a sensitivity violation; got:\n" + "\n".join(v.render() for v in report["failing"])


def test_active_loop_without_runner_fails(tmp_path):
    """An active loop record with no runner / last_run FAILS."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    inert = textwrap.dedent(
        """\
        ---
        id: L-inert
        type: loop
        title: "inert-but-active"
        created: "2026-01-01"
        updated: "2026-01-01"
        sensitivity: internal
        status: active
        tags:
          - loop
        cadence: weekly
        runner:
        last_run:
        next_review:
        trigger_conditions:
        ---

        Body: this loop claims to be active but has no runner.
        """
    )
    _make_note(root, "Meta.nosync/Loops/loop-inert.md", inert)
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert not report["ok"]
    codes = _codes(report)
    assert "loop-runner" in codes
    assert "loop-last-run" in codes


def test_unenforced_doctrine_guarantee_fails(tmp_path):
    """A SECURITY guarantee naming no enforcer (and not 'advisory') FAILS."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    leaky = _CLEAN_DOCTRINE + "\n- All raw exports are forbidden under every circumstance.\n"
    (root / "DOCTRINE.md").write_text(leaky, encoding="utf-8")
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert not report["ok"]
    assert any(
        v.code == "doctrine-unenforced" and v.path == "DOCTRINE.md" for v in report["failing"]
    ), "expected doctrine-unenforced; got:\n" + "\n".join(v.render() for v in report["failing"])


def test_hash_mutated_immutable_record_fails(tmp_path):
    """A record whose on-disk hash != ledger-registered hash FAILS."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    note_rel = "Memory.nosync/Sources/2026-01-01_src.md"
    note = textwrap.dedent(
        """\
        ---
        id: S-20260101-001
        type: source
        title: "An immutable source record"
        created: "2026-01-01"
        updated: "2026-01-01"
        sensitivity: confidential
        status: active
        tags:
          - source
        ---

        ORIGINAL immutable body.
        """
    )
    _make_note(root, note_rel, note)
    # Register the ORIGINAL hash in the immutability ledger.
    original_hash = oracle_lint.content_sha256((root / note_rel).read_text(encoding="utf-8"))
    led = root / "Meta.nosync" / "ledgers" / "record_hashes.jsonl"
    ledger.append(
        led,
        {
            "type": "source",
            "path": note_rel,
            "content_sha256": original_hash,
        },
        id_prefix="HASH",
    )
    # Clean run should pass (hash matches).
    clean = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert clean["ok"], "registered+unmodified record should pass:\n" + "\n".join(
        v.render() for v in clean["failing"]
    )
    # Now mutate the body in place (the forbidden edit-in-place).
    (root / note_rel).write_text(note.replace("ORIGINAL", "TAMPERED"), encoding="utf-8")
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert not report["ok"]
    assert any(v.code == "immutable-mutated" for v in report["failing"]), "\n".join(
        v.render() for v in report["failing"]
    )


def test_duplicate_registry_drop_id_fails(tmp_path):
    """Two rows with the same drop_id in a registry ledger FAILS."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    reg = root / "Workproduct.nosync" / "_INPUT" / ".registry.jsonl"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps({"drop_id": "IN-20260101-001", "ts": "2026-01-01T00:00:00", "file": "a.txt"})
        + "\n"
        + json.dumps({"drop_id": "IN-20260101-001", "ts": "2026-01-01T00:00:01", "file": "b.txt"})
        + "\n",
        encoding="utf-8",
    )
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert not report["ok"]
    assert any(v.code == "registry-dup-id" for v in report["failing"]), "\n".join(
        v.render() for v in report["failing"]
    )


def test_planted_secret_fails(tmp_path):
    """A leaked credential anywhere under root FAILS."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    leaked = _make_note(
        root,
        "Memory.nosync/Sources/2026-01-01_leak.md",
        textwrap.dedent(
            """\
            ---
            id: S-20260101-002
            type: source
            title: "Leaky note"
            created: "2026-01-01"
            updated: "2026-01-01"
            sensitivity: restricted
            status: active
            tags:
              - oops
            ---

            token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789
            """
        ),
    )
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    assert not report["ok"]
    assert any(v.code == "secret" for v in report["failing"]), "\n".join(
        v.render() for v in report["failing"]
    )
    assert leaked.exists()  # linter is read-only; never mutates the tree


# --------------------------------------------------------------------------- #
# baseline behavior
# --------------------------------------------------------------------------- #
def test_baseline_downgrades_listed_violation(tmp_path):
    """A baselined key becomes a warning; a NON-baselined sibling still fails."""
    root, schemas_dir = build_clean_oracle(tmp_path)
    # Inject a single, deterministic violation: an unenforced doctrine guarantee.
    leaky = _CLEAN_DOCTRINE + "\n- All raw exports are forbidden under every circumstance.\n"
    (root / "DOCTRINE.md").write_text(leaky, encoding="utf-8")

    # First run: discover the exact violation key.
    pre = oracle_lint.run(root, baseline_path=None, schemas_dir=schemas_dir)
    keys = [v.key for v in pre["failing"] if v.code == "doctrine-unenforced"]
    assert keys, "setup: expected a doctrine-unenforced violation"
    target_key = keys[0]

    # Baseline that key.
    bl = Path(tmp_path) / "known-failures.txt"
    bl.write_text(oracle_lint.BASELINE_HEADER + target_key + "\n", encoding="utf-8")
    report = oracle_lint.run(root, baseline_path=bl, schemas_dir=schemas_dir)
    assert report["ok"], "baselined sole violation should pass:\n" + "\n".join(
        v.render() for v in report["failing"]
    )
    assert any(v.key == target_key for v in report["warnings"])

    # A NEW, non-baselined violation is still caught (baseline never masks regressions).
    poisoned = _CLEAN_ORACLE_YML + '\nexternal_data_root: "/Users/victim/x"\n'
    (root / "oracle.yml").write_text(poisoned, encoding="utf-8")
    report2 = oracle_lint.run(root, baseline_path=bl, schemas_dir=schemas_dir)
    assert not report2["ok"]
    assert any(v.code == "external-path" for v in report2["failing"])


# --------------------------------------------------------------------------- #
# baseline file shipped with the kernel
# --------------------------------------------------------------------------- #
def test_shipped_known_failures_is_header_only(kernel_dir):
    """known-failures.txt ships with a header and an EMPTY baseline."""
    bl = kernel_dir / "known-failures.txt"
    if not bl.exists():
        pytest.skip("known-failures.txt unavailable")
    keys = oracle_lint.load_baseline(bl)
    assert keys == set(), f"shipped baseline must be empty; found: {sorted(keys)}"


# --------------------------------------------------------------------------- #
# REAL spawn output against REAL shipped schemas + REAL baseline
#
# The hand-built clean fixture above validates a curated tree against an
# in-test, permissive schema set. That is necessary for isolation but it MASKS
# the product-level question: does an actual freshly-spawned oracle pass lint
# against the schemas it actually ships and an EMPTY known-failures baseline?
# It uses NO --schemas-dir override (so the REAL _tools/schemas/ are validated
# against) and the REAL shipped known-failures.txt.
# --------------------------------------------------------------------------- #
def test_real_spawn_output_lints_clean(spawned_oracle):
    """A freshly spawned oracle must PASS lint against its OWN shipped schemas
    and the EMPTY shipped baseline -- with ZERO failing violations.

    No schemas_dir override: validation runs against the real ``_tools/schemas/``
    living inside the spawned tree. The baseline is the real shipped
    ``known-failures.txt`` (which other tests assert is empty), so 'known-failures
    only' does NOT apply -- every check must genuinely pass.
    """
    root = spawned_oracle
    baseline = root / "known-failures.txt"
    baseline_path = baseline if baseline.exists() else None
    report = oracle_lint.run(root, baseline_path=baseline_path, schemas_dir=None)
    assert report["ok"], (
        "a freshly spawned oracle must lint clean against its OWN shipped schemas "
        "and empty baseline; failing:\n"
        + "\n".join(v.render() for v in report["failing"])
    )
    assert report["failing"] == []
    # The dev/test/build machinery (tests/, .pytest_cache, *.egg-info,
    # .kernel-manifest.json) is intentionally NOT charged to the spawn's lint:
    # no secret/external-path violation may originate from inside tests/.
    assert not any(
        v.path.split("/")[0] in ("tests", ".pytest_cache")
        or v.path.endswith(".egg-info")
        for v in report["all"]
    ), "lint must not scan kernel dev/test machinery:\n" + "\n".join(
        v.render() for v in report["all"]
    )


def test_real_spawn_secret_and_external_classes_clean(spawned_oracle):
    """Spawn output carries ZERO 'secret' and ZERO 'external-path' violations.

    These two classes were the entire single blocker (89 [secret] +
    22 [external-path] on a fresh spawn). Pin them to zero so a regression in the
    tree-walk exclusions or the external-path regex tune fails loudly here.
    """
    root = spawned_oracle
    report = oracle_lint.run(root, baseline_path=None, schemas_dir=None)
    secrets = [v for v in report["all"] if v.code == "secret"]
    ext = [v for v in report["all"] if v.code == "external-path"]
    assert secrets == [], "fresh spawn leaked secret findings:\n" + "\n".join(
        v.render() for v in secrets
    )
    assert ext == [], "fresh spawn flagged external paths:\n" + "\n".join(
        v.render() for v in ext
    )


# --------------------------------------------------------------------------- #
# CLI exit codes
# --------------------------------------------------------------------------- #
def test_cli_returns_zero_on_clean(tmp_path, capsys):
    root, schemas_dir = build_clean_oracle(tmp_path)
    rc = oracle_lint.main([str(root), "--schemas-dir", str(schemas_dir)])
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_returns_one_on_violation(tmp_path, capsys):
    root, schemas_dir = build_clean_oracle(tmp_path)
    (root / "oracle.yml").write_text(
        _CLEAN_ORACLE_YML + '\nexternal_data_root: "/Users/x/y"\n', encoding="utf-8"
    )
    rc = oracle_lint.main([str(root), "--schemas-dir", str(schemas_dir)])
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# INTEGRATION -- the REAL shipped schema + REAL spawn output
# --------------------------------------------------------------------------- #
# The fixture-driven tests above own an in-test, permissive ``_ORACLE_YML_SCHEMA``
# and a hand-written ``_CLEAN_ORACLE_YML``. That decoupling lets the unit suite
# stay green while a freshly SPAWNED oracle, linted against the SHIPPED
# ``_tools/schemas/oracle_yml.schema.json``, FAILS. The tests below close that gap
# by exercising the real artifacts (the shipped schema + the shipped oracle.yml +
# the real spawn output) so the integration can never go green-while-broken again.

_PLACEHOLDER_RENDER = {
    "{{COMPANY_NAME}}": "Integration Test Co",
    "{{CODENAME}}": "TESTORACLE",
    "{{DATE}}": "2026-01-01",
    "{{ADMIN_NAME}}": "Test Admin",
}


def _render_placeholders(text: str) -> str:
    for k, v in _PLACEHOLDER_RENDER.items():
        text = text.replace(k, v)
    return text


def test_shipped_oracle_yml_validates_against_shipped_schema(kernel_dir):
    """The SHIPPED oracle.yml must satisfy the SHIPPED oracle_yml.schema.json.

    This is the load-bearing integration check the permissive in-test schema was
    hiding: the kernel's own ``oracle.yml`` template, parsed by the real
    ``oracle_yaml.safe_load`` and validated against the real shipped JSON schema,
    must produce ZERO schema errors. (4 enum mismatches on
    security.mode/external_processing_default/raw_data_export, the
    backup.tier_0..3 key names, and connectors.known being null-vs-array are
    exactly what this catches.)
    """
    oracle_yml = kernel_dir / "oracle.yml"
    schema_path = kernel_dir / "_tools" / "schemas" / "oracle_yml.schema.json"
    if not oracle_yml.exists() or not schema_path.exists():
        pytest.skip("shipped oracle.yml or schema unavailable")

    rendered = _render_placeholders(oracle_yml.read_text(encoding="utf-8"))
    data = oracle_yaml.safe_load(rendered)
    assert isinstance(data, dict), "shipped oracle.yml must parse to a mapping"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    errors = schema_check.validate(data, schema)
    assert errors == [], (
        "shipped oracle.yml does not satisfy its own shipped schema:\n  - "
        + "\n  - ".join(errors)
    )


def test_spawned_oracle_passes_real_lint(spawned_oracle):
    """A REAL spawned oracle, linted against the REAL shipped schemas, PASSES.

    Runs the actual spawn script (via the ``spawned_oracle`` fixture) and lints
    the spawned tree with ``schemas_dir=None`` so the linter loads the SHIPPED
    ``_tools/schemas/`` -- the exact configuration the product ships and the one
    the fixture-based unit tests bypass. No ``oracle-yml-schema`` violation, no
    leaked ``secret`` (spawn must not copy test-fixture/dev secrets into the
    oracle), and no ``external-path`` sovereignty leak may survive. Empty baseline:
    'known-failures only' does not apply -- a clean spawn is clean.
    """
    report = oracle_lint.run(spawned_oracle, baseline_path=None, schemas_dir=None)

    schema_fails = [v for v in report["failing"] if v.code == "oracle-yml-schema"]
    secret_fails = [v for v in report["failing"] if v.code == "secret"]
    extpath_fails = [v for v in report["failing"] if v.code == "external-path"]

    assert not schema_fails, (
        "spawned oracle.yml fails the shipped schema:\n  - "
        + "\n  - ".join(v.render() for v in schema_fails)
    )
    assert not secret_fails, (
        "spawned oracle leaks secrets (spawn must exclude test/dev artifacts):\n  - "
        + "\n  - ".join(v.render() for v in secret_fails[:20])
    )
    assert not extpath_fails, (
        "spawned oracle carries external host paths:\n  - "
        + "\n  - ".join(v.render() for v in extpath_fails[:20])
    )
    assert report["ok"], (
        "freshly spawned oracle must pass the real lint with an empty baseline; "
        "failing:\n  - " + "\n  - ".join(v.render() for v in report["failing"][:40])
    )


def test_spawned_oracle_has_working_root_local_wrapper(spawned_oracle, tmp_path):
    """A fresh spawn ships a portable ``./oracle`` entrypoint.

    Agents must not have to rediscover ``python3 _tools/oracle_cli.py`` or rely
    on a user-level PATH shim. The wrapper must work from the root and when
    invoked by absolute path from another cwd.
    """
    wrapper = spawned_oracle / "oracle"
    assert wrapper.is_file(), "spawned oracle must include a root-local ./oracle wrapper"
    assert wrapper.stat().st_mode & 0o111, "./oracle wrapper must be executable"

    proc = subprocess.run(
        [str(wrapper), "--help"],
        cwd=str(spawned_oracle),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "usage: oracle <verb>" in proc.stdout

    proc = subprocess.run(
        [
            str(wrapper),
            "policy",
            "role",
            "--actor",
            "Test Admin",
            "--role",
            "admin",
            "--capability",
            "update_oracle_config",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "GRANTED" in proc.stdout
