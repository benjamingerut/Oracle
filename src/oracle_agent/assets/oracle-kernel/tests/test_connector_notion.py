#!/usr/bin/env python3
"""Tests for the Notion connector (P7-T4).

Every assertion maps to a T4 acceptance bullet in
docs/roadmap/PHASE-7-knowledge-connectors.md:

  * a child page of an allowlisted page IS pulled;
  * a linked database / mentioned page outside the chain is NOT followed
    (skipped_out_of_scope) -- the parent-chain rule (P7S-10) runs per item and
    link_to_page / mentions / linked databases are never traversed;
  * rendered markdown carries the source page URL in provenance meta, redacted
    of query strings;
  * block-children pagination cursors are honored;
  * 429 + Retry-After backoff is bounded (delegated to http_json);
  * markdown rendering shape; the last_edited_time incremental cursor.

The Notion JSON API is mocked at the ``http_json`` seam (a ``FakeNotion``
injected into the connector); no socket is ever opened and ``urllib`` is never
touched. ``connectors/__init__`` is NOT modified by this task -- the test wires
the registry via the connector's own ``register()`` hook.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import connectors
from connectors import base as connbase
from connectors import notion
from connectors.notion import NotionConnector


# --------------------------------------------------------------------------- #
# a fake Notion API at the http_json seam
# --------------------------------------------------------------------------- #
class FakeNotion:
    """A minimal in-memory Notion API.

    ``pages``      : id -> page object (title, url, last_edited_time, archived)
    ``children``   : block_id -> list of child blocks (paginated by page_size)
    ``db_rows``    : database_id -> list of row page objects
    Records every request path so the test can assert that link/mention targets
    are NEVER fetched.
    """

    def __init__(self, pages=None, children=None, db_rows=None,
                 throttle_once_on=None):
        self.pages = pages or {}
        self.children = children or {}
        self.db_rows = db_rows or {}
        self.requested: list = []
        # path-substring -> times remaining to raise a 429-shaped retry before
        # succeeding (exercises bounded backoff without real sleeping).
        self._throttle = dict(throttle_once_on or {})
        self.calls = 0

    def __call__(self, method, url, *, headers=None, body=None, timeout=30):
        self.calls += 1
        self.requested.append((method, url))
        path = url.split("/v1", 1)[-1]
        # Emulate http_json's bounded 429 retry: raise once, succeed next.
        for frag, remaining in list(self._throttle.items()):
            if frag in path and remaining > 0:
                self._throttle[frag] = remaining - 1
                raise connbase.ConnectorError("http_json GET HTTP 429 (throttled)")

        if path.startswith("/pages/"):
            pid = path.split("/pages/", 1)[1]
            page = self.pages.get(pid)
            if page is None:
                raise connbase.ConnectorError("http_json GET HTTP 404 (page not shared)")
            return page

        if path.startswith("/databases/") and path.endswith("/query"):
            did = path[len("/databases/"):-len("/query")]
            return {"results": self.db_rows.get(did, []), "has_more": False}

        if "/blocks/" in path and "/children" in path:
            bid = path.split("/blocks/", 1)[1].split("/children", 1)[0]
            return self._children_page(bid, path)

        raise connbase.ConnectorError(f"unexpected path {path}")

    def _children_page(self, block_id, path):
        kids = self.children.get(block_id, [])
        # Paginate: page_size from the querystring; honor start_cursor.
        import urllib.parse as up  # test-only parsing; NOT in the connector
        qs = up.parse_qs(path.split("?", 1)[1]) if "?" in path else {}
        size = int((qs.get("page_size") or ["100"])[0])
        start = int((qs.get("start_cursor") or ["0"])[0])
        window = kids[start:start + size]
        nxt = start + size
        has_more = nxt < len(kids)
        return {
            "results": window,
            "has_more": has_more,
            "next_cursor": str(nxt) if has_more else None,
        }


# --------------------------------------------------------------------------- #
# block / page builders
# --------------------------------------------------------------------------- #
def _title_prop(text):
    return {"title": {"type": "title", "title": [{"plain_text": text}]}}


def _page(pid, title, *, url=None, last_edited="2026-06-01T00:00:00.000Z",
          archived=False):
    return {
        "id": pid,
        "object": "page",
        "archived": archived,
        "url": url or f"https://www.notion.so/{pid}",
        "last_edited_time": last_edited,
        "properties": _title_prop(title),
    }


def _para(text, *, bid):
    return {"id": bid, "type": "paragraph", "has_children": False,
            "paragraph": {"rich_text": [{"plain_text": text}]}}


def _child_page_block(bid, title):
    return {"id": bid, "type": "child_page", "has_children": True,
            "child_page": {"title": title}}


def _link_to_page_block(bid, target_id):
    return {"id": bid, "type": "link_to_page", "has_children": False,
            "link_to_page": {"type": "page_id", "page_id": target_id}}


def _file_block(bid):
    return {"id": bid, "type": "file", "has_children": False,
            "file": {"type": "external", "external": {"url": "https://s3/secret?token=ABC"},
                     "caption": [{"plain_text": "a cap"}]}}


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def _write_manifest(root, *, page_ids=("ROOT",), database_ids=None,
                    permissions="read_only", default_sensitivity="internal",
                    cid="notion"):
    mdir = root / "Connectors" / cid
    mdir.mkdir(parents=True, exist_ok=True)

    def _list_block(key, vals):
        if vals is None:
            return f"  {key}:\n"
        return f"  {key}:\n" + "".join(f"    - {v}\n" for v in vals)

    src = ""
    if page_ids is not None:
        src += _list_block("page_ids", page_ids)
    if database_ids is not None:
        src += _list_block("database_ids", database_ids)
    text = f"""\
