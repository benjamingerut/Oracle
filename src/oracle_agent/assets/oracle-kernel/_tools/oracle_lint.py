#!/usr/bin/env python3
"""oracle_lint.py -- the schema-validating oracle linter (FLOOR enforcement).

The linter uses semantic validation built on the floor primitives:

  * ``oracle_yaml.safe_load``   -- parse oracle.yml / *.manifest.yaml / note
                                   frontmatter through the strict safe-subset
                                   loader (never mis-parses, raises on the
                                   constructs the rest of the system can't read).
  * ``schema_check.validate``   -- validate parsed objects against the JSON
                                   schemas shipped under ``_tools/schemas/``.
  * ``secret_scan.scan_tree``   -- find leaked credentials anywhere under root.
  * ``ledger.load``             -- read the immutability ledger to compare a
                                   record's on-disk content hash to its
                                   registered hash.

Checks (ALL violations are collected; the linter never short-circuits):

  1. oracle.yml parses and matches ``oracle_yml.schema.json``.
  2. ``Connectors/**/*.manifest.yaml`` match ``connector.schema.json``.
  3. Memory/Meta notes have valid frontmatter (``note_frontmatter.schema.json``)
     incl. the sensitivity enum and subtype-in-ontology.
  4. AgentResources managed skills have valid ``SKILL.md`` packages.
  5. Findings / Contradictions / Models / Recommendations meet their schemas
     (claim_tier enum, confidence 0..1, NON-EMPTY evidence + disconfirmer, ...).
  6. Registry integrity: ``_INPUT``/``_OUTPUT`` registry lines parse, drop_ids
     are unique, and ``REGISTRY.md`` matches a freshly rendered table.
  7. Content-hash immutability for source/finding/decision/directive records:
     on-disk content hash MUST equal the hash registered in the ledger.
  8. Secrets via ``secret_scan`` over the whole tree.
  9. External-path scan over ALL root files INCLUDING oracle.yml (an absolute
     ``/Users/...`` / ``/Volumes/...`` / ``C:\\`` path in config or doctrine is
     a sovereignty leak and FAILS).
 10. Doctrine->Enforcer: any ``denied/required/must/forbidden/never`` guarantee
     in DOCTRINE.md / BACKUP-RECOVERY.md that does NOT name a
     registered enforcer (a ``_tools/<module>.py`` reference, a backticked
     ``./oracle <group> ...`` command, or ``safe_paths``/``policy``/...) AND is not
     explicitly stamped ``advisory`` => FAIL. This is the single discipline that
     keeps doctrine binding.
 11. Loop records: any ``status: active`` loop note lacking ``runner`` or
     ``last_run`` => FAIL.

A machine-checked ``known-failures.txt`` baseline downgrades listed violations to
warnings (so a known, tracked gap doesn't block the gate while it is being
worked) without ever silently hiding a NEW regression.

Stdlib only. Imports floor siblings (oracle_yaml, schema_check, secret_scan,
ledger) as bare modules -- conftest puts ``_tools`` on sys.path; at runtime the
tools sit in the same directory.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------- #
# floor-sibling imports (bare first, package fallback)
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - exercised both ways across run contexts
    import oracle_yaml
    import schema_check
    import secret_scan
    import ledger
    import source_record
    import truth_map
except Exception:  # pragma: no cover - package-style fallback
    from . import oracle_yaml  # type: ignore
    from . import schema_check  # type: ignore
    from . import secret_scan  # type: ignore
    from . import ledger  # type: ignore
    from . import source_record  # type: ignore
    from . import truth_map  # type: ignore


# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
NOTE_ROOTS = ("Memory.nosync", "Meta.nosync")
SKILLS_ROOT = "AgentResources.nosync/Skills"

# Files whose absolute-path "smell" we never tolerate (sovereignty leak).
# We scan ALL files under root, but these get an explicit check even when the
# tree-wide pass would otherwise skip a config file.
#
# The Windows-drive branch is deliberately precise. A *real* drive path is a
# drive letter at a token boundary (not the tail of an ordinary word), followed
# by ``:\`` and a path segment -- e.g. ``"C:\Users\..."``. The negative
# lookbehind plus the trailing path-char class stop the old branch
# (``[A-Za-z]:\\?``) from mis-firing on ordinary Python string literals that end
# in a ``:\n`` escape (``"Available groups:\n"``, ``"invalid record:\n"``), where
# the matched fragment was really ``<word-letter>:\<escape>`` and not a path.
EXTERNAL_PATH_RE = re.compile(
    r"(/Users/|/home/[^/\s]+/|/Volumes/|(?<![A-Za-z0-9])[A-Za-z]:\\[\\A-Za-z0-9_.])"
)

# Allowlisted appearances of an absolute-ish path that are NOT leaks:
# placeholders, examples, and the documented kill-switch / config pointers.
_PATH_ALLOW_RE = re.compile(
    r"(\{\{[A-Z_]+\}\}|/path/to/|/abs/|example|<[^>]+>|\$\{?[A-Z_]+\}?)",
    re.IGNORECASE,
)

# Immutable record types: content hash on disk must equal ledger-registered hash.
IMMUTABLE_TYPES = {"source", "finding", "decision", "directive"}

# Doctrine guarantee verbs. A line carrying one of these in a guarantee file is a
# *claim of enforcement* and must name its enforcer or be labelled advisory.
_GUARANTEE_VERB_RE = re.compile(
    r"\b(denied|denies|deny|required|requires|must|forbidden|forbids|"
    r"never|prohibited|enforced|enforces|blocked|blocks|refused|refuses|rejected)\b",
    re.IGNORECASE,
)

# Something that names an enforcer: a tool module reference, a backticked
# ``./oracle <group>`` command, or one of the known enforcer module/CLI nouns.
_ENFORCER_RE = re.compile(
    r"(`(?:\./)?oracle\s+[a-z]+|_tools/[a-z_]+\.py|\b("
    r"safe_paths|policy|secret_scan|oracle_lint|ledger|actions|answer_protocol|"
    r"artifact_io|loops|skills|backup|upgrade|setup_audit|harness)\b)",
    re.IGNORECASE,
)

# Explicit advisory stamp: a guarantee the agent obeys but code does not enforce.
_ADVISORY_RE = re.compile(r"\badvisory\b", re.IGNORECASE)

GUARANTEE_FILES = ("DOCTRINE.md", "BACKUP-RECOVERY.md")


# --------------------------------------------------------------------------- #
# violation model
# --------------------------------------------------------------------------- #
class Violation:
    """One lint problem. ``key`` is the stable identity used to baseline it.

    The key is intentionally line-independent in spirit but includes the file +
    a short code + a normalized detail so the SAME violation matches across runs
    while a NEW one in a different place does not get masked by the baseline.
    """

    __slots__ = ("code", "path", "line", "message")

    def __init__(self, code: str, path: str, line: int | None, message: str):
        self.code = code
        self.path = path
        self.line = line
        self.message = message

    @property
    def key(self) -> str:
        loc = f":{self.line}" if self.line else ""
        return f"{self.code}::{self.path}{loc}"

    def render(self) -> str:
        loc = f":{self.line}" if self.line else ""
        return f"{self.path}{loc}: [{self.code}] {self.message}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "key": self.key,
        }


# --------------------------------------------------------------------------- #
# frontmatter
# --------------------------------------------------------------------------- #
def extract_frontmatter(text: str) -> tuple[str | None, int]:
    """Return (frontmatter_body, body_start_lineno) or (None, 0).

    A note's frontmatter is the block between the FIRST line ``---`` and the next
    line that is exactly ``---``. The body between the fences must be block-style
    YAML (parsed by oracle_yaml). The fence lines themselves are stripped.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body = "\n".join(lines[1:i])
            # 1-based line number of the first frontmatter content line.
            return body, 2
    return None, 0


