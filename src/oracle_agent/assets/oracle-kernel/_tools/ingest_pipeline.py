#!/usr/bin/env python3
"""ingest_pipeline.py -- the knowledge ingestion orchestrator (stdlib-only).

Pipeline contract (references/knowledge-pipeline.md):

    extract  ->  chunk  ->  index  ->  source_record  ->  derive  ->  classify

This module is the conductor: it wires the engine stages together, routes every
filesystem write through ``safe_paths``, classifies intake sensitivity AT LOG
TIME, and -- critically -- NEVER destroys the input. The original file in
``_INPUT`` is read, never moved or truncated by this module; promotion of the
raw bytes into a lane (with ``safe_copy_verify_delete``) is the job of
``artifact_io.ingest``, not the pipeline.

Stage ownership / coupling:
  * ``chunker``        -- sibling in THIS unit; imported normally.
  * ``intake_classify``-- sibling in THIS unit; imported normally.
  * ``extractors``     -- sibling engine package; LAZY. A built-in stdlib
                          extractor for txt/md/json/csv/tsv/html is used as a
                          fallback so the pipeline produces chunks even before
                          the full extractor registry lands.
  * ``knowledge_index``-- sibling engine module; LAZY + optional. Indexing is
                          skipped (recorded as ``skipped``) if absent.
  * ``source_record``  -- sibling engine module; LAZY + optional. If absent, the
                          pipeline still emits a source-record-SHAPED dict (so a
                          caller/test sees the provenance card) but marks it
                          ``persisted: false``.
  * ``derive``         -- sibling engine module; LAZY + optional. Skipped if
                          absent.
  * ``policy``         -- floor module; LAZY. Used to confirm the intake
                          sensitivity is processable in the current environment.

Because every cross-unit dependency is optional, ``run`` degrades to a clean,
useful result (extract+chunk+classify+source-card) on a bare floor, and lights
up index/persist/derive automatically as those siblings appear.

Public API:
    PipelineResult            -- result record (dict-able)
    run(root, file, *, lane=None, connector=None, sensitivity=None,
        admin_override=None, derive=False, index=True, actor=None) -> dict

Stdlib only.
"""
from __future__ import annotations

import hashlib
import html.parser
import io
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import chunker
import intake_classify

__all__ = ["run", "run_batch", "stage_external", "iter_ingestable", "extract_text", "PipelineResult"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:  # read-only source; not a write target
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def _is_within_input(root: Path, candidate: Path) -> bool:
    """True iff ``candidate`` resolves inside ``root/Workproduct.nosync/_INPUT``.

    Uses the floor ``safe_paths.is_within`` so containment logic is not
    re-implemented here.
    """
    try:
        import safe_paths
    except Exception:  # pragma: no cover - safe_paths is floor, always present
        return False
    input_root = Path(root) / "Workproduct.nosync" / "_INPUT"
    return safe_paths.is_within(input_root, candidate)


# --------------------------------------------------------------------------- #
# built-in stdlib extraction fallback
#
# The full extractor registry (extractors/) is a sibling unit. So the pipeline
# never hard-fails before that lands, we ship a minimal, dependency-free
# extractor here for the always-available text formats. When extractors IS
# present we prefer it (it covers office/pdf/html-with-offsets and emits richer
# structural offsets).
# --------------------------------------------------------------------------- #
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".log", ".rst", ".yml", ".yaml", ".xml"}
_CSV_SUFFIXES = {".csv", ".tsv"}
_HTML_SUFFIXES = {".html", ".htm"}


