#!/usr/bin/env python3
"""setup_audit.py -- DEEP bootstrap audit for a spawned oracle root.

This audit proves the oracle is actually wired, not merely scaffolded. It is
read-only: it never writes to the oracle, so it is safe to run at any time
(including post-spawn and inside upgrade.py).

Checks (each turns the audit RED on failure):

  1. Required doctrine files + tree directories + per-dir _CONTEXT.md exist.
  2. oracle.yml PARSES via the safe-subset loader and is a mapping (not garbage),
     and carries the load-bearing top-level keys.
  3. kernel.tools_version is a non-empty, non-placeholder stamp and
     kernel.tools_sha256 is present (string key exists; may be empty pre-manifest
     but the KEY must exist so upgrade.py has somewhere to write).
  4. The active loops exist as REAL records (frontmatter files) under
     Meta.nosync/Loops/, each with status: active, a runner, and a populated
     last_run (an active loop with last_run null/missing is "inert" and fails).
  5. Meta.nosync/ledgers/ exists (the tracked, classified registry home).
  6. backup last_verified_restore in BACKUP-RECOVERY.md is populated OR is
     explicitly marked pending/deferred-by-admin (never silently blank).
  7. .gitignore TRACKS the ledgers (a negation rule for Meta.nosync/ledgers/),
     so the institutional store is backed up and recoverable.
  8. Every SIGNIFICANT ingested _INPUT row (recorded in the input registry) has
     a corresponding Sources/ record -- ingested material that produced no
     source note is an accountability gap.

Exit 0 == PASS (audit green); exit 1 == FAIL with an itemized problem list.

Stdlib only. Imports the floor's oracle_yaml + ledger as bare siblings (tests
inject _tools on sys.path) with a package fallback.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

__all__ = ["audit", "main"]


# --------------------------------------------------------------------------- #
# sibling-import shim
# --------------------------------------------------------------------------- #
def _import_yaml():
    try:
        import oracle_yaml  # type: ignore
        return oracle_yaml
    except Exception:  # pragma: no cover - package fallback
        from . import oracle_yaml  # type: ignore
        return oracle_yaml


def _import_ledger():
    try:
        import ledger  # type: ignore
        return ledger
    except Exception:  # pragma: no cover - package fallback
        from . import ledger  # type: ignore
        return ledger


# --------------------------------------------------------------------------- #
# expected scaffold
# --------------------------------------------------------------------------- #
REQUIRED_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "oracle.yml",
    "ORACLE-ARCHITECTURE.md",
    "DOCTRINE.md",
    "TRUTH-MAP.md",
    "BACKUP-RECOVERY.md",
    "BOOTSTRAP-STATUS.md",
    "PLAYBOOKS/answer.md",
    "PLAYBOOKS/ingest.md",
    "PLAYBOOKS/review.md",
    "PLAYBOOKS/brief.md",
    "PLAYBOOKS/session.md",
    "PLAYBOOKS/loops.md",
    "PLAYBOOKS/admin-setup.md",
    ".gitignore",
    ".env.example",
    "oracle",
    "load-env.sh",
]

REQUIRED_DIRS = [
    "Memory.nosync",
    "Meta.nosync",
    "Meta.nosync/ledgers",
    "Meta.nosync/Loops",
    "Meta.nosync/Sessions",
    "Connectors",
    "Workproduct.nosync/_INPUT",
    "Workproduct.nosync/_OUTPUT",
    "Analysis.nosync",
    "_data.nosync",
    "dashboards.nosync",
    "tmp.nosync",
    "_tools",
    "AgentResources.nosync",
    "AgentResources.nosync/Skills",
]

# Dirs we DON'T demand a _CONTEXT.md from (special-purpose).
_NO_CONTEXT_REQUIRED = {
    "Workproduct.nosync/_INPUT",
    "Workproduct.nosync/_OUTPUT",
    "Meta.nosync/ledgers",
    "Meta.nosync/Loops",
    "_tools",
}

# The loops that MUST be instantiated as runnable records at spawn.
ACTIVE_LOOP_IDS = [
    "memory-matriculation",
    "source-capture",
    "workproduct-io",
    "user-feedback-learning",
    "skill-repository-learning",
    "insight-synthesis",
    "leadership-briefing",
    "value-scorecard",
    "improvement-lifecycle",
    "meta-health",
    "stale-finding-refresh",
    "architecture-retrospective",
]

# Top-level oracle.yml keys the rest of the kernel depends on.
REQUIRED_YML_KEYS = [
    "company",
    "oracle",
    "security",
    "governance",
    "session_interfaces",
    "ontology",
    "workproduct",
    "connectors",
    "loops",
    "backup",
    "kernel",
]

_PLACEHOLDER_VERSIONS = {"", "tbd", "todo", "changeme", "x.y.z", "0", "none"}
_PENDING_TOKENS = ("pending", "deferred", "not yet", "not-yet", "admin", "tbd", "n/a")


# --------------------------------------------------------------------------- #
# frontmatter helpers
# --------------------------------------------------------------------------- #
_FM_FENCE = re.compile(r"^---\s*$", re.MULTILINE)


def _read_frontmatter(path: Path) -> Optional[dict]:
    """Return the parsed YAML frontmatter mapping of a note, or None.

    Frontmatter is the block between the first pair of ``---`` fences. Its body
    must be block-style YAML (the kernel subset). Returns None when there is no
    frontmatter or it does not parse to a mapping.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.lstrip().startswith("---"):
        return None
    # Find the two fence lines.
    lines = text.splitlines()
    # locate first '---'
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "---":
            start = i
            break
    if start is None:
        return None
    end = None
    for j in range(start + 1, len(lines)):
        if lines[j].strip() == "---":
            end = j
            break
    if end is None:
        return None
    body = "\n".join(lines[start + 1:end])
    yaml_mod = _import_yaml()
    try:
        data = yaml_mod.safe_load(body)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _is_populated(value) -> bool:
    """A timestamp/field counts as populated only if it is a real, non-null,
    non-placeholder value."""
    if value is None:
        return False
    s = str(value).strip().lower()
    if not s or s in ("null", "none", "~", "tbd", "todo", "n/a", "na"):
        return False
    return True


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #
def _check_scaffold(root: Path, problems: list) -> None:
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            problems.append(f"missing file: {rel}")
    wrapper = root / "oracle"
    if wrapper.is_file() and not (wrapper.stat().st_mode & 0o111):
        problems.append("oracle wrapper is not executable")
    for rel in REQUIRED_DIRS:
        if not (root / rel).is_dir():
            problems.append(f"missing dir: {rel}")
    for rel in REQUIRED_DIRS:
        if rel in _NO_CONTEXT_REQUIRED:
            continue
        ctx = root / rel / "_CONTEXT.md"
        if (root / rel).is_dir() and not ctx.exists():
            problems.append(f"missing context: {rel}/_CONTEXT.md")


