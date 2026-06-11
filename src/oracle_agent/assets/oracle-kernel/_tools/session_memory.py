#!/usr/bin/env python3
"""session_memory.py -- session capture, decomposition, and dreaming.

Oracle should not merely remember that a session happened. A material session is
an intake stream: it may contain evidence, reusable business claims, open
questions, contradictions, retrieval strategies, skill usage, and procedural
signals. This module records the session as Meta memory, then decomposes it into
the existing Oracle behavioral homes:

* ``Meta.nosync/Sessions/`` gets the episodic session record.
* ``Memory.nosync/Findings/`` gets review-gated, evidence-linked candidate
  business claims.
* ``Memory.nosync/Questions/`` gets unresolved questions.
* ``Memory.nosync/Contradictions/`` gets possible conflicts.
* ``Memory.nosync/Queries/`` and ``Meta.nosync/Improvements/`` get retrieval and
  procedural candidates when a session teaches how future context should be
  assembled.
* ``_data.nosync/derived/mempalace`` and ``_data.nosync/derived/graphify`` get
  rebuildable derived recall/graph files with pointers back to canonical Oracle
  records. These files never become answer authority.

The decomposition is deterministic and stdlib-only. It does not infer rich
business facts from raw transcript text on its own; callers/agents pass the
structured claims/questions/conflicts they observed, and the daily dreaming loop
matriculates those into the right canonical candidate stores.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - exercised in flat and package contexts
    import ledger
    import policy
    import safe_paths
except Exception:  # pragma: no cover
    from . import ledger  # type: ignore
    from . import policy  # type: ignore
    from . import safe_paths  # type: ignore


META_BASE = "Meta.nosync"
MEMORY_BASE = "Memory.nosync"
SESSION_LEDGER = "Meta.nosync/ledgers/session_memory.jsonl"
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted", "secret"]
_SENS_RANK = {s: i for i, s in enumerate(SENSITIVITY_ORDER)}
_STRICTEST = len(SENSITIVITY_ORDER) - 1


def _now_default() -> datetime:
    return datetime.now()


def _now_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _today_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _ledger_path(root: Path) -> Path:
    return Path(root) / SESSION_LEDGER


def _sens_rank(label: Optional[str]) -> int:
    if label is None:
        return _STRICTEST
    return _SENS_RANK.get(str(label).strip().lower(), _STRICTEST)


def _valid_sensitivity(label: str) -> str:
    value = str(label or "internal").strip().lower()
    return value if value in _SENS_RANK else "internal"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, tuple):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _append_unique(base: list[str], *items: str) -> list[str]:
    out: list[str] = []
    for item in [*base, *items]:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def _yaml_scalar(value: Any) -> str:
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
        or re.match(r"^\d{4}-\d{2}-\d{2}", s) is not None
        or any(c in s for c in (":", "#", "'", '"', "[", "]", "{", "}", "&", "*", "!", "|", ">", "\n"))
        or s.lower() in ("true", "false", "null", "yes", "no")
        or (s and s[0] in "-?@`%")
    )
    if needs_quote:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s


def _frontmatter_lines(fm: dict) -> list[str]:
    lines: list[str] = []
    for key, value in fm.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {_yaml_scalar(item)}")
            else:
                lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    return lines


def _render_note(fm: dict, body: str) -> str:
    return "\n".join(["---", *_frontmatter_lines(fm), "---", "", body.rstrip(), ""])


def _write_note(root: Path, *, base: str, rel: str, fm: dict, body: str) -> str:
    dst = safe_paths.contain(root, rel, base=base)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(_render_note(fm, body), encoding="utf-8")  # safe_paths-internal: dst from contain()
    return _rel(root, dst)


def _rel(root: Path, path: Path | str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except Exception:
        return str(path)


def _body_section(title: str, values: list[str]) -> list[str]:
    lines = [f"## {title}", ""]
    if values:
        lines.extend(f"- {v}" for v in values)
    else:
        lines.append("_(none recorded)_")
    lines.append("")
    return lines


def _session_body(row: dict) -> str:
    lines = [
        f"# Session {row['drop_id']}",
        "",
        "## User request",
        "",
        str(row.get("user_request") or "_(not recorded)_"),
        "",
        "## Answer or work performed",
        "",
        str(row.get("answer_summary") or "_(not recorded)_"),
        "",
    ]
    for key, title in (
        ("business_objects", "Business objects"),
        ("source_ids", "Sources touched"),
        ("remote_datasets", "Remote datasets queried"),
        ("skills", "Skills loaded"),
        ("tools", "Tools used"),
        ("queries", "Queries or retrieval strategies"),
        ("learned_claims", "Learned business claims"),
        ("open_questions", "Open questions"),
        ("contradictions", "Possible contradictions"),
        ("recommendations", "Recommendations"),
        ("decisions", "Decisions"),
    ):
        lines.extend(_body_section(title, _as_list(row.get(key))))
    if row.get("latency_ms") not in (None, ""):
        lines.extend(["## Efficiency telemetry", "", f"- latency_ms: {row['latency_ms']}", ""])
    return "\n".join(lines).rstrip() + "\n"


def capture_session(
    root: Path,
    *,
    user_request: str,
    answer_summary: str = "",
    business_objects: Optional[list[str]] = None,
    source_ids: Optional[list[str]] = None,
    skills: Optional[list[str]] = None,
    tools: Optional[list[str]] = None,
    queries: Optional[list[str]] = None,
    learned_claims: Optional[list[str]] = None,
    open_questions: Optional[list[str]] = None,
    contradictions: Optional[list[str]] = None,
    recommendations: Optional[list[str]] = None,
    decisions: Optional[list[str]] = None,
    remote_datasets: Optional[list[str]] = None,
    latency_ms: Optional[int] = None,
    sensitivity: str = "internal",
    actor: str = "",
    role: str = "unknown",
    now: Optional[datetime] = None,
) -> dict:
    """Capture one material session as structured Meta memory.

    ``role`` is pure attribution (P5S-13): it is recorded on the ledger row and
    the session note's frontmatter so audit names *who* under *what* role, but
    it NEVER changes what this verb does -- capture is role-invariant. The
    ``"unknown"`` default is reserved for bare kernel-CLI writes; the shell
    surfaces (gateway, ``oracle chat``) always pass an explicitly resolved role
    (P5S-14).
    """
    root = Path(root)
    now = now or _now_default()
    request = str(user_request or "").strip()
    if not request:
        raise ValueError("session capture requires --user-request")
    sens = _valid_sensitivity(sensitivity)
    row = {
        "action": "capture",
        "kind": "session",
        "ts": _now_iso(now),
        "user_request": request,
        "answer_summary": str(answer_summary or "").strip(),
        "business_objects": _as_list(business_objects),
        "source_ids": _as_list(source_ids),
        "skills": _as_list(skills),
        "tools": _as_list(tools),
        "queries": _as_list(queries),
        "learned_claims": _as_list(learned_claims),
        "open_questions": _as_list(open_questions),
        "contradictions": _as_list(contradictions),
        "recommendations": _as_list(recommendations),
        "decisions": _as_list(decisions),
        "remote_datasets": _as_list(remote_datasets),
        "latency_ms": latency_ms,
        "sensitivity": sens,
        "actor": actor or "",
        "role": str(role or "unknown"),
        "status": "captured",
    }
    session_id = ledger.append(_ledger_path(root), row, id_prefix="SES")
    row["drop_id"] = session_id

    title = request[:72].rstrip() or f"Session {session_id}"
    fm = {
        "id": session_id,
        "type": "session",
        "title": f"Session: {title}",
        "created": _today_str(now),
        "updated": _today_str(now),
        "sensitivity": sens,
        "status": "captured",
        "tags": ["meta", "session", "dreaming"],
        "actor": actor or "",
        "role": str(role or "unknown"),
        "session_drop_id": session_id,
        "business_objects": row["business_objects"],
        "source_ids": row["source_ids"],
        "skills": row["skills"],
        "tools": row["tools"],
    }
    rel = _write_note(
        root,
        base=META_BASE,
        rel=f"Sessions/{_today_str(now)}_{safe_paths.safe_slug(session_id)}.md",
        fm=fm,
        body=_session_body(row),
    )
    return {
        "session_id": session_id,
        "note_path": rel,
        "ledger": SESSION_LEDGER,
        "status": "captured",
    }


def _load_capture_rows(root: Path, rows: Optional[list[dict]] = None) -> list[dict]:
    if rows is None:
        rows, _warnings = ledger.load(_ledger_path(root))
    out = [r for r in rows if r.get("action") == "capture" and r.get("kind") == "session"]
    out.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("drop_id", ""))))
    return out


def _processed_ids(root: Path, rows: Optional[list[dict]] = None) -> set[str]:
    if rows is None:
        rows, _warnings = ledger.load(_ledger_path(root))
    return {
        str(r.get("session_id", "")).strip()
        for r in rows
        if r.get("action") == "decompose" and str(r.get("session_id", "")).strip()
    }


def list_sessions(root: Path, *, pending: bool = False) -> list[dict]:
    captures = _load_capture_rows(Path(root))
    if not pending:
        return captures
    done = _processed_ids(Path(root))
    return [r for r in captures if str(r.get("drop_id", "")) not in done]


def _find_capture(root: Path, session_id: str) -> Optional[dict]:
    sid = str(session_id or "").strip()
    for row in _load_capture_rows(root):
        if str(row.get("drop_id", "")).strip() == sid:
            return row
    return None


def _mint_record_id(prefix: str, now: datetime, session_id: str, index: int) -> str:
    session_slug = safe_paths.safe_slug(session_id)
    return f"{prefix}-{now.strftime('%Y%m%d')}-{session_slug}-{index:03d}"


def _write_finding(root: Path, session: dict, claim: str, index: int, now: datetime) -> str:
    rec_id = _mint_record_id("FND-SESSION", now, str(session.get("drop_id", "session")), index)
    source_ids = _as_list(session.get("source_ids"))
    evidence_source = source_ids[0] if source_ids else str(session.get("drop_id", ""))
    fm = {
        "id": rec_id,
        "type": "finding",
        "title": claim[:96].rstrip() or "Session-derived finding",
        "created": _today_str(now),
        "updated": _today_str(now),
        "sensitivity": _valid_sensitivity(session.get("sensitivity", "internal")),
        "status": "needs_review",
        "tags": ["finding", "session-derived", "needs-review"],
        "claim_tier": "OBS",
        "confidence": 0.4,
        "evidence": f"Session {session.get('drop_id')} captured this claim; source_ids={', '.join(source_ids) or 'session testimony only'}",
        "decision_relevance": "Session-derived business memory; review before relying on it for decisions.",
        "disconfirmer": "Authoritative source review, admin correction, or later contradiction rejects this session-derived claim.",
        "as_of": _today_str(now),
        "source_id": evidence_source,
        "evidence_offsets": f"session:{session.get('drop_id')}",
    }
    body = "\n".join([
        f"# {fm['title']}",
        "",
        "## Claim",
        "",
        claim,
        "",
        "## Evidence",
        "",
        str(fm["evidence"]),
        "",
        "## Review gate",
        "",
        "This finding is session-derived and remains `needs_review` until promoted by a reviewer.",
    ])
    return _write_note(
        root,
        base=MEMORY_BASE,
        rel=f"Findings/{_today_str(now)}_{safe_paths.safe_slug(rec_id)}.md",
        fm=fm,
        body=body,
    )


def _write_question(root: Path, session: dict, question: str, index: int, now: datetime) -> str:
    rec_id = _mint_record_id("QST-SESSION", now, str(session.get("drop_id", "session")), index)
    fm = {
        "id": rec_id,
        "type": "question",
        "title": question[:96].rstrip() or "Session-derived question",
        "created": _today_str(now),
        "updated": _today_str(now),
        "sensitivity": _valid_sensitivity(session.get("sensitivity", "internal")),
        "status": "open",
        "tags": ["question", "session-derived"],
        "source_session": str(session.get("drop_id", "")),
    }
    body = "\n".join([
        f"# {fm['title']}",
        "",
        "## The question",
        "",
        question,
        "",
        "## Why it matters",
        "",
        "Captured during a material session; resolve with authoritative sources before relying on the answer.",
        "",
        "## Candidate sources",
        "",
        *(f"- {s}" for s in (_as_list(session.get("source_ids")) or ["_(not identified)_"])),
    ])
    return _write_note(
        root,
        base=MEMORY_BASE,
        rel=f"Questions/{_today_str(now)}_{safe_paths.safe_slug(rec_id)}.md",
        fm=fm,
        body=body,
    )


def _write_contradiction(root: Path, session: dict, conflict: str, index: int, now: datetime) -> str:
    rec_id = _mint_record_id("CTR-SESSION", now, str(session.get("drop_id", "session")), index)
    fm = {
        "id": rec_id,
        "type": "contradiction",
        "title": conflict[:96].rstrip() or "Session-derived contradiction",
        "created": _today_str(now),
        "updated": _today_str(now),
        "sensitivity": _valid_sensitivity(session.get("sensitivity", "internal")),
        "status": "open",
        "tags": ["contradiction", "session-derived", "needs-review"],
        "severity": "medium",
        "classification": "watch",
        "claims_in_conflict": conflict,
        "possible_causes": "Session memory surfaced a possible mismatch; source authority and definitions may differ.",
        "resolution_plan": "Review cited sources or create a resolving Source/Finding; reclassify if decision-relevant.",
        "resolving_source": "",
        "decision_relevance": "Unknown until reviewed; caveat material answers if this touches the answer object.",
        "source_session": str(session.get("drop_id", "")),
    }
    body = "\n".join([
        f"# {fm['title']}",
        "",
        "## Claims in conflict",
        "",
        conflict,
        "",
        "## Review gate",
        "",
        "This contradiction was decomposed from session memory and needs review.",
    ])
    return _write_note(
        root,
        base=MEMORY_BASE,
        rel=f"Contradictions/{_today_str(now)}_{safe_paths.safe_slug(rec_id)}.md",
        fm=fm,
        body=body,
    )


def _write_query_note(root: Path, session: dict, now: datetime) -> Optional[str]:
    queries = _as_list(session.get("queries"))
    if not queries:
        return None
    rec_id = f"QRY-SESSION-{now.strftime('%Y%m%d')}-{safe_paths.safe_slug(str(session.get('drop_id', 'session')))}"
    fm = {
        "id": rec_id,
        "type": "query",
        "title": f"Session retrieval strategy {session.get('drop_id')}",
        "created": _today_str(now),
        "updated": _today_str(now),
        "sensitivity": _valid_sensitivity(session.get("sensitivity", "internal")),
        "status": "needs_review",
        "tags": ["query", "session-derived", "retrieval-memory"],
        "source_session": str(session.get("drop_id", "")),
    }
    body = "\n".join([
        f"# {fm['title']}",
        "",
        "## Queries or retrieval strategies",
        "",
        *(f"- {q}" for q in queries),
        "",
        "## Scope",
        "",
        "Session-derived retrieval memory. Review before promoting to a stable reusable query.",
    ])
    return _write_note(
        root,
        base=MEMORY_BASE,
        rel=f"Queries/{_today_str(now)}_{safe_paths.safe_slug(rec_id)}.md",
        fm=fm,
        body=body,
    )


def _write_procedural_improvement(root: Path, session: dict, now: datetime) -> Optional[str]:
    signals = _as_list(session.get("skills")) + _as_list(session.get("tools")) + _as_list(session.get("remote_datasets"))
    if not signals:
        return None
    rec_id = f"IMP-SESSION-{now.strftime('%Y%m%d')}-{safe_paths.safe_slug(str(session.get('drop_id', 'session')))}"
    fm = {
        "id": rec_id,
        "type": "improvement",
        "title": f"Session procedural memory {session.get('drop_id')}",
        "created": _today_str(now),
        "updated": _today_str(now),
        "sensitivity": _valid_sensitivity(session.get("sensitivity", "internal")),
        "status": "needs_review",
        "tags": ["meta", "improvement", "session-derived", "procedural-memory"],
        "source_session": str(session.get("drop_id", "")),
    }
    body = "\n".join([
        f"# {fm['title']}",
        "",
        "## Procedural signals",
        "",
        *(f"- {s}" for s in signals),
        "",
        "## Review standard",
        "",
        "Promote only durable procedure into AgentResources.nosync/Skills; keep business facts in Memory.nosync.",
    ])
    return _write_note(
        root,
        base=META_BASE,
        rel=f"Improvements/{_today_str(now)}_{safe_paths.safe_slug(rec_id)}.md",
        fm=fm,
        body=body,
    )


def decompose_session(
    root: Path,
    session_id: str,
    *,
    force: bool = False,
    now: Optional[datetime] = None,
    capture: Optional[dict] = None,
) -> dict:
    """Matriculate one captured session into canonical candidate records.

    ``capture`` lets a batch caller (``dream``) hand in the already-loaded
    capture row, so a pass over N pending sessions does not re-read the whole
    ledger N more times; that caller owns the already-decomposed check.
    """
    root = Path(root)
    now = now or _now_default()
    session = capture if capture is not None else _find_capture(root, session_id)
    if session is None:
        raise ValueError(f"session not found: {session_id}")
    if capture is None:
        done = _processed_ids(root)
        if str(session_id) in done and not force:
            return {"session_id": session_id, "status": "already-decomposed", "created": {}}

    created: dict[str, list[str]] = {
        "findings": [],
        "questions": [],
        "contradictions": [],
        "queries": [],
        "improvements": [],
    }
    for i, claim in enumerate(_as_list(session.get("learned_claims")), start=1):
        created["findings"].append(_write_finding(root, session, claim, i, now))
    for i, question in enumerate(_as_list(session.get("open_questions")), start=1):
        created["questions"].append(_write_question(root, session, question, i, now))
    for i, conflict in enumerate(_as_list(session.get("contradictions")), start=1):
        created["contradictions"].append(_write_contradiction(root, session, conflict, i, now))
    query_path = _write_query_note(root, session, now)
    if query_path:
        created["queries"].append(query_path)
    improvement_path = _write_procedural_improvement(root, session, now)
    if improvement_path:
        created["improvements"].append(improvement_path)

    row = {
        "action": "decompose",
        "kind": "session_decomposition",
        "session_id": session_id,
        "created": created,
        "created_counts": {k: len(v) for k, v in created.items()},
        "status": "ok",
        "ts": _now_iso(now),
    }
    drop_id = ledger.append(_ledger_path(root), row, id_prefix="SDC")
    return {"session_id": session_id, "drop_id": drop_id, "status": "ok", "created": created}


def _session_text(row: dict) -> str:
    parts = [
        f"Session {row.get('drop_id')}",
        f"Request: {row.get('user_request', '')}",
        f"Answer: {row.get('answer_summary', '')}",
    ]
    for key in ("business_objects", "learned_claims", "open_questions", "contradictions", "queries", "skills", "tools"):
        vals = _as_list(row.get(key))
        if vals:
            parts.append(f"{key}: " + "; ".join(vals))
    return "\n".join(parts).strip()


def _policy_verdict(sensitivity: str, environment: str) -> str:
    try:
        return str(policy.check_processing(sensitivity, environment))
    except Exception:
        return "deny"


def _minimize(text: str, chars: int = 700) -> str:
    if len(text) <= chars:
        return text
    return text[:chars].rstrip() + "\n\n[...minimized by Oracle session_memory policy...]"


def _derived_file(root: Path, rel: str) -> Path:
    dst = safe_paths.contain(root, rel, base="_data.nosync")
    dst.parent.mkdir(parents=True, exist_ok=True)
    return dst


_MANIFEST_REL = "derived/session-memory/manifest.json"


def _load_manifest(root: Path) -> Optional[dict]:
    """Last export manifest, or None when absent/unreadable."""
    try:
        path = safe_paths.contain(Path(root), _MANIFEST_REL, base="_data.nosync")
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def export_derived(
    root: Path,
    *,
    max_sensitivity: str = "internal",
    environment: str = "local_deterministic",
    now: Optional[datetime] = None,
    captures: Optional[list[dict]] = None,
) -> dict:
    """Write MemPalace/Graphify-ready derived session memory files."""
    root = Path(root)
    now = now or _now_default()
    ceiling = _valid_sensitivity(max_sensitivity)
    all_captures = captures if captures is not None else _load_capture_rows(root)
    sessions = []
    verdict_counts = {"allow": 0, "allow-minimized": 0, "deny": 0}
    for row in all_captures:
        sens = _valid_sensitivity(row.get("sensitivity", "internal"))
        if _sens_rank(sens) > _sens_rank(ceiling):
            continue
        verdict = _policy_verdict(sens, environment)
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        if verdict == "deny":
            continue
        text = _session_text(row)
        if verdict == "allow-minimized":
            text = _minimize(text)
        item = dict(row)
        item["text"] = text
        item["canonical_authority"] = "oracle"
        item["answer_authority"] = "never"
        sessions.append(item)

    nodes: list[dict] = []
    edges: list[dict] = []
    for row in sessions:
        sid = str(row.get("drop_id", ""))
        nodes.append({
            "id": f"session:{sid}",
            "type": "Session",
            "label": sid,
            "sensitivity": row.get("sensitivity", "internal"),
            "canonical_authority": "oracle",
            "answer_authority": "never",
        })
        for bo in _as_list(row.get("business_objects")):
            bid = "business_object:" + safe_paths.safe_slug(bo)
            nodes.append({"id": bid, "type": "BusinessObject", "label": bo})
            edges.append({"from": f"session:{sid}", "to": bid, "type": "asked_about"})
        for source in _as_list(row.get("source_ids")):
            tid = "source:" + source
            nodes.append({"id": tid, "type": "Source", "label": source})
            edges.append({"from": f"session:{sid}", "to": tid, "type": "used_source"})
        for skill in _as_list(row.get("skills")):
            tid = "skill:" + safe_paths.safe_slug(skill)
            nodes.append({"id": tid, "type": "Skill", "label": skill})
            edges.append({"from": f"session:{sid}", "to": tid, "type": "loaded_skill"})
        for tool in _as_list(row.get("tools")):
            tid = "tool:" + safe_paths.safe_slug(tool)
            nodes.append({"id": tid, "type": "Tool", "label": tool})
            edges.append({"from": f"session:{sid}", "to": tid, "type": "used_tool"})
        for query in _as_list(row.get("queries")):
            tid = "query:" + safe_paths.safe_slug(query)[:96]
            nodes.append({"id": tid, "type": "Query", "label": query})
            edges.append({"from": f"session:{sid}", "to": tid, "type": "queried_with"})

    # De-duplicate nodes by id, preserving first occurrence.
    seen_nodes: set[str] = set()
    unique_nodes: list[dict] = []
    for node in nodes:
        nid = str(node.get("id", ""))
        if nid and nid not in seen_nodes:
            seen_nodes.add(nid)
            unique_nodes.append(node)

    mem_md = _derived_file(root, "derived/mempalace/raw/oracle-session-memory.md")
    mem_jsonl = _derived_file(root, "derived/mempalace/raw/oracle-session-memory.jsonl")
    graph_nodes = _derived_file(root, "derived/graphify/raw/oracle-session-graph-nodes.jsonl")
    graph_edges = _derived_file(root, "derived/graphify/raw/oracle-session-graph-edges.jsonl")
    manifest_path = _derived_file(root, _MANIFEST_REL)

    md_lines = [
        "# Oracle Session Memory",
        "",
        "Derived from Meta.nosync/Sessions and session_memory.jsonl.",
        "This is a derived recall artifact, not answer authority.",
        f"Generated: {_now_iso(now)}",
        "",
    ]
    for row in sessions:
        md_lines.extend([f"## {row.get('drop_id')}", "", str(row.get("text", "")).strip(), ""])
    mem_md.write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")  # safe_paths-internal: _derived_file → contain()
    mem_jsonl.write_text(  # safe_paths-internal: _derived_file → contain()
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in sessions)
        + ("\n" if sessions else ""),
        encoding="utf-8",
    )
    graph_nodes.write_text(  # safe_paths-internal: _derived_file → contain()
        "\n".join(json.dumps(n, ensure_ascii=False, sort_keys=True) for n in unique_nodes)
        + ("\n" if unique_nodes else ""),
        encoding="utf-8",
    )
    graph_edges.write_text(  # safe_paths-internal: _derived_file → contain()
        "\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in edges)
        + ("\n" if edges else ""),
        encoding="utf-8",
    )
    manifest = {
        "contract_version": "oracle.session_memory.v1",
        "prepared_at": _now_iso(now),
        "canonical_authority": "oracle",
        "answer_authority": "never",
        "artifact_scope": "derived_rebuildable",
        "max_sensitivity": ceiling,
        "environment": environment,
        "capture_count": len(all_captures),
        "session_count": len(sessions),
        "node_count": len(unique_nodes),
        "edge_count": len(edges),
        "verdict_counts": verdict_counts,
        "files": [
            _rel(root, mem_md),
            _rel(root, mem_jsonl),
            _rel(root, graph_nodes),
            _rel(root, graph_edges),
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")  # safe_paths-internal: _derived_file → contain()
    drop_id = ledger.append(
        _ledger_path(root),
        {"action": "export-derived", "kind": "session_memory_export", **manifest},
        id_prefix="SMX",
    )
    manifest["ledger_id"] = drop_id
    return manifest


def dream(
    root: Path,
    *,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Daily dreaming pass: decompose pending sessions and refresh derived memory.

    The derived recall files cover every capture ever made, so rendering them
    is O(total sessions). A checkpoint that decomposed nothing skips the
    export when the manifest already covers the current capture count; the
    ``export-derived`` CLI verb still forces a full refresh.
    """
    root = Path(root)
    now = now or _now_default()
    rows, _warnings = ledger.load(_ledger_path(root))
    captures = _load_capture_rows(root, rows)
    done = _processed_ids(root, rows)
    pending = [r for r in captures if str(r.get("drop_id", "")) not in done]
    if limit is not None:
        pending = pending[: max(0, int(limit))]
    decomposed = []
    errors = []
    for row in pending:
        sid = str(row.get("drop_id", ""))
        try:
            decomposed.append(decompose_session(root, sid, now=now, capture=row))
        except Exception as exc:
            errors.append({"session_id": sid, "error": f"{type(exc).__name__}: {exc}"})
    manifest = _load_manifest(root)
    export_current = (
        not decomposed
        and not errors
        and manifest is not None
        and manifest.get("capture_count") == len(captures)
    )
    if export_current:
        derived = dict(manifest)
        derived["skipped"] = True
    else:
        derived = export_derived(root, now=now, captures=captures)
    status = "ok" if not errors else "partial"
    drop_id = ledger.append(
        _ledger_path(root),
        {
            "action": "dream",
            "kind": "memory_dreaming",
            "status": status,
            "processed": len(decomposed),
            "errors": errors,
            "derived": {
                "session_count": derived.get("session_count", 0),
                "node_count": derived.get("node_count", 0),
                "edge_count": derived.get("edge_count", 0),
                "skipped": bool(derived.get("skipped")),
            },
            "ts": _now_iso(now),
        },
        id_prefix="DRM",
    )
    return {
        "status": status,
        "drop_id": drop_id,
        "processed": len(decomposed),
        "errors": errors,
        "derived": derived,
    }