class _HTMLText(html.parser.HTMLParser):
    """Minimal HTML -> text extractor that records a char offset per block tag."""

    _BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "table", "ul", "ol",
    }
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: List[str] = []
        self._skip_depth = 0
        self.offsets: List[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            cur = sum(len(s) for s in self._buf)
            self.offsets.append({"offset": cur, "label": tag})

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._buf.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._buf.append(data)

    def get_text(self) -> str:
        return "".join(self._buf)


def _extract_builtin(path: Path) -> dict:
    """Dependency-free extraction for text/csv/html. Always succeeds for those.

    Returns the standard extractor shape: ``{text, offsets, meta, needs_ocr}``.
    Offsets are structural markers (row/page/block) as ``{offset, label}``.
    """
    suffix = path.suffix.lower()
    meta = {"extractor": "ingest_pipeline.builtin", "suffix": suffix}

    if suffix in _CSV_SUFFIXES:
        import csv

        delim = "\t" if suffix == ".tsv" else ","
        out = io.StringIO()
        offsets: List[dict] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
                reader = csv.reader(fh, delimiter=delim)
                for i, row in enumerate(reader):
                    offsets.append({"offset": out.tell(), "label": f"row{i}"})
                    out.write(" | ".join(row))
                    out.write("\n")
        except OSError as exc:
            return {"text": "", "offsets": [], "meta": {**meta, "error": str(exc)}, "needs_ocr": False}
        return {"text": out.getvalue(), "offsets": offsets, "meta": meta, "needs_ocr": False}

    if suffix in _HTML_SUFFIXES:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"text": "", "offsets": [], "meta": {**meta, "error": str(exc)}, "needs_ocr": False}
        parser = _HTMLText()
        try:
            parser.feed(raw)
            parser.close()
        except Exception:  # pragma: no cover - malformed HTML still yields buffered text
            pass
        return {"text": parser.get_text(), "offsets": parser.offsets, "meta": meta, "needs_ocr": False}

    if suffix in _TEXT_SUFFIXES or suffix == "":
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"text": "", "offsets": [], "meta": {**meta, "error": str(exc)}, "needs_ocr": False}
        # Paragraph offsets for evidence citation.
        offsets = []
        pos = 0
        for para in text.split("\n\n"):
            offsets.append({"offset": pos, "label": "para"})
            pos += len(para) + 2
        return {"text": text, "offsets": offsets, "meta": meta, "needs_ocr": False}

    # Unknown/binary suffix and no extractor registry: degrade gracefully.
    return {
        "text": "",
        "offsets": [],
        "meta": {**meta, "extraction_unavailable": True},
        "needs_ocr": True,
    }


def extract_text(path) -> dict:
    """Extract ``{text, offsets, meta, needs_ocr}`` from a file.

    Prefers the sibling ``extractors`` registry when importable; otherwise uses
    the built-in stdlib fallback. Never raises on a single file -- an
    unextractable file returns empty text with ``needs_ocr``/``extraction_unavailable``.
    """
    p = Path(path)
    # Prefer the richer registry if it has landed.
    try:
        import extractors  # companion engine package

        result = extractors.extract(p)
        if isinstance(result, dict) and "text" in result:
            result.setdefault("offsets", [])
            result.setdefault("meta", {})
            result.setdefault("needs_ocr", False)
            # If the registry could not handle this type, fall back to builtin.
            if result.get("text") or not result.get("meta", {}).get("extraction_unavailable"):
                return result
    except Exception:
        pass
    return _extract_builtin(p)


# --------------------------------------------------------------------------- #
# optional sibling stages
# --------------------------------------------------------------------------- #
def _index_chunks(root: Path, source_id: str, chunks, sensitivity: str, provenance: dict) -> dict:
    """Index chunks via the knowledge_index sibling if present; else skip."""
    try:
        import knowledge_index  # sibling engine module (optional)
    except Exception:
        return {"status": "skipped", "reason": "knowledge_index unavailable", "count": 0}
    try:
        n = knowledge_index.index_chunks(
            root=root,
            source_id=source_id,
            chunks=[c.to_dict() if hasattr(c, "to_dict") else c for c in chunks],
            sensitivity=sensitivity,
            provenance=provenance,
        )
        return {"status": "indexed", "count": int(n) if n is not None else len(chunks)}
    except Exception as exc:
        return {"status": "error", "reason": str(exc), "count": 0}


def _source_id_from_fm(fm: dict) -> str:
    """Extract the index source_id (sha256[:12]) from a source note's frontmatter.

    The ingest pipeline indexes chunks under ``sha256[:12]`` where sha256 comes
    from ``captured_sha256`` (the content hash captured at ingest time).  Older
    notes may carry ``sha256_12`` directly.  If neither is available the note's
    ``id``/``source_id`` field is returned as a best-effort fallback.
    """
    captured = str(fm.get("captured_sha256") or "").strip()
    if captured:
        return captured[:12]
    sha_12 = str(fm.get("sha256_12") or "").strip()
    if sha_12:
        return sha_12
    # Best-effort: plain id or source_id field.
    return str(fm.get("source_id") or fm.get("id") or "").strip()