def parse_frontmatter(text: str) -> tuple[dict | None, str | None]:
    """Parse a note's frontmatter into a dict.

    Returns ``(data, error)``. ``data`` is None when there is no frontmatter or
    when the body is not a mapping; ``error`` carries the reason (an
    oracle_yaml.UnsupportedYAML message or a structural complaint).
    """
    body, _ = extract_frontmatter(text)
    if body is None:
        return None, "no frontmatter block (expected leading '---' fence)"
    try:
        data = oracle_yaml.safe_load(body)
    except Exception as exc:  # UnsupportedYAML or parse failure
        return None, f"frontmatter not parseable as block YAML: {exc}"
    if data is None:
        return None, "empty frontmatter block"
    if not isinstance(data, dict):
        return None, "frontmatter is not a mapping"
    return data, None


# --------------------------------------------------------------------------- #
# schema loading
# --------------------------------------------------------------------------- #
def _default_schemas_dir() -> Path:
    return Path(__file__).resolve().parent / "schemas"


def load_schema(schemas_dir: Path, name: str) -> dict | None:
    """Load a JSON schema by file name; return None (not crash) if absent."""
    p = schemas_dir / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _iter_notes(root: Path) -> Iterable[Path]:
    """Yield every Memory/Meta markdown note that is a real record.

    Skips ``_CONTEXT.md``, ``_template.md``/``loop-template.md``, anything
    under ``__pycache__``/``.git``, and ``Meta.nosync/tool-backups/`` -- the
    timestamped ``_tools`` rollback copies ``upgrade.py`` writes are frozen
    kernel machinery, not notes; any ``.md`` inside one (e.g. a module README)
    would otherwise raise a fresh, un-baselinable ``note-frontmatter``
    violation on every upgrade.
    """
    for note_root in NOTE_ROOTS:
        base = root / note_root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            if any(part in (".git", "__pycache__", "tool-backups") for part in p.parts):
                continue
            name = p.name
            if name == "_CONTEXT.md":
                continue
            if name.endswith("_template.md") or name == "loop-template.md" or name.startswith("_template"):
                continue
            yield p


def _note_type(data: dict) -> str:
    return str(data.get("type", "")).strip().lower()


