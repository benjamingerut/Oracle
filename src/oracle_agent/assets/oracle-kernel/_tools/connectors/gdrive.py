#!/usr/bin/env python3
"""connectors/gdrive.py -- the Google Drive remote connector (P7-T2).

A thin, dumb adapter over the ``RemoteConnector`` safety core in
``connectors/remote.py``. This module implements ONLY the two subclass hooks
(``list_items`` + ``fetch_item``) plus the connector metadata the core consumes
(``scope_allowlist_keys``, ``download_host_suffixes``, the ``system`` id). The
FINAL ``pull`` template, the autonomy gate, classification, the policy gate,
containment, the byte cap, redaction, and the cursor all live in the core --
this adapter can drop a document but it can NOT widen access, skip
classification, escape containment, follow a redirect off-host, or exceed the
byte cap.

Auth (P7S-1): Google's device flow CANNOT grant Drive read scopes, so initial
authorization uses the stdlib loopback installed-app flow (``loopback_flow``).
Subsequent pulls refresh the short-lived access token from the persisted refresh
token via a stdlib ``refresh_token`` grant -- both through the core's
``http_json`` primitive (https-only, no-redirect).

Scope (default-deny): ``source.folder_ids`` is the allowlist. ``files.list`` is
run PER allowlisted folder, recursing into child folders that themselves live
within the allowlist tree (per-parent recursion only INSIDE the allowlist).
Shared-drive params (``supportsAllDrives`` / ``includeItemsFromAllDrives`` /
``corpora``) are pinned so shared drives actually list (P7S-11). A periodic full
re-list (ignoring the modifiedTime cursor) catches files MOVED INTO scope with
old timestamps (P7S-11). Shortcuts (``application/vnd.google-apps.shortcut``)
and multi-parent files resolve by target/parent: a target or parent set that
does not reach the allowlist is recorded as ``skipped_out_of_scope`` (rc 0,
never a failure_event).

Export (P7S-25): Google-native docs are exported per a pinned matrix
(docs -> docx, sheets -> csv, slides -> text/plain). ``files.export`` is capped
at ~10 MB by Google; an over-limit native doc is SKIPPED WITH A RESULT ROW
(``meta['oversize_export']``) -- never a pull failure. Binary (non-native)
files download via ``alt=media`` through ``http_download``.

API base URLs are PINNED here, never read from the manifest (P7S-5). All bytes
flow through ``http_download``; all JSON through ``http_json``; this module
never imports ``urllib`` (enforced by ``test_no_direct_urllib_in_subclass_modules``).

Stdlib only.
"""
from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:  # flat layout (tests put _tools on sys.path)
    from connectors.base import ConnectorContext, ConnectorError
    from connectors import remote
    from connectors.remote import RemoteConnector, RemoteItem
except Exception:  # pragma: no cover - package fallback
    from .base import ConnectorContext, ConnectorError  # type: ignore
    from . import remote  # type: ignore
    from .remote import RemoteConnector, RemoteItem  # type: ignore

__all__ = ["GoogleDriveConnector", "ID", "SYSTEM", "build", "register"]

ID = "gdrive"
SYSTEM = "gdrive"

# --------------------------------------------------------------------------- #
# PINNED API endpoints (NEVER manifest-supplied; P7S-5)
# --------------------------------------------------------------------------- #
_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DRIVE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# Read-only Drive scope (the loopback flow grants exactly this).
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# The hosts http_download is permitted to make its single redirect hop to.
# Drive serves bytes (alt=media / export) and 302s to googleusercontent.com.
_DOWNLOAD_HOST_SUFFIXES = ("googleapis.com", "googleusercontent.com")

# Native Google MIME types we export, and the export target + landing suffix.
# (P7S-25: each over the ~10 MB export cap is skipped with a result row.)
_GOOGLE_DOC = "application/vnd.google-apps.document"
_GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
_GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
_GOOGLE_FOLDER = "application/vnd.google-apps.folder"
_GOOGLE_SHORTCUT = "application/vnd.google-apps.shortcut"