def _superseded_source_ids(root: Path, origin_filename: str, new_source_id: str) -> list:
    """Return index source_ids that share the same origin file and are not the new source.

    Uses ``source_catalog`` to look up prior source records by their
    ``origin_filename`` frontmatter field.  If ``source_catalog`` is absent or
    fails, falls back to scanning the Sources folder directly for notes whose
    frontmatter carries a matching ``origin_filename``.

    Returns a list of (possibly empty) previous source_ids so callers can
    remove their index chunks.  The source_id for the index is always the first
    12 hex chars of ``captured_sha256``.
    """
    old_ids: list = []
    try:
        try:
            import source_catalog as _sc  # type: ignore
        except Exception:  # pragma: no cover - package import fallback
            try:
                from . import source_catalog as _sc  # type: ignore
            except Exception:
                _sc = None  # type: ignore

        if _sc is not None:
            snap = _sc.snapshot(root)
            for entry in snap.entries:
                fm = entry.get("fm") or {}
                if fm.get("origin_filename") == origin_filename:
                    sid = _source_id_from_fm(fm)
                    if sid and sid != new_source_id:
                        old_ids.append(sid)
        else:
            # Fallback: direct folder walk.
            try:
                import answer_protocol as _ap  # type: ignore
            except Exception:  # pragma: no cover
                try:
                    from . import answer_protocol as _ap  # type: ignore
                except Exception:
                    return old_ids
            folder = root / "Memory.nosync" / "Sources"
            if folder.is_dir():
                for p in sorted(folder.glob("*.md")):
                    if p.name.startswith("_"):
                        continue
                    fm = _ap.read_frontmatter(p)
                    if fm.get("origin_filename") == origin_filename:
                        sid = _source_id_from_fm(fm)
                        if sid and sid != new_source_id:
                            old_ids.append(sid)
    except Exception:
        pass
    return old_ids


def _remove_superseded_chunks(
    root: Path, origin_filename: str, new_source_id: str
) -> dict:
    """Delete index chunks for any prior source that has the same origin file.

    Called AFTER the new source's chunks are already registered so the index
    never has a gap.  Fail-closed contract:
      * If supersession cannot be determined (catalog absent, exception), we
        emit a review-queue-visible warning in the returned dict rather than
        silently duplicating or destroying data.
      * Deletion is best-effort per superseded source: a partial failure
        records which ids were cleaned and which errored.
    """
    try:
        import knowledge_index  # type: ignore
    except Exception:
        return {"status": "skipped", "reason": "knowledge_index unavailable", "removed": []}

    try:
        old_ids = _superseded_source_ids(root, origin_filename, new_source_id)
    except Exception as exc:
        # Cannot determine supersession -- emit warning, keep old chunks.
        return {
            "status": "warning",
            "reason": (
                f"supersession lookup failed ({exc}); old chunks may remain "
                f"for origin={origin_filename!r}. Manual reindex may be needed."
            ),
            "removed": [],
        }

    if not old_ids:
        return {"status": "ok", "removed": []}

    removed: list = []
    errors: list = []
    with knowledge_index.KnowledgeIndex(root) as idx:
        for sid in old_ids:
            try:
                count = idx.delete_source(sid)
                removed.append({"source_id": sid, "chunks_deleted": count})
            except Exception as exc:
                errors.append({"source_id": sid, "error": str(exc)})

    result: dict = {"removed": removed}
    if errors:
        result["status"] = "partial"
        result["errors"] = errors
    else:
        result["status"] = "ok"
    return result