def _nonempty(value: Any) -> bool:
    """A field counts as 'present and non-empty' if it is a non-blank scalar or
    a non-empty list/dict. ``None`` / ``''`` / ``[]`` / ``{}`` are empty."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def content_sha256(text: str) -> str:
    """Stable content hash of a note's BODY (frontmatter excluded).

    Immutability is about the asserted content, so we hash the body below the
    frontmatter fence. If there is no frontmatter we hash the whole text.
    """
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body = "\n".join(lines[i + 1 :])
                return hashlib.sha256(body.encode("utf-8")).hexdigest()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# individual checks -- each appends Violations, never raises for content
# --------------------------------------------------------------------------- #
def check_oracle_yml(root: Path, schemas_dir: Path, out: list[Violation]) -> dict | None:
    """Parse + schema-validate oracle.yml. Returns the parsed dict (or None)."""
    p = root / "oracle.yml"
    rel = "oracle.yml"
    if not p.exists():
        out.append(Violation("oracle-yml-missing", rel, None, "oracle.yml not found at root"))
        return None
    text = _read(p)
    if text is None:
        out.append(Violation("oracle-yml-unreadable", rel, None, "oracle.yml could not be read"))
        return None
    try:
        data = oracle_yaml.safe_load(text)
    except Exception as exc:
        out.append(Violation("oracle-yml-parse", rel, None, f"oracle.yml not parseable: {exc}"))
        return None
    if not isinstance(data, dict):
        out.append(Violation("oracle-yml-shape", rel, None, "oracle.yml top-level is not a mapping"))
        return None
    schema = load_schema(schemas_dir, "oracle_yml.schema.json")
    if schema is not None:
        for err in schema_check.validate(data, schema):
            out.append(Violation("oracle-yml-schema", rel, None, err))
    return data


def check_connectors(root: Path, schemas_dir: Path, out: list[Violation]) -> None:
    base = root / "Connectors"
    if not base.is_dir():
        return
    schema = load_schema(schemas_dir, "connector.schema.json")
    for p in sorted(base.rglob("*.manifest.yaml")):
        rel = _rel(root, p)
        text = _read(p)
        if text is None:
            out.append(Violation("connector-unreadable", rel, None, "manifest could not be read"))
            continue
        try:
            data = oracle_yaml.safe_load(text)
        except Exception as exc:
            out.append(Violation("connector-parse", rel, None, f"manifest not parseable: {exc}"))
            continue
        # Template manifests legitimately carry placeholders; still validate
        # shape but tolerate a placeholder-only file by checking it is a mapping.
        if not isinstance(data, dict):
            out.append(Violation("connector-shape", rel, None, "manifest top-level is not a mapping"))
            continue
        if schema is not None:
            for err in schema_check.validate(data, schema):
                out.append(Violation("connector-schema", rel, None, err))


def check_notes(
    root: Path,
    schemas_dir: Path,
    oracle_cfg: dict | None,
    out: list[Violation],
) -> None:
    """Validate every Memory/Meta note's frontmatter + type-specific schema."""
    note_schema = load_schema(schemas_dir, "note_frontmatter.schema.json")
    type_schemas = {
        "finding": load_schema(schemas_dir, "finding.schema.json"),
        "contradiction": load_schema(schemas_dir, "contradiction.schema.json"),
        "model": load_schema(schemas_dir, "model.schema.json"),
        "recommendation": load_schema(schemas_dir, "recommendation.schema.json"),
        "loop": load_schema(schemas_dir, "loop.schema.json"),
    }
    subtypes = _ontology_subtypes(oracle_cfg)

    for p in _iter_notes(root):
        rel = _rel(root, p)
        text = _read(p)
        if text is None:
            out.append(Violation("note-unreadable", rel, None, "note could not be read"))
            continue
        data, err = parse_frontmatter(text)
        if data is None:
            out.append(Violation("note-frontmatter", rel, 1, err or "invalid frontmatter"))
            continue

        # Common frontmatter (incl. sensitivity enum) via note_frontmatter schema.
        if note_schema is not None:
            for e in schema_check.validate(data, note_schema):
                out.append(Violation("note-schema", rel, 1, e))
        else:
            # Minimal hard floor when the schema file is absent: sensitivity must
            # be present and a known label.
            sens = str(data.get("sensitivity", "")).strip()
            if sens == "":
                out.append(Violation("note-sensitivity", rel, 1, "missing required 'sensitivity'"))
            elif sens not in ("public", "internal", "confidential", "restricted", "secret"):
                out.append(
                    Violation("note-sensitivity", rel, 1, f"sensitivity {sens!r} not a valid label")
                )

        # subtype must be in the ontology enum when both are present.
        subtype = data.get("subtype")
        if _nonempty(subtype) and subtypes is not None and str(subtype) not in subtypes:
            out.append(
                Violation(
                    "note-subtype",
                    rel,
                    1,
                    f"subtype {subtype!r} not in ontology.entity_subtypes {subtypes!r}",
                )
            )

        ntype = _note_type(data)

        # Type-specific schema.
        tschema = type_schemas.get(ntype)
        if tschema is not None:
            for e in schema_check.validate(data, tschema):
                out.append(Violation(f"{ntype}-schema", rel, 1, e))

        # Hard non-empty-evidence/disconfirmer floor for findings, independent of
        # whether the schema file is present (this is the load-bearing accuracy
        # guarantee the negative test relies on).
        if ntype == "finding":
            _check_finding_floor(rel, data, out)
        elif ntype == "contradiction":
            _check_contradiction_floor(rel, data, out)
        elif ntype == "loop":
            _check_loop_floor(rel, data, out)
        elif ntype == "improvement":
            _check_improvement_floor(rel, data, out)


def _check_finding_floor(rel: str, data: dict, out: list[Violation]) -> None:
    tier = data.get("claim_tier")
    valid_tiers = {"OBS", "INF", "SPEC", "SPEC-horizon"}
    if not _nonempty(tier):
        out.append(Violation("finding-claim-tier", rel, 1, "finding missing claim_tier"))
    elif str(tier) not in valid_tiers:
        out.append(
            Violation("finding-claim-tier", rel, 1, f"claim_tier {tier!r} not in {sorted(valid_tiers)}")
        )
    conf = data.get("confidence")
    if conf is None:
        out.append(Violation("finding-confidence", rel, 1, "finding missing confidence"))
    elif not isinstance(conf, (int, float)) or isinstance(conf, bool):
        out.append(Violation("finding-confidence", rel, 1, f"confidence {conf!r} not numeric"))
    elif not (0.0 <= float(conf) <= 1.0):
        out.append(Violation("finding-confidence", rel, 1, f"confidence {conf} not in 0..1"))
    if not _nonempty(data.get("evidence")):
        out.append(Violation("finding-evidence", rel, 1, "finding has empty/missing evidence"))
    if not _nonempty(data.get("disconfirmer")):
        out.append(Violation("finding-disconfirmer", rel, 1, "finding has empty/missing disconfirmer"))


def _check_contradiction_floor(rel: str, data: dict, out: list[Violation]) -> None:
    valid_status = {"open", "investigating", "resolved", "accepted_residual", "superseded"}
    valid_sev = {"low", "medium", "high", "critical"}
    status = data.get("status")
    if not _nonempty(status):
        out.append(Violation("contradiction-status", rel, 1, "contradiction missing status"))
    elif str(status) not in valid_status:
        out.append(
            Violation("contradiction-status", rel, 1, f"status {status!r} not in {sorted(valid_status)}")
        )
    sev = data.get("severity")
    if _nonempty(sev) and str(sev) not in valid_sev:
        out.append(
            Violation("contradiction-severity", rel, 1, f"severity {sev!r} not in {sorted(valid_sev)}")
        )
    if not _nonempty(data.get("claims_in_conflict")):
        out.append(
            Violation("contradiction-claims", rel, 1, "contradiction has empty claims_in_conflict")
        )


