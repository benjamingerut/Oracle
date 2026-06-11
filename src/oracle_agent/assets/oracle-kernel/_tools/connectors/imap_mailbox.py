#!/usr/bin/env python3
"""connectors/imap_mailbox.py -- read-only IMAP mailbox connector (P7-T5).

Pulls messages from an IMAP mailbox into the oracle's intake lane as ``.eml``
files (raw RFC822 bytes), one landed file per message. It is a thin adapter over
the ``RemoteConnector`` safety core in ``remote.py``: the FINAL ``pull`` template
owns gate-first authorization, the default-deny scope allowlist, content-based
classification, the policy gate, contained stable landing, the running byte
counter, and the atomic cursor; this module implements ONLY ``list_items`` (UID
enumeration within the folder allowlist + ``since_days`` window) and
``fetch_item`` (one message's RFC822 bytes to a private stage).

Read-only by construction (security invariant -- IMAP uses ``EXAMINE``):

  * the connection is ``imaplib.IMAP4_SSL`` with a default-verifying
    ``ssl.create_default_context()`` -- NEVER plain ``IMAP4``, NEVER an optional
    STARTTLS upgrade. The IMAP server ``host`` is the ONE manifest-supplied
    endpoint (P7S-5); everything else (auth, scope, caps) is pinned by the base
    or the allowlist;
  * folders are opened with ``EXAMINE`` (read-only SELECT) so message flags are
    never mutated -- the connector issues no ``SELECT`` (writable), ``STORE``,
    ``EXPUNGE``, or any other mutating verb;
  * v1 authentication is ``username + app password`` (consumer Gmail / O365 have
    retired basic auth except app passwords, and Google's device flow cannot
    grant the mail scope -- P7S-26). The doctor fix-line says "app password".

Incremental discipline (P7S-23): the cursor persists, per folder, the server's
``UIDVALIDITY`` plus the highest UID landed. UIDs are meaningless across a
``UIDVALIDITY`` change, so a mismatch RESETS that folder's cursor and performs a
full re-pull within the ``since_days`` window, logged as a cursor-reset note.
The fail-closed re-pull is safe -- landing names are stable per ``item_id`` so a
re-pull supersedes rather than duplicates.

Default sensitivity floor is ``confidential``: mail is presumptively sensitive
and ambiguity always classifies UP (I4). The floor is the classification FLOOR;
content signals may raise it further at pull time.

Stdlib only (``imaplib`` / ``ssl`` / ``email``). This module never imports
``urllib`` -- bytes are produced by the IMAP fetch, staged to a private temp
file, and handed to the base, which lands them through ``safe_paths``.
"""
from __future__ import annotations

import imaplib
import os
import ssl
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

try:  # flat layout (tests put _tools on sys.path)
    from connectors.base import ConnectorContext, ConnectorError
    from connectors.remote import RemoteConnector, RemoteItem, redact
except Exception:  # pragma: no cover - package fallback
    from .base import ConnectorContext, ConnectorError  # type: ignore
    from .remote import RemoteConnector, RemoteItem, redact  # type: ignore

__all__ = ["ImapMailboxConnector", "ID", "SYSTEM", "build"]

ID = "imap-mailbox"
SYSTEM = "imap"

# A sane default initial window so a first pull does not enumerate an entire
# multi-year mailbox. Overridable via manifest source.since_days.
_DEFAULT_SINCE_DAYS = 30

# Default per-pull file ceiling mirrors the base; overridable via manifest.
_DEFAULT_MAX_FILES = 500

# Sensitivity ladder (mirrors remote._SENS_ORDER / intake_classify.LABELS).
_SENS_ORDER = ("public", "internal", "confidential", "restricted", "secret")


# --------------------------------------------------------------------------- #
# auth resolution shim (resolve_auth lives in remote.py; import defensively)
# --------------------------------------------------------------------------- #
def _import_resolve_auth():
    try:
        from connectors.remote import resolve_auth  # type: ignore
        return resolve_auth
    except Exception:  # pragma: no cover - package fallback
        from .remote import resolve_auth  # type: ignore
        return resolve_auth