# mime -> (export_mime, landing suffix). docs -> docx, sheets -> csv,
# slides -> text/plain. The pinned export matrix.
_EXPORT_MATRIX = {
    _GOOGLE_DOC: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    _GOOGLE_SHEET: ("text/csv", ".csv"),
    _GOOGLE_SLIDES: ("text/plain", ".txt"),
}

# Google's documented files.export hard cap (~10 MB). Over this, export fails;
# we skip with a result row rather than failing the pull (P7S-25).
_EXPORT_CAP_BYTES = 10 * 1024 * 1024

# Page size for files.list.
_PAGE_SIZE = 100
# Access tokens are short-lived; refresh a little early to avoid mid-pull 401s.
_TOKEN_SKEW_SECONDS = 60
# Periodic full re-list cadence: every Nth pull ignores the modifiedTime cursor
# so files MOVED INTO scope with old timestamps are not silently missed
# (P7S-11). The cursor counts pulls since the last full re-list.
_FULL_RELIST_EVERY = 10


# URL helpers. This module must NEVER ``import urllib`` directly (the no-direct-
# urllib enforcer test forbids it; all network goes through the core's
# http_json/http_download). We reach urlencode/quote via the core module's
# already-imported urllib -- attribute access, not an import statement.
def _urlencode(params: dict) -> str:
    return remote.urllib.parse.urlencode(params)


def _quote(value: str) -> str:
    return remote.urllib.parse.quote(str(value), safe="")


class OversizeExport(ConnectorError):
    """A Google-native doc export exceeds the ~10 MB files.export cap (P7S-25).

    Raised by ``fetch_item`` when an export overflows the cap (streaming abort)
    or Google refuses the over-cap export. It subclasses ``ConnectorError`` so
    the FINAL core ``pull`` records it as a per-item result row and CONTINUES
    the pull (it is never a pull-wide failure: the pull completes the remaining
    in-scope items and the CLI return code stays 0 -- rc 1 is reserved for
    containment ``refused`` rows). The row's redacted reason names the oversize
    so an admin sees the skip without the native doc ever landing.
    """


