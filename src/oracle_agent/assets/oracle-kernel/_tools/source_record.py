#!/usr/bin/env python3
"""source_record.py -- the immutable Source-record generator + registrar.

A Source is an immutable snapshot of evidence (a pull, a doc, testimony, a
meeting, a schema snapshot, an artifact, a web capture, or a manual
observation). This module creates a schema-valid ``Memory.nosync/Sources/``
note with the common note frontmatter PLUS the source-specific provenance
fields (raw location, content sha256, locality, sensitivity, as-of date, and a
grain card), and REGISTERS it in the Sources ledger with its content hash.

Immutability is mechanical, not conventional:
  * The note's content sha256 is computed over the rendered bytes and recorded
    both in the frontmatter (``content_sha256``) and in the ledger row.
  * If the underlying input changes you do not edit the old source -- you call
    ``supersede(...)`` which writes a NEW source carrying ``supersedes:`` and
    stamps the prior source ``superseded_by:`` + ``status: superseded``, then
    registers the supersession in the ledger. ``oracle_lint`` FAILS on any
    on-disk/ledger hash mismatch, so a silent edit is detectable.

Path safety: the only user-/config-influenced segment of the destination is the
source slug, which is routed through ``safe_paths.safe_slug`` and
``safe_paths.contain(base='Memory.nosync')`` before the note is written. The
note bytes are written with ``Path.write_text`` to that contained path -- not an
``open(...)`` write and not an unvalidated destination -- which is the
documented exception the no-bypass guard does not flag (mirrors how
``artifact_io`` renders a generated record).

Public API:
    create(root, payload, *, actor='system', role='system') -> dict
    supersede(root, prior_id, payload, *, actor='system', role='system') -> dict
    load_record(root, source_id) -> dict | None
    list_records(root) -> list[dict]
    verify_record(root, source_id) -> dict   # on-disk vs ledger hash

CLI:
    python3 source_record.py --root R create --title T --provenance P \
        [--raw-location L] [--locality snapshot_local] [--sensitivity internal] \
        [--as-of YYYY-MM-DD] [--grain G] [--source-system S] [--content-sha C] \
        [--tags a,b] [--actor A] [--role R]
    python3 source_record.py --root R supersede --prior SRC-... [same flags]
    python3 source_record.py --root R list
    python3 source_record.py --root R verify --id SRC-...

Stdlib only. Imports floor siblings (safe_paths, ledger, oracle_yaml,
schema_check) bare-or-package so it works flat (tests inject _tools) and as a
package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

__all__ = [
    "create",
    "supersede",
    "load_record",
    "list_records",
    "verify_record",
    "render_note",
    "LOCALITY_ENUM",
]

LOCALITY_ENUM = ("external_only", "snapshot_local", "mirror_local")
_SENSITIVITY_ENUM = ("public", "internal", "confidential", "restricted", "secret")
_SOURCES_SUBDIR = "Sources"
_LEDGER_NAME = "source_record.jsonl"
_BASE = "Memory.nosync"
_DATE_LIKE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")


# --------------------------------------------------------------------------- #
# sibling-import shim (flat OR package)
# --------------------------------------------------------------------------- #
def _imp(name: str):
    try:
        return __import__(name)
    except Exception:  # pragma: no cover - package fallback
        import importlib
        return importlib.import_module(f".{name}", package=__package__)


def _safe_paths():
    return _imp("safe_paths")


def _ledger():
    return _imp("ledger")


def _schema_check():
    return _imp("schema_check")


def _policy():
    return _imp("policy")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ledger_path(root: Path) -> Path:
    return Path(root) / "Meta.nosync" / "ledgers" / _LEDGER_NAME


def _sources_dir(root: Path) -> Path:
    return Path(root) / _BASE / _SOURCES_SUBDIR


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _s(v) -> str:
    """Coerce any parsed-frontmatter value to a JSON-safe string.

    The YAML loader may return ``datetime.date`` for unquoted ISO dates; ledger
    rows must be JSON-serialisable, so we stringify non-str scalars.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def _yaml_scalar(v) -> str:
    """Render a scalar as a block-style YAML value (quote when needed)."""
    if v is None:
        return ""
    s = str(v)
    if s == "":
        return '""'
    # Quote if the value could be misread (leading/trailing space, special chars,
    # or a leading character that YAML would interpret).
    risky = any(c in s for c in (":", "#", "&", "*", "!", "{", "}", "[", "]", "|", ">", '"', "'", "\n"))
    risky = risky or bool(_DATE_LIKE_RE.match(s))
    if risky or s != s.strip() or s[0] in "-?@`%":
        inner = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return f'"{inner}"'
    return s