def _check_loop_floor(rel: str, data: dict, out: list[Violation]) -> None:
    status = str(data.get("status", "")).strip().lower()
    if status == "active":
        if not _nonempty(data.get("runner")):
            out.append(
                Violation("loop-runner", rel, 1, "active loop has no 'runner' (module:function or 'agent-worklist')")
            )
        # last_run must be PRESENT (may legitimately be null-once-never-run only
        # for proposed loops; an ACTIVE loop must have been instantiated with a
        # real last_run at spawn).
        if "last_run" not in data or data.get("last_run") in (None, ""):
            out.append(Violation("loop-last-run", rel, 1, "active loop has no 'last_run'"))


def _check_improvement_floor(rel: str, data: dict, out: list[Violation]) -> None:
    """An APPLIED improvement must be verifiable: it carries a machine-checkable
    ``expected_signal`` (event + target) or an explicit ``verify: manual``
    stamp. An applied change with no way to ever know whether it worked is
    self-improvement theatre -- the improvement-lifecycle loop cannot close it.
    """
    status = str(data.get("status", "")).strip().lower()
    if status != "applied":
        return
    if str(data.get("verify", "")).strip().lower() == "manual":
        return
    sig = data.get("expected_signal")
    valid = (
        isinstance(sig, dict)
        and str(sig.get("event", "")).strip()
        in ("feedback_event", "value_event", "failure_event")
        and _nonempty(sig.get("target"))
    )
    if not valid:
        out.append(
            Violation(
                "improvement-unverifiable",
                rel,
                1,
                "applied improvement has neither a machine-checkable "
                "expected_signal (event+target) nor 'verify: manual'",
            )
        )


_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _body_after_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :]).strip()
    return text.strip()


def check_skills(root: Path, out: list[Violation]) -> None:
    """Validate managed skill packages under AgentResources.nosync/Skills."""
    base = root / SKILLS_ROOT
    if not base.exists():
        return
    if not base.is_dir():
        out.append(Violation("skill-root", SKILLS_ROOT, None, "Skills root is not a directory"))
        return
    required = ("name", "description", "status", "sensitivity", "provenance", "created", "updated", "tags")
    statuses = {"active", "stale", "archived"}
    provenance = {"agent", "admin", "manual", "seed", "imported"}
    sensitivities = {"public", "internal", "confidential", "restricted", "secret"}
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith(".") or child.name.startswith("_"):
            continue
        rel_dir = _rel(root, child)
        md = child / "SKILL.md"
        rel = _rel(root, md)
        if not md.exists():
            out.append(Violation("skill-package", rel_dir, None, "skill package missing SKILL.md"))
            continue
        text = _read(md)
        if text is None:
            out.append(Violation("skill-unreadable", rel, None, "SKILL.md could not be read"))
            continue
        data, err = parse_frontmatter(text)
        if data is None:
            out.append(Violation("skill-frontmatter", rel, 1, err or "invalid skill frontmatter"))
            continue
        for key in required:
            if not _nonempty(data.get(key)):
                out.append(Violation("skill-schema", rel, 1, f"skill missing required {key!r}"))
        name = str(data.get("name", "")).strip()
        if name and not _SKILL_NAME_RE.fullmatch(name):
            out.append(Violation("skill-name", rel, 1, "skill name must be a safe lowercase slug"))
        if name and name != child.name:
            out.append(Violation("skill-name", rel, 1, f"skill name {name!r} does not match directory {child.name!r}"))
        if _nonempty(data.get("status")) and data.get("status") not in statuses:
            out.append(Violation("skill-status", rel, 1, f"status {data.get('status')!r} not in {sorted(statuses)}"))
        if _nonempty(data.get("provenance")) and data.get("provenance") not in provenance:
            out.append(
                Violation("skill-provenance", rel, 1, f"provenance {data.get('provenance')!r} not in {sorted(provenance)}")
            )
        if _nonempty(data.get("sensitivity")) and data.get("sensitivity") not in sensitivities:
            out.append(
                Violation("skill-sensitivity", rel, 1, f"sensitivity {data.get('sensitivity')!r} not valid")
            )
        if _nonempty(data.get("created")) and not re.search(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}", str(data.get("created"))):
            out.append(Violation("skill-date", rel, 1, "created must start with YYYY-MM-DD"))
        if _nonempty(data.get("updated")) and not re.search(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}", str(data.get("updated"))):
            out.append(Violation("skill-date", rel, 1, "updated must start with YYYY-MM-DD"))
        if "tags" in data and not isinstance(data.get("tags"), list):
            out.append(Violation("skill-tags", rel, 1, "tags must be a block list"))
        if not _body_after_frontmatter(text):
            out.append(Violation("skill-body", rel, 1, "skill body must be non-empty"))


def _ontology_subtypes(cfg: dict | None) -> set[str] | None:
    if not isinstance(cfg, dict):
        return None
    ont = cfg.get("ontology")
    if not isinstance(ont, dict):
        return None
    subs = ont.get("entity_subtypes")
    if not isinstance(subs, list):
        return None
    return {str(x) for x in subs}


# --------------------------------------------------------------------------- #
# external-path scan (ALL files incl. oracle.yml)
# --------------------------------------------------------------------------- #
# Tree-walk skip sets are owned by secret_scan (the other whole-tree walker)
# so both walkers stay binary-aware and bounded together.
_SKIP_TREE_DIRS = secret_scan._SKIP_DIRS
_SKIP_TREE_SUFFIXES = set(secret_scan._SKIP_SUFFIXES) | {".pyc"}

