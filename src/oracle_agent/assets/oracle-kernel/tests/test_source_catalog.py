#!/usr/bin/env python3
"""Tests for source_catalog.py -- the self-healing Source-frontmatter catalog.

The contract under test: markdown notes stay canonical; the catalog is a
derived cache that (a) re-parses ONLY new/changed notes, (b) heals itself on
deletes, edits, and PARSE_VERSION bumps, (c) degrades to an in-memory parse
when SQLite is unusable, and (d) produces gather results identical to the
direct folder walk it replaces.
"""
from __future__ import annotations

from pathlib import Path

import answer_protocol
import knowledge_index
import source_catalog


def _write_source(root: Path, name: str, *, object_name: str, authority: str = "erp", body: str = "b") -> Path:
    folder = root / "Memory.nosync" / "Sources"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.md"
    p.write_text(
        "\n".join([
            "---",
            f"id: {name}",
            "type: source",
            f"title: {name}",
            "created: 2026-06-01",
            "sensitivity: internal",
            "status: active",
            f"business_object: {object_name}",
            f"source_system: {authority}",
            f"authority_id: {authority}",
            "as_of: 2026-06-01",
            "---",
            "",
            body,
            "",
        ]),
        encoding="utf-8",
    )
    return p


def test_snapshot_parses_and_indexes_sources(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_source(root, "src-a", object_name="Revenue", authority="erp")
    _write_source(root, "src-b", object_name="Churn", authority="crm")

    snap = source_catalog.snapshot(root)
    assert [e["name"] for e in snap.entries] == ["src-a.md", "src-b.md"]
    assert snap.entries[0]["fm"]["business_object"] == "Revenue"
    assert "src-a" in snap.by_id_key
    assert "erp" in snap.by_label_key
    assert "revenue" in snap.by_object
    assert snap.by_object["revenue"][0]["name"] == "src-a.md"


def test_snapshot_heals_on_edit_delete_and_create(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    a = _write_source(root, "src-a", object_name="Revenue")
    _write_source(root, "src-b", object_name="Churn")
    assert len(source_catalog.snapshot(root).entries) == 2

    # Edit: object changes are picked up (size/mtime drift).
    _write_source(root, "src-a", object_name="Margin", body="changed body longer")
    snap = source_catalog.snapshot(root)
    assert "margin" in snap.by_object and "revenue" not in snap.by_object

    # Delete + create.
    a.unlink()
    _write_source(root, "src-c", object_name="Fleet")
    snap = source_catalog.snapshot(root)
    assert [e["name"] for e in snap.entries] == ["src-b.md", "src-c.md"]
    assert "fleet" in snap.by_object


def test_snapshot_is_read_only_on_pristine_roots(tmp_path, minimal_oracle):
    """No knowledge.db yet -> the catalog must not create one (read surfaces
    like status/review/dashboard stay write-free on a fresh root)."""
    root = minimal_oracle(tmp_path)
    _write_source(root, "src-a", object_name="Revenue")
    before = sorted(p for p in root.rglob("*") if p.is_file())
    snap = source_catalog.snapshot(root)
    assert [e["name"] for e in snap.entries] == ["src-a.md"]
    assert sorted(p for p in root.rglob("*") if p.is_file()) == before
    assert not source_catalog.db_path(root).exists()


def test_persistence_avoids_reparsing_unchanged_notes(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    knowledge_index.KnowledgeIndex(root)  # first ingest creates the DB
    for i in range(5):
        _write_source(root, f"src-{i}", object_name=f"Object-{i}")
    source_catalog.snapshot(root)

    calls = {"n": 0}
    real = answer_protocol.read_frontmatter

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(answer_protocol, "read_frontmatter", counting)
    source_catalog._SNAPSHOTS.clear()  # force a cold (cross-process) rebuild
    snap = source_catalog.snapshot(root)
    assert len(snap.entries) == 5
    assert calls["n"] == 0  # everything served from the SQLite catalog

    _write_source(root, "src-1", object_name="Edited", body="now different")
    source_catalog._SNAPSHOTS.clear()
    source_catalog.snapshot(root)
    assert calls["n"] == 1  # only the edited note re-parsed


def test_parse_version_bump_reparses_everything(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    knowledge_index.KnowledgeIndex(root)  # first ingest creates the DB
    for i in range(3):
        _write_source(root, f"src-{i}", object_name=f"Object-{i}")
    source_catalog.snapshot(root)

    calls = {"n": 0}
    real = answer_protocol.read_frontmatter

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(answer_protocol, "read_frontmatter", counting)
    monkeypatch.setattr(source_catalog, "PARSE_VERSION", source_catalog.PARSE_VERSION + 1)
    source_catalog._SNAPSHOTS.clear()
    snap = source_catalog.snapshot(root)
    assert len(snap.entries) == 3
    assert calls["n"] == 3


def test_corrupt_db_degrades_to_in_memory_parse(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_source(root, "src-a", object_name="Revenue")
    db = source_catalog.db_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("this is not a sqlite database", encoding="utf-8")

    source_catalog._SNAPSHOTS.clear()
    snap = source_catalog.snapshot(root)  # must not raise
    assert [e["name"] for e in snap.entries] == ["src-a.md"]
    assert "revenue" in snap.by_object


def test_db_path_matches_knowledge_index(tmp_path):
    assert source_catalog.db_path(tmp_path) == knowledge_index.default_db_path(tmp_path)


def test_gather_parity_with_direct_walk(tmp_path, minimal_oracle, monkeypatch):
    """Catalog-backed gathers return exactly what the folder walk returns."""
    root = minimal_oracle(tmp_path)
    _write_source(root, "src-a", object_name="Revenue", authority="erp")
    _write_source(root, "src-b", object_name="Revenue", authority="legacy-books")
    _write_source(root, "src-c", object_name="Churn", authority="crm")

    via_catalog_auth = answer_protocol.gather_sources(root, "Revenue", "erp")
    via_catalog_obj = answer_protocol.gather_object_evidence(root, "Revenue")

    # Force the fallback path and compare.
    monkeypatch.setattr(answer_protocol, "_source_snapshot", lambda root: None)
    via_walk_auth = answer_protocol.gather_sources(root, "Revenue", "erp")
    via_walk_obj = answer_protocol.gather_object_evidence(root, "Revenue")

    assert via_catalog_auth == via_walk_auth
    assert via_catalog_obj == via_walk_obj
    assert [fm["id"] for fm in via_catalog_obj] == ["src-a", "src-b"]
    # id-match (truth map naming a concrete Source id) also resolves:
    assert [fm["id"] for fm in answer_protocol.gather_sources(root, "Anything", "src-c")] == ["src-c"]