def _frontmatter_lines(fm: dict) -> list[str]:
    """Render a flat frontmatter mapping as block-style YAML lines.

    Lists are rendered as block sequences (one ``- item`` per line). Empty
    lists render as a bare key (parses to None), per the YAML subset rule
    (never inline ``[]``). Source authority fields such as
    ``authoritative_for`` need the same list behavior as ``tags``.
    """
    lines: list[str] = []
    for key, val in fm.items():
        if isinstance(val, list):
            items = val or []
            if items:
                lines.append(f"{key}:")
                for item in items:
                    lines.append(f"  - {_yaml_scalar(item)}")
            else:
                lines.append(f"{key}:")
            continue
        if val is None or val == "":
            # Bare key -> parses to None (the subset's empty value).
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    return lines


def render_note(frontmatter: dict, body: str) -> str:
    """Render a complete markdown note (frontmatter fence + body)."""
    fm_lines = _frontmatter_lines(frontmatter)
    parts = ["---", *fm_lines, "---", "", body.rstrip("\n"), ""]
    return "\n".join(parts)


def _build_frontmatter(
    source_id: str,
    payload: dict,
    *,
    content_sha: str,
    supersedes: Optional[str] = None,
    actor: str = "system",
    role: str = "system",
) -> dict:
    """Assemble the immutable Source frontmatter from a payload.

    Required common fields: id/type/title/created/updated/sensitivity/status/tags.
    Source fields: provenance, raw_location, locality, as_of, grain, source_system,
    captured_sha256 (the hash of the underlying material, if known) and the
    note's own content_sha256.
    """
    today = _today()
    sens = str(payload.get("sensitivity", "internal")).strip().lower()
    if sens not in _SENSITIVITY_ENUM:
        sens = "internal"
    locality = str(payload.get("locality", "snapshot_local")).strip().lower()
    if locality not in LOCALITY_ENUM:
        locality = "snapshot_local"
    tags = payload.get("tags") or ["source"]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    captured_sha = (
        payload.get("captured_sha256")
        or payload.get("sha256")
        or payload.get("source_sha256")
        or payload.get("content_sha256")
        or ""
    )
    fm = {
        "id": source_id,
        "type": "source",
        "title": payload.get("title") or f"Source {source_id}",
        "created": today,
        "updated": today,
        "sensitivity": sens,
        "status": "active",
        "tags": tags,
        "actor": actor,
        "role": role,
        "provenance": payload.get("provenance", ""),
        "raw_location": payload.get("raw_location", ""),
        "locality": locality,
        "as_of": payload.get("as_of") or today,
        "grain": payload.get("grain", ""),
        "source_system": payload.get("source_system", ""),
        "captured_sha256": captured_sha,
        "content_sha256": content_sha,
    }
    for key in (
        "business_object",
        "object",
        "authoritative_for",
        "authority_id",
        "primary_source",
        "connector",
        "origin_filename",
        "input_drop_id",
    ):
        value = payload.get(key)
        if value not in (None, "", []):
            fm[key] = value
    sup = supersedes or payload.get("supersedes")
    if sup:
        fm["supersedes"] = sup
    return fm


def _build_body(payload: dict) -> str:
    """Human-readable body: provenance narrative + grain card."""
    lines = [
        f"# {payload.get('title') or 'Source'}",
        "",
        "## Provenance",
        "",
        payload.get("provenance", "_(not stated)_") or "_(not stated)_",
        "",
        "## Grain card",
        "",
        payload.get("grain", "_(grain not stated; describe entity, time, and unit)_")
        or "_(grain not stated)_",
        "",
        "## Notes",
        "",
        payload.get("notes", "_(none)_") or "_(none)_",
    ]
    return "\n".join(lines)


