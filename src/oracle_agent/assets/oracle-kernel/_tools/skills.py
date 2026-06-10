#!/usr/bin/env python3
"""skills.py -- managed oracle-local skill repository.

Skills are the oracle's portable procedural memory. They live under
``AgentResources.nosync/Skills/<name>/SKILL.md`` and move with the oracle rather
than depending on a host-machine skill store. This module is the sole managed
lifecycle API:

    list, view, create, patch, record-use, archive, report

There is deliberately no delete command. Archive moves a package into
``Skills/.archive/`` and appends a metadata-only ``skill_event`` row. Every
mutation uses contained paths and ``ledger.append``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover
    import safe_paths
    import ledger
    import secret_scan
    from oracle_yaml import safe_load, UnsupportedYAML
except Exception:  # pragma: no cover
    from . import safe_paths  # type: ignore
    from . import ledger  # type: ignore
    from . import secret_scan  # type: ignore
    from .oracle_yaml import safe_load, UnsupportedYAML  # type: ignore


AGENT_BASE = "AgentResources.nosync"
SKILLS_REL = "Skills"
ARCHIVE_REL = "Skills/.archive"
SKILL_FILE = "SKILL.md"
SKILL_LEDGER_REL = "Meta.nosync/ledgers/skill_event.jsonl"

STATUSES = {"active", "stale", "archived"}
PROVENANCE = {"agent", "admin", "manual", "seed", "imported"}
SENSITIVITIES = {"public", "internal", "confidential", "restricted", "secret"}


def _now() -> datetime:
    return datetime.now()


def _today(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%d")


def _now_compact(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).strftime("%Y%m%d-%H%M%S")


def _skills_dir(root: Path) -> Path:
    return Path(root) / AGENT_BASE / SKILLS_REL


def _archive_dir(root: Path) -> Path:
    return Path(root) / AGENT_BASE / ARCHIVE_REL


def _skill_slug(name: str) -> str:
    return safe_paths.safe_slug(name)


def _skill_dir(root: Path, name: str) -> Path:
    slug = _skill_slug(name)
    return safe_paths.contain(root, f"{SKILLS_REL}/{slug}", base=AGENT_BASE)


def _skill_md(root: Path, name: str) -> Path:
    slug = _skill_slug(name)
    return safe_paths.contain(root, f"{SKILLS_REL}/{slug}/{SKILL_FILE}", base=AGENT_BASE)


def _ledger_path(root: Path) -> Path:
    return Path(root) / SKILL_LEDGER_REL


def _write_contained(dst: Path, text: str) -> None:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(dst))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _scalar_yaml(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    s = str(value)
    needs_quote = (
        s == ""
        or s.strip() != s
        or any(c in s for c in (":", "#", "'", '"', "[", "]", "{", "}", "&", "*", "!", "|", ">"))
        or s.lower() in ("true", "false", "null", "yes", "no")
    )
    if needs_quote:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _render_frontmatter(fm: dict) -> str:
    out: list[str] = []
    for key, value in fm.items():
        if isinstance(value, list):
            out.append(f"{key}:")
            for item in value:
                out.append(f"  - {_scalar_yaml(item)}")
        else:
            out.append(f"{key}: {_scalar_yaml(value)}")
    return "\n".join(out)


def render_skill(fm: dict, body: str) -> str:
    return "---\n" + _render_frontmatter(fm) + "\n---\n\n" + body.strip() + "\n"


def split_skill(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ValueError("skill missing YAML frontmatter")
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError("skill frontmatter fence not closed")
    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip()
    try:
        fm = safe_load(fm_text) if fm_text.strip() else {}
    except UnsupportedYAML as exc:
        raise ValueError(f"skill frontmatter not in safe subset: {exc}") from exc
    if not isinstance(fm, dict):
        raise ValueError("skill frontmatter is not a mapping")
    return fm, body


def validate_skill_text(text: str, *, expected_name: Optional[str] = None) -> tuple[dict, str]:
    fm, body = split_skill(text)
    required = ("name", "description", "status", "sensitivity", "provenance", "created", "updated", "tags")
    errors: list[str] = []
    for key in required:
        value = fm.get(key)
        if value is None or value == "" or value == []:
            errors.append(f"missing required field {key!r}")
    if fm.get("status") not in STATUSES:
        errors.append(f"status must be one of {sorted(STATUSES)}")
    if fm.get("provenance") not in PROVENANCE:
        errors.append(f"provenance must be one of {sorted(PROVENANCE)}")
    if fm.get("sensitivity") not in SENSITIVITIES:
        errors.append(f"sensitivity must be one of {sorted(SENSITIVITIES)}")
    if not isinstance(fm.get("tags"), list):
        errors.append("tags must be a block list")
    name = str(fm.get("name", "")).strip()
    if name and safe_paths.safe_slug(name) != name:
        errors.append("name must already be a safe slug")
    if expected_name and name != _skill_slug(expected_name):
        errors.append(f"name {name!r} does not match package {_skill_slug(expected_name)!r}")
    if not body:
        errors.append("skill body must be non-empty")
    findings = secret_scan.scan_text(text)
    if findings:
        first = findings[0]
        errors.append(f"secret-like content detected ({first.get('pattern')} line {first.get('line')})")
    if errors:
        raise ValueError("; ".join(errors))
    return fm, body


def _append_event(
    root: Path,
    *,
    action: str,
    skill: str,
    actor: str = "",
    reason: str = "",
    path: str = "",
    extra: Optional[dict] = None,
) -> str:
    row = {
        "kind": "skill_event",
        "action": action,
        "skill": skill,
        "actor": actor or "",
        "reason": reason or "",
        "path": path or "",
    }
    if extra:
        row.update(extra)
    return ledger.append(_ledger_path(root), row, id_prefix="SKILL")


def list_skills(root: Path) -> list[dict]:
    base = _skills_dir(root)
    out: list[dict] = []
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith(".") or child.name.startswith("_"):
            continue
        md = child / SKILL_FILE
        if not md.exists():
            out.append({"name": child.name, "status": "invalid", "path": str(md), "error": "missing SKILL.md"})
            continue
        try:
            fm, _body = validate_skill_text(md.read_text(encoding="utf-8"), expected_name=child.name)
            out.append(
                {
                    "name": fm["name"],
                    "description": fm["description"],
                    "status": fm["status"],
                    "provenance": fm["provenance"],
                    "sensitivity": fm["sensitivity"],
                    "updated": fm["updated"],
                    "path": str(md),
                }
            )
        except ValueError as exc:
            out.append({"name": child.name, "status": "invalid", "path": str(md), "error": str(exc)})
    return out


def view_skill(root: Path, name: str, *, file_path: Optional[str] = None) -> str:
    slug = _skill_slug(name)
    if file_path:
        rel = f"{SKILLS_REL}/{slug}/{file_path}"
        p = safe_paths.contain(root, rel, base=AGENT_BASE)
        skill_root = _skill_dir(root, slug)
        if not safe_paths.is_within(skill_root, p):
            raise ValueError("requested file escapes skill package")
    else:
        p = _skill_md(root, slug)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"skill file not found: {p}")
    return p.read_text(encoding="utf-8")


def create_skill(
    root: Path,
    name: str,
    *,
    description: str,
    body: str,
    tags: Optional[list[str]] = None,
    sensitivity: str = "internal",
    provenance: str = "agent",
    actor: str = "",
    reason: str = "",
    now: Optional[datetime] = None,
) -> dict:
    root = Path(root)
    slug = _skill_slug(name)
    dst = _skill_md(root, slug)
    if dst.exists():
        raise FileExistsError(f"skill already exists: {slug}")
    today = _today(now)
    tag_list = ["skill"]
    for tag in tags or []:
        t = str(tag).strip()
        if t and t not in tag_list:
            tag_list.append(t)
    fm = {
        "name": slug,
        "description": str(description).strip(),
        "status": "active",
        "sensitivity": sensitivity,
        "provenance": provenance,
        "created": today,
        "updated": today,
        "tags": tag_list,
    }
    text = render_skill(fm, body)
    validate_skill_text(text, expected_name=slug)
    _write_contained(dst, text)
    drop_id = _append_event(
        root,
        action="create",
        skill=slug,
        actor=actor,
        reason=reason,
        path=str(dst.relative_to(root)),
        extra={"provenance": provenance, "sensitivity": sensitivity},
    )
    return {"drop_id": drop_id, "skill": slug, "path": str(dst)}


def patch_skill(
    root: Path,
    name: str,
    *,
    find: Optional[str] = None,
    replace: Optional[str] = None,
    append: Optional[str] = None,
    actor: str = "",
    reason: str = "",
    now: Optional[datetime] = None,
) -> dict:
    root = Path(root)
    slug = _skill_slug(name)
    p = _skill_md(root, slug)
    if not p.exists():
        raise FileNotFoundError(f"skill not found: {slug}")
    text = p.read_text(encoding="utf-8")
    fm, body = validate_skill_text(text, expected_name=slug)
    mode = ""
    if append is not None:
        body = body.rstrip() + "\n\n" + str(append).strip()
        mode = "append"
    else:
        if find is None or replace is None:
            raise ValueError("patch requires --append or both --find and --replace")
        if find not in body:
            raise ValueError("patch find text not present in skill body")
        body = body.replace(find, replace, 1)
        mode = "replace"
    fm["updated"] = _today(now)
    new_text = render_skill(fm, body)
    validate_skill_text(new_text, expected_name=slug)
    _write_contained(p, new_text)
    drop_id = _append_event(
        root,
        action="patch",
        skill=slug,
        actor=actor,
        reason=reason,
        path=str(p.relative_to(root)),
        extra={"mode": mode},
    )
    return {"drop_id": drop_id, "skill": slug, "path": str(p), "mode": mode}


def record_use(root: Path, name: str, *, actor: str = "", reason: str = "") -> dict:
    root = Path(root)
    slug = _skill_slug(name)
    p = _skill_md(root, slug)
    if not p.exists():
        raise FileNotFoundError(f"skill not found: {slug}")
    drop_id = _append_event(
        root,
        action="use",
        skill=slug,
        actor=actor,
        reason=reason,
        path=str(p.relative_to(root)),
    )
    return {"drop_id": drop_id, "skill": slug}


def archive_skill(root: Path, name: str, *, actor: str = "", reason: str = "") -> dict:
    root = Path(root)
    slug = _skill_slug(name)
    src = _skill_dir(root, slug)
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"skill not found: {slug}")
    md = src / SKILL_FILE
    if md.exists():
        text = md.read_text(encoding="utf-8")
        fm, body = validate_skill_text(text, expected_name=slug)
        fm["status"] = "archived"
        fm["updated"] = _today()
        _write_contained(md, render_skill(fm, body))
    archive_base = _archive_dir(root)
    archive_base.mkdir(parents=True, exist_ok=True)
    dst = safe_paths.contain(root, f"{ARCHIVE_REL}/{slug}-{_now_compact()}", base=AGENT_BASE)
    os.replace(str(src), str(dst))
    drop_id = _append_event(
        root,
        action="archive",
        skill=slug,
        actor=actor,
        reason=reason,
        path=str(dst.relative_to(root)),
    )
    return {"drop_id": drop_id, "skill": slug, "archive_path": str(dst)}


def report(root: Path) -> dict:
    rows, warnings = ledger.load(_ledger_path(root))
    by_skill: dict[str, dict] = {}
    for row in rows:
        skill = str(row.get("skill", ""))
        if not skill:
            continue
        rec = by_skill.setdefault(skill, {"skill": skill, "creates": 0, "patches": 0, "uses": 0, "archives": 0})
        action = row.get("action")
        if action == "create":
            rec["creates"] += 1
        elif action == "patch":
            rec["patches"] += 1
        elif action == "use":
            rec["uses"] += 1
        elif action == "archive":
            rec["archives"] += 1
    return {"skills": list_skills(root), "events": rows, "warnings": warnings, "by_skill": sorted(by_skill.values(), key=lambda r: r["skill"])}


def _read_body_arg(body: Optional[str], body_file: Optional[str]) -> str:
    if body_file:
        return Path(body_file).read_text(encoding="utf-8")
    return body or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills", description="managed oracle-local skills")
    parser.add_argument("--root", default=".", help="oracle root (default: .)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list skills")
    p_list.add_argument("--json", action="store_true")

    p_view = sub.add_parser("view", help="view a skill or package file")
    p_view.add_argument("name")
    p_view.add_argument("--file")

    p_create = sub.add_parser("create", help="create a managed skill")
    p_create.add_argument("name")
    p_create.add_argument("--description", required=True)
    p_create.add_argument("--body")
    p_create.add_argument("--body-file")
    p_create.add_argument("--tag", action="append", default=[])
    p_create.add_argument("--sensitivity", default="internal")
    p_create.add_argument("--provenance", default="agent")
    p_create.add_argument("--actor", default="")
    p_create.add_argument("--reason", default="")
    p_create.add_argument("--json", action="store_true")

    p_patch = sub.add_parser("patch", help="patch a managed skill")
    p_patch.add_argument("name")
    p_patch.add_argument("--find")
    p_patch.add_argument("--replace")
    p_patch.add_argument("--append")
    p_patch.add_argument("--actor", default="")
    p_patch.add_argument("--reason", default="")
    p_patch.add_argument("--json", action="store_true")

    p_use = sub.add_parser("record-use", help="record that a skill was used")
    p_use.add_argument("name")
    p_use.add_argument("--actor", default="")
    p_use.add_argument("--reason", default="")
    p_use.add_argument("--json", action="store_true")

    p_archive = sub.add_parser("archive", help="archive a skill package")
    p_archive.add_argument("name")
    p_archive.add_argument("--actor", default="")
    p_archive.add_argument("--reason", default="")
    p_archive.add_argument("--json", action="store_true")

    p_report = sub.add_parser("report", help="show skill ledger report")
    p_report.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.cmd == "list":
            data = list_skills(root)
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print(f"skills: {len(data)}")
                for item in data:
                    desc = item.get("description") or item.get("error", "")
                    print(f"  {item.get('name'):<28} {item.get('status'):<8} {desc}")
            return 0
        if args.cmd == "view":
            sys.stdout.write(view_skill(root, args.name, file_path=args.file))
            return 0
        if args.cmd == "create":
            data = create_skill(
                root,
                args.name,
                description=args.description,
                body=_read_body_arg(args.body, args.body_file),
                tags=args.tag,
                sensitivity=args.sensitivity,
                provenance=args.provenance,
                actor=args.actor,
                reason=args.reason,
            )
        elif args.cmd == "patch":
            data = patch_skill(
                root,
                args.name,
                find=args.find,
                replace=args.replace,
                append=args.append,
                actor=args.actor,
                reason=args.reason,
            )
        elif args.cmd == "record-use":
            data = record_use(root, args.name, actor=args.actor, reason=args.reason)
        elif args.cmd == "archive":
            data = archive_skill(root, args.name, actor=args.actor, reason=args.reason)
        elif args.cmd == "report":
            data = report(root)
        else:  # pragma: no cover
            return 2
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2, default=str))
        else:
            print(json.dumps(data, default=str))
        return 0
    except Exception as exc:
        print(f"skills: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