# Kernel DEV/TEST/BUILD machinery -- identical across every spawn, version-
# controlled and hash-pinned by .kernel-manifest.json, NOT sovereign data. The
# external-path and secret tree-walks deliberately skip it: the shipped tests/
# carry deliberately-fake fixture credentials (test_secret_scan.py) and example
# external-path lines purely to prove the scanners work, and known-failures.txt
# embeds example violation keys in comments. Scanning them would charge a fresh
# spawn for the kernel's own self-tests against an intentionally-EMPTY baseline
# that (per its header) ships clean. Sovereign roots (Memory.nosync,
# Meta.nosync), config (oracle.yml) and doctrine (*.md) stay fully scanned, so a
# real leaked credential or a hardcoded /Users/... path is still caught.
_SKIP_TREE_PART_NAMES = {"tests", ".pytest_cache", "tool-backups"}
_SKIP_TREE_PART_SUFFIXES = (".egg-info",)
_SKIP_TREE_FILE_NAMES = {".kernel-manifest.json", "known-failures.txt"}


def _is_kernel_machinery(rel_parts: tuple[str, ...], name: str) -> bool:
    """True if a path is kernel dev/test/build machinery (skip in tree walks).

    Covers the ``tests/`` suite, ``.pytest_cache``, any ``*.egg-info`` build
    dir, the timestamped ``tool-backups/`` rollback copies that ``upgrade.py``
    writes under ``Meta.nosync`` (frozen, hash-verified kernel snapshots --
    re-flagging them would mint a NEW un-baselinable violation key on every
    upgrade), the generated ``.kernel-manifest.json`` hash list, and the
    ``known-failures.txt`` baseline (whose comments carry EXAMPLE violation
    keys). Match is on path COMPONENTS so a nested file under tests/ is caught
    too.
    """
    for part in rel_parts:
        if part in _SKIP_TREE_PART_NAMES:
            return True
        if part.endswith(_SKIP_TREE_PART_SUFFIXES):
            return True
    return name in _SKIP_TREE_FILE_NAMES


# Roots/files the security.scan_exclude policy can NEVER exempt: authored
# sovereign content is where a pasted credential or hardcoded path actually
# hurts, so it stays scanned no matter what the globs say.
_SCAN_SOVEREIGN_ROOTS = ("Memory.nosync", "Meta.nosync")


def _scan_exclude_predicate(cfg: dict | None) -> "Callable[[str], bool] | None":
    """Build the ``security.scan_exclude`` predicate from oracle.yml (or None).

    ``security.scan_exclude:`` is an optional list of root-relative globs
    (fnmatch; a bare directory name exempts that whole subtree) that keeps a
    large immutable raw export out of the whole-tree secret/external-path
    walks. Sovereign roots (``Memory.nosync``, ``Meta.nosync``), ``oracle.yml``
    and root-level doctrine ``*.md`` are ALWAYS scanned regardless of globs.
    """
    if not isinstance(cfg, dict):
        return None
    sec = cfg.get("security")
    pats = sec.get("scan_exclude") if isinstance(sec, dict) else None
    if not isinstance(pats, list) or not pats:
        return None
    globs = [str(p).rstrip("/") for p in pats if str(p).strip()]
    if not globs:
        return None

    def excluded(rel: str) -> bool:
        parts = rel.split("/")
        if parts[0] in _SCAN_SOVEREIGN_ROOTS:
            return False
        if len(parts) == 1 and (rel == "oracle.yml" or rel.endswith(".md")):
            return False
        for pat in globs:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + "/*"):
                return True
        return False

    return excluded


def check_external_paths(
    root: Path, out: list[Violation], exclude: "Callable[[str], bool] | None" = None
) -> None:
    """Flag absolute host paths anywhere under root, oracle.yml INCLUDED.

    A hardcoded ``/Users/<name>/...`` (or ``/Volumes/``, ``C:\\``) in config or
    doctrine breaks portability/sovereignty -- the kernel must be relocatable.
    Placeholders (``{{...}}``, ``/path/to/``, ``<...>``, ``example``) are exempt.

    Bounded like ``secret_scan.scan_tree``: skip dirs are pruned during the
    walk, binary and oversized files are never read -- a data-heavy oracle
    (multi-GB ``.nosync`` exports, office deliverables) must not hang or
    exhaust memory on this gate. ``exclude`` is the security.scan_exclude
    policy predicate (see ``_scan_exclude_predicate``).
    """
    for p in secret_scan.iter_files(root, exclude=exclude):
        if p.suffix.lower() in _SKIP_TREE_SUFFIXES:
            continue
        # Never scan the linter's own source for paths (it carries the pattern).
        if p.name == "oracle_lint.py":
            continue
        rel_parts = p.relative_to(root).parts if p.is_relative_to(root) else p.parts
        # Skip kernel dev/test/build machinery (tests/, .pytest_cache, *.egg-info,
        # tool-backups/, .kernel-manifest.json, known-failures.txt) -- see
        # _is_kernel_machinery.
        if _is_kernel_machinery(rel_parts, p.name):
            continue
        try:
            if p.stat().st_size > secret_scan._MAX_BYTES:
                continue
        except OSError:
            continue
        if secret_scan.is_binary_file(p):
            continue
        text = _read(p)
        if text is None:
            continue
        rel = _rel(root, p)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if EXTERNAL_PATH_RE.search(line) and not _PATH_ALLOW_RE.search(line):
                out.append(
                    Violation("external-path", rel, lineno, f"hardcoded external path: {line.strip()[:120]}")
                )


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #
# The ONE sanctioned connector-secret store (P7S-3): exactly the literal
# root-relative path ``.env.nosync`` -- never a glob, never a directory, never a
# nested ``*/.env.nosync``. It is written ONLY by the shell's
# ``write_root_env_secret`` and the kernel's ``persist_rotated_token`` (contained,
# atomic, 0o600). Without this, the first real connector token would turn
# ``./oracle lint`` red forever. Enforced narrow by
# ``tests/test_connectors_remote.py::test_lint_exempts_exactly_env_nosync``.
SECRET_SCAN_EXEMPT_LITERAL = ".env.nosync"