def _check_oracle_yml(root: Path, problems: list) -> Optional[dict]:
    cfg = root / "oracle.yml"
    if not cfg.is_file():
        problems.append("oracle.yml missing (cannot validate config)")
        return None
    yaml_mod = _import_yaml()
    try:
        data = yaml_mod.safe_load(cfg.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f"oracle.yml does not parse: {exc}")
        return None
    if not isinstance(data, dict):
        problems.append("oracle.yml did not parse to a mapping (corrupt config)")
        return None
    for key in REQUIRED_YML_KEYS:
        if key not in data:
            problems.append(f"oracle.yml missing top-level key: {key}")
    return data


def _check_kernel_stamp(data: Optional[dict], problems: list) -> None:
    if data is None:
        return
    kernel = data.get("kernel")
    if not isinstance(kernel, dict):
        problems.append("oracle.yml: kernel section missing or not a mapping")
        return
    version = kernel.get("tools_version")
    if version is None or str(version).strip().lower() in _PLACEHOLDER_VERSIONS:
        problems.append(
            f"oracle.yml: kernel.tools_version is unset/placeholder ({version!r})"
        )
    # tools_sha256 KEY must exist (value may be filled by render_kernel_manifest;
    # an empty string is acceptable pre-manifest, but the key must be present so
    # upgrade.py has a slot to write to).
    if "tools_sha256" not in kernel:
        problems.append("oracle.yml: kernel.tools_sha256 key missing")


def _check_active_loops(root: Path, problems: list) -> None:
    loops_dir = root / "Meta.nosync" / "Loops"
    if not loops_dir.is_dir():
        problems.append("Meta.nosync/Loops directory missing (no loop records)")
        return
    # Build id -> frontmatter map of every loop record present.
    records: dict[str, dict] = {}
    for md in sorted(loops_dir.glob("*.md")):
        if md.name.startswith("_") or md.name == "loop-template.md":
            continue
        fm = _read_frontmatter(md)
        if not fm:
            continue
        loop_id = str(fm.get("id", "")).strip()
        if loop_id:
            records[loop_id] = fm

    for loop_id in ACTIVE_LOOP_IDS:
        fm = records.get(loop_id)
        if fm is None:
            problems.append(f"active loop record missing: {loop_id}")
            continue
        status = str(fm.get("status", "")).strip().lower()
        # 'paused' is a legitimate runtime state (meta-health pauses a
        # repeatedly failing loop, visibly, via the Review Inbox) -- the record
        # is still real and runnable once reactivated.
        if status not in ("active", "paused"):
            problems.append(
                f"loop {loop_id}: expected status 'active' (or 'paused'), got {status!r}"
            )
        if not _is_populated(fm.get("runner")):
            problems.append(f"loop {loop_id}: active loop has no runner")
        if not _is_populated(fm.get("last_run")):
            problems.append(
                f"loop {loop_id}: active loop has no last_run (inert, not runnable)"
            )


