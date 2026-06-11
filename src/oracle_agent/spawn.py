#!/usr/bin/env python3
"""spawn_oracle.py -- spawn a self-contained company oracle from the seed kernel.

This spawn tool:

* Stamps ``kernel.tools_version`` and ``kernel.tools_sha256`` into the spawned
  ``oracle.yml`` from a freshly rendered ``.kernel-manifest.json`` (the integrity
  baseline ``upgrade.py`` / ``oracle_lint.py`` consume).
* Instantiates the 5 ACTIVE loops as REAL runnable records under
  ``Meta.nosync/Loops/`` -- each with a real ``runner``, ``last_run`` and
  ``next_review`` (an active loop with a null ``last_run`` is inert and FAILS
  lint / setup_audit).
* Seeds the empty FTS index directory (``_data.nosync/index/``) and best-effort
  initializes the index DB so retrieval is ready on first use.
* ``chmod +x`` on the root-local ``oracle`` wrapper and ``load-env.sh``.
* ``--force`` performs a TOOL-LAYER-ONLY merge: it refreshes ``_tools/`` and the
  kernel manifest, but NEVER overwrites ``oracle.yml`` / ``Memory.nosync`` /
  ``Meta.nosync`` (a spawned oracle's sovereign data + doctrine + tuning). This
  keeps customized oracle state intact.
* Runs ``setup_audit`` + ``oracle_lint`` post-spawn and surfaces their verdict.

CLI (binding contract)::

    python3 scripts/spawn_oracle.py --root PATH --company-name N \\
        [--codename C] --admin-name A [--force] [--no-postcheck]

Stdlib only. Renders the kernel's literal ``{{COMPANY_NAME}}`` / ``{{CODENAME}}``
/ ``{{ADMIN_NAME}}`` / ``{{DATE}}`` placeholders. The kernel ships those
placeholders LITERAL; spawn is the only thing that renders them.

This SKILL-side tool writes into a freshly created oracle root whose layout it
controls end-to-end (it is the author of every byte under ``--root``); the
spawned kernel's own writers route user-/config-influenced paths through
``safe_paths``. Spawn itself uses ``shutil``/``write_text`` to materialize the
fixed kernel tree -- this script is host tooling, not a kernel module, and is not
in scope for the no-bypass guard.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Suffixes whose CONTENT we render placeholders into. Everything else is copied
# byte-for-byte (binary safety).
TEXT_SUFFIXES = {
    ".md", ".yml", ".yaml", ".txt", ".py", ".sh", ".json", ".toml",
    ".example", ".template", ".cfg", ".ini", ".env", "",
}

# Tool layer (refreshed even on --force) and the kernel manifest.
_TOOLS_DIR = "_tools"
_MANIFEST = ".kernel-manifest.json"
_CLI_WRAPPER = "oracle"
_RUNTIME_DIRS = (
    "tmp.nosync",
    "Analysis.nosync",
    "_data.nosync",
    "dashboards.nosync",
    "AgentResources.nosync",
)

# Dev-only / rebuildable artifacts that live in the kernel ASSET (so the build's
# own CI can verify the kernel in place) but MUST NOT ship into a spawned
# end-user oracle. The kernel's ``tests/`` tree in particular carries
# deliberately-fake secret fixtures and (stale) ``.pytest_cache`` blobs; copying
# them in makes a freshly spawned oracle FAIL its own lint/secret_scan on the
# kernel's test material instead of on real company content. ``upgrade.py``
# degrades gracefully when ``tests/`` is absent (lint stays the binding gate),
# so omitting them is safe. Names are matched against any path COMPONENT.
_EXCLUDED_DIR_NAMES = frozenset({
    "tests",          # the kernel's own pytest suite + fake-secret fixtures
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".coverage",
    "htmlcov",
    "build",
    "dist",
})
# Suffixes for rebuildable / dev-only files anywhere in the tree.
_EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo", ".egg-info"})
# Exact basenames of dev-only files to skip wherever they appear.
_EXCLUDED_BASENAMES = frozenset({".DS_Store", ".coverage"})

# Sovereign trees a --force merge must NEVER overwrite.
_SOVEREIGN_TREES = ("oracle.yml", "Memory.nosync", "Meta.nosync", "Connectors")

LOOP_MODEL_POLICY = {
    "version": "loop-model-policy/v1",
    "applies_to": ["scheduled", "headless", "agent-worklist"],
    "deterministic_code_first": True,
    "default_model_selection": "cheapest_fully_capable",
    "premium_model_use": {
        "allowed_when_any": [
            "explicit_admin_approval",
            "documented_on_demand_complexity",
        ],
        "rationale_required": True,
    },
    "multi_agent_passes": {
        "allowed_when_any": [
            "explicit_admin_approval",
            "documented_on_demand_complexity",
        ],
        "rationale_required": True,
    },
    "rationale": {
        "required_for": ["premium_model_use", "multi_agent_passes"],
        "record_in": [
            "loop_completion_notes",
            "durable_run_artifact",
        ],
    },
    "forbid_expensive_default_model": True,
}

# The active loops instantiated at spawn. Each becomes a real, lint-clean
# record under Meta.nosync/Loops/loop-<id>.md. ``id`` is the SHORT id (matching
# setup_audit.ACTIVE_LOOP_IDS and the LOOPS.md registry); the filename carries the
# ``loop-`` prefix. ``next_review_after`` advances last_run by a cadence-sized
# window so the due-ness engine has a real horizon (event loops review next day).
ACTIVE_LOOPS = [
    {
        "id": "memory-matriculation",
        "title": "Memory matriculation",
        "cadence": "every-session",
        "runner": "builtin:memory-matriculation",
        "next_review_days": 1,
        "tags": ["meta", "loop", "memory", "dreaming"],
        "triggers": [
            "a material session was captured in Meta.nosync/Sessions",
            "new material logged to Workproduct.nosync/_INPUT",
            "a source record was created or superseded",
            "a finding, question, or contradiction was added or changed",
        ],
        "health": (
            "green when captured sessions have been decomposed into canonical "
            "Memory/Meta records, derived recall/graph files are refreshed, no _INPUT "
            "row older than its cadence lacks a Source record, and no on-disk/ledger "
            "hash mismatch is outstanding"
        ),
        "purpose": (
            "Keep company memory current and trustworthy. As new material arrives, "
            "and as sessions teach new business information, decompose it into the "
            "right behavioral-type notes, link them, verify the links, refresh "
            "derived MemPalace/Graphify recall surfaces, and refresh anything that "
            "has decayed. This loop turns raw intake and session memory into "
            "matriculated, queryable, immutable-where-it-matters memory."
        ),
        "process": [
            "Run `_tools/session_memory.py dream` to decompose captured session memory "
            "into Findings, Questions, Contradictions, Queries, Improvements, and "
            "derived MemPalace/Graphify artifacts.",
            "List `_INPUT` rows that lack a `Memory.nosync/Sources/` record.",
            "For each, run `_tools/ingest_pipeline.py` (extract -> chunk -> index -> "
            "immutable Source record).",
            "Emit review-gated Finding / Question / Contradiction candidates "
            "(`status: needs_review`) -- never auto-trust.",
            "Link new notes to existing entities, sources, and contradictions.",
            "Verify on-disk content hashes against the ledger; flag any mismatch for "
            "supersession rather than silent edit.",
        ],
    },
    {
        "id": "source-capture",
        "title": "Source capture",
        "cadence": "on-event",
        "runner": "agent-worklist",
        "next_review_days": 1,
        "tags": ["meta", "loop", "sources", "provenance"],
        "triggers": [
            "evidence was pulled by a connector",
            "a file was ingested into Workproduct.nosync/_INPUT",
            "a derivation references a source that has no Sources/ record yet",
        ],
        "health": (
            "green when every ingested/pulled artifact has an immutable, content-hashed "
            "Sources/ record before any derivation built on it is trusted"
        ),
        "purpose": (
            "Whenever evidence is pulled or ingested, snapshot it with provenance into "
            "`Memory.nosync/Sources/` (immutable, content-hashed) BEFORE any derivation "
            "is trusted. Provenance is the spine of accuracy: no claim ships without a "
            "source it can be traced to."
        ),
        "process": [
            "Detect newly pulled/ingested artifacts lacking a `Sources/` record.",
            "Create an immutable, schema-valid Source record via "
            "`_tools/source_record.py` (provenance, sha256, locality, sensitivity, "
            "grain card, as_of).",
            "Register the record in the ledger with its content hash.",
            "Link the Source to the connector/intake that produced it.",
        ],
    },
    {
        "id": "workproduct-io",
        "title": "Workproduct I/O hygiene",
        "cadence": "every-session",
        "runner": "agent-worklist",
        "next_review_days": 1,
        "tags": ["meta", "loop", "workproduct"],
        "triggers": [
            "a file was logged to _INPUT",
            "an artifact was emitted to _OUTPUT",
            "a Workproduct lane received or created an artifact",
        ],
        "health": (
            "green when every _INPUT/_OUTPUT row is registered, contained to a valid "
            "lane, and nothing is orphaned or unrouted"
        ),
        "purpose": (
            "Every session, keep `Workproduct.nosync/` registries clean and "
            "matriculated: inputs logged, outputs emitted under policy, nothing "
            "orphaned. The workproduct engine stays trustworthy only if its I/O is "
            "disciplined."
        ),
        "process": [
            "Reconcile `_INPUT/.registry.jsonl` and `_OUTPUT/.registry.jsonl` against "
            "the files actually present.",
            "Confirm every artifact is contained to a valid `routing_lane` "
            "(`safe_paths.assert_lane`).",
            "Confirm every emit was policy-gated and recorded an `export_event`.",
            "Flag orphans (file with no registry row, or row with no file).",
        ],
    },
    {
        "id": "user-feedback-learning",
        "title": "User feedback learning",
        "cadence": "on-event",
        "runner": "builtin:user-feedback-learning",
        "next_review_days": 1,
        "tags": ["meta", "loop", "feedback", "self-improvement"],
        "triggers": [
            "a feedback_event was written by _tools/capture.py",
            "a value_event or failure_event landed",
            "the admin corrected or steered an answer",
        ],
        "health": (
            "green when every feedback/value/failure event has been consumed into a "
            "User-Model update or an Improvement, with nothing unprocessed past its cadence"
        ),
        "purpose": (
            "On every feedback event (written by `_tools/capture.py`), learn the "
            "admin's value, style, and corrections so the oracle improves toward THIS "
            "leader, not a generic one. This is the loop that makes the oracle "
            "self-improving rather than static."
        ),
        "process": [
            "List unconsumed `feedback_event` / `value_event` / `failure_event` rows.",
            "Update the relevant `Meta.nosync/User-Models/` note (preferences, style, "
            "corrections, value signals).",
            "Open an `Improvement` when a correction implies a durable change.",
            "Mark events consumed and append a `loop_runs` row.",
        ],
    },
    {
        "id": "skill-repository-learning",
        "title": "Skill repository learning",
        "cadence": "on-event",
        "runner": "builtin:skill-repository-learning",
        "next_review_days": 1,
        "tags": ["meta", "loop", "skills", "self-improvement"],
        "triggers": [
            "a feedback_event reveals a durable workflow correction",
            "a failure_event reveals a reusable recovery procedure",
            "a value_event confirms a repeated workflow created value",
        ],
        "health": (
            "green when every durable procedural signal has either updated a "
            "managed skill or been explicitly rejected as non-reusable"
        ),
        "purpose": (
            "Turn repeated successful workflows, user corrections, and failure "
            "recoveries into portable oracle-local skills under "
            "`AgentResources.nosync/Skills/`. This loop improves procedural "
            "capability without mixing company facts into skills."
        ),
        "process": [
            "List unconsumed `feedback_event` / `value_event` / `failure_event` rows.",
            "Separate durable procedural signals from one-off preferences or company facts.",
            "Use `_tools/skills.py` to create, patch, record-use, or archive managed skills.",
            "Record event consumption only after the relevant skill decision is made.",
            "Append a `loop_runs` row with the skill changes or rejection rationale.",
        ],
    },
    {
        "id": "insight-synthesis",
        "title": "Insight synthesis",
        "cadence": "weekly",
        "runner": "builtin:insight-synthesis",
        "next_review_days": 7,
        "tags": ["meta", "loop", "models", "synthesis", "intelligence"],
        "triggers": [
            "findings accumulated on a business object with no explanatory Model",
            "findings on an object are newer than its Model's last_validated",
            "a Model passed its staleness budget",
        ],
        "health": (
            "green when no finding cluster of 3+ lacks a Model, no Model is older "
            "than the newest finding it should explain, and no Model is past its "
            "staleness budget without a recorded re-validation"
        ),
        "purpose": (
            "Consolidate memory into understanding. Findings that only accumulate "
            "never become insight: this loop clusters findings by business object, "
            "compares each cluster against the Models folder, and drives the agent "
            "to propose, update, or re-validate explanatory Models (review-gated). "
            "This is how the oracle grows in coherence, not just volume."
        ),
        "process": [
            "Run `_tools/synthesis.py worklist` (or `./oracle loops run "
            "insight-synthesis`) to compute the deterministic worklist.",
            "For each propose-model item: read the cluster's findings and write a "
            "Model note (status: needs_review) compressing how that part of the "
            "company works, citing the findings as evidence.",
            "For each update/revalidate item: reconcile the Model with newer "
            "evidence, supersede it if its explanation no longer holds, and stamp "
            "`last_validated`.",
            "Route every model-level material claim through `./oracle answer` "
            "before stating it as established.",
        ],
    },
    {
        "id": "leadership-briefing",
        "title": "Leadership briefing",
        "cadence": "weekly",
        "runner": "builtin:leadership-briefing",
        "next_review_days": 7,
        "tags": ["meta", "loop", "briefing", "deliverable", "intelligence"],
        "triggers": [
            "the briefing cadence elapsed",
            "a must_resolve contradiction opened",
            "the admin asked what they should know",
        ],
        "health": (
            "green when a dated brief exists in Workproduct.nosync/_STANDING for "
            "the current cadence window, its registry row is recorded, and its "
            "'needs authority' appendix items have been surfaced to the admin"
        ),
        "purpose": (
            "Be the thought partner, not the librarian. On cadence, compose what "
            "leadership should know NOW: what changed, decisions waiting, open "
            "contradictions, authority coverage, stale questions -- every claim "
            "routed through the answer protocol, withheld objects listed with the "
            "exact commands that unlock them."
        ),
        "process": [
            "Run `./oracle brief publish` (the builtin runner does this on "
            "scheduled passes) to generate and file the deterministic skeleton.",
            "Append agent enrichment: interpretation, momentum, the 1-3 things "
            "most deserving attention (new material claims pass `./oracle answer`).",
            "Deliver through the admin's configured channel and capture the "
            "leader's reaction via `./oracle capture feedback|value`.",
            "Surface the 'needs authority' appendix as setup work in the session "
            "summary or the Review Inbox follow-up.",
        ],
    },
    {
        "id": "value-scorecard",
        "title": "Value scorecard",
        "cadence": "monthly",
        "runner": "builtin:value-scorecard",
        "next_review_days": 30,
        "tags": ["meta", "loop", "scorecard", "self-improvement"],
        "triggers": [
            "the monthly window elapsed",
            "the admin asked whether the oracle is actually helping",
        ],
        "health": (
            "green when a dated scorecard exists for the current window with every "
            "score citing ledger drop_ids and an explicit trend vs the prior window"
        ),
        "purpose": (
            "The oracle measures itself, by ledger. Roll the window's "
            "feedback/value/failure/answer events into one cited scorecard: "
            "grounded-rate trend, net value, failure recurrence, signal latency, "
            "improvement throughput, admin leverage. Self-improvement claims that "
            "are not measured are theatre; a regressing scorecard convenes the "
            "architecture retrospective immediately."
        ),
        "process": [
            "Run `./oracle scorecard gen` (the builtin runner does this) to compute "
            "the window's KPIs from ledgers alone and write the dated note under "
            "`Meta.nosync/Value-Scorecards/`.",
            "Read the trend verdict; a regression makes architecture-retrospective "
            "due immediately (the due-ness engine wires this).",
            "Carry forward the one concrete improvement the scorecard names.",
        ],
    },
    {
        "id": "improvement-lifecycle",
        "title": "Improvement lifecycle",
        "cadence": "weekly",
        "runner": "builtin:improvement-lifecycle",
        "next_review_days": 7,
        "tags": ["meta", "loop", "improvements", "self-improvement"],
        "triggers": [
            "an improvement was applied and its expected_signal window is open",
            "a proposed improvement aged past its decision budget",
        ],
        "health": (
            "green when no improvement sits in proposed past its age budget, every "
            "applied improvement carries a verifiable expected_signal or a manual "
            "stamp, and adjudications cite ledger evidence"
        ),
        "purpose": (
            "Close the improvement loop: proposed -> applied -> verified, with the "
            "verdict computed from OBSERVED ledger evidence (the same discipline as "
            "recommendation adjudication), never from someone asserting 'done'. An "
            "applied improvement with no observed signal is still unverified."
        ),
        "process": [
            "Run `./oracle improvements adjudicate` (the builtin runner does this): "
            "every applied improvement with a machine-checkable expected_signal is "
            "verified/regressed against the event ledgers.",
            "Work the returned worklist: decide stale proposals, manually verify "
            "manual-stamped items, judge expired predicates.",
            "Complete with `./oracle loops complete improvement-lifecycle --status ok`.",
        ],
    },
    {
        "id": "meta-health",
        "title": "Meta health",
        "cadence": "weekly",
        "runner": "builtin:meta-health",
        "next_review_days": 7,
        "tags": ["meta", "loop", "telemetry", "self-improvement"],
        "triggers": [
            "a loop recorded repeated failed runs",
            "captured events aged unconsumed past their budget",
            "autonomy denial/failure patterns accumulated",
        ],
        "health": (
            "green when no active loop has three consecutive failed runs, no "
            "unconsumed event is past its age budget, and skill/autonomy hygiene "
            "candidates have been decided"
        ),
        "purpose": (
            "Consume the oracle's own telemetry so nothing rots silently: pause "
            "repeatedly failing loops (fail-safe), enforce the 'no captured signal "
            "ages silently' guarantee, surface unused skills for archive, and draft "
            "autonomy allowlist/level proposals from the action ledger -- the admin "
            "approves, the oracle never grants itself rope."
        ),
        "process": [
            "Run `./oracle meta-health run` (the builtin runner does this).",
            "Work the worklist: fix-or-retire paused loops, drain aged signals by "
            "running their consuming loops, decide skill archive candidates, review "
            "drafted autonomy proposals.",
            "Complete with `./oracle loops complete meta-health --status ok`.",
        ],
    },
    {
        "id": "stale-finding-refresh",
        "title": "Stale finding refresh",
        "cadence": "weekly",
        "runner": "builtin:stale-finding-refresh",
        "next_review_days": 7,
        "tags": ["meta", "loop", "findings", "freshness"],
        "triggers": [
            "a confirmed finding passed its staleness budget without re-validation",
        ],
        "health": (
            "green when no confirmed finding is older than its staleness budget "
            "without a recorded re-validation, supersession, or retirement"
        ),
        "purpose": (
            "Memory must not fossilize. Confirmed findings decay as the business "
            "moves; this sweep surfaces every confirmed finding past its staleness "
            "budget so it is re-validated, superseded, or retired. A fossilized "
            "finding is a future wrong answer."
        ),
        "process": [
            "Run `./oracle synthesis --root . worklist` or `./oracle loops run "
            "stale-finding-refresh` for the deterministic sweep.",
            "For each item: re-check the finding against current sources; stamp "
            "`last_validated`, supersede, or retire it.",
            "Complete with `./oracle loops complete stale-finding-refresh --status ok`.",
        ],
    },
    {
        "id": "architecture-retrospective",
        "title": "Architecture retrospective",
        "cadence": "quarterly",
        "runner": "builtin:architecture-retrospective",
        "next_review_days": 90,
        "tags": ["meta", "loop", "architecture", "self-improvement"],
        "triggers": [
            "the quarterly cadence elapsed",
            "a value scorecard regressed",
            "a loop was paused as degraded",
            "a critical failure event landed",
        ],
        "health": (
            "green when the latest retrospective is within cadence, its verdict is "
            "recorded, and every architecture change it proposed exists as a "
            "ready-to-approve Improvement or architecture decision"
        ),
        "purpose": (
            "The oracle examines its own architecture on evidence, not vibes: are "
            "the loops the right loops, is there schema debt, are the chokepoints "
            "holding, what do the period's failures say systemically? Regression "
            "triggers convene it early. Output is ready-to-approve change proposals "
            "-- the admin decides rather than authors; structural changes still "
            "require the change_architecture capability."
        ),
        "process": [
            "Run `./oracle loops run architecture-retrospective` -- the builtin "
            "gathers the evidence dossier (scorecard trend, paused loops, failure "
            "modes, loop inventory, aged signals).",
            "Conduct the retrospective per the dossier instructions; record a "
            "Retrospectives/ note with a clear verdict.",
            "File every change as an Improvement (status: proposed, with "
            "expected_signal); structural changes as ready-to-approve proposals "
            "plus an architecture decision (ADR).",
            "Complete with `./oracle loops complete architecture-retrospective "
            "--status ok`.",
        ],
    },
]


def slugify(value: str) -> str:
    """Lowercase -> [a-z0-9-] -> collapse repeats -> strip. Non-empty fallback."""
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "company"


def render_text(text: str, mapping: dict[str, str]) -> str:
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def render_relpath(path: Path, mapping: dict[str, str]) -> Path:
    return Path(*[render_text(part, mapping) for part in path.parts])


def _is_under(rel: Path, tree: str) -> bool:
    """True iff a kernel-relative path is the tree itself or lives inside it."""
    rel_posix = rel.as_posix()
    return rel_posix == tree or rel_posix.startswith(tree + "/")


def copy_render(src: Path, dst: Path, mapping: dict[str, str], *, overwrite: bool) -> None:
    """Copy one kernel file to ``dst`` (creating dirs), rendering placeholders for
    text suffixes. When ``overwrite`` is False, an existing ``dst`` is left intact.
    """
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        return
    if dst.exists() and not overwrite:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix in TEXT_SUFFIXES:
        dst.write_text(render_text(src.read_text(encoding="utf-8"), mapping), encoding="utf-8")
    else:
        shutil.copy2(str(src), str(dst))


def _is_excluded(rel: Path) -> bool:
    """True iff a kernel-relative path is a dev-only / rebuildable artifact that
    must not ship into a spawned oracle.

    A path is excluded if ANY of its components is an excluded directory name
    (e.g. ``tests``, ``__pycache__``, ``.pytest_cache``, a ``*.egg-info`` dir),
    or the leaf is an excluded basename / suffix (``*.pyc``, ``.DS_Store``).
    Matching on every component means we drop both the directory itself AND
    everything nested beneath it in a single pass.
    """
    for part in rel.parts:
        if part in _EXCLUDED_DIR_NAMES:
            return True
        if part.endswith(".egg-info"):
            return True
    leaf = rel.name
    if leaf in _EXCLUDED_BASENAMES:
        return True
    return rel.suffix in _EXCLUDED_SUFFIXES


def _iter_kernel(kernel: Path):
    """Yield every path under the kernel, skipping dev-only/rebuildable artifacts
    (the kernel's own ``tests/`` suite, caches, ``*.egg-info``, ``*.pyc`` ...).
    """
    for src in sorted(kernel.rglob("*")):
        if _is_excluded(src.relative_to(kernel)):
            continue
        yield src


def copy_kernel(kernel: Path, root: Path, mapping: dict[str, str], *, force: bool) -> None:
    """Materialize the kernel under ``root``.

    Fresh spawn: copy/render everything.
    ``--force``: refresh ONLY the tool layer (``_tools/``) and the kernel
    manifest; every sovereign tree (oracle.yml / Memory.nosync / Meta.nosync /
    Connectors) and every other existing file is left byte-for-byte intact.
    """
    for src in _iter_kernel(kernel):
        rel = render_relpath(src.relative_to(kernel), mapping)
        # The spawned root's .gitignore ships as gitignore.template so the
        # SKILL repo's git can track the kernel scaffold (a nested .gitignore
        # would hide the *.nosync template trees from the parent repo).
        if rel.as_posix() == "gitignore.template":
            rel = Path(".gitignore")
        dst = root / rel

        if not force:
            # Fresh spawn: never clobber a pre-existing file (defensive), but
            # create everything that is missing.
            copy_render(src, dst, mapping, overwrite=False)
            continue

        # --force: tool-layer-only merge.
        if any(_is_under(rel, tree) for tree in _SOVEREIGN_TREES):
            # Sovereign: only create if entirely absent; never overwrite.
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            elif not dst.exists():
                copy_render(src, dst, mapping, overwrite=False)
            continue

        is_tool = _is_under(rel, _TOOLS_DIR)
        is_manifest = rel.as_posix() == _MANIFEST
        is_cli_wrapper = rel.as_posix() == _CLI_WRAPPER
        # Refresh tools + manifest + root wrapper (overwrite); for any other
        # path, create-if-missing.
        copy_render(src, dst, mapping, overwrite=(is_tool or is_manifest or is_cli_wrapper))


def ensure_runtime_dirs(root: Path) -> None:
    """Create required empty runtime directories that may have no tracked files."""
    for rel in _RUNTIME_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)


def ensure_executables(root: Path) -> None:
    """Set executable bits on root-local command wrappers/scripts.

    Text files are rendered with ``write_text`` during spawn, so executable bits
    from the template cannot be relied on after materialization.
    """
    for rel in ("oracle", "load-env.sh"):
        path = root / rel
        if path.exists():
            path.chmod(path.stat().st_mode | 0o111)


# --------------------------------------------------------------------------- #
# kernel manifest + version stamp
# --------------------------------------------------------------------------- #
def _load_render_manifest():
    """Import the package-local manifest module (oracle_agent.manifest)."""
    from oracle_agent import manifest as render_kernel_manifest

    return render_kernel_manifest


def kernel_asset_dir() -> Path:
    """Path to the vendored oracle-kernel template shipped inside this package."""
    return Path(__file__).resolve().parent / "assets" / "oracle-kernel"


# Sentinel files whose absence means the vendored kernel template itself is
# truncated (e.g. an installer/packaging step stripped part of the tree). A
# spawn from an incomplete template is guaranteed to fail its own post-spawn
# audit on every machine, so fail BEFORE writing anything, with the fix.
_KERNEL_SENTINELS = (
    "oracle.yml",
    "tmp.nosync/_CONTEXT.md",
    "_tools/setup_audit.py",
    "_tools/oracle_lint.py",
)


def check_kernel_asset(kernel: Path) -> list[str]:
    """Return the kernel-relative sentinel paths missing from the template."""
    return [rel for rel in _KERNEL_SENTINELS if not (kernel / rel).is_file()]


def stamp_kernel_version(root: Path) -> dict:
    """Render ``.kernel-manifest.json`` for the spawned root and stamp
    ``kernel.tools_version`` + ``kernel.tools_sha256`` into its ``oracle.yml``.

    The manifest is rendered against the JUST-SPAWNED root (so the hashes reflect
    the tools actually on disk in this oracle). The aggregate sha256 is the single
    fingerprint stamped into ``oracle.yml``.
    """
    rkm = _load_render_manifest()
    manifest = rkm.render(root)  # writes <root>/.kernel-manifest.json
    version = str(manifest.get("tools_version", "3.0.0"))
    agg = str(manifest.get("aggregate_sha256", ""))
    _patch_oracle_yml_kernel(root / "oracle.yml", version, agg)
    return manifest


_KERNEL_HDR = re.compile(r"^kernel:\s*$")
_TOP_KEY = re.compile(r"^[A-Za-z0-9_]+:\s*")


def _patch_oracle_yml_kernel(yml_path: Path, version: str, sha: str) -> None:
    """Surgically rewrite the ``tools_version`` and ``tools_sha256`` lines inside
    the ``kernel:`` block of a block-style oracle.yml, preserving every other line.

    Stays strictly within the floor's YAML subset (``key: "value"``). The file is
    a CONSTANT internal path derived from ``root`` (not user-influenced), so a
    line-oriented ``write_text`` is correct here.
    """
    if not yml_path.exists():
        return
    lines = yml_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_kernel = False
    saw_version = saw_sha = False
    for line in lines:
        if _KERNEL_HDR.match(line):
            in_kernel = True
            out.append(line)
            continue
        if in_kernel:
            # A new top-level key (no leading whitespace) ends the kernel block.
            if line and not line[0].isspace() and _TOP_KEY.match(line):
                # Backfill any missing children before leaving the block.
                if not saw_version:
                    out.append(f'  tools_version: "{version}"')
                    saw_version = True
                if not saw_sha:
                    out.append(f'  tools_sha256: "{sha}"')
                    saw_sha = True
                in_kernel = False
                out.append(line)
                continue
            stripped = line.strip()
            if stripped.startswith("tools_version:"):
                out.append(f'  tools_version: "{version}"')
                saw_version = True
                continue
            if stripped.startswith("tools_sha256:"):
                out.append(f'  tools_sha256: "{sha}"')
                saw_sha = True
                continue
        out.append(line)
    # If the kernel block ran to EOF without a following top-level key, backfill.
    if in_kernel:
        if not saw_version:
            out.append(f'  tools_version: "{version}"')
        if not saw_sha:
            out.append(f'  tools_sha256: "{sha}"')
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    yml_path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# active loop instantiation
# --------------------------------------------------------------------------- #
def _yaml_q(s: str) -> str:
    """Double-quote a scalar for the block-style YAML subset, escaping quotes."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _yaml_q(str(value))


def _append_yaml_key(out: list[str], key: str, value, *, indent: int = 0) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        out.append(f"{pad}{key}:")
        for child_key, child_value in value.items():
            _append_yaml_key(out, str(child_key), child_value, indent=indent + 2)
        return
    if isinstance(value, list):
        out.append(f"{pad}{key}:")
        for item in value:
            if isinstance(item, (dict, list)):
                out.append(f"{' ' * (indent + 2)}-")
                _append_yaml_value(out, item, indent=indent + 4)
            else:
                out.append(f"{' ' * (indent + 2)}- {_yaml_scalar(item)}")
        return
    out.append(f"{pad}{key}: {_yaml_scalar(value)}")


def _append_yaml_value(out: list[str], value, *, indent: int) -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _append_yaml_key(out, str(child_key), child_value, indent=indent)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                out.append(f"{' ' * indent}-")
                _append_yaml_value(out, item, indent=indent + 2)
            else:
                out.append(f"{' ' * indent}- {_yaml_scalar(item)}")
    else:
        out.append(f"{' ' * indent}{_yaml_scalar(value)}")


def _render_loop_record(spec: dict, today: str, next_review: str) -> str:
    """Render one active loop record (frontmatter + body) in the strict subset.

    Frontmatter is block-style only: each sequence is one ``- item`` per line; no
    flow collections, anchors, tags, or multi-doc markers. Matches
    ``loop.schema.json`` (every required active-loop field present).
    """
    fm: list[str] = ["---"]
    fm.append(f"id: {spec['id']}")
    fm.append("type: loop")
    fm.append(f"title: {spec['title']}")
    fm.append(f'created: "{today}"')
    fm.append(f'updated: "{today}"')
    fm.append("sensitivity: internal")
    fm.append("status: active")
    fm.append("tags:")
    for t in spec["tags"]:
        fm.append(f"  - {t}")
    fm.append(f"cadence: {spec['cadence']}")
    fm.append(f"runner: {spec['runner']}")
    fm.append(f'last_run: "{today}"')
    fm.append(f'next_review: "{next_review}"')
    _append_yaml_key(fm, "model_policy", spec.get("model_policy", LOOP_MODEL_POLICY))
    fm.append("trigger_conditions:")
    for tc in spec["triggers"]:
        fm.append(f"  - {tc}")
    fm.append(f"health_signal: {spec['health']}")
    fm.append("---")

    body: list[str] = []
    body.append("")
    body.append(
        "> Concrete, lint-clean **active** loop record instantiated at spawn. An "
        "`active` loop MUST keep `runner`, `last_run`, and `next_review` populated "
        "(real ISO dates) or `oracle_lint` will fail. To retire it, flip `status`."
    )
    body.append("")
    body.append("## Purpose")
    body.append("")
    body.append(spec["purpose"])
    body.append("")
    body.append("## Cadence")
    body.append("")
    body.append(
        f"`{spec['cadence']}` — combined with `last_run` the due-ness engine "
        f"(`_tools/loops.py compute_due`) decides when this loop is due."
    )
    body.append("")
    body.append("## Trigger Conditions")
    body.append("")
    for tc in spec["triggers"]:
        body.append(f"- {tc}")
    body.append("")
    body.append("## Process")
    body.append("")
    for i, step in enumerate(spec["process"], start=1):
        body.append(f"{i}. {step}")
    body.append("")
    body.append("## Runner")
    body.append("")
    body.append(
        f"`{spec['runner']}` — `loops.run` dispatches this loop and `loops.record` "
        f"appends the `loop_runs` row, advancing `last_run` / `next_review`."
    )
    body.append("")
    body.append("## Model Policy")
    body.append("")
    body.append(
        "`model_policy` is machine-readable frontmatter. Loop work uses "
        "deterministic/no-LLM code first; if a model is required, use the "
        "cheapest fully capable model by default. Premium models or multi-agent "
        "passes require explicit admin approval or documented on-demand "
        "complexity, and any such use needs a recorded rationale."
    )
    body.append("")
    body.append("## Health Signal")
    body.append("")
    body.append(spec["health"] + ".")
    body.append("")
    return "\n".join(fm) + "\n" + "\n".join(body) + "\n"


def instantiate_active_loops(root: Path, today: str) -> list[Path]:
    """Write active loop records into ``Meta.nosync/Loops/``.

    Idempotent on --force: an existing loop record (which may carry an advanced
    ``last_run`` from real runs) is treated as sovereign tuning and left intact.
    Only missing active-loop records are created.
    """
    loops_dir = root / "Meta.nosync" / "Loops"
    loops_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    d0 = datetime.strptime(today, "%Y-%m-%d").date()
    for spec in ACTIVE_LOOPS:
        dst = loops_dir / f"loop-{spec['id']}.md"
        if dst.exists():
            continue  # sovereign: never clobber a record that may have run
        nr = (d0 + timedelta(days=int(spec["next_review_days"]))).isoformat()
        dst.write_text(_render_loop_record(spec, today, nr), encoding="utf-8")
        written.append(dst)
    return written


# --------------------------------------------------------------------------- #
# FTS index seed
# --------------------------------------------------------------------------- #
def seed_index(root: Path) -> bool:
    """Ensure the derived FTS index dir exists and best-effort initialize the DB.

    The index lives at ``_data.nosync/index/`` (derived, rebuildable). We always
    create the directory; we additionally try to instantiate ``knowledge_index``
    so the empty DB is ready on first query. If the index module is unavailable
    or sqlite is constrained, we degrade gracefully (dir-only) -- the index is
    rebuildable, so an empty start is always safe.
    """
    idx_dir = root / "_data.nosync" / "index"
    idx_dir.mkdir(parents=True, exist_ok=True)
    tools_dir = root / "_tools"
    tools_dir_str = str(tools_dir)
    inserted = False
    if tools_dir_str not in sys.path:
        sys.path.insert(0, tools_dir_str)
        inserted = True
    try:
        import knowledge_index  # type: ignore

        knowledge_index.KnowledgeIndex(root)  # creating it initializes the DB
        return True
    except Exception:
        return False
    finally:
        if inserted and tools_dir_str in sys.path:
            sys.path.remove(tools_dir_str)


# --------------------------------------------------------------------------- #
# post-spawn checks
# --------------------------------------------------------------------------- #
def _run_tool(root: Path, tool_rel: str, *extra: str) -> tuple[int, str]:
    """Run a kernel tool as a subprocess from the spawned root; return (rc, output).

    Running from ``root`` lets the tool's own bare ``import safe_paths`` resolve
    via its package-local conventions, and passes ``.`` as the root positional.
    """
    script = root / tool_rel
    if not script.exists():
        return (None, f"(skipped: {tool_rel} not present)")  # type: ignore[return-value]
    cmd = [sys.executable, str(script), ".", *extra]
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return (proc.returncode, out.strip())


def post_spawn_checks(root: Path) -> int:
    """Run setup_audit + oracle_lint; print verdicts; return worst rc (0 = ok).

    A non-zero verdict is REPORTED but does not delete the spawn -- a freshly
    spawned oracle may legitimately need admin customization before it lints
    fully clean (e.g. backup last_verified_restore pending). The caller decides.
    """
    worst = 0
    for tool_rel, label in (
        ("_tools/setup_audit.py", "setup_audit"),
        ("_tools/oracle_lint.py", "oracle_lint"),
    ):
        rc, out = _run_tool(root, tool_rel)
        if rc is None:
            print(f"  {label}: skipped ({out})")
            continue
        verdict = "PASS" if rc == 0 else f"ISSUES (rc={rc})"
        print(f"  {label}: {verdict}")
        if out:
            for line in out.splitlines():
                print(f"    {line}")
        worst = max(worst, rc)
    return worst


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="target oracle root to create")
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--codename", help="lowercase codename; defaults from company name")
    parser.add_argument("--admin-name", required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="tool-layer-only refresh; NEVER overwrites oracle.yml/Memory.nosync/Meta.nosync",
    )
    parser.add_argument(
        "--no-postcheck",
        action="store_true",
        help="skip the post-spawn setup_audit + oracle_lint run",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="do not fail the spawn on manifest-stamp or post-check failures "
        "(deliberate spawn-then-customize pipelines only)",
    )
    args = parser.parse_args(argv)

    kernel = kernel_asset_dir()
    if not kernel.exists():
        print(f"missing kernel: {kernel}", file=sys.stderr)
        return 2
    missing = check_kernel_asset(kernel)
    if missing:
        print(
            "FATAL: the installed kernel template is incomplete -- missing "
            f"{', '.join(missing)} under {kernel}.\n"
            "An oracle spawned from it would fail its own post-spawn audit. "
            "This is an install/packaging defect, not a setup mistake: "
            "re-run installer/install.sh from a clean checkout.",
            file=sys.stderr,
        )
        return 2

    # Placeholder values are substituted into YAML frontmatter, Python
    # docstrings, and markdown. Reject characters that can corrupt those
    # contexts rather than rendering a subtly broken oracle.
    for label, value in (("--company-name", args.company_name), ("--admin-name", args.admin_name)):
        bad = set(value) & {"{", "}", "\n", "\r", "\t", '"'}
        if bad:
            print(
                f"{label} contains unsupported character(s) {sorted(bad)!r}; "
                "use plain text (quotes/braces/newlines break rendered files)",
                file=sys.stderr,
            )
            return 2
        if not value.strip():
            print(f"{label} must not be blank", file=sys.stderr)
            return 2

    root = Path(args.root).expanduser().resolve()
    codename = slugify(args.codename or args.company_name)
    today = date.today().isoformat()
    mapping = {
        "COMPANY_NAME": args.company_name,
        "CODENAME": codename,
        "ADMIN_NAME": args.admin_name,
        "DATE": today,
    }

    if root.exists() and any(root.iterdir()) and not args.force:
        print(f"target exists and is not empty: {root}", file=sys.stderr)
        print("rerun with --force for a tool-layer-only refresh (data is preserved)", file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)

    # 1. Materialize the kernel (fresh = full; --force = tool-layer only).
    copy_kernel(kernel, root, mapping, force=args.force)
    ensure_runtime_dirs(root)

    # 2. Make root-local command wrappers/scripts executable.
    ensure_executables(root)

    # 3. Stamp kernel.tools_version + tools_sha256 from a fresh manifest.
    #    (Both fresh and --force refresh the tool layer, so re-stamp either way.)
    try:
        manifest = stamp_kernel_version(root)
        print(f"stamped kernel: {manifest.get('tools_version')} "
              f"({len(manifest.get('files', {}))} tools, "
              f"sha {str(manifest.get('aggregate_sha256',''))[:12]})")
    except Exception as exc:
        # An unstamped kernel breaks upgrade integrity checks later. Fail the
        # spawn unless the admin explicitly accepts a degraded result.
        if args.allow_degraded:
            print(f"WARNING: kernel manifest/version stamp failed: {exc}", file=sys.stderr)
        else:
            print(
                f"FATAL: kernel manifest/version stamp failed: {exc}\n"
                "(re-run with --allow-degraded to accept an unstamped spawn)",
                file=sys.stderr,
            )
            return 1

    # 4. Instantiate active loop records (idempotent; never clobbers a
    #    record that may have advanced its last_run via real runs).
    written = instantiate_active_loops(root, today)
    if written:
        print(f"instantiated {len(written)} active loop record(s)")
    else:
        print("active loop records already present (preserved)")

    # 5. Seed the empty FTS index (derived, rebuildable).
    if seed_index(root):
        print("seeded empty knowledge index")
    else:
        print("seeded knowledge index directory (DB init deferred)")

    print(f"spawned oracle: {root}" + ("  [--force tool-layer refresh]" if args.force else ""))

    # 6. Post-spawn audit + lint.
    if args.no_postcheck:
        print("post-spawn checks skipped (--no-postcheck)")
        return 0
    print("post-spawn checks:")
    worst = post_spawn_checks(root)
    if worst == 0:
        print("oracle is bootstrapped and clean.")
        return 0
    # A fresh spawn that fails its own audit/lint is a broken product, not a
    # customization opportunity. Fail loudly; --allow-degraded opts out for
    # deliberate spawn-then-customize pipelines.
    if args.allow_degraded:
        print("oracle spawned DEGRADED; address the issues above (--allow-degraded).")
        return 0
    print(
        "FATAL: post-spawn checks failed on a fresh spawn. The oracle was "
        "written but is not clean; fix the kernel or re-run with "
        "--allow-degraded if this is a deliberate partial setup.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