def _make_source_card(
    root: Path,
    src: Path,
    sha256: str,
    sensitivity: str,
    classification: dict,
    chunk_count: int,
    connector: Optional[str],
    extract_meta: dict,
    actor: Optional[str],
    business_object: Optional[str],
    authoritative_for: Optional[list[str]],
    source_system: Optional[str],
    authority_id: Optional[str],
) -> dict:
    """Build the immutable source-record-SHAPED provenance card.

    This is the canonical dict shape a Sources/ note carries. We always build it
    so callers/tests can inspect provenance; persistence to a Memory.nosync/
    Sources/ note is delegated to the ``source_record`` sibling when present.
    """
    return {
        "type": "source",
        "title": src.name,
        "provenance": (
            f"Ingested from Workproduct.nosync/_INPUT/{src.name} via "
            f"{connector or 'manual'}; captured_sha256={sha256}."
        ),
        "raw_location": f"Workproduct.nosync/_INPUT/{src.name}",
        "origin_filename": src.name,
        "sha256": sha256,
        "sha256_12": sha256[:12],
        "byte_size": (src.stat().st_size if src.exists() else None),
        "suffix": src.suffix.lower(),
        "locality": "snapshot_local",
        "capture_tier": "snapshot",
        "sensitivity": sensitivity,
        "connector": connector or "manual",
        "source_system": source_system or connector or "manual",
        "authority_id": authority_id or source_system or connector or "",
        "business_object": business_object or "",
        "authoritative_for": authoritative_for or ([business_object] if business_object else []),
        "chunk_count": chunk_count,
        "grain": (
            f"One captured file ({src.name}); {chunk_count} extracted chunk(s); "
            f"suffix={src.suffix.lower() or '(none)'}."
        ),
        "needs_ocr": bool(extract_meta.get("needs_ocr")),
        # A needs-ocr tag routes the source into the Review Inbox so the
        # operating agent transcribes it (multimodal) and re-ingests.
        "tags": (["source", "needs-ocr"] if extract_meta.get("needs_ocr") else ["source"]),
        "extractor": extract_meta.get("extractor") or extract_meta.get("meta", {}).get("extractor"),
        "classification_signals": classification.get("signals", []),
        "as_of": _now_iso(),
        "ingested_at": _now_iso(),
        "actor": actor or "ingest_pipeline",
        "status": "active",
    }


_AUTHORITY_CANDIDATE_KEYS = (
    "business_object",
    "authoritative_for",
    "authority_id",
    "primary_source",
)


def _has_authority_candidate(card: dict) -> bool:
    return any(card.get(k) not in (None, "", []) for k in _AUTHORITY_CANDIDATE_KEYS)


def _authority_candidate_payload(card: dict, reason: str) -> dict:
    """Return a non-authority Source payload that preserves the proposal.

    The fallback intentionally removes authority-bearing frontmatter fields so
    ``source_record.create`` requires only ``provide_documents``. The proposal is
    carried in the body and tags for admin review.
    """
    payload = dict(card)
    proposed = {
        "business_object": card.get("business_object") or "",
        "authoritative_for": card.get("authoritative_for") or [],
        "authority_id": card.get("authority_id") or "",
        "primary_source": card.get("primary_source") or "",
        "source_system": card.get("source_system") or "",
        "connector": card.get("connector") or "",
    }
    for key in _AUTHORITY_CANDIDATE_KEYS:
        payload.pop(key, None)
    tags = payload.get("tags") or ["source"]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if "authority-candidate" not in tags:
        tags.append("authority-candidate")
    payload["tags"] = tags
    existing_notes = str(payload.get("notes") or "_(none)_")
    proposal_lines = [
        "## Authority candidate",
        "",
        "This source was captured as ordinary evidence. The following authority proposal requires admin review before it can ground material Oracle answers:",
        "",
        f"- business_object: {proposed['business_object'] or '(none)'}",
        f"- authoritative_for: {', '.join(proposed['authoritative_for']) if isinstance(proposed['authoritative_for'], list) and proposed['authoritative_for'] else '(none)'}",
        f"- source_system: {proposed['source_system'] or '(none)'}",
        f"- authority_id: {proposed['authority_id'] or '(none)'}",
        f"- primary_source: {proposed['primary_source'] or '(none)'}",
        f"- connector: {proposed['connector'] or '(none)'}",
        f"- review_reason: {reason}",
    ]
    payload["notes"] = existing_notes.rstrip() + "\n\n" + "\n".join(proposal_lines)
    return payload