def check_secrets(
    root: Path, out: list[Violation], exclude: "Callable[[str], bool] | None" = None
) -> None:
    for f in secret_scan.scan_tree(root, exclude=exclude):
        rel = f.get("file", "<tree>")
        # Never re-flag the linter's own pattern catalogue or the secret scanner.
        if rel in ("_tools/oracle_lint.py", "_tools/secret_scan.py"):
            continue
        # Exempt EXACTLY the root's own .env.nosync (the sanctioned connector
        # secret store; P7S-3). The match is the literal root-relative path -- a
        # nested ".env.nosync" under any subdir is still scanned.
        if rel.replace("\\", "/") == SECRET_SCAN_EXEMPT_LITERAL:
            continue
        # Skip kernel dev/test/build machinery -- the shipped tests/ carry
        # deliberately-fake fixture credentials (test_secret_scan.py) that only
        # exist to prove the scanner works; .kernel-manifest.json holds sha256
        # tool hashes; these are not sovereign data that could leak a real
        # credential. See _is_kernel_machinery.
        rel_norm = rel.replace("\\", "/")
        if _is_kernel_machinery(tuple(rel_norm.split("/")), Path(rel_norm).name):
            continue
        out.append(
            Violation(
                "secret",
                rel,
                f.get("line"),
                f"possible secret ({f.get('pattern')}) at offset {f.get('offset')}",
            )
        )


# --------------------------------------------------------------------------- #
# registry integrity
# --------------------------------------------------------------------------- #
def check_registries(root: Path, out: list[Violation]) -> None:
    """For _INPUT and _OUTPUT: the ledger lines parse, drop_ids are unique, and
    REGISTRY.md (if present) matches a freshly rendered table from the ledger."""
    wp = root / "Workproduct.nosync"
    if not wp.is_dir():
        return
    for lane in ("_INPUT", "_OUTPUT"):
        ledger_path = wp / lane / ".registry.jsonl"
        reg_md = wp / lane / "REGISTRY.md"
        rel_ledger = _rel(root, ledger_path)
        if ledger_path.exists():
            rows, warnings = ledger.load(ledger_path)
            for w in warnings:
                if "unparseable" in w or "not a JSON object" in w:
                    out.append(Violation("registry-parse", rel_ledger, None, w))
            seen: dict[str, int] = {}
            for r in rows:
                did = str(r.get("drop_id", ""))
                if did:
                    seen[did] = seen.get(did, 0) + 1
            dups = sorted(d for d, n in seen.items() if n > 1)
            for d in dups:
                out.append(Violation("registry-dup-id", rel_ledger, None, f"duplicate drop_id {d!r}"))
            # REGISTRY.md must match a fresh render of the ledger.
            if reg_md.exists():
                rel_md = _rel(root, reg_md)
                rendered = ledger.render_table(ledger_path).strip()
                on_disk = (_read(reg_md) or "").strip()
                if _registry_body(on_disk) != _registry_body(rendered):
                    out.append(
                        Violation(
                            "registry-drift",
                            rel_md,
                            None,
                            "REGISTRY.md does not match a fresh render of the ledger",
                        )
                    )


def _registry_body(text: str) -> str:
    """Normalize a REGISTRY markdown: keep only the table rows (lines beginning
    with '|'), trimming surrounding prose/headers so the comparison is about the
    DATA, not the document chrome."""
    rows = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    return "\n".join(rows)


# --------------------------------------------------------------------------- #
# content-hash immutability
# --------------------------------------------------------------------------- #
def check_immutability(root: Path, out: list[Violation]) -> None:
    """For immutable record types, the on-disk body hash MUST equal the hash
    registered in the immutability ledger. A mismatch means a record that should
    be superseded (write-new) was silently edited in place => FAIL.

    The ledger is ``Meta.nosync/ledgers/record_hashes.jsonl`` with rows shaped
    ``{drop_id, ts, type, path, content_sha256}``. Records WITHOUT a ledger entry
    are not flagged here (registration is the source_record tool's job); this
    check fails ONLY on a genuine on-disk/ledger MISMATCH.
    """
    ledger_path = root / "Meta.nosync" / "ledgers" / "record_hashes.jsonl"
    if not ledger_path.exists():
        return
    rows, _ = ledger.load(ledger_path)
    # Keep the LATEST registered hash per path (later rows win -- supersession).
    registered: dict[str, str] = {}
    for r in rows:
        rpath = str(r.get("path", "")).strip()
        rhash = str(r.get("content_sha256", "")).strip()
        rtype = str(r.get("type", "")).strip().lower()
        if not rpath or not rhash:
            continue
        if rtype and rtype not in IMMUTABLE_TYPES:
            continue
        registered[rpath] = rhash
    for rpath, rhash in registered.items():
        note = root / rpath
        if not note.exists():
            out.append(
                Violation("immutable-missing", rpath, None, "registered immutable record no longer on disk")
            )
            continue
        text = _read(note)
        if text is None:
            continue
        actual = content_sha256(text)
        if actual != rhash:
            out.append(
                Violation(
                    "immutable-mutated",
                    rpath,
                    None,
                    f"content hash {actual[:12]} != registered {rhash[:12]} (edit-in-place of immutable record)",
                )
            )


def check_source_record_immutability(root: Path, out: list[Violation]) -> None:
    """Verify Source notes against the source_record ledger.

    Source records use a self-hash algorithm that excludes the
    ``content_sha256`` field while hashing the rendered note. That contract is
    encoded in ``source_record.verify_record`` rather than in the generic
    ``record_hashes.jsonl`` ledger, so lint must consult it directly.
    """
    ledger_path = root / "Meta.nosync" / "ledgers" / "source_record.jsonl"
    if not ledger_path.exists():
        return
    try:
        records = source_record.list_records(root)
    except Exception as exc:
        out.append(
            Violation(
                "source-record-ledger-unreadable",
                _rel(root, ledger_path),
                None,
                f"could not read source_record ledger: {exc}",
            )
        )
        return
    for rec in records:
        sid = str(rec.get("source_id", "")).strip()
        if not sid:
            continue
        report = source_record.verify_record(root, sid)
        if report.get("ok"):
            continue
        rpath = str(rec.get("path") or _rel(root, ledger_path))
        for issue in report.get("issues") or ["source record verification failed"]:
            out.append(
                Violation(
                    "source-record-mutated",
                    rpath,
                    None,
                    f"{sid}: {issue}",
                )
            )