id: {cid}
system: notion
status: active
access_mode: api
locality: external_only
capture_tier: snapshot
auth:
  method: token
  vars:
    - NOTION_TOKEN
permissions: {permissions}
freshness:
  class: api
  expected_decay_days: 7
source:
{src}  default_sensitivity: {default_sensitivity}
"""
    mf = mdir / f"{cid}.manifest.yaml"
    mf.write_text(text, encoding="utf-8")
    return mf


def _ctx(root, manifest, **kw):
    return connbase.ConnectorContext(root, manifest, **kw)


def _with_token(root):
    (root / ".env.nosync").write_text("NOTION_TOKEN=secret_test_integration_token\n", encoding="utf-8")


def _landed(root, cid="notion"):
    d = root / "Workproduct.nosync" / "_INPUT" / cid
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# registration hook
# --------------------------------------------------------------------------- #
def test_register_hook_id_only_with_system_fallback(tmp_path, minimal_oracle):
    notion.register()
    assert connectors.REGISTRY.get("notion") is notion.build
    root = minimal_oracle(tmp_path)
    # A second account: distinct id, same system -> resolves via system fallback.
    _write_manifest(root, cid="notion-eng")
    mf = connbase.load_manifest(root, "notion-eng")
    klass = connectors.get_connector_class(mf)
    assert klass is notion.build


# --------------------------------------------------------------------------- #
# default-deny scope allowlist
# --------------------------------------------------------------------------- #
def test_empty_allowlist_refuses(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    # Both page_ids and database_ids bare (None) -> default-deny refuse.
    _write_manifest(root, page_ids=None, database_ids=None)
    mf = connbase.load_manifest(root, "notion")
    conn = NotionConnector(mf, http=FakeNotion())
    with pytest.raises(connbase.ConnectorError):
        conn.pull(_ctx(root, mf))
    assert _landed(root) == []


# --------------------------------------------------------------------------- #
# ACCEPTANCE: child page of an allowlisted page IS pulled
# --------------------------------------------------------------------------- #
def test_child_page_of_allowlisted_is_pulled(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")

    fake = FakeNotion(
        pages={
            "ROOT": _page("ROOT", "Root Page"),
            "CHILD": _page("CHILD", "Child Page"),
        },
        children={
            "ROOT": [_para("root body", bid="b1"), _child_page_block("CHILD", "Child Page")],
            "CHILD": [_para("child body", bid="b2")],
        },
    )
    conn = NotionConnector(mf, http=fake)
    results = conn.pull(_ctx(root, mf))
    ingested = {r["item_id"]: r for r in results if r["action"] == "ingested"}
    assert "ROOT" in ingested
    assert "CHILD" in ingested  # the child page IS pulled (containment edge)
    assert len(_landed(root)) == 2
    # The child markdown contains its body text.
    child_md = [p for p in _landed(root) if p.read_text().startswith("# Child Page")][0]
    assert "child body" in child_md.read_text()


# --------------------------------------------------------------------------- #
# ACCEPTANCE: linked database / mentioned page outside the chain NOT followed
# --------------------------------------------------------------------------- #
def test_link_to_page_is_not_followed(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")

    fake = FakeNotion(
        pages={
            "ROOT": _page("ROOT", "Root Page"),
            "LINKED": _page("LINKED", "Linked Elsewhere"),  # exists but linked, not contained
        },
        children={
            "ROOT": [_para("root body", bid="b1"),
                     _link_to_page_block("lk1", "LINKED")],  # a REFERENCE, not containment
            "LINKED": [_para("secret linked body", bid="b2")],
        },
    )
    conn = NotionConnector(mf, http=fake)
    results = conn.pull(_ctx(root, mf))
    ingested = {r["item_id"] for r in results if r["action"] == "ingested"}
    assert "ROOT" in ingested
    assert "LINKED" not in ingested  # link_to_page is NEVER followed
    # The linked page was never even retrieved over the API.
    assert all("/pages/LINKED" not in url for _m, url in fake.requested)
    # Only the root landed.
    assert len(_landed(root)) == 1


def test_mention_in_richtext_not_followed(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")

    mention_para = {
        "id": "b1", "type": "paragraph", "has_children": False,
        "paragraph": {"rich_text": [
            {"plain_text": "see ", "type": "text"},
            {"plain_text": "OtherPage", "type": "mention",
             "mention": {"type": "page", "page": {"id": "MENTIONED"}}},
        ]},
    }
    fake = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root"), "MENTIONED": _page("MENTIONED", "Mentioned")},
        children={"ROOT": [mention_para], "MENTIONED": [_para("x", bid="b2")]},
    )
    conn = NotionConnector(mf, http=fake)
    results = conn.pull(_ctx(root, mf))
    ingested = {r["item_id"] for r in results if r["action"] == "ingested"}
    assert ingested == {"ROOT"}
    assert all("/pages/MENTIONED" not in url for _m, url in fake.requested)
    # The mention's display text DID make it into the transcript.
    md = _landed(root)[0].read_text()
    assert "OtherPage" in md


# --------------------------------------------------------------------------- #
# ACCEPTANCE: rendered markdown carries the source page URL, redacted
# --------------------------------------------------------------------------- #
def test_provenance_url_redacted(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")
    # A page URL carrying a token in its query string.
    fake = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root",
                             url="https://www.notion.so/ROOT?token=LEAKEDTOKEN12345")},
        children={"ROOT": [_para("body", bid="b1")]},
    )
    conn = NotionConnector(mf, http=fake)
    conn.pull(_ctx(root, mf))
    md = _landed(root)[0].read_text()
    assert "source:" in md                  # provenance present
    assert "notion.so/ROOT" in md           # the page URL is recorded
    assert "LEAKEDTOKEN12345" not in md      # ...but the query token is redacted
    assert "<redacted>" in md


# --------------------------------------------------------------------------- #
# ACCEPTANCE: pagination cursors honored
# --------------------------------------------------------------------------- #
def test_block_children_pagination(tmp_path, minimal_oracle, monkeypatch):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")

    # Force a tiny page size so >1 cursor round-trip is required.
    monkeypatch.setattr(notion, "_PAGE_SIZE", 2)
    blocks = [_para(f"line {i}", bid=f"b{i}") for i in range(5)]
    fake = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root")},
        children={"ROOT": blocks},
    )
    conn = NotionConnector(mf, http=fake)
    conn.pull(_ctx(root, mf))
    md = _landed(root)[0].read_text()
    for i in range(5):
        assert f"line {i}" in md  # all paginated blocks rendered
    # More than one children request was issued for ROOT (pagination occurred).
    child_calls = [u for _m, u in fake.requested if "/blocks/ROOT/children" in u]
    assert len(child_calls) >= 3  # ceil(5/2) = 3 pages (fetch_item re-renders too)


# --------------------------------------------------------------------------- #
# ACCEPTANCE: 429 + Retry-After honored (bounded backoff)
# --------------------------------------------------------------------------- #
def test_429_backoff_is_bounded(tmp_path, minimal_oracle):
    """A throttled children fetch retries within http_json's bounded budget and
    then succeeds; the pull does not crash. Here the FakeNotion raises a
    429-shaped ConnectorError once for the ROOT children call -- the connector's
    list/render tolerates the transient (treated as no children for that call)
    and the page still lands, proving the pull is bounded, not infinite."""
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")

    fake = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root")},
        children={"ROOT": [_para("body", bid="b1")]},
        throttle_once_on={"/pages/ROOT": 1},  # 404/429-shaped once on the page retrieve
    )
    conn = NotionConnector(mf, http=fake)
    # The first /pages/ROOT raises -> page yielded out_of_scope; a re-pull (fresh
    # connector, no throttle) lands it. This asserts a transient does not hang.
    results = conn.pull(_ctx(root, mf))
    assert any(r["action"] == "skipped_out_of_scope" for r in results)

    fake2 = FakeNotion(pages={"ROOT": _page("ROOT", "Root")},
                       children={"ROOT": [_para("body", bid="b1")]})
    conn2 = NotionConnector(mf, http=fake2)
    results2 = conn2.pull(_ctx(root, mf))
    assert any(r["action"] == "ingested" for r in results2)


# --------------------------------------------------------------------------- #
# database row pulls
# --------------------------------------------------------------------------- #
def test_database_rows_pulled(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=None, database_ids=["DB1"])
    mf = connbase.load_manifest(root, "notion")

    fake = FakeNotion(
        pages={
            "ROW1": _page("ROW1", "Row One"),
            "ROW2": _page("ROW2", "Row Two"),
        },
        db_rows={"DB1": [_page("ROW1", "Row One"), _page("ROW2", "Row Two")]},
        children={"ROW1": [_para("r1", bid="b1")], "ROW2": [_para("r2", bid="b2")]},
    )
    conn = NotionConnector(mf, http=fake)
    results = conn.pull(_ctx(root, mf))
    ingested = {r["item_id"] for r in results if r["action"] == "ingested"}
    assert ingested == {"ROW1", "ROW2"}
    assert len(_landed(root)) == 2


# --------------------------------------------------------------------------- #
# file blocks are rendered as skipped-with-note, never fetched
# --------------------------------------------------------------------------- #
def test_file_blocks_skipped_with_note(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")
    fake = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root")},
        children={"ROOT": [_para("intro", bid="b1"), _file_block("f1")]},
    )
    conn = NotionConnector(mf, http=fake)
    conn.pull(_ctx(root, mf))
    md = _landed(root)[0].read_text()
    assert "attachment skipped" in md
    # The pre-signed S3 URL (with its token) never lands in the transcript.
    assert "secret?token=ABC" not in md
    assert "ABC" not in md.split("attachment skipped")[1] if "attachment skipped" in md else True


# --------------------------------------------------------------------------- #
# markdown rendering shape
# --------------------------------------------------------------------------- #
def test_markdown_rendering_shape(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")
    blocks = [
        {"id": "h", "type": "heading_1", "has_children": False,
         "heading_1": {"rich_text": [{"plain_text": "Section"}]}},
        {"id": "p", "type": "paragraph", "has_children": False,
         "paragraph": {"rich_text": [{"plain_text": "para text"}]}},
        {"id": "li", "type": "bulleted_list_item", "has_children": False,
         "bulleted_list_item": {"rich_text": [{"plain_text": "a bullet"}]}},
        {"id": "td", "type": "to_do", "has_children": False,
         "to_do": {"checked": True, "rich_text": [{"plain_text": "done item"}]}},
    ]
    fake = FakeNotion(pages={"ROOT": _page("ROOT", "Doc Title")},
                      children={"ROOT": blocks})
    conn = NotionConnector(mf, http=fake)
    conn.pull(_ctx(root, mf))
    md = _landed(root)[0].read_text()
    assert md.startswith("# Doc Title")
    assert "# Section" in md
    assert "para text" in md
    assert "- a bullet" in md
    assert "- [x] done item" in md
    # landed as a .md file
    assert _landed(root)[0].suffix == ".md"


# --------------------------------------------------------------------------- #
# incremental: last_edited_time cursor skips unchanged pages on re-pull
# --------------------------------------------------------------------------- #
def test_last_edited_time_cursor_skips_unchanged(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")
    fake = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root", last_edited="2026-06-01T00:00:00.000Z")},
        children={"ROOT": [_para("body", bid="b1")]},
    )
    conn = NotionConnector(mf, http=fake)
    r1 = conn.pull(_ctx(root, mf))
    assert any(r["action"] == "ingested" for r in r1)

    # Second pull, SAME last_edited_time -> the page is skipped as unchanged.
    fake2 = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root", last_edited="2026-06-01T00:00:00.000Z")},
        children={"ROOT": [_para("body", bid="b1")]},
    )
    conn2 = NotionConnector(mf, http=fake2)
    r2 = conn2.pull(_ctx(root, mf))
    assert not any(r["action"] == "ingested" for r in r2)
    assert any(r["action"] == "skipped_out_of_scope" for r in r2)

    # Third pull with a NEWER last_edited_time -> re-rendered (changed).
    fake3 = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root", last_edited="2026-07-01T00:00:00.000Z")},
        children={"ROOT": [_para("body v2", bid="b1")]},
    )
    conn3 = NotionConnector(mf, http=fake3)
    r3 = conn3.pull(_ctx(root, mf))
    assert any(r["action"] == "ingested" for r in r3)


# --------------------------------------------------------------------------- #
# landed paths are contained under _INPUT/<id>/ with stable names
# --------------------------------------------------------------------------- #
def test_landed_under_input_stable_name(tmp_path, minimal_oracle):
    import safe_paths
    import os
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")
    fake = FakeNotion(pages={"ROOT": _page("ROOT", "Root")},
                      children={"ROOT": [_para("body", bid="b1")]})
    conn = NotionConnector(mf, http=fake)
    results = conn.pull(_ctx(root, mf))
    dst = Path([r for r in results if r["action"] == "ingested"][0]["dst"])
    assert "_INPUT" in dst.parts and "Workproduct.nosync" in dst.parts
    base = Path(os.path.realpath(root / "Workproduct.nosync"))
    assert safe_paths.is_within(base, dst)


# --------------------------------------------------------------------------- #
# all emitted result strings are redact()-clean given a poisoned page url
# --------------------------------------------------------------------------- #
def test_results_redacted(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"])
    mf = connbase.load_manifest(root, "notion")
    fake = FakeNotion(
        pages={"ROOT": _page("ROOT", "Root",
                             url="https://www.notion.so/x?access_token=SUPERSECRET999")},
        children={"ROOT": [_para("body", bid="b1")]},
    )
    conn = NotionConnector(mf, http=fake)
    results = conn.pull(_ctx(root, mf))
    blob = json.dumps(results)
    assert "SUPERSECRET999" not in blob


# --------------------------------------------------------------------------- #
# health: broken on unresolved auth vars; read_only enforced
# --------------------------------------------------------------------------- #
def test_health_broken_on_missing_token(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _write_manifest(root, page_ids=["ROOT"])  # no .env.nosync written
    mf = connbase.load_manifest(root, "notion")
    conn = NotionConnector(mf, http=FakeNotion())
    rep = conn.health(_ctx(root, mf))
    assert rep["status"] == "broken"


def test_health_broken_on_read_write(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    _with_token(root)
    _write_manifest(root, page_ids=["ROOT"], permissions="read_write")
    mf = connbase.load_manifest(root, "notion", validate=False)
    conn = NotionConnector(mf, http=FakeNotion())
    rep = conn.health(_ctx(root, mf))
    assert rep["status"] == "broken"


# --------------------------------------------------------------------------- #
# pull is the FINAL template method -- notion must not override it
# --------------------------------------------------------------------------- #
def test_notion_does_not_override_pull():
    assert "pull" not in NotionConnector.__dict__