def _persist_source_record_as(
    root: Path,
    card: dict,
    *,
    actor: Optional[str],
    role: Optional[str],
) -> dict:
    """Persist a Source while honoring supplied actor/role.

    If a non-admin role cannot create an authority-bearing Source, preserve the
    file as ordinary evidence and turn the authority metadata into an
    admin-review candidate instead of letting ingestion fail or bypassing the
    role gate.
    """
    try:
        import source_record  # sibling engine module (optional)
    except Exception:
        return {"persisted": False, "reason": "source_record unavailable"}

    effective_actor = actor or "ingest_pipeline"
    effective_role = role or "system"
    try:
        rec = source_record.create(
            root=Path(root),
            payload=dict(card),
            actor=effective_actor,
            role=effective_role,
        )
        sid = None
        if isinstance(rec, dict):
            sid = rec.get("id") or rec.get("drop_id") or rec.get("source_id")
        return {
            "persisted": True,
            "record": rec,
            "source_id": sid,
            "authority_candidate": False,
        }
    except PermissionError as exc:
        if not _has_authority_candidate(card):
            return {"persisted": False, "reason": str(exc)}
        candidate = _authority_candidate_payload(card, str(exc))
        try:
            rec = source_record.create(
                root=Path(root),
                payload=candidate,
                actor=effective_actor,
                role=effective_role,
            )
            sid = None
            if isinstance(rec, dict):
                sid = rec.get("id") or rec.get("drop_id") or rec.get("source_id")
            return {
                "persisted": True,
                "record": rec,
                "source_id": sid,
                "authority_candidate": True,
                "authority_candidate_reason": str(exc),
            }
        except Exception as fallback_exc:
            return {"persisted": False, "reason": str(fallback_exc)}
    except Exception as exc:
        return {"persisted": False, "reason": str(exc)}


def _derive_candidates(root: Path, source_id: Optional[str]) -> dict:
    """Emit review-gated derivations via the derive sibling if present."""
    if not source_id:
        return {"status": "skipped", "reason": "no source_id", "count": 0}
    try:
        import derive  # sibling engine module (optional)
    except Exception:
        return {"status": "skipped", "reason": "derive unavailable", "count": 0}
    try:
        out = derive.from_source(root=Path(root), source_id=source_id)
        count = len(out) if isinstance(out, (list, tuple)) else int(out or 0)
        return {"status": "derived", "count": count}
    except Exception as exc:
        return {"status": "error", "reason": str(exc), "count": 0}


def _processing_verdict(root: Path, sensitivity: str) -> dict:
    """Confirm the intake sensitivity is processable locally (advisory record)."""
    try:
        import policy  # floor module
    except Exception:
        return {"environment": "local_deterministic", "verdict": "unknown"}
    try:
        verdict = policy.check_processing(sensitivity, "local_deterministic")
        return {"environment": "local_deterministic", "verdict": verdict}
    except Exception as exc:
        return {"environment": "local_deterministic", "verdict": "error", "reason": str(exc)}