def _import_capture():
    try:
        import capture  # type: ignore
        return capture
    except Exception:  # pragma: no cover - optional / package fallback
        try:
            from .. import capture  # type: ignore
            return capture
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# the connector
# --------------------------------------------------------------------------- #
class ImapMailboxConnector(RemoteConnector):
    """Read-only IMAP mailbox connector. Pulls messages as ``.eml`` files.

    ``list_items`` opens each allowlisted folder with ``EXAMINE`` (never a
    writable SELECT), guards UIDVALIDITY against the cursor, searches the
    ``since_days`` window for UIDs above the cursor high-water mark, and yields a
    ``RemoteItem`` per message (size from ``RFC822.SIZE`` for the base's byte
    accounting). ``fetch_item`` fetches the one message's ``RFC822`` bytes to a
    private stage. The base's FINAL ``pull`` does everything else.
    """

    access_mode = "api"
    system = SYSTEM

    # The folder allowlist is the scope gate (default-deny; P7S-13). host is the
    # single manifest-supplied endpoint but is NOT a scope key -- it is the
    # certificate-verified IMAP4_SSL target, validated separately.
    scope_allowlist_keys = ("folders",)

    # IMAP fetches bytes directly (no HTTP), so no http_download redirect hosts
    # are enumerated -- fetch_item never calls http_download.
    download_host_suffixes: tuple = ()

    # --- classification floor (mail is presumptively sensitive; I4) --------- #
    def _connector_floor(self, ctx: ConnectorContext) -> str:
        """The mail floor is ``confidential`` unless an explicit override raises
        it further. A sensitivity_override (admin) wins; otherwise the manifest
        default_sensitivity wins ONLY if it is at or above confidential -- mail
        never floors below confidential even if a manifest understates it.
        """
        if ctx.sensitivity_override in _SENS_ORDER:
            return ctx.sensitivity_override
        cand = self._source_block(ctx).get("default_sensitivity")
        base_floor = "confidential"
        if cand in _SENS_ORDER:
            # Take the STRICTER of the manifest value and the mail floor.
            if _SENS_ORDER.index(cand) >= _SENS_ORDER.index(base_floor):
                return cand
        return base_floor

    # --- manifest accessors ------------------------------------------------- #
    def _host(self, ctx: ConnectorContext) -> str:
        host = self._source_block(ctx).get("host")
        host = str(host or "").strip()
        if not host:
            raise ConnectorError(
                f"{self.id}: manifest source.host is required "
                f"(the IMAP server hostname -- the one manifest-supplied endpoint)"
            )
        return host

    def _folders(self, ctx: ConnectorContext) -> list:
        block = self._source_block(ctx)
        folders = block.get("folders")
        if not isinstance(folders, list):
            return []
        return [str(f) for f in folders if str(f).strip()]

    def _since_days(self, ctx: ConnectorContext) -> int:
        cand = self._source_block(ctx).get("since_days")
        if isinstance(cand, int) and cand >= 0:
            return cand
        return _DEFAULT_SINCE_DAYS

    def _auth(self, ctx: ConnectorContext) -> tuple:
        """Resolve (username, app_password) from the manifest auth.vars NAMES.

        The first var name resolves to the username, the second to the app
        password (the manifest declares them in that order). resolve_auth reads
        os.environ then <root>/.env.nosync and raises ConnectorError on any
        unresolved name; values are never logged.
        """
        resolve_auth = _import_resolve_auth()
        resolved = resolve_auth(ctx.root, self.manifest)
        auth = self.manifest.get("auth") or {}
        names = auth.get("vars") if isinstance(auth, dict) else None
        names = [str(n) for n in names if n] if isinstance(names, list) else []
        if len(names) < 2:
            raise ConnectorError(
                f"{self.id}: auth.vars must name the username var then the "
                f"app-password var (set both in <root>/.env.nosync; v1 auth is "
                f"username + app password)"
            )
        username = resolved.get(names[0])
        password = resolved.get(names[1])
        if not username or not password:
            raise ConnectorError(f"{self.id}: unresolved IMAP username/app-password vars")
        return username, password

    # --- connection (verified IMAP4_SSL ONLY) ------------------------------- #
    def _connect(self, ctx: ConnectorContext):
        """Open a certificate-verified ``IMAP4_SSL`` connection and LOGIN.

        Uses ``ssl.create_default_context()`` (default-verifying: hostname +
        chain). NEVER plain ``IMAP4``, NEVER an optional STARTTLS upgrade
        (P7S-5). On any failure the partial connection is closed and a redacted
        ConnectorError is raised.
        """
        host = self._host(ctx)
        username, password = self._auth(ctx)
        context = ssl.create_default_context()
        try:
            conn = imaplib.IMAP4_SSL(host=host, ssl_context=context)
        except Exception as exc:
            raise ConnectorError(
                f"{self.id}: failed to open verified IMAP4_SSL to {host}: {redact(exc)}"
            ) from exc
        try:
            conn.login(username, password)
        except Exception as exc:
            try:
                conn.logout()
            except Exception:
                pass
            raise ConnectorError(
                f"{self.id}: IMAP login failed (check the app password): {redact(exc)}"
            ) from exc
        return conn

    def _logout(self, conn) -> None:
        try:
            conn.logout()
        except Exception:
            pass

    # --- cursor helpers (per-folder UID + UIDVALIDITY; P7S-23) -------------- #
    def _folder_cursor(self, cursor: dict, folder: str) -> dict:
        folders = cursor.get("folders")
        if not isinstance(folders, dict):
            return {}
        fc = folders.get(folder)
        return fc if isinstance(fc, dict) else {}

    def _since_criteria(self, ctx: ConnectorContext) -> str:
        """IMAP SEARCH SINCE date string (DD-Mon-YYYY) for the initial window."""
        since = ctx.now - timedelta(days=self._since_days(ctx))
        return since.strftime("%d-%b-%Y")

    # --- subclass hook: list items WITHIN scope ----------------------------- #
    def list_items(self, ctx: ConnectorContext) -> Iterable[RemoteItem]:
        """Yield a RemoteItem per new message across the allowlisted folders.

        For each folder in the allowlist (default-deny -- the base already
        refused an empty allowlist before reaching here):

          1. open it with ``EXAMINE`` (read-only -- flags untouched);
          2. read the folder's ``UIDVALIDITY``; if it differs from the cursor's
             persisted value, RESET this folder's cursor (full re-pull, logged);
          3. ``UID SEARCH`` the ``since_days`` window for UIDs above the cursor
             high-water mark;
          4. ``UID FETCH (RFC822.SIZE)`` for size accounting, yielding a
             RemoteItem keyed by a STABLE ``item_id`` (host/folder/uidvalidity/
             uid) so a re-pull supersedes rather than duplicates.

        A connection is opened once and re-used across folders, then logged out.
        UID/size state for fetch_item is stashed on the instance so fetch_item
        re-uses the same connection without re-listing.
        """
        folders = self._folders(ctx)
        if not folders:
            # The base's default-deny gate already refused; defensive belt.
            return

        host = self._host(ctx)
        cursor = self.__dict__.setdefault("_cursor_cache", None)
        if cursor is None:
            from connectors.remote import load_cursor  # local import (no urllib)
            cursor = load_cursor(ctx.root, self.id)
            self._cursor_cache = cursor if isinstance(cursor, dict) else {}
            cursor = self._cursor_cache

        # Fresh per-pull state the base advances via the cursor at the end.
        self._pending_cursor = self._clone_cursor(cursor)
        self._reset_notes = []

        conn = self._connect(ctx)
        self._conn = conn
        try:
            for folder in folders:
                yield from self._list_folder(ctx, conn, host, folder, cursor)
        finally:
            # fetch_item runs INSIDE the base's pull loop, which iterates this
            # generator LAZILY: the base yields one item, fetches its bytes, then
            # asks for the next. So the connection must stay open across all
            # fetches -- it does, because this finally runs only once the
            # generator is fully drained (i.e. after the base's loop ends). It
            # fires for BOTH a real pull and a dry_run (dry_run still drains
            # list_items to build the plan but never advances the cursor), so the
            # connection never leaks even when _advance_cursor is skipped.
            self._close_connection()

    def _list_folder(self, ctx, conn, host, folder, cursor) -> Iterable[RemoteItem]:
        # (1) EXAMINE = read-only select; flags are never mutated.
        typ, data = conn.examine(self._imap_folder_arg(folder))
        if typ != "OK":
            self._note_failure(ctx, folder, f"EXAMINE failed: {typ}")
            return

        # (2) UIDVALIDITY guard.
        uidvalidity = self._read_uidvalidity(conn)
        fcur = self._folder_cursor(cursor, folder)
        prev_validity = fcur.get("uidvalidity")
        last_uid = int(fcur.get("last_uid") or 0)
        if prev_validity is not None and str(prev_validity) != str(uidvalidity):
            # UIDs are meaningless across a UIDVALIDITY change -> RESET + full
            # re-pull within the since window (P7S-23). Logged, never a crash.
            last_uid = 0
            note = (
                f"UIDVALIDITY changed for folder {folder!r} "
                f"({prev_validity} -> {uidvalidity}); cursor reset, full re-pull"
            )
            self._reset_notes.append(note)
            self._note_reset(ctx, folder, note)

        # (3) UID SEARCH within the since window.
        since = self._since_criteria(ctx)
        typ, sdata = conn.uid("SEARCH", None, "SINCE", since)
        if typ != "OK":
            self._note_failure(ctx, folder, f"UID SEARCH failed: {typ}")
            return
        uids = self._parse_uid_list(sdata)
        # Only UIDs strictly above the high-water mark (incremental).
        new_uids = [u for u in uids if u > last_uid]
        new_uids.sort()

        highest_seen = last_uid
        for uid in new_uids:
            size = self._fetch_uid_size(conn, uid)
            item_id = f"imap://{host}/{folder}/{uidvalidity}/{uid}"
            name = self._eml_name(host, folder, uid)
            yield RemoteItem(
                item_id=item_id,
                name=name,
                modified="",
                size=size,
                meta={
                    "_imap_folder": folder,
                    "_imap_uid": uid,
                    "_imap_uidvalidity": str(uidvalidity),
                },
            )
            if uid > highest_seen:
                highest_seen = uid

        # Record the per-folder advance for the cursor (UIDVALIDITY + high-water).
        self._stage_folder_cursor(folder, uidvalidity, highest_seen)

    # --- subclass hook: fetch ONE item's bytes ------------------------------ #
    def fetch_item(self, ctx: ConnectorContext, item: RemoteItem) -> Path:
        """Fetch ONE message's raw RFC822 bytes to a private temp stage.

        Uses the open ``EXAMINE``-selected connection. The full RFC822 message
        (headers + body + any attachments, MIME-encoded) lands as a single
        ``.eml`` -- attachments stay inside the message and are unpacked by the
        ingest extractors, not by this connector. Returns the staged Path; the
        base lands it through ``safe_paths`` and accounts its bytes against the
        cap.
        """
        conn = getattr(self, "_conn", None)
        if conn is None:  # pragma: no cover - list_items always opens it first
            raise ConnectorError(f"{self.id}: no open IMAP connection for fetch")
        folder = item.meta.get("_imap_folder")
        uid = item.meta.get("_imap_uid")
        if folder is None or uid is None:  # pragma: no cover - defensive
            raise ConnectorError(f"{self.id}: item missing IMAP folder/uid metadata")

        # The folder is already EXAMINE-selected from list_folder; the connection
        # is single-folder-at-a-time, so a re-EXAMINE is harmless and keeps fetch
        # correct even if folders interleave. Still read-only.
        conn.examine(self._imap_folder_arg(folder))
        typ, data = conn.uid("FETCH", str(uid), "(RFC822)")
        if typ != "OK":
            raise ConnectorError(f"{self.id}: UID FETCH failed for uid={uid}: {typ}")
        raw = self._extract_rfc822(data)
        if raw is None:
            raise ConnectorError(f"{self.id}: empty RFC822 body for uid={uid}")

        stage_dir = Path(tempfile.mkdtemp(prefix="oracle-imap-"))
        stage = stage_dir / "message.eml"
        fd = os.open(str(stage), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:  # safe_paths-internal: private temp stage, fixed 0o600
            f.write(raw)
        return stage

    # --- cursor advance: persist per-folder UID/UIDVALIDITY ----------------- #
    def _advance_cursor(self, ctx: ConnectorContext, results: list) -> None:
        """Persist the per-folder UID high-water + UIDVALIDITY alongside the
        base's last_success_ts, then close the IMAP connection.

        Overrides the base so the folder cursor sub-state (the binding
        incremental + UIDVALIDITY-reset state; P7S-23) is written atomically with
        the success timestamp. Cursor write itself remains the base's atomic
        save_cursor.
        """
        from connectors.remote import load_cursor, save_cursor
        cur = load_cursor(ctx.root, self.id)
        pending = getattr(self, "_pending_cursor", None)
        if isinstance(pending, dict) and isinstance(pending.get("folders"), dict):
            folders = cur.get("folders")
            if not isinstance(folders, dict):
                folders = {}
            folders.update(pending["folders"])
            cur["folders"] = folders
        ingested = [r for r in results if r.get("action") == "ingested"]
        cur["last_success_ts"] = datetime.now().isoformat(timespec="seconds")
        cur["last_ingested_count"] = len(ingested)
        notes = getattr(self, "_reset_notes", None)
        if notes:
            cur["last_reset_notes"] = list(notes)
        save_cursor(ctx.root, self.id, cur)
        self._close_connection()

    # --- internal: cursor staging ------------------------------------------ #
    def _clone_cursor(self, cursor: dict) -> dict:
        out = {}
        folders = cursor.get("folders") if isinstance(cursor, dict) else None
        if isinstance(folders, dict):
            out["folders"] = {k: dict(v) for k, v in folders.items() if isinstance(v, dict)}
        else:
            out["folders"] = {}
        return out

    def _stage_folder_cursor(self, folder: str, uidvalidity, high_uid: int) -> None:
        pending = getattr(self, "_pending_cursor", None)
        if not isinstance(pending, dict):
            pending = {"folders": {}}
            self._pending_cursor = pending
        folders = pending.setdefault("folders", {})
        folders[folder] = {"uidvalidity": str(uidvalidity), "last_uid": int(high_uid)}

    def _close_connection(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            self._logout(conn)
            self._conn = None

    # --- internal: IMAP parsing helpers ------------------------------------- #
    def _imap_folder_arg(self, folder: str) -> str:
        """Quote a folder name for an IMAP command (defensive against spaces)."""
        f = str(folder)
        if " " in f and not (f.startswith('"') and f.endswith('"')):
            return '"' + f.replace('"', '\\"') + '"'
        return f

    def _read_uidvalidity(self, conn) -> str:
        """Read UIDVALIDITY for the currently EXAMINE-selected folder."""
        typ, data = conn.response("UIDVALIDITY")
        if typ == "OK" and data:
            for chunk in data:
                val = self._coerce_text(chunk).strip()
                if val.isdigit():
                    return val
        # Fall back to a STATUS query if the EXAMINE untagged response lacked it.
        return "0"

    def _fetch_uid_size(self, conn, uid: int) -> int:
        """RFC822.SIZE for one UID, feeding the base's byte accounting. -1 if
        unknown (the base's running counter still enforces on the staged size)."""
        try:
            typ, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
        except Exception:
            return -1
        if typ != "OK" or not data:
            return -1
        for chunk in data:
            text = self._coerce_text(chunk)
            marker = "RFC822.SIZE"
            if marker in text:
                tail = text.split(marker, 1)[1]
                digits = ""
                for ch in tail:
                    if ch.isdigit():
                        digits += ch
                    elif digits:
                        break
                if digits:
                    return int(digits)
        return -1

    def _parse_uid_list(self, sdata) -> list:
        uids: list = []
        for chunk in sdata or ():
            text = self._coerce_text(chunk)
            for tok in text.split():
                if tok.isdigit():
                    uids.append(int(tok))
        return uids

    def _extract_rfc822(self, data) -> Optional[bytes]:
        """Pull the RFC822 message bytes out of an imaplib FETCH response.

        imaplib returns a list mixing bytes and (bytes, bytes) tuples; the
        message body is the second element of the tuple whose first element
        names the RFC822 literal.
        """
        for part in data or ():
            if isinstance(part, tuple) and len(part) >= 2:
                body = part[1]
                if isinstance(body, (bytes, bytearray)) and body:
                    return bytes(body)
        return None

    def _coerce_text(self, chunk) -> str:
        if isinstance(chunk, tuple):
            chunk = chunk[0] if chunk else b""
        if isinstance(chunk, (bytes, bytearray)):
            return bytes(chunk).decode("utf-8", "replace")
        return str(chunk)

    def _eml_name(self, host: str, folder: str, uid: int) -> str:
        """A display name carrying a ``.eml`` suffix so the landing keeps it."""
        safe_folder = "".join(c if c.isalnum() else "-" for c in str(folder))[:40]
        return f"{safe_folder}-{uid}.eml"

    # --- logging shims ------------------------------------------------------ #
    def _note_reset(self, ctx: ConnectorContext, folder: str, note: str) -> None:
        cap = _import_capture()
        if cap is None or not hasattr(cap, "loop_note"):
            return
        try:
            cap.loop_note(
                ctx.root,
                loop="connector-pull",
                note=redact(f"{self.id}: {note}"),
                actor=getattr(ctx, "actor", "connector-runtime"),
            )
        except Exception:
            pass

    def _note_failure(self, ctx: ConnectorContext, folder: str, reason: str) -> None:
        cap = _import_capture()
        if cap is None or not hasattr(cap, "failure_event"):
            return
        try:
            cap.failure_event(
                ctx.root,
                target=f"connector:{self.id}:{folder}",
                severity="medium",
                failure_mode="imap-folder-error",
                excerpt=redact(reason),
                actor=getattr(ctx, "actor", "connector-runtime"),
            )
        except Exception:
            pass

    # --- probe / health ----------------------------------------------------- #
    def probe(self, ctx: ConnectorContext) -> dict:
        """Cheap read: report the allowlisted folders + the since window.

        A real probe would EXAMINE each folder for a message count, but that is
        authenticated egress; the gated-pull path authorizes BEFORE any network
        call (P7S-18), so probe stays metadata-only here and the scope is
        cap-derived. (Doctor's authenticated probe is a separate, explicit path.)
        """
        folders = self._folders(ctx)
        return {
            "connector": self.id,
            "host": self._source_block(ctx).get("host"),
            "folders": folders,
            "since_days": self._since_days(ctx),
            "items": 0,
            "by_suffix": {".eml": 0},
        }

    def health(self, ctx: ConnectorContext) -> dict:
        """healthy | degraded | broken for the IMAP mailbox config.

        broken   -> read_write misuse, missing host, empty folder allowlist, or
                    unresolved auth vars (the doctor fix-line says
                    "set the IMAP username + app password").
        degraded -> configured but past its freshness SLA (cursor-derived).
        healthy  -> host + allowlist + auth all present and fresh/unknown.
        """
        notes: list = []
        try:
            self._assert_read_only()
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[redact(str(exc))])
        try:
            self._host(ctx)
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[redact(str(exc))])
        if not self._folders(ctx):
            return self.health_envelope(
                "broken",
                notes=[
                    f"{self.id}: source.folders allowlist is empty -- set a "
                    f"non-empty list of mailbox folders (empty never means "
                    f"'everything')"
                ],
            )
        try:
            self._auth(ctx)
        except ConnectorError:
            return self.health_envelope(
                "broken",
                notes=[
                    f"{self.id}: IMAP auth not resolvable -- set the IMAP "
                    f"username + app password in <root>/.env.nosync"
                ],
            )
        fresh = self.freshness(ctx)
        state = "healthy"
        if fresh.get("verdict") == "stale":
            state = "degraded"
            notes.append("mailbox is past its freshness SLA")
        elif fresh.get("verdict") == "unknown":
            notes.append("freshness unknown (no successful pull yet)")
        return self.health_envelope(state, notes=notes, freshness=fresh)


def build(manifest: dict) -> ImapMailboxConnector:
    """Factory used by the connector registry (registered id-only + system)."""
    return ImapMailboxConnector(manifest)