def _validate(fm: dict) -> list[str]:
    """Validate frontmatter against note_frontmatter + finding-style source needs.

    Uses the shipped note_frontmatter.schema.json when available; always enforces
    the non-empty provenance + as_of fields a load-bearing source requires.
    """
    errors: list[str] = []
    schema_path = (
        Path(__file__).resolve().parent / "schemas" / "note_frontmatter.schema.json"
    )
    if schema_path.exists():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            sc = _schema_check()
            errors.extend(sc.validate(fm, schema))
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"schema load/validate error: {exc}")
    # Source-specific minimums (a load-bearing source must carry provenance).
    if not str(fm.get("provenance", "")).strip():
        errors.append("source: provenance is empty (provenance is required)")
    if not str(fm.get("as_of", "")).strip():
        errors.append("source: as_of is empty (an as-of date is required)")
    return errors


def _authority_bearing_payload(payload: dict) -> bool:
    for key in ("id", "source_id", "authority_id", "primary_source"):
        value = payload.get(key)
        if value not in (None, "", []):
            return True
    for key in ("business_object", "object", "authoritative_for"):
        value = payload.get(key)
        if value not in (None, "", []):
            return True
    return False


def _enforce_role(root: Path, payload: dict, actor: str, role: str) -> None:
    """Enforce declared role for caller-supplied roles.

    ``system`` is reserved for kernel/bootstrap paths that do not yet have a
    verified session identity. Any explicit human role is checked against
    oracle.yml. Authority-bearing Sources require the stronger truth-authority
    capability; ordinary source capture uses the user's document-provision cap.
    """
    role = (role or "").strip()
    if role in ("", "system"):
        return
    capability = (
        "change_truth_authority"
        if _authority_bearing_payload(payload)
        else "provide_documents"
    )
    _policy().require_role(actor or "unknown", role, capability, root=root)


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def _mint_id(root: Path) -> str:
    """Mint and reserve a collision-safe SRC-YYYYMMDD-NNN id.

    The reservation is appended through ``ledger.append(..., id_prefix="SRC")``,
    so the scan-and-write happens under the ledger's exclusive lock. Reservation
    rows intentionally do not carry ``source_id``; ``list_records`` and
    ``verify_record`` see only completed registrations.
    """
    led = _ledger()
    row = {
        "event": "reserve_source_id",
        "status": "reserved",
    }
    return led.append(_ledger_path(root), row, id_prefix="SRC")


def _destination(root: Path, source_id: str, title: str) -> Path:
    """Contained destination for the note under Memory.nosync/Sources/."""
    sp = _safe_paths()
    today = sp.today()
    base_slug = sp.safe_slug(title or source_id)
    filename = f"{today}_{sp.safe_slug(source_id)}-{base_slug}.md"
    return sp.contain(root, f"{_SOURCES_SUBDIR}/{filename}", base=_BASE)