# --------------------------------------------------------------------------- #
# result record
# --------------------------------------------------------------------------- #
class PipelineResult(dict):
    """A plain ``dict`` subclass so the result is JSON-serialisable and also
    supports attribute access for the common fields used in tests."""

    @property
    def chunks(self):
        return self.get("chunks", [])

    @property
    def source_record(self):
        return self.get("source_record", {})

    @property
    def sensitivity(self):
        return self.get("sensitivity")


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def run(
    root,
    file,
    *,
    lane: Optional[str] = None,
    connector: Optional[str] = None,
    sensitivity: Optional[str] = None,
    admin_override: Optional[str] = None,
    derive: bool = False,
    index: bool = True,
    actor: Optional[str] = None,
    role: Optional[str] = None,
    business_object: Optional[str] = None,
    authoritative_for: Optional[list[str]] = None,
    source_system: Optional[str] = None,
    authority_id: Optional[str] = None,
    chunk_size: int = chunker.DEFAULT_SIZE,
    chunk_overlap: int = chunker.DEFAULT_OVERLAP,
) -> PipelineResult:
    """Run the ingestion pipeline over one ``_INPUT`` file. Non-destructive.

    Steps:
      1. validate the file is inside ``_INPUT`` (containment; never read random
         host paths through the pipeline),
      2. extract text + structural offsets,
      3. chunk with offset tracking,
      4. classify intake sensitivity (stricter-row-wins; explicit
         ``sensitivity`` is used as a FLOOR via the connector_default channel;
         ``admin_override`` wins outright),
      5. index the chunks (optional sibling; skipped cleanly if absent),
      6. build the immutable source-record-shaped provenance card and persist it
         (optional sibling; card always returned),
      7. optionally emit review-gated derivations (optional sibling),
      8. return a full :class:`PipelineResult`.

    The input file is never moved, truncated, or deleted by this function.

    Args:
        root:          oracle root.
        file:          path to a file inside ``_INPUT``.
        lane:          optional Workproduct lane this material is associated with
                       (recorded for provenance; promotion is artifact_io's job).
        connector:     source connector id (recorded; "manual" if None).
        sensitivity:   caller-asserted FLOOR sensitivity (e.g. a connector's
                       declared default). The classifier never goes below it.
        admin_override:explicit admin label; wins outright.
        derive:        if True, run the review-gated derivation stage.
        index:         if True (default), attempt to index chunks.
        actor:         recorded provenance actor.
        role:          optional declared role; when supplied, Source persistence
                       enforces the same role gate as direct source creation.
        business_object:
                       optional business object this source directly bears on.
        authoritative_for:
                       optional business objects this source can authoritatively
                       ground; defaults to ``[business_object]`` when supplied.
        source_system: optional authority/source-system label used to match
                       TRUTH-MAP Primary source.
        authority_id:  optional explicit authority id used to match TRUTH-MAP
                       Primary source.

    Returns:
        :class:`PipelineResult` with keys: ok, file, sha256, sensitivity,
        classification, chunks, chunk_count, extract, index, source_record,
        source_persist, derive, processing, lane, connector, errors.

    Raises:
        ValueError: only for a containment failure (file not inside _INPUT) or a
                    missing/non-file input -- the security-load-bearing checks.
                    All optional-stage failures are captured into ``errors`` and
                    do not raise.
    """
    root = Path(root)
    src = Path(file)
    errors: List[str] = []

    if not src.exists() or not src.is_file():
        raise ValueError(f"ingest_pipeline.run: input is not a file: {src}")
    if not _is_within_input(root, src):
        raise ValueError(
            f"ingest_pipeline.run: input must live inside Workproduct.nosync/_INPUT: {src}"
        )

    sha256 = _sha256_file(src)

    # 2) extract
    extracted = extract_text(src)
    text = extracted.get("text") or ""
    offsets = extracted.get("offsets") or []
    extract_meta = {
        "extractor": (extracted.get("meta") or {}).get("extractor"),
        "needs_ocr": bool(extracted.get("needs_ocr")),
        "meta": extracted.get("meta") or {},
    }
    if not text and extracted.get("needs_ocr"):
        errors.append("extraction produced no text (needs_ocr); downstream stages limited")

    # 3) chunk (offset-exact)
    chunks = chunker.chunk(text, size=chunk_size, overlap=chunk_overlap, offsets=offsets)

    # 4) classify at log time (stricter-row-wins)
    classification = intake_classify.classify(
        text=text,
        filename=src.name,
        size=(src.stat().st_size if src.exists() else None),
        connector_default=sensitivity,
        admin_override=admin_override,
    )
    label = classification["label"]

    processing = _processing_verdict(root, label)

    # 5) index (optional)
    provenance = {"source_sha256": sha256, "origin_filename": src.name, "connector": connector or "manual"}
    new_index_source_id = sha256[:12]
    if index:
        index_result = _index_chunks(root, new_index_source_id, chunks, label, provenance)
        # 5.5) Remove superseded source chunks -- only after the new chunks are
        # registered so the index never has a gap.  Fail-closed: a lookup failure
        # keeps old chunks and emits a review-visible warning; it never raises.
        supersede_result = _remove_superseded_chunks(root, src.name, new_index_source_id)
        if supersede_result.get("status") == "warning":
            errors.append(supersede_result["reason"])
        index_result["supersede"] = supersede_result
    else:
        index_result = {"status": "skipped", "reason": "index disabled by caller", "count": 0}

    # 6) source-record card (always) + persist (optional)
    card = _make_source_card(
        root,
        src,
        sha256,
        label,
        classification,
        len(chunks),
        connector,
        extract_meta,
        actor,
        business_object,
        authoritative_for,
        source_system,
        authority_id,
    )
    persist = _persist_source_record_as(root, card, actor=actor, role=role)
    source_id = persist.get("source_id") if persist.get("persisted") else card["sha256_12"]
    card["source_id"] = source_id
    card["persisted"] = bool(persist.get("persisted"))

    # 7) derive (optional, review-gated)
    if derive:
        derive_result = _derive_candidates(root, persist.get("source_id"))
    else:
        derive_result = {"status": "skipped", "reason": "derive not requested", "count": 0}

    # 8) truth-map draft-row proposal (v2 bootstrap UX): evidence that names a
    # business object auto-proposes a DRAFT row so the same-session answer
    # upgrades from refused to supported. Promotion to confirmed stays an
    # explicit admin act. Best-effort; never fails the ingest.
    truth_result = _propose_truth_rows(
        root,
        card,
        persisted=bool(persist.get("persisted")),
        candidate=bool(persist.get("authority_candidate")),
        actor=actor,
    )

    return PipelineResult(
        ok=True,
        file=str(src),
        sha256=sha256,
        sensitivity=label,
        classification=classification,
        chunks=[c.to_dict() for c in chunks],
        chunk_count=len(chunks),
        extract=extract_meta,
        index=index_result,
        source_record=card,
        source_persist=persist,
        derive=derive_result,
        processing=processing,
        truth_map=truth_result,
        lane=lane,
        connector=connector or "manual",
        errors=errors,
    )