def _check_backup_verified(root: Path, problems: list) -> None:
    backup_doc = root / "BACKUP-RECOVERY.md"
    if not backup_doc.is_file():
        # Already reported by scaffold check; nothing more to verify.
        return
    text = backup_doc.read_text(encoding="utf-8", errors="replace")
    # Look for a 'last_verified_restore' field/line.
    m = re.search(
        r"last_verified_restore\s*[:=]\s*(.*)", text, re.IGNORECASE
    )
    if not m:
        problems.append(
            "BACKUP-RECOVERY.md: no last_verified_restore field found"
        )
        return
    value = m.group(1).strip().strip("`").strip()
    if _is_populated(value):
        return
    # Blank/null is only acceptable if the doc explicitly marks it pending.
    lowered = text.lower()
    if any(tok in lowered for tok in _PENDING_TOKENS):
        return
    problems.append(
        "BACKUP-RECOVERY.md: last_verified_restore is blank and not marked pending"
    )


def _check_gitignore_tracks_ledgers(root: Path, problems: list) -> None:
    gi = root / ".gitignore"
    if not gi.is_file():
        return  # scaffold check reports the missing file
    text = gi.read_text(encoding="utf-8", errors="replace")
    if "*.nosync" not in text:
        problems.append(".gitignore does not ignore *.nosync")
    # Ledgers must be TRACKED: there must be a negation un-ignoring the ledgers
    # path (e.g. '!Meta.nosync/ledgers/' or '!Meta.nosync/' + deeper rules).
    tracks = any(
        line.strip().startswith("!") and "ledgers" in line
        for line in text.splitlines()
    )
    if not tracks:
        problems.append(
            ".gitignore does not track Meta.nosync/ledgers/ "
            "(ledgers must be un-ignored so the registry is backed up)"
        )


def _input_registry_path(root: Path) -> Path:
    return root / "Workproduct.nosync" / "_INPUT" / ".registry.jsonl"


def _check_ingested_have_sources(root: Path, problems: list) -> None:
    """Every significant ingested input row must have a Sources/ record.

    An input row is "significant" when it carries a truthy ``significant`` flag,
    or when it lacks the flag entirely (default-significant: silence is not a
    waiver). A row may declare it produced a source via ``source_id`` /
    ``source_record``; we accept that pointer if the named Sources note exists,
    OR we accept any Sources note whose frontmatter references the row's
    ``drop_id`` / ``sha256``.
    """
    reg = _input_registry_path(root)
    if not reg.exists():
        return  # nothing ingested yet -> nothing to reconcile
    ledger = _import_ledger()
    rows, _warn = ledger.load(reg)
    if not rows:
        return

    sources_dir = root / "Memory.nosync" / "Sources"
    # Index Sources/ frontmatter once.
    source_notes: list[dict] = []
    source_names: set[str] = set()
    if sources_dir.is_dir():
        for md in sources_dir.glob("*.md"):
            if md.name.startswith("_"):
                continue
            source_names.add(md.stem)
            fm = _read_frontmatter(md)
            if fm:
                source_notes.append(fm)

    def _has_source(row: dict) -> bool:
        # explicit pointer to a named source note
        for key in ("source_id", "source_record", "source"):
            ref = row.get(key)
            if _is_populated(ref):
                ref_s = str(ref).strip()
                if ref_s in source_names:
                    return True
                # match by id field inside a source note's frontmatter
                for fm in source_notes:
                    if str(fm.get("id", "")).strip() == ref_s:
                        return True
        # back-reference: a source note that points at this input row
        row_id = str(row.get("drop_id", "")).strip()
        row_hash = str(row.get("sha256", "")).strip()
        for fm in source_notes:
            for k in ("input_drop_id", "from_input", "ingest_drop_id", "drop_id"):
                if row_id and str(fm.get(k, "")).strip() == row_id:
                    return True
            for k in ("sha256", "content_sha256", "source_sha256"):
                if row_hash and str(fm.get(k, "")).strip() == row_hash:
                    return True
        return False

    for row in rows:
        if not isinstance(row, dict):
            continue
        sig = row.get("significant")
        is_significant = True if sig is None else bool(sig)
        if not is_significant:
            continue
        if not _has_source(row):
            ident = row.get("drop_id") or row.get("slug") or row.get("file") or "?"
            problems.append(
                f"ingested input {ident!r} has no corresponding Sources/ record"
            )


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def audit(root: Path) -> list:
    """Run the full deep audit and return a (possibly empty) list of problems."""
    root = Path(root)
    problems: list = []
    _check_scaffold(root, problems)
    data = _check_oracle_yml(root, problems)
    _check_kernel_stamp(data, problems)
    _check_active_loops(root, problems)
    _check_backup_verified(root, problems)
    _check_gitignore_tracks_ledgers(root, problems)
    _check_ingested_have_sources(root, problems)
    return problems


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Deep setup audit for an oracle root")
    parser.add_argument("root", nargs="?", default=".", help="oracle root path")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    problems = audit(root)

    if args.json:
        import json as _json
        print(_json.dumps(
            {"root": str(root), "ok": not problems, "problems": problems},
            indent=2, ensure_ascii=False,
        ))
        return 0 if not problems else 1

    if problems:
        print("SETUP AUDIT: FAIL")
        for p in problems:
            print(f"- {p}")
        return 1
    print("SETUP AUDIT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