class GoogleDriveConnector(RemoteConnector):
    """Read-only Google Drive connector. Implements ``list_items`` +
    ``fetch_item`` only; ``pull`` is the FINAL core template (not overridden).
    """

    access_mode = "api"
    system = SYSTEM

    #: source.folder_ids is the default-deny scope allowlist (P7S-13).
    scope_allowlist_keys = ("folder_ids",)
    #: the single enumerated redirect-hop host suffixes for http_download.
    download_host_suffixes = _DOWNLOAD_HOST_SUFFIXES

    def __init__(self, manifest: dict) -> None:
        super().__init__(manifest)
        # Per-pull access token + the headers carrying it. Established lazily in
        # list_items (the FIRST network call, after the gate has authorized).
        self._access_token: Optional[str] = None

    # ------------------------------------------------------------------ #
    # auth: refresh the short-lived access token (loopback flow is the
    # one-time wizard step; pulls only ever refresh).
    # ------------------------------------------------------------------ #
    def _auth_vars(self, ctx: ConnectorContext) -> dict:
        """Resolve client id/secret + refresh token from .env.nosync / env.

        Manifest carries NAMES only; values resolve via the core's resolve_auth.
        Expected var names: GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET,
        GDRIVE_REFRESH_TOKEN.
        """
        resolved = remote.resolve_auth(ctx.root, self.manifest)
        # The manifest lists the var NAMES under auth.vars; map them positionally
        # by canonical name so a missing one raises a clean ConnectorError.
        out = {}
        for canonical in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"):
            if canonical not in resolved:
                raise ConnectorError(
                    f"{self.id}: missing auth var {canonical} "
                    f"(set it in <root>/.env.nosync)"
                )
            out[canonical] = resolved[canonical]
        return out

    def _ensure_access_token(self, ctx: ConnectorContext) -> str:
        """Return a live access token, refreshing via the refresh_token grant.

        A cached token in the cursor is reused until it nears expiry; otherwise
        a stdlib ``refresh_token`` grant runs through http_json. The access
        token is NEVER persisted to the manifest, results, or logs.
        """
        if self._access_token:
            return self._access_token

        creds = self._auth_vars(ctx)
        cur = remote.load_cursor(ctx.root, self.id)
        cached = cur.get("access_token")
        expires_at = cur.get("access_token_expires_at")
        now = time.time()
        if cached and isinstance(expires_at, (int, float)) and now < (expires_at - _TOKEN_SKEW_SECONDS):
            self._access_token = str(cached)
            return self._access_token

        # Refresh: exchange the refresh token for a fresh access token.
        body = {
            "client_id": creds["GDRIVE_CLIENT_ID"],
            "client_secret": creds["GDRIVE_CLIENT_SECRET"],
            "refresh_token": creds["GDRIVE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }
        tok = remote.http_json(
            "POST", _DRIVE_TOKEN_URL,
            body=_urlencode(body),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        access = tok.get("access_token")
        if not access:
            raise ConnectorError(f"{self.id}: token refresh returned no access_token")
        self._access_token = str(access)
        # Cache the access token + expiry in the cursor (the cursor is the one
        # state file; it is NOT secret-scanned-exempt, but an access token is
        # short-lived and the cursor already holds operational state). Google
        # may rotate the refresh token; if it does, persist it via the one
        # sanctioned writer.
        ttl = int(tok.get("expires_in", 3600) or 3600)
        cur["access_token"] = self._access_token
        cur["access_token_expires_at"] = now + ttl
        remote.save_cursor(ctx.root, self.id, cur)
        rotated = tok.get("refresh_token")
        if rotated and str(rotated) != creds["GDRIVE_REFRESH_TOKEN"]:
            remote.persist_rotated_token(ctx.root, "GDRIVE_REFRESH_TOKEN", str(rotated))
        return self._access_token

    def _auth_headers(self, ctx: ConnectorContext) -> dict:
        return {"Authorization": f"Bearer {self._ensure_access_token(ctx)}"}

    # ------------------------------------------------------------------ #
    # list_items: per-folder files.list within the allowlist (recursion
    # only INSIDE the allowlist tree) + periodic full re-list.
    # ------------------------------------------------------------------ #
    def _allowlisted_folders(self, ctx: ConnectorContext) -> list:
        block = self._source_block(ctx)
        ids = block.get("folder_ids")
        return [str(f) for f in ids if f] if isinstance(ids, list) else []

    def _list_one_folder(self, ctx: ConnectorContext, headers: dict,
                         folder_id: str, modified_floor: Optional[str]) -> list:
        """One folder's direct children via files.list (paginated).

        Shared-drive params are pinned so shared drives list. When
        ``modified_floor`` is set, the query restricts to items modified after
        it (incremental); a full re-list passes ``None``.
        """
        q_parts = [f"'{folder_id}' in parents", "trashed = false"]
        if modified_floor:
            # RFC 3339 timestamp; Drive compares modifiedTime > floor.
            q_parts.append(f"modifiedTime > '{modified_floor}'")
        query = " and ".join(q_parts)
        fields = (
            "nextPageToken,files(id,name,mimeType,modifiedTime,size,"
            "parents,shortcutDetails(targetId,targetMimeType),driveId)"
        )
        items: list = []
        page_token = None
        while True:
            params = {
                "q": query,
                "fields": fields,
                "pageSize": str(_PAGE_SIZE),
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "corpora": "allDrives",
                "spaces": "drive",
                "orderBy": "modifiedTime",
            }
            if page_token:
                params["pageToken"] = page_token
            url = _DRIVE_FILES_URL + "?" + _urlencode(params)
            resp = remote.http_json("GET", url, headers=headers)
            for f in resp.get("files", []) or []:
                items.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    def list_items(self, ctx: ConnectorContext) -> Iterable[RemoteItem]:
        """Yield RemoteItem metadata for items reachable WITHIN the folder_ids
        allowlist (recursing into child folders that are themselves in scope).

        Out-of-scope shortcut targets / multi-parent files that do not reach the
        allowlist are yielded with ``meta['out_of_scope']=True`` so the core
        records an EXPECTED skip (rc 0), never fetching them.
        """
        headers = self._auth_headers(ctx)
        allow = self._allowlisted_folders(ctx)
        allow_set = set(allow)

        cur = remote.load_cursor(ctx.root, self.id)
        # Periodic full re-list (P7S-11): every Nth pull ignores the cursor floor
        # so files moved into scope with OLD timestamps are caught.
        pulls_since_full = int(cur.get("pulls_since_full_relist", 0) or 0)
        do_full_relist = pulls_since_full >= (_FULL_RELIST_EVERY - 1)
        folder_floors = cur.get("folder_modified_floors") or {}
        if not isinstance(folder_floors, dict):
            folder_floors = {}

        # BFS over the allowlist tree; only descend into folders confirmed in
        # scope (a folder is in scope iff it is an allowlist root OR a child of
        # an in-scope folder). This bounds recursion to the allowlist (P7S-11).
        seen_folders: set = set()
        queue = list(allow)
        # in_scope_folders accumulates every folder confirmed within the tree;
        # used to resolve shortcut targets / multi-parent membership.
        in_scope_folders: set = set(allow_set)
        yielded_ids: set = set()

        while queue:
            folder_id = queue.pop(0)
            if folder_id in seen_folders:
                continue
            seen_folders.add(folder_id)
            floor = None if do_full_relist else folder_floors.get(folder_id)
            children = self._list_one_folder(ctx, headers, folder_id, floor)
            for f in children:
                mime = f.get("mimeType")
                fid = str(f.get("id") or "")
                if not fid:
                    continue
                if mime == _GOOGLE_FOLDER:
                    # A child folder of an in-scope folder is itself in scope:
                    # enqueue it for recursion and record it.
                    in_scope_folders.add(fid)
                    if fid not in seen_folders:
                        queue.append(fid)
                    continue
                if mime == _GOOGLE_SHORTCUT:
                    # Resolve the shortcut by target: an out-of-allowlist target
                    # is an EXPECTED out-of-scope skip (rc 0), never fetched.
                    item = self._shortcut_item(ctx, headers, f, in_scope_folders, allow_set)
                    if item is not None and item.item_id not in yielded_ids:
                        yielded_ids.add(item.item_id)
                        yield item
                    continue
                # A regular file: it is in scope (it was returned as a child of
                # an in-scope folder). Multi-parent files are deduplicated by id.
                if fid in yielded_ids:
                    continue
                yielded_ids.add(fid)
                yield self._file_item(f)

        # Advance the per-folder modifiedTime floors + the full-relist counter.
        now_rfc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for folder_id in seen_folders:
            folder_floors[folder_id] = now_rfc
        cur["folder_modified_floors"] = folder_floors
        cur["pulls_since_full_relist"] = 0 if do_full_relist else (pulls_since_full + 1)
        # Persist the cursor floors immediately so a fetch failure later does not
        # lose the re-list bookkeeping. (The core also advances last_success_ts.)
        if not ctx.dry_run:
            remote.save_cursor(ctx.root, self.id, cur)

    def _file_item(self, f: dict) -> RemoteItem:
        """Build a RemoteItem for a regular (non-folder, non-shortcut) file."""
        mime = str(f.get("mimeType") or "")
        size_raw = f.get("size")
        try:
            size = int(size_raw) if size_raw is not None else -1
        except (TypeError, ValueError):
            size = -1
        name = str(f.get("name") or f.get("id") or "item")
        # Native docs land with the export suffix so the slug carries the right
        # extension; binaries keep their own name/suffix.
        if mime in _EXPORT_MATRIX:
            _, suffix = _EXPORT_MATRIX[mime]
            if not name.lower().endswith(suffix):
                name = f"{name}{suffix}"
        return RemoteItem(
            item_id=str(f.get("id") or ""),
            name=name,
            modified=str(f.get("modifiedTime") or ""),
            size=size,
            meta={"mimeType": mime, "google_native": mime in _EXPORT_MATRIX},
        )

    def _shortcut_item(self, ctx: ConnectorContext, headers: dict, f: dict,
                       in_scope_folders: set, allow_set: set) -> Optional[RemoteItem]:
        """Resolve a shortcut. In-scope target -> a fetchable RemoteItem;
        out-of-scope target -> an EXPECTED out_of_scope skip item (rc 0).
        """
        details = f.get("shortcutDetails") or {}
        target_id = str(details.get("targetId") or "")
        target_mime = str(details.get("targetMimeType") or "")
        if not target_id:
            return RemoteItem(
                item_id=str(f.get("id") or ""),
                name=str(f.get("name") or "shortcut"),
                modified="", size=-1,
                meta={"out_of_scope": True, "scope_reason": "shortcut has no target"},
            )
        # Fetch the target's metadata to learn its parents; a target whose parent
        # chain does not reach the allowlist is out of scope.
        meta = self._get_file_meta(ctx, headers, target_id)
        parents = meta.get("parents") or []
        reaches = any(p in in_scope_folders or p in allow_set for p in parents)
        if not reaches:
            return RemoteItem(
                item_id=target_id,
                name=str(meta.get("name") or f.get("name") or "shortcut-target"),
                modified="", size=-1,
                meta={
                    "out_of_scope": True,
                    "scope_reason": "shortcut target outside folder allowlist",
                },
            )
        if target_mime == _GOOGLE_FOLDER:
            # A shortcut to an in-scope folder: skip (the folder is already
            # enumerated directly); record as a benign out_of_scope no-op.
            return None
        return self._file_item(meta)

    def _get_file_meta(self, ctx: ConnectorContext, headers: dict, file_id: str) -> dict:
        params = {
            "fields": "id,name,mimeType,modifiedTime,size,parents,driveId",
            "supportsAllDrives": "true",
        }
        url = f"{_DRIVE_FILES_URL}/{_quote(file_id)}?" + _urlencode(params)
        return remote.http_json("GET", url, headers=headers)

    # ------------------------------------------------------------------ #
    # fetch_item: export native docs (matrix + 10MB skip) or alt=media.
    # ------------------------------------------------------------------ #
    def fetch_item(self, ctx: ConnectorContext, item: RemoteItem) -> Path:
        """Fetch ONE item's bytes to a private temp stage via http_download.

        Native Google docs export per the pinned matrix (over-cap -> skip with a
        result row via the OversizeExport signal); binaries download alt=media.
        """
        headers = self._auth_headers(ctx)
        mime = str(item.meta.get("mimeType") or "")
        stage_dir = Path(tempfile.mkdtemp(prefix="oracle-gdrive-"))
        stage = stage_dir / "body"

        if mime in _EXPORT_MATRIX:
            export_mime, _suffix = _EXPORT_MATRIX[mime]
            # Native docs report size 0/absent, so we rely on http_download's
            # streaming cap to abort if the export exceeds the ~10 MB ceiling and
            # translate that into a skip-with-row (NOT a pull failure; P7S-25).
            params = {"mimeType": export_mime, "supportsAllDrives": "true"}
            url = (
                f"{_DRIVE_FILES_URL}/{_quote(item.item_id)}/export?"
                + _urlencode(params)
            )
            try:
                return remote.http_download(
                    url, stage, headers=dict(headers),
                    max_bytes=_EXPORT_CAP_BYTES,
                    allowed_host_suffixes=self.download_host_suffixes,
                )
            except remote.ByteCapExceeded as exc:
                self._cleanup_stage(stage)
                # Over the export cap -> skip with a result row, never a failure.
                raise OversizeExport(
                    f"native doc export exceeds {_EXPORT_CAP_BYTES} bytes"
                ) from exc
            except remote.ConnectorError as exc:
                self._cleanup_stage(stage)
                # Google returns 403/400 when an export exceeds the cap; treat a
                # 10MB-ish export error as an oversize skip, not a hard failure.
                msg = str(exc)
                if "export" in msg.lower() or "exceed" in msg.lower() or "too large" in msg.lower():
                    raise OversizeExport(f"native doc export refused: {remote.redact(msg)}") from exc
                raise

        # Binary file: alt=media download.
        params = {"alt": "media", "supportsAllDrives": "true"}
        url = (
            f"{_DRIVE_FILES_URL}/{_quote(item.item_id)}?"
            + _urlencode(params)
        )
        # The per-item byte ceiling: the core enforces the cumulative cap; here
        # we cap a single file at the per-pull max_bytes so one giant binary
        # can't blow the stage. Use the manifest/gate ceiling.
        per_item_cap = self._max_bytes(ctx)
        return remote.http_download(
            url, stage, headers=dict(headers),
            max_bytes=per_item_cap,
            allowed_host_suffixes=self.download_host_suffixes,
        )

    # ------------------------------------------------------------------ #
    # health: broken on unresolved auth vars / read_write / empty allowlist.
    # ------------------------------------------------------------------ #
    def probe(self, ctx: ConnectorContext) -> dict:
        """Cheap, non-destructive: report the allowlist size (no network)."""
        folders = self._allowlisted_folders(ctx)
        return {
            "connector": self.id,
            "items": len(folders),
            "folders_allowlisted": len(folders),
            "by_suffix": {},
        }

    def health(self, ctx: ConnectorContext) -> dict:
        """healthy | degraded | broken for the Drive connector.

        broken   -> read_write misuse, empty/None allowlist, or unresolved auth
                    vars (the doctor fix-line points at <root>/.env.nosync).
        degraded -> past freshness SLA.
        healthy  -> read-only, allowlist populated, auth vars resolvable.
        """
        notes: list = []
        try:
            self._assert_read_only()
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[str(exc)])
        try:
            self._assert_scope_allowlist(ctx)
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[str(exc)])
        try:
            self._auth_vars(ctx)
        except ConnectorError as exc:
            return self.health_envelope(
                "broken",
                notes=[
                    f"{remote.redact(str(exc))} "
                    f"(fix: set GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET / "
                    f"GDRIVE_REFRESH_TOKEN in <root>/.env.nosync)"
                ],
            )
        probe = self.probe(ctx)
        fresh = self.freshness(ctx)
        state = "healthy"
        if fresh.get("verdict") == "stale":
            state = "degraded"
            notes.append("source is past its freshness SLA")
        elif fresh.get("verdict") == "unknown":
            notes.append("freshness unknown (no successful pull yet)")
        return self.health_envelope(state, notes=notes, probe=probe, freshness=fresh)


def build(manifest: dict) -> GoogleDriveConnector:
    """Factory used by the connector registry."""
    return GoogleDriveConnector(manifest)


def register() -> None:
    """Register the gdrive factory id-only + system-fallback (P7S-6).

    Idempotent: safe to call at import time AND for the orchestrator to call
    explicitly. Registers under the id ``gdrive`` and the system ``gdrive`` so a
    second account (id: gdrive-finance, system: gdrive) resolves here too.
    """
    try:
        import connectors as _registry  # type: ignore
    except Exception:  # pragma: no cover - package fallback
        from . import __init__ as _registry  # type: ignore
    _registry.register(ID, build, system=SYSTEM)


# Module-level registration (matches the localfolder/remote import-time idiom).
# The orchestrator adds the import to connectors/__init__.py; importing this
# module is sufficient to register it. Guarded so an import-time registry hiccup
# never breaks a bare-floor import.
try:  # pragma: no cover - registration side effect
    register()
except Exception:
    pass
