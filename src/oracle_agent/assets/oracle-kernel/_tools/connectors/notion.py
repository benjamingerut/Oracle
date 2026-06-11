#!/usr/bin/env python3
"""connectors/notion.py -- the read-only Notion connector (P7-T4).

Notion is a thin, dumb adapter over the safety core in ``remote.py``. It pulls
allowlisted Notion pages and database rows -- and their transitive children --
into the oracle's intake lane as rendered markdown. It owns ONLY the two
subclass hooks ``list_items`` (metadata WITHIN scope) and ``fetch_item`` (bytes
to a private stage); the FINAL ``pull`` template, the gate, classification,
containment, the byte cap, redaction, and the cursor all live in
``RemoteConnector``.

Auth (P7S-1 does NOT apply): Notion INTERNAL integrations use a STATIC
integration token -- no OAuth dance, no device/loopback flow. The token name is
declared in the manifest ``auth.vars`` and resolved via ``resolve_auth`` from
``<root>/.env.nosync`` or the process env. The API base URL and version are
PINNED in this module (P7S-5), never read from the manifest.

Pinned scope semantics (P7S-10): an item is in scope IFF its parent chain
reaches an allowlisted page/database via ``child_page`` / ``child_block`` edges.
Allowlisted roots and their TRANSITIVE children (descended only through
``child_page`` and ``child_block`` block edges, and database rows of an
allowlisted database) are pulled. ``link_to_page``, mentions, and linked
databases are NEVER followed -- a link is a reference, not a containment edge.
The parent-chain check runs PER ITEM (integration-level sharing is not trusted
as the scope boundary): every discovered page is verified to chain back to an
allowlisted root before it is yielded in-scope; anything else is yielded as an
EXPECTED ``skipped_out_of_scope`` row (rc 0, never a failure signal; P7S-12).

Rendering: the block tree of each page is rendered to markdown with stdlib only
and landed as a ``.md`` file. File / image / pdf blocks are NOT downloaded
(Notion serves them from short-lived pre-signed S3 URLs that are out of the
connector's enumerated download-host policy and carry credentials in the query
string; P7S-9); they are rendered as a skipped-with-note line so the transcript
records their presence without fetching attacker-influenced bytes. Provenance
meta carries the source page URL, REDACTED of query strings.

Incremental: a per-page ``last_edited_time`` cursor lets a re-pull skip pages
that have not changed since the last successful pull. Landing names are stable
(``<sha256(item_id)[:12]>_<slug>.md``) so a re-pull supersedes rather than
duplicating. Pagination (block-children + database-query ``start_cursor`` /
``next_cursor`` / ``has_more``) is honored; 429 + Retry-After backoff is bounded
by ``http_json``.

Stdlib only. ``urllib`` is NEVER imported here -- every network call goes
through ``remote.http_json`` (the no-direct-urllib enforcer test in
``test_connectors_remote.py`` guards this; P7S-8).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Iterable, Optional

try:  # flat layout (tests put _tools on sys.path)
    from connectors.base import ConnectorContext, ConnectorError
    from connectors.remote import RemoteConnector, RemoteItem, http_json, redact, resolve_auth
except Exception:  # pragma: no cover - package fallback
    from .base import ConnectorContext, ConnectorError  # type: ignore
    from .remote import RemoteConnector, RemoteItem, http_json, redact, resolve_auth  # type: ignore

__all__ = ["NotionConnector", "build", "register", "ID", "SYSTEM"]

ID = "notion"
SYSTEM = "notion"

# PINNED in code, never manifest-supplied (P7S-5). Notion's REST base + the
# dated API version header the integration is built against.
_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"

# A hard ceiling on pages walked per pull so a deeply-nested or maliciously-wide
# tree cannot fan out unbounded (the byte/file caps in the base are the binding
# ceilings; this just bounds the metadata traversal). Independent of max_files.
_MAX_PAGES_WALKED = 5000
# Block-children page size (Notion max is 100).
_PAGE_SIZE = 100


# --------------------------------------------------------------------------- #
# the connector
# --------------------------------------------------------------------------- #
class NotionConnector(RemoteConnector):
    """Read-only Notion connector: allowlisted pages/databases + their
    transitive children, rendered to markdown."""

    access_mode = "api"
    #: Either a non-empty page_ids list OR a non-empty database_ids list admits
    #: a pull; the default-deny base refuses when BOTH are empty/None/non-list.
    scope_allowlist_keys = ("page_ids", "database_ids")
    #: Notion does not download attachment bytes through http_download (file
    #: blocks are rendered as notes, not fetched), so no download host suffixes.
    download_host_suffixes: tuple = ()

    def __init__(self, manifest: dict, *, http=None) -> None:
        super().__init__(manifest)
        # The JSON seam: tests inject a fake here; production uses http_json.
        self._http = http or http_json
        self._auth_token: Optional[str] = None
        # item_id -> last_edited_time, populated during list_items so the cursor
        # advance can record the incremental marker per ingested page (the base
        # result dict does not carry it).
        self._edit_marks: dict = {}

    # -- auth ---------------------------------------------------------------- #
    def _token(self, ctx: ConnectorContext) -> str:
        """Resolve the static integration token from the manifest auth.vars.

        Cached per pull. Never logged, never placed in a result dict."""
        if self._auth_token:
            return self._auth_token
        resolved = resolve_auth(ctx.root, self.manifest)
        # Use the first declared auth var's value as the integration token.
        names = (self.manifest.get("auth") or {}).get("vars") or []
        names = [str(n) for n in names if n]
        if not names:
            raise ConnectorError(f"{self.id}: no auth.vars declared (need the integration token name)")
        self._auth_token = resolved[names[0]]
        return self._auth_token

    def _headers(self, ctx: ConnectorContext) -> dict:
        return {
            "Authorization": f"Bearer {self._token(ctx)}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # -- low-level API (all JSON via the injected seam) ---------------------- #
    def _get(self, ctx: ConnectorContext, path: str) -> dict:
        return self._http("GET", f"{_API_BASE}{path}", headers=self._headers(ctx)) or {}

    def _post(self, ctx: ConnectorContext, path: str, body: dict) -> dict:
        return self._http("POST", f"{_API_BASE}{path}", headers=self._headers(ctx), body=body) or {}

    def _retrieve_page(self, ctx: ConnectorContext, page_id: str) -> dict:
        return self._get(ctx, f"/pages/{page_id}")

    def _retrieve_database(self, ctx: ConnectorContext, database_id: str) -> dict:
        return self._get(ctx, f"/databases/{database_id}")

    def _block_children(self, ctx: ConnectorContext, block_id: str) -> Iterable[dict]:
        """Yield ALL child blocks of ``block_id``, following pagination cursors."""
        cursor = None
        while True:
            qs = f"?page_size={_PAGE_SIZE}"
            if cursor:
                qs += f"&start_cursor={cursor}"
            resp = self._get(ctx, f"/blocks/{block_id}/children{qs}")
            for blk in resp.get("results") or []:
                if isinstance(blk, dict):
                    yield blk
            if resp.get("has_more") and resp.get("next_cursor"):
                cursor = resp["next_cursor"]
                continue
            break

    def _query_database_rows(self, ctx: ConnectorContext, database_id: str) -> Iterable[dict]:
        """Yield ALL page rows of a database, following query pagination."""
        cursor = None
        while True:
            body: dict = {"page_size": _PAGE_SIZE}
            if cursor:
                body["start_cursor"] = cursor
            resp = self._post(ctx, f"/databases/{database_id}/query", body)
            for row in resp.get("results") or []:
                if isinstance(row, dict):
                    yield row
            if resp.get("has_more") and resp.get("next_cursor"):
                cursor = resp["next_cursor"]
                continue
            break

    # -- scope traversal (P7S-10) -------------------------------------------- #
    def list_items(self, ctx: ConnectorContext) -> Iterable[RemoteItem]:
        """Walk the allowlisted roots and their TRANSITIVE children only.

        Descent edges are ``child_page`` and ``child_block`` block types plus
        database rows of an allowlisted database. ``link_to_page``, mentions and
        linked databases are NEVER traversed. Every yielded page is verified --
        per item -- to chain back to an allowlisted root; the verification is
        carried in ``meta['scope_root']``. Pages the API surfaces that do NOT
        chain to an allowlist root are yielded as ``out_of_scope`` (rc 0).

        The cursor's ``last_edited_time`` map lets an unchanged page be reported
        as ``skipped_unchanged`` rather than re-rendered.
        """
        block = self._source_block(ctx)
        page_ids = [str(p) for p in (block.get("page_ids") or []) if isinstance(block.get("page_ids"), list) and p]
        database_ids = [str(d) for d in (block.get("database_ids") or []) if isinstance(block.get("database_ids"), list) and d]

        from connectors.remote import load_cursor  # local import: shared helper
        cursor = load_cursor(ctx.root, self.id)
        seen_edits = cursor.get("page_edits") if isinstance(cursor.get("page_edits"), dict) else {}

        visited: set = set()
        walked = 0

        # Queue of (page_id, scope_root) to descend. A page enters the queue ONLY
        # via a containment edge from an allowlisted root, so membership in the
        # queue IS the per-item parent-chain proof (P7S-10).
        queue: list = []
        for pid in page_ids:
            queue.append((pid, pid))

        # Database roots: each row page is in-scope under that database root.
        for did in database_ids:
            try:
                rows = list(self._query_database_rows(ctx, did))
            except ConnectorError:
                rows = []
            for row in rows:
                rid = str(row.get("id") or "")
                if rid:
                    queue.append((rid, did))

        while queue:
            if walked >= _MAX_PAGES_WALKED:
                break
            page_id, scope_root = queue.pop(0)
            norm = _norm_id(page_id)
            if norm in visited:
                continue
            visited.add(norm)
            walked += 1

            try:
                page = self._retrieve_page(ctx, page_id)
            except ConnectorError as exc:
                # A page we cannot retrieve (un-shared with the integration, or
                # deleted) is an expected skip, never a failure.
                yield RemoteItem(
                    item_id=page_id, name="notion-page", modified="", size=-1,
                    meta={"out_of_scope": True, "scope_reason": redact(f"page not retrievable: {exc}")},
                )
                continue
            if page.get("archived") or page.get("in_trash"):
                continue

            last_edited = str(page.get("last_edited_time") or "")
            title = _page_title(page)
            url = redact(str(page.get("url") or ""))

            # Render-or-skip-unchanged decision keys on last_edited_time.
            prior = seen_edits.get(norm)
            unchanged = bool(prior) and prior == last_edited and not ctx.dry_run

            item = RemoteItem(
                item_id=page_id,
                name=f"{title}.md",
                modified=last_edited,
                size=-1,
                meta={
                    "scope_root": scope_root,
                    "url": url,
                    "title": title,
                    "last_edited_time": last_edited,
                    "unchanged": unchanged,
                },
            )
            if unchanged:
                # Report the skip but advance nothing; the base only lands fresh
                # items. We surface it as an expected out_of_scope-style note so
                # the base does not attempt a fetch.
                item.meta["out_of_scope"] = True
                item.meta["scope_reason"] = "unchanged since last pull (last_edited_time cursor)"
                yield item
                continue

            # Record the incremental marker for the cursor advance (keyed by the
            # exact item_id the base will echo in the result dict).
            self._edit_marks[page_id] = last_edited

            # Descend ONLY via child_page / child_block edges BEFORE yielding so
            # child PAGES are enqueued as in-scope under the SAME scope_root and
            # picked up by later iterations of this loop.
            self._descend_children(ctx, page_id, scope_root, queue, visited)
            yield item

    def _descend_children(self, ctx, block_id, scope_root, queue, visited) -> list:
        """Walk a block's children once; enqueue child PAGES (containment edges)
        and recurse into child_block sub-trees. Returns discovered child page
        ids (for provenance). link_to_page / mentions are NEVER enqueued."""
        child_page_ids: list = []
        stack = [block_id]
        local_seen: set = set()
        while stack:
            bid = stack.pop()
            if bid in local_seen:
                continue
            local_seen.add(bid)
            try:
                children = list(self._block_children(ctx, bid))
            except ConnectorError:
                continue
            for blk in children:
                btype = blk.get("type")
                bid_child = str(blk.get("id") or "")
                if btype == "child_page" and bid_child:
                    # A child_page is a CONTAINMENT edge -> in scope under root.
                    if _norm_id(bid_child) not in visited:
                        queue.append((bid_child, scope_root))
                    child_page_ids.append(bid_child)
                elif btype == "child_database":
                    # A database nested inside an allowlisted page: its rows are
                    # contained children. Enqueue rows under the SAME root.
                    if bid_child:
                        try:
                            for row in self._query_database_rows(ctx, bid_child):
                                rid = str(row.get("id") or "")
                                if rid and _norm_id(rid) not in visited:
                                    queue.append((rid, scope_root))
                        except ConnectorError:
                            pass
                elif blk.get("has_children") and btype not in _NON_CONTAINMENT_TYPES and bid_child:
                    # A plain block (toggle, column, etc.) that contains MORE
                    # blocks -- a child_block containment edge. Recurse.
                    stack.append(bid_child)
                # link_to_page, mentions, linked_database, child references via
                # rich-text are intentionally NOT followed.
        return child_page_ids

    # -- fetch: render the page's block tree to markdown --------------------- #
    def fetch_item(self, ctx: ConnectorContext, item: RemoteItem) -> Path:
        """Render the page's own block tree (NOT its child pages -- those are
        separate items) to markdown and stage it to a private temp file.

        No bytes are fetched over the network as a download; the markdown is
        assembled from JSON already inside the connector's process frame, so
        ``http_download`` is not used (and ``urllib`` is never imported)."""
        page_id = item.item_id
        title = item.meta.get("title") or "Untitled"
        url = item.meta.get("url") or ""

        lines: list = [f"# {title}", ""]
        if url:
            lines.append(f"> source: {redact(url)}")
            lines.append("")
        lines.extend(self._render_blocks(ctx, page_id, depth=0))

        text = "\n".join(lines).rstrip() + "\n"
        stage_dir = Path(tempfile.mkdtemp(prefix="oracle-notion-"))
        stage = stage_dir / "page.md"
        import os
        fd = os.open(str(stage), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # safe_paths-internal: private temp stage, 0o600
            f.write(text)
        return stage

    def _render_blocks(self, ctx: ConnectorContext, block_id: str, depth: int) -> list:
        """Render a block's direct children to markdown lines. Recurses into
        plain container blocks but STOPS at child_page boundaries (those are
        separate items)."""
        if depth > 50:  # defensive recursion bound
            return []
        out: list = []
        try:
            children = list(self._block_children(ctx, block_id))
        except ConnectorError:
            return out
        for blk in children:
            out.extend(self._render_one(ctx, blk, depth))
        return out

    def _render_one(self, ctx: ConnectorContext, blk: dict, depth: int) -> list:
        btype = str(blk.get("type") or "")
        data = blk.get(btype) if isinstance(blk.get(btype), dict) else {}
        indent = "  " * depth
        out: list = []

        if btype == "child_page":
            # A child page is a SEPARATE item; render a link note only.
            out.append(f"{indent}- [child page] {_clean(data.get('title'))}")
            return out
        if btype == "child_database":
            out.append(f"{indent}- [child database] {_clean(data.get('title'))}")
            return out
        if btype in _FILE_BLOCK_TYPES:
            # File/image/pdf: render a skipped-with-note line (P7S-9). Never
            # download the pre-signed URL.
            cap = _rich_text_to_md(data.get("caption"))
            out.append(f"{indent}- [{btype} attachment skipped — not fetched]"
                       + (f" {cap}" if cap else ""))
            return out

        rt = _rich_text_to_md(data.get("rich_text"))
        if btype in ("paragraph",):
            if rt:
                out.append(f"{indent}{rt}")
        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}[btype]
            out.append(f"{level} {rt}")
        elif btype == "bulleted_list_item":
            out.append(f"{indent}- {rt}")
        elif btype == "numbered_list_item":
            out.append(f"{indent}1. {rt}")
        elif btype == "to_do":
            mark = "x" if data.get("checked") else " "
            out.append(f"{indent}- [{mark}] {rt}")
        elif btype == "toggle":
            out.append(f"{indent}- {rt}")
        elif btype == "quote":
            out.append(f"{indent}> {rt}")
        elif btype == "callout":
            out.append(f"{indent}> {rt}")
        elif btype == "code":
            lang = str(data.get("language") or "")
            out.append(f"{indent}```{lang}")
            out.append(rt)
            out.append(f"{indent}```")
        elif btype == "divider":
            out.append(f"{indent}---")
        elif btype == "table_row":
            # Each cell IS a rich_text array (a list of spans).
            cells = data.get("cells") or []
            rendered = [_clean(_rich_text_to_md(cell)) for cell in cells]
            out.append(f"{indent}| " + " | ".join(rendered) + " |")
        elif rt:
            out.append(f"{indent}{rt}")

        # Recurse into plain containers (toggle, column, etc.) but NOT child
        # pages/databases (handled above with an early return).
        if blk.get("has_children") and btype not in _FILE_BLOCK_TYPES:
            out.extend(self._render_blocks(ctx, str(blk.get("id") or ""), depth + 1))
        return out

    # -- cursor advance (per-page last_edited_time) -------------------------- #
    def _advance_cursor(self, ctx: ConnectorContext, results: list) -> None:
        """Extend the base cursor advance with the per-page last_edited_time map
        so the next pull can skip unchanged pages (incremental)."""
        from connectors.remote import load_cursor, save_cursor
        cur = load_cursor(ctx.root, self.id)
        edits = cur.get("page_edits") if isinstance(cur.get("page_edits"), dict) else {}
        for r in results:
            if r.get("action") == "ingested":
                pid = r.get("item_id")
                le = self._edit_marks.get(pid)
                if pid and le:
                    edits[_norm_id(str(pid))] = str(le)
        cur["page_edits"] = edits
        from datetime import datetime as _dt
        cur["last_success_ts"] = _dt.now().isoformat(timespec="seconds")
        cur["last_ingested_count"] = len([r for r in results if r.get("action") == "ingested"])
        save_cursor(ctx.root, self.id, cur)

    # -- probe / health ------------------------------------------------------ #
    def probe(self, ctx: ConnectorContext) -> dict:
        """Cheap, non-destructive read: count allowlisted roots (no traversal)."""
        block = self._source_block(ctx)
        pids = block.get("page_ids") if isinstance(block.get("page_ids"), list) else []
        dids = block.get("database_ids") if isinstance(block.get("database_ids"), list) else []
        return {
            "connector": self.id,
            "items": len(pids) + len(dids),
            "page_roots": len(pids),
            "database_roots": len(dids),
            "by_suffix": {".md": len(pids) + len(dids)},
        }

    def health(self, ctx: ConnectorContext) -> dict:
        """healthy | degraded | broken for the Notion integration.

        broken   -> read_write misuse, unresolved auth vars, or an empty scope
                    allowlist (default-deny).
        """
        notes: list = []
        try:
            self._assert_read_only()
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[redact(str(exc))])
        try:
            resolve_auth(ctx.root, self.manifest)
        except ConnectorError as exc:
            return self.health_envelope(
                "broken",
                notes=[redact(str(exc)),
                       "fix: set the integration token in <root>/.env.nosync "
                       "(Notion > Settings > Connections > internal integration token)"],
            )
        try:
            self._assert_scope_allowlist(ctx)
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[redact(str(exc))])

        probe = self.probe(ctx)
        fresh = self.freshness(ctx)
        state = "healthy"
        if fresh.get("verdict") == "stale":
            state = "degraded"
            notes.append("source is past its freshness SLA")
        elif fresh.get("verdict") == "unknown":
            notes.append("freshness unknown (no successful pull yet)")
        return self.health_envelope(state, notes=notes, probe=probe, freshness=fresh)


# --------------------------------------------------------------------------- #
# rendering helpers (pure, stdlib)
# --------------------------------------------------------------------------- #
# Block types that are references/attachments, NOT containment edges. A
# link_to_page is a REFERENCE: it must never be followed for scope.
_NON_CONTAINMENT_TYPES = frozenset({
    "link_to_page", "child_page", "child_database",
    "file", "image", "pdf", "video", "audio", "embed", "bookmark",
})
_FILE_BLOCK_TYPES = frozenset({"file", "image", "pdf", "video", "audio"})


def _norm_id(page_id: str) -> str:
    """Normalize a Notion id (strip dashes, lowercase) for stable comparison."""
    return str(page_id or "").replace("-", "").lower()


def _rich_text_to_md(rich) -> str:
    """Flatten a Notion rich_text array to a markdown string.

    Mentions and links are rendered as their plain text -- the link TARGET is
    not followed for scope (P7S-10); only its display text enters the
    transcript."""
    if not isinstance(rich, list):
        return ""
    parts: list = []
    for span in rich:
        if not isinstance(span, dict):
            continue
        txt = span.get("plain_text")
        if txt is None:
            t = span.get("text") if isinstance(span.get("text"), dict) else {}
            txt = t.get("content") if isinstance(t, dict) else ""
        txt = str(txt or "")
        ann = span.get("annotations") if isinstance(span.get("annotations"), dict) else {}
        if ann.get("code"):
            txt = f"`{txt}`"
        if ann.get("bold"):
            txt = f"**{txt}**"
        if ann.get("italic"):
            txt = f"*{txt}*"
        parts.append(txt)
    return _clean("".join(parts))


def _clean(s) -> str:
    """Collapse newlines to spaces so a rich-text run stays on one md line."""
    return str(s or "").replace("\r", " ").replace("\n", " ").strip()


def _page_title(page: dict) -> str:
    """Extract a page's title from its properties (the title-typed property)."""
    props = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    for _name, prop in props.items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            t = _rich_text_to_md(prop.get("title"))
            if t:
                return t
    # Fallback for non-database pages whose title lives elsewhere.
    return "Untitled"


# --------------------------------------------------------------------------- #
# registry hook (orchestrator wires this; tests call it directly)
# --------------------------------------------------------------------------- #
def build(manifest: dict) -> NotionConnector:
    """Factory used by the connector registry."""
    return NotionConnector(manifest)


def register() -> None:
    """Register this connector id-only with system-fallback (P7S-6).

    The orchestrator calls this from ``connectors/__init__`` import wiring; tests
    call it directly. Idempotent."""
    try:
        import connectors  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        from .. import connectors  # type: ignore
    connectors.register(ID, build, system=SYSTEM)