def run_memory_dreaming_loop(root, loop, ctx) -> dict:
    """Loop runner contract: ``fn(root, loop, ctx)`` for loops.py."""
    now = ctx.get("now") if isinstance(ctx, dict) else None
    result = dream(Path(root), now=now if isinstance(now, datetime) else None)
    return {
        "status": "ok" if result["status"] == "ok" else "fail",
        "health_signal": "healthy" if result["status"] == "ok" else "degraded",
        "processed": result["processed"],
        "errors": result["errors"],
        "derived": result["derived"],
    }


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Capture and decompose Oracle session memory")
    parser.add_argument("--root", default=".", help="oracle root")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cap = sub.add_parser("capture", help="capture a material session")
    p_cap.add_argument("--user-request", required=True)
    p_cap.add_argument("--answer-summary", default="")
    p_cap.add_argument("--business-object", action="append", default=[])
    p_cap.add_argument("--source-id", action="append", default=[])
    p_cap.add_argument("--skill", action="append", default=[])
    p_cap.add_argument("--tool", action="append", default=[])
    p_cap.add_argument("--query", action="append", default=[])
    p_cap.add_argument("--learned-claim", action="append", default=[])
    p_cap.add_argument("--open-question", action="append", default=[])
    p_cap.add_argument("--contradiction", action="append", default=[])
    p_cap.add_argument("--recommendation", action="append", default=[])
    p_cap.add_argument("--decision", action="append", default=[])
    p_cap.add_argument("--remote-dataset", action="append", default=[])
    p_cap.add_argument("--latency-ms", type=int)
    p_cap.add_argument("--sensitivity", default="internal", choices=SENSITIVITY_ORDER)
    p_cap.add_argument("--actor", default="")
    p_cap.add_argument(
        "--role", default="unknown",
        help="attribution only -- recorded, never gates this verb (P5S-13). "
             "'unknown' is reserved for bare kernel-CLI writes (P5S-14).",
    )
    p_cap.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="list captured sessions")
    p_list.add_argument("--pending", action="store_true")
    p_list.add_argument("--json", action="store_true")

    p_dec = sub.add_parser("decompose", help="decompose one captured session")
    p_dec.add_argument("session_id")
    p_dec.add_argument("--force", action="store_true")
    p_dec.add_argument("--json", action="store_true")

    p_dream = sub.add_parser("dream", help="decompose pending sessions and refresh derived memory")
    p_dream.add_argument("--limit", type=int)
    p_dream.add_argument("--json", action="store_true")

    p_exp = sub.add_parser("export-derived", help="refresh derived session memory only")
    p_exp.add_argument("--max-sensitivity", default="internal", choices=SENSITIVITY_ORDER)
    p_exp.add_argument("--environment", default="local_deterministic", choices=policy.ENVIRONMENTS)
    p_exp.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.cmd == "capture":
            out = capture_session(
                root,
                user_request=args.user_request,
                answer_summary=args.answer_summary,
                business_objects=args.business_object,
                source_ids=args.source_id,
                skills=args.skill,
                tools=args.tool,
                queries=args.query,
                learned_claims=args.learned_claim,
                open_questions=args.open_question,
                contradictions=args.contradiction,
                recommendations=args.recommendation,
                decisions=args.decision,
                remote_datasets=args.remote_dataset,
                latency_ms=args.latency_ms,
                sensitivity=args.sensitivity,
                actor=args.actor,
                role=args.role,
            )
            if args.json:
                _print_json(out)
            else:
                print(f"session: {out['session_id']} -> {out['note_path']}")
            return 0
        if args.cmd == "list":
            out = list_sessions(root, pending=args.pending)
            if args.json:
                _print_json(out)
            else:
                for row in out:
                    print(f"{row.get('drop_id')} {row.get('ts')} {row.get('user_request')}")
            return 0
        if args.cmd == "decompose":
            out = decompose_session(root, args.session_id, force=args.force)
            if args.json:
                _print_json(out)
            else:
                print(f"decomposed {args.session_id}: {out['status']}")
            return 0
        if args.cmd == "dream":
            out = dream(root, limit=args.limit)
            if args.json:
                _print_json(out)
            else:
                print(f"dream: {out['status']} processed={out['processed']}")
            return 0 if out["status"] == "ok" else 1
        if args.cmd == "export-derived":
            out = export_derived(
                root,
                max_sensitivity=args.max_sensitivity,
                environment=args.environment,
            )
            if args.json:
                _print_json(out)
            else:
                print(f"exported session memory: sessions={out['session_count']} nodes={out['node_count']}")
            return 0
    except (ValueError, OSError) as exc:
        print(f"session-memory: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