# --------------------------------------------------------------------------- #
# Truth map authority resolution
# --------------------------------------------------------------------------- #
def _listish(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def _norm_authority(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _object_matches(value: Any, business_object: str) -> bool:
    return truth_map.normalize_object(str(value or "")) == truth_map.normalize_object(
        business_object
    )


def _source_claims_object(fm: dict, business_object: str) -> bool:
    vals: list = []
    for key in ("business_object", "object", "authoritative_for"):
        vals.extend(_listish(fm.get(key)))
    return any(_object_matches(v, business_object) for v in vals)


def _source_resolves_authority(fm: dict, primary: str, business_object: str) -> bool:
    if str(fm.get("status", "")).strip().lower() == "superseded":
        return False
    norm_primary = _norm_authority(primary)
    if not norm_primary:
        return False

    source_ids: list = []
    for key in ("id", "source_id"):
        source_ids.extend(_listish(fm.get(key)))
    if any(_norm_authority(v) == norm_primary for v in source_ids):
        return True

    authority_values: list = []
    for key in (
        "authority_id",
        "primary_source",
        "source_system",
        "connector",
        "title",
    ):
        authority_values.extend(_listish(fm.get(key)))
    if not any(_norm_authority(v) == norm_primary for v in authority_values):
        return False
    return _source_claims_object(fm, business_object)


def _any_source_resolves(root: Path, primary: str, business_object: str) -> bool:
    folder = root / "Memory.nosync" / "Sources"
    if not folder.is_dir():
        return False
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        text = _read(p)
        if text is None:
            continue
        fm, _ = parse_frontmatter(text)
        if isinstance(fm, dict) and _source_resolves_authority(fm, primary, business_object):
            return True
    return False


def _connector_resolves_authority(root: Path, primary: str, business_object: str) -> bool:
    base = root / "Connectors"
    if not base.is_dir():
        return False
    norm_primary = _norm_authority(primary)
    for p in sorted(base.rglob("*.manifest.yaml")):
        text = _read(p)
        if text is None:
            continue
        try:
            data = oracle_yaml.safe_load(text)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        names = [data.get("id"), data.get("system")]
        if not any(_norm_authority(v) == norm_primary for v in names):
            continue
        if any(_object_matches(v, business_object) for v in _listish(data.get("authoritative_for"))):
            return True
    return False


def check_truth_map_authorities(root: Path, out: list[Violation]) -> None:
    """Confirmed truth-map rows must resolve to a concrete Source or connector.

    Draft rows can name planned/manual authorities without blocking bootstrap.
    Once a row is marked ``confirmed``, its Primary source must be machine-
    resolvable to either an active Source record for that object or a connector
    manifest that claims ``authoritative_for`` that object.
    """
    path = root / "TRUTH-MAP.md"
    if not path.exists():
        return
    try:
        rows = truth_map.load_rows(root)
    except Exception as exc:
        out.append(
            Violation(
                "truth-map-unreadable",
                _rel(root, path),
                None,
                f"could not parse truth map: {exc}",
            )
        )
        return
    for row in rows:
        status = str(row.get("status", "")).strip().lower()
        if status != "confirmed":
            continue
        business_object = str(row.get("business_object", "") or "").strip()
        primary = str(row.get("primary source", "") or "").strip()
        if not truth_map.primary_source_is_authoritative(primary):
            continue
        if _any_source_resolves(root, primary, business_object):
            continue
        if _connector_resolves_authority(root, primary, business_object):
            continue
        out.append(
            Violation(
                "truth-map-authority-unresolved",
                _rel(root, path),
                None,
                (
                    f"confirmed row {business_object!r} primary source {primary!r} "
                    "does not resolve to an active Source id/source_system/connector "
                    "for that object or a connector authoritative_for entry"
                ),
            )
        )


# --------------------------------------------------------------------------- #
# Doctrine -> Enforcer
# --------------------------------------------------------------------------- #
# Doc budgets: the v2 cognitive-load contract. The operating card must stay a
# card; playbooks must stay self-contained one-screen-ish guides. Budgets are
# generous enough for real content and tight enough to stop doctrine sprawl.
DOC_BUDGETS = {
    "AGENTS.md": 150,
    "CLAUDE.md": 40,
    "DOCTRINE.md": 220,
    "PLAYBOOKS/answer.md": 120,
    "PLAYBOOKS/ingest.md": 120,
    "PLAYBOOKS/review.md": 120,
    "PLAYBOOKS/brief.md": 120,
    "PLAYBOOKS/session.md": 120,
    "PLAYBOOKS/loops.md": 120,
    "PLAYBOOKS/admin-setup.md": 120,
}


def check_doc_budgets(root: Path, out: list[Violation]) -> None:
    """v2 doc-budget gate: an over-budget operating doc FAILS the build.

    The whole point of the playbook system is that a mid-tier agent reads ONE
    short card per session and one playbook per workflow. Letting these docs
    grow without bound silently recreates the v1 cognitive-load failure.
    """
    for rel, budget in DOC_BUDGETS.items():
        p = root / rel
        if not p.exists():
            continue  # presence is setup_audit's job
        text = _read(p)
        if text is None:
            continue
        n = len(text.splitlines())
        if n > budget:
            out.append(
                Violation(
                    "doc-over-budget",
                    rel,
                    None,
                    f"{n} lines exceeds the {budget}-line budget; cut or move "
                    "content to a reference doc",
                )
            )


def check_doctrine_enforcers(root: Path, out: list[Violation]) -> None:
    """Constraint 4: every guarantee in SECURITY/PROCESSING-MATRIX/GOVERNANCE
    either names its enforcing tool OR is stamped 'advisory'. A guarantee line
    (one carrying a denial/requirement/prohibition verb) that does neither is a
    FALSE ASSURANCE and FAILS.

    To stay precise we only treat *bulleted* or *table* assertion lines as
    guarantees (prose paragraphs/headers are exempt), and we let a guarantee
    inherit an enforcer named in its own line OR in a clearly-attached
    parenthetical/`code` reference on that line.
    """
    for fname in GUARANTEE_FILES:
        p = root / fname
        if not p.exists():
            continue
        text = _read(p)
        if text is None:
            continue
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            # Only assertion lines: markdown bullets ('- ', '* ') or table rows.
            is_bullet = line.startswith("- ") or line.startswith("* ")
            is_table_row = line.startswith("|") and line.count("|") >= 2
            if not (is_bullet or is_table_row):
                continue
            if is_table_row and re.fullmatch(r"\|[\s:|-]+\|", line):
                continue  # separator row
            if not _GUARANTEE_VERB_RE.search(line):
                continue
            if _ADVISORY_RE.search(line):
                continue  # explicitly advisory -- honest, allowed
            if _ENFORCER_RE.search(line):
                continue  # names an enforcer -- binding, allowed
            out.append(
                Violation(
                    "doctrine-unenforced",
                    fname,
                    lineno,
                    "guarantee names no enforcer and is not labelled 'advisory': "
                    + line[:120],
                )
            )


# --------------------------------------------------------------------------- #
# known-failures baseline
# --------------------------------------------------------------------------- #
BASELINE_HEADER = """\
# oracle known-failures baseline
#
# Each non-comment, non-blank line is a STABLE violation key
# (`<code>::<path>[:<line>]`, exactly as printed by `oracle lint`). A key listed
# here is DOWNGRADED from a failing violation to a warning, so a known, tracked
# gap does not block the gate while it is being worked off -- WITHOUT ever
# masking a NEW regression (a violation whose key is not listed still fails).
#
# Discipline: add a key only with a tracking note; remove it the moment the
# underlying issue is fixed. A clean oracle ships with an EMPTY baseline.
# ---------------------------------------------------------------------------
"""


def load_baseline(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        keys.add(s)
    return keys


# --------------------------------------------------------------------------- #
# top-level lint
# --------------------------------------------------------------------------- #
def lint(root: Path, schemas_dir: Path | None = None) -> list[Violation]:
    """Run EVERY check and return the full, un-baselined list of violations."""
    root = Path(root).resolve()
    schemas_dir = Path(schemas_dir) if schemas_dir else _default_schemas_dir()
    out: list[Violation] = []

    cfg = check_oracle_yml(root, schemas_dir, out)
    check_connectors(root, schemas_dir, out)
    check_notes(root, schemas_dir, cfg, out)
    check_skills(root, out)
    check_registries(root, out)
    check_immutability(root, out)
    check_source_record_immutability(root, out)
    check_truth_map_authorities(root, out)
    scan_exclude = _scan_exclude_predicate(cfg)
    check_external_paths(root, out, exclude=scan_exclude)
    check_secrets(root, out, exclude=scan_exclude)
    check_doctrine_enforcers(root, out)
    check_doc_budgets(root, out)

    out.sort(key=lambda v: (v.path, v.line or 0, v.code))
    return out


def partition(
    violations: list[Violation], baseline: set[str]
) -> tuple[list[Violation], list[Violation]]:
    """Split violations into (failing, baselined-warnings)."""
    failing: list[Violation] = []
    warned: list[Violation] = []
    for v in violations:
        if v.key in baseline:
            warned.append(v)
        else:
            failing.append(v)
    return failing, warned


def run(root: Path, baseline_path: Path | None = None, schemas_dir: Path | None = None) -> dict:
    """High-level entrypoint used by the CLI and by tests.

    Returns a report dict::

        {
          "ok": bool,                 # True iff no FAILING (un-baselined) violations
          "failing": [Violation...],
          "warnings": [Violation...], # baselined, downgraded
          "all": [Violation...],
        }
    """
    violations = lint(root, schemas_dir=schemas_dir)
    baseline = load_baseline(baseline_path)
    failing, warned = partition(violations, baseline)
    return {
        "ok": not failing,
        "failing": failing,
        "warnings": warned,
        "all": violations,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Schema-validating oracle linter")
    parser.add_argument("root", nargs="?", default=".", help="oracle root (default: cwd)")
    parser.add_argument(
        "--baseline",
        default=None,
        help="path to known-failures.txt (default: <root>/known-failures.txt if present)",
    )
    parser.add_argument(
        "--schemas-dir",
        default=None,
        help="override the JSON schemas directory (default: _tools/schemas next to this file)",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if args.baseline is not None:
        baseline_path: Path | None = Path(args.baseline)
    else:
        default_bl = root / "known-failures.txt"
        baseline_path = default_bl if default_bl.exists() else None

    schemas_dir = Path(args.schemas_dir) if args.schemas_dir else None
    report = run(root, baseline_path=baseline_path, schemas_dir=schemas_dir)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": report["ok"],
                    "failing": [v.to_dict() for v in report["failing"]],
                    "warnings": [v.to_dict() for v in report["warnings"]],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if report["ok"] else 1

    if report["warnings"]:
        print(f"ORACLE LINT: {len(report['warnings'])} baselined warning(s):")
        for v in report["warnings"]:
            print(f"  ~ {v.render()}")
    if report["ok"]:
        print("ORACLE LINT: PASS")
        return 0
    print(f"ORACLE LINT: FAIL ({len(report['failing'])} violation(s))")
    for v in report["failing"]:
        print(f"  - {v.render()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