def _propose_truth_rows(
    root: Path,
    card: dict,
    *,
    persisted: bool,
    candidate: bool,
    actor: Optional[str],
) -> dict:
    """Best-effort draft truth-map row proposal from ingest metadata.

    Only runs when the Source actually persisted. When the Source was demoted
    to an authority-candidate (role gate), the proposed row keeps a TBD primary
    source -- the evidence supports answers (exit 2) but the authority wiring
    remains an admin decision.
    """
    objects = [o for o in (card.get("authoritative_for") or []) if o]
    if not objects and card.get("business_object"):
        objects = [card["business_object"]]
    if not persisted or not objects:
        return {"status": "skipped", "proposals": []}
    try:
        import truth_map as _tm  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        try:
            from . import truth_map as _tm  # type: ignore
        except Exception:
            return {"status": "skipped", "reason": "truth_map unavailable", "proposals": []}
    primary = "TBD" if candidate else (card.get("authority_id") or card.get("source_system") or "TBD")
    proposals = []
    for bo in objects:
        try:
            res = _tm.propose_row(
                root,
                bo,
                primary_source=primary,
                actor=actor or "ingest_pipeline",
            )
            proposals.append({"business_object": bo, "action": res.get("action")})
        except Exception as exc:
            proposals.append({"business_object": bo, "action": "error", "reason": str(exc)})
    return {"status": "proposed", "proposals": proposals}


# --------------------------------------------------------------------------- #
# staging + batch (v2: "./oracle ingest <anything>")
# --------------------------------------------------------------------------- #
def stage_external(root, src) -> Path:
    """Non-destructively copy an outside file into ``_INPUT`` for ingestion.

    The destination filename is the original name (containment-validated via
    ``safe_paths.contain``); on collision with DIFFERENT content the name gains
    a short content-hash suffix. The original file is never touched. Returns
    the staged path inside ``_INPUT``.
    """
    import safe_paths

    root = Path(root)
    src = Path(src)
    if not src.is_file():
        raise ValueError(f"stage_external: not a file: {src}")
    digest = _sha256_file(src)
    dest = safe_paths.contain(root, f"_INPUT/{src.name}")
    if dest.exists():
        if _sha256_file(dest) == digest:
            return dest  # identical content already staged
        stem, suffix = src.stem, src.suffix
        dest = safe_paths.contain(root, f"_INPUT/{stem}-{digest[:8]}{suffix}")
        if dest.exists() and _sha256_file(dest) == digest:
            return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())  # safe_paths-internal: dest from safe_paths.contain()
    if _sha256_file(dest) != digest:
        raise ValueError(f"stage_external: copy verification failed for {src}")
    return dest


# File names that are never material (housekeeping artifacts).
_SKIP_NAMES = {".DS_Store", "Thumbs.db", ".gitignore", "REGISTRY.md", "_CONTEXT.md"}


def iter_ingestable(path: Path):
    """Yield ingestable files under ``path`` (a file or directory, recursive)."""
    p = Path(path)
    if p.is_file():
        if p.name not in _SKIP_NAMES and not p.name.startswith("._"):
            yield p
        return
    if p.is_dir():
        for child in sorted(p.rglob("*")):
            if not child.is_file():
                continue
            if child.name in _SKIP_NAMES or child.name.startswith("._"):
                continue
            if "__pycache__" in child.parts or ".git" in child.parts:
                continue
            yield child