def create(
    root,
    payload: dict,
    *,
    actor: str = "system",
    role: str = "system",
) -> dict:
    """Create and register an immutable Source record. Returns the ledger row.

    The note is rendered, its content sha256 computed and stamped into the
    frontmatter, validated against the schema, written to a CONTAINED path under
    Memory.nosync/Sources/, and registered in the source_record ledger with the
    content hash. Raises ValueError on schema/containment failure (writing
    nothing on validation failure).
    """
    root = Path(root)
    if not isinstance(payload, dict):
        raise TypeError("source_record.create requires a dict payload")
    _enforce_role(root, payload, actor, role)

    source_id = str(payload.get("id") or _mint_id(root)).strip()
    if not source_id:
        raise ValueError("source_record.create: source id is empty")
    if payload.get("id") and load_record(root, source_id) is not None:
        raise ValueError(f"source_record.create: duplicate source id {source_id!r}")

    # First render WITHOUT the content hash to compute a stable body+frontmatter,
    # then stamp the hash and re-render so the on-disk bytes contain their own
    # content_sha256. The hash is computed over the hash-free rendering, giving a
    # deterministic value that does not depend on itself.
    fm0 = _build_frontmatter(source_id, payload, content_sha="", actor=actor, role=role)
    body = _build_body(payload)
    hashfree = render_note(fm0, body)
    content_sha = _content_sha256(hashfree)

    fm = _build_frontmatter(source_id, payload, content_sha=content_sha, actor=actor, role=role)
    note_text = render_note(fm, body)

    errors = _validate(fm)
    if errors:
        raise ValueError("source_record.create: invalid record:\n  " + "\n  ".join(errors))

    dest = _destination(root, source_id, fm["title"])
    if dest.exists():
        raise ValueError(f"source_record.create: destination already exists: {_relpath(dest, root)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Contained, generated-content write (documented no-bypass exception).
    dest.write_text(note_text, encoding="utf-8")

    row = {
        "source_id": source_id,
        "title": fm["title"],
        "path": _relpath(dest, root),
        "sensitivity": fm["sensitivity"],
        "locality": fm["locality"],
        "as_of": fm["as_of"],
        "provenance": fm["provenance"],
        "captured_sha256": fm.get("captured_sha256", ""),
        "content_sha256": content_sha,
        "hashfree_sha256": content_sha,
        "source_system": fm.get("source_system", ""),
        "connector": fm.get("connector", ""),
        "business_object": fm.get("business_object", ""),
        "authoritative_for": fm.get("authoritative_for", ""),
        "authority_id": fm.get("authority_id", ""),
        "supersedes": fm.get("supersedes", ""),
        "status": "active",
        "actor": actor,
        "role": role,
    }
    led = _ledger()
    drop_id = led.append(_ledger_path(root), row, id_prefix="SREG")
    row["drop_id"] = drop_id
    return row


def _relpath(p: Path, root: Path) -> str:
    """Path of ``p`` relative to ``root``, robust to symlinked roots.

    ``safe_paths.contain`` returns a realpath-resolved destination (e.g.
    ``/private/var/...`` on macOS where ``/var`` is a symlink), so we resolve
    BOTH sides before computing the relative path. Falls back to the absolute
    string if (somehow) the destination is not under root.
    """
    try:
        return str(Path(p).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(p)


# --------------------------------------------------------------------------- #
# supersede
# --------------------------------------------------------------------------- #
def supersede(
    root,
    prior_id: str,
    payload: dict,
    *,
    actor: str = "system",
    role: str = "system",
) -> dict:
    """Write a NEW source that supersedes ``prior_id`` and mark the prior one.

    The new record carries ``supersedes: prior_id``. The prior record's
    frontmatter is updated to ``status: superseded`` + ``superseded_by:`` (the
    only permitted mutation of an immutable record -- a pointer, recorded in the
    ledger), and a supersession row is appended. Returns the NEW ledger row.
    """
    root = Path(root)
    payload = dict(payload or {})
    payload["supersedes"] = prior_id
    new_row = create(root, payload, actor=actor, role=role)

    # Update the prior note in place with the superseded_by pointer (the single
    # legal mutation: a forward link, not a content edit). Re-hash and re-register.
    prior = load_record(root, prior_id)
    if prior is not None and prior.get("path"):
        prior_path = Path(root) / prior["path"]
        if prior_path.exists():
            _stamp_superseded(prior_path, root, new_row["source_id"], actor, role)

    led = _ledger()
    led.append(
        _ledger_path(root),
        {
            "event": "supersede",
            "prior_id": prior_id,
            "new_id": new_row["source_id"],
            "actor": actor,
            "role": role,
        },
        id_prefix="SUP",
    )
    return new_row


def _stamp_superseded(path: Path, root: Path, new_id: str, actor: str, role: str) -> None:
    """Mark a prior source note status=superseded + superseded_by=new_id.

    Re-renders the note from its parsed frontmatter so the file stays
    schema-valid, re-computes content_sha256, and appends an updated ledger row
    reflecting the new on-disk hash (so the immutability check stays consistent).
    """
    fm, body = _parse_note(path.read_text(encoding="utf-8"))
    if fm is None:
        return
    fm["status"] = "superseded"
    fm["superseded_by"] = new_id
    fm["updated"] = _today()
    # Recompute content hash over the hash-free rendering.
    fm_hashfree = dict(fm)
    fm_hashfree["content_sha256"] = ""
    hashfree = render_note(fm_hashfree, body)
    new_sha = _content_sha256(hashfree)
    fm["content_sha256"] = new_sha
    note_text = render_note(fm, body)
    # ``path`` came from a contained ledger entry; re-contain defensively so the
    # write target is provably under Memory.nosync.
    sp = _safe_paths()
    rel_to_base = Path(path).resolve().relative_to((Path(root) / _BASE).resolve())
    safe_dest = sp.contain(root, str(rel_to_base), base=_BASE)
    safe_dest.write_text(note_text, encoding="utf-8")  # contained generated write
    # The ledger path field is ROOT-relative (matches create()), so verify can
    # locate the note via ``root / path``.
    root_rel = _relpath(safe_dest, root)
    led = _ledger()
    led.append(
        _ledger_path(root),
        {
            "source_id": _s(fm.get("id", "")),
            "title": _s(fm.get("title", "")),
            "path": root_rel,
            "sensitivity": _s(fm.get("sensitivity", "")),
            "locality": _s(fm.get("locality", "")),
            "as_of": _s(fm.get("as_of", "")),
            "provenance": _s(fm.get("provenance", "")),
            "captured_sha256": _s(fm.get("captured_sha256", "")),
            "content_sha256": new_sha,
            "hashfree_sha256": new_sha,
            "source_system": _s(fm.get("source_system", "")),
            "connector": _s(fm.get("connector", "")),
            "business_object": _s(fm.get("business_object", "")),
            "authoritative_for": fm.get("authoritative_for", ""),
            "authority_id": _s(fm.get("authority_id", "")),
            "status": "superseded",
            "superseded_by": new_id,
            "actor": actor,
            "role": role,
        },
        id_prefix="SREG",
    )


# --------------------------------------------------------------------------- #
# read / verify
# --------------------------------------------------------------------------- #
def _parse_note(text: str):
    """Parse a frontmatter note into (frontmatter_dict, body_str).

    Returns (None, text) if there is no leading '---' fence. The frontmatter
    body is parsed with the safe-subset YAML loader.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    oy = _imp("oracle_yaml")
    try:
        fm = oy.safe_load(fm_text)
    except Exception:
        return None, text
    if not isinstance(fm, dict):
        return None, text
    return fm, body


def load_record(root, source_id: str) -> Optional[dict]:
    """Return the latest ledger row for ``source_id``, or None."""
    led = _ledger()
    rows, _ = led.load(_ledger_path(Path(root)))
    latest = None
    for r in rows:
        if r.get("source_id") == source_id:
            latest = r
    return latest


def list_records(root) -> list[dict]:
    """All registered source rows (latest per id, active first)."""
    led = _ledger()
    rows, _ = led.load(_ledger_path(Path(root)))
    by_id: dict[str, dict] = {}
    for r in rows:
        sid = r.get("source_id")
        if sid:
            by_id[sid] = r
    return list(by_id.values())


def verify_record(root, source_id: str) -> dict:
    """Compare the on-disk note's content hash to the ledger hash.

    Returns a report dict with ``ok`` False if the note was edited out-of-band
    (the immutability violation oracle_lint also detects).
    """
    root = Path(root)
    row = load_record(root, source_id)
    report = {"source_id": source_id, "ok": True, "issues": []}
    if row is None:
        report["ok"] = False
        report["issues"].append("no ledger row for source_id")
        return report
    path = root / row.get("path", "")
    if not path.exists():
        report["ok"] = False
        report["issues"].append(f"note missing on disk: {path}")
        return report
    fm, body = _parse_note(path.read_text(encoding="utf-8"))
    if fm is None:
        report["ok"] = False
        report["issues"].append("note has no parseable frontmatter")
        return report
    fm_hashfree = dict(fm)
    fm_hashfree["content_sha256"] = ""
    recomputed = _content_sha256(render_note(fm_hashfree, body))
    ledger_sha = row.get("content_sha256", "")
    disk_sha = fm.get("content_sha256", "")
    if recomputed != ledger_sha:
        report["ok"] = False
        report["issues"].append(
            f"on-disk content hash {recomputed} != ledger hash {ledger_sha}"
        )
    if disk_sha and disk_sha != ledger_sha:
        report["ok"] = False
        report["issues"].append(
            f"frontmatter content_sha256 {disk_sha} != ledger hash {ledger_sha}"
        )
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _payload_from_args(args) -> dict:
    tags = None
    if getattr(args, "tags", None):
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    authoritative_for = None
    if getattr(args, "authoritative_for", None):
        authoritative_for = [
            t.strip() for t in args.authoritative_for.split(",") if t.strip()
        ]
    return {
        "title": args.title,
        "provenance": args.provenance,
        "raw_location": args.raw_location,
        "locality": args.locality,
        "sensitivity": args.sensitivity,
        "as_of": args.as_of,
        "grain": args.grain,
        "source_system": args.source_system,
        "content_sha256": args.content_sha,
        "captured_sha256": args.captured_sha,
        "business_object": args.business_object,
        "authoritative_for": authoritative_for,
        "authority_id": args.authority_id,
        "primary_source": args.primary_source,
        "connector": args.connector,
        "origin_filename": args.origin_filename,
        "input_drop_id": args.input_drop_id,
        "notes": args.notes,
        "tags": tags,
    }


def _add_create_flags(p) -> None:
    p.add_argument("--title", required=True)
    p.add_argument("--provenance", required=True)
    p.add_argument("--raw-location", dest="raw_location", default="")
    p.add_argument("--locality", default="snapshot_local", choices=list(LOCALITY_ENUM))
    p.add_argument("--sensitivity", default="internal", choices=list(_SENSITIVITY_ENUM))
    p.add_argument("--as-of", dest="as_of", default="")
    p.add_argument("--grain", default="")
    p.add_argument("--source-system", dest="source_system", default="")
    p.add_argument("--content-sha", dest="content_sha", default="")
    p.add_argument("--captured-sha", dest="captured_sha", default="")
    p.add_argument("--business-object", dest="business_object", default="")
    p.add_argument(
        "--authoritative-for",
        dest="authoritative_for",
        default="",
        help="comma-separated business objects this Source can authoritatively ground",
    )
    p.add_argument("--authority-id", dest="authority_id", default="")
    p.add_argument("--primary-source", dest="primary_source", default="")
    p.add_argument("--connector", default="")
    p.add_argument("--origin-filename", dest="origin_filename", default="")
    p.add_argument("--input-drop-id", dest="input_drop_id", default="")
    p.add_argument("--notes", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--actor", default="system")
    p.add_argument("--role", default="system")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Immutable Source-record generator")
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create + register a Source")
    _add_create_flags(p_create)

    p_sup = sub.add_parser("supersede", help="supersede an existing Source")
    p_sup.add_argument("--prior", required=True)
    _add_create_flags(p_sup)

    sub.add_parser("list", help="list registered sources")

    p_verify = sub.add_parser("verify", help="verify on-disk vs ledger hash")
    p_verify.add_argument("--id", required=True)

    args = ap.parse_args(argv)
    root = Path(args.root)

    try:
        if args.cmd == "create":
            row = create(root, _payload_from_args(args), actor=args.actor, role=args.role)
            print(json.dumps(row, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "supersede":
            row = supersede(
                root, args.prior, _payload_from_args(args),
                actor=args.actor, role=args.role,
            )
            print(json.dumps(row, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "list":
            print(json.dumps(list_records(root), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "verify":
            report = verify_record(root, args.id)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["ok"] else 1
    except (ValueError, TypeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