def run_batch(root, paths, **kwargs) -> dict:
    """Ingest many files and/or directories in one call.

    Paths outside ``_INPUT`` are staged in non-destructively first; paths
    already inside ``_INPUT`` ingest in place. Per-file failures are recorded,
    never fatal to the batch. Returns a summary dict with per-file results.
    """
    root = Path(root)
    results = []
    ok = failed = 0
    for given in paths:
        for f in iter_ingestable(Path(given)):
            try:
                target = f if _is_within_input(root, f) else stage_external(root, f)
                res = run(root, target, **kwargs)
                ok += 1
                results.append(
                    {
                        "file": str(f),
                        "staged": str(target) if target != f else None,
                        "ok": True,
                        "sensitivity": res.get("sensitivity"),
                        "chunk_count": res.get("chunk_count"),
                        "needs_ocr": bool((res.get("extract") or {}).get("needs_ocr")),
                        "source_id": (res.get("source_record") or {}).get("source_id"),
                        "truth_map": res.get("truth_map"),
                    }
                )
            except Exception as exc:
                failed += 1
                results.append({"file": str(f), "ok": False, "error": str(exc)})
    return {
        "ok": failed == 0,
        "ingested": ok,
        "failed": failed,
        "results": results,
    }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        prog="ingest_pipeline.py",
        description="Orchestrate extract->chunk->index->source-record->derive->classify.",
    )
    ap.add_argument("--root", default=".", help="oracle root")
    sub = ap.add_subparsers(dest="cmd")
    runp = sub.add_parser("run", help="ingest one _INPUT file")
    runp.add_argument("--file", required=True)
    runp.add_argument("--lane")
    runp.add_argument("--connector")
    runp.add_argument("--sensitivity", help="floor sensitivity (connector default)")
    runp.add_argument("--admin-override", dest="admin_override")
    runp.add_argument("--derive", action="store_true")
    runp.add_argument("--no-index", dest="no_index", action="store_true")
    runp.add_argument("--actor")
    runp.add_argument("--role")
    runp.add_argument("--business-object", dest="business_object")
    runp.add_argument(
        "--authoritative-for",
        dest="authoritative_for",
        help="comma-separated business objects this source can authoritatively ground",
    )
    runp.add_argument("--source-system", dest="source_system")
    runp.add_argument("--authority-id", dest="authority_id")
    runp.add_argument("--json", action="store_true")

    bat = sub.add_parser(
        "batch",
        help="ingest files and/or directories; outside paths are staged into _INPUT non-destructively",
    )
    bat.add_argument("paths", nargs="+", help="files or directories to ingest")
    bat.add_argument("--lane")
    bat.add_argument("--connector")
    bat.add_argument("--sensitivity", help="floor sensitivity (connector default)")
    bat.add_argument("--derive", action="store_true")
    bat.add_argument("--actor")
    bat.add_argument("--role")
    bat.add_argument("--business-object", dest="business_object")
    bat.add_argument("--source-system", dest="source_system")
    bat.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "batch":
        result = run_batch(
            args.root,
            args.paths,
            lane=args.lane,
            connector=args.connector,
            sensitivity=args.sensitivity,
            derive=args.derive,
            actor=args.actor,
            role=args.role,
            business_object=args.business_object,
            source_system=args.source_system,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"batch: ingested={result['ingested']} failed={result['failed']}")
            for r in result["results"]:
                if r.get("ok"):
                    ocr = " NEEDS-OCR" if r.get("needs_ocr") else ""
                    print(f"  ok  {Path(r['file']).name}: sensitivity={r['sensitivity']} chunks={r['chunk_count']}{ocr}")
                else:
                    print(f"  ERR {Path(r['file']).name}: {r['error']}")
        return 0 if result["ok"] else 1

    if args.cmd != "run":
        ap.print_help()
        return 2

    try:
        authoritative_for = None
        if args.authoritative_for:
            authoritative_for = [
                x.strip() for x in args.authoritative_for.split(",") if x.strip()
            ]
        result = run(
            args.root,
            args.file,
            lane=args.lane,
            connector=args.connector,
            sensitivity=args.sensitivity,
            admin_override=args.admin_override,
            derive=args.derive,
            index=not args.no_index,
            actor=args.actor,
            role=args.role,
            business_object=args.business_object,
            authoritative_for=authoritative_for,
            source_system=args.source_system,
            authority_id=args.authority_id,
        )
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"ingested {Path(result['file']).name}: "
            f"sensitivity={result['sensitivity']} "
            f"chunks={result['chunk_count']} "
            f"index={result['index']['status']} "
            f"source_persisted={result['source_record']['persisted']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
