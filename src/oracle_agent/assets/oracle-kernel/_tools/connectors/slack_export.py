#!/usr/bin/env python3
"""connectors/slack_export.py -- the OFFLINE Slack workspace-export connector.

A Slack workspace export is an admin-downloaded ``.zip`` (no token, no network):
``channels.json`` + ``users.json`` at the root, and one folder per channel
holding per-day JSON message files (``<channel>/YYYY-MM-DD.json``). This
connector reads that local zip from ``source.path``, honours a default-deny
channel allowlist, and lands a readable per-channel-per-day **markdown
transcript** into the oracle's intake lane through the shared safety core.

It is a ``RemoteConnector`` subclass (``access_mode: file_drop``): the network
primitives are simply never used -- ``fetch_item`` stages a transcript that was
already rendered from the local zip during ``list_items``, so NO socket is ever
opened. Subclassing the core buys the whole safety discipline for free:

  * gate-first authorization (a gated pull authorizes BEFORE any work);
  * default-deny channel allowlist (``scope_allowlist_keys = ("channels",)`` --
    ``None``/missing/``[]``/non-list all refuse, I4/P7S-13);
  * content-based sensitivity classification at pull time (manifest floor);
  * the policy gate (deny = skip);
  * ``safe_paths``-contained landing under ``_INPUT/<id>/`` with a STABLE,
    UNIQUE-per-item name (re-render of a day supersedes; two days land apart);
  * the running landed-byte counter / ``max_files`` cap;
  * ``redact()`` on every string that leaves the pull;
  * an atomic cursor.

**Idempotency by export hash (P7S-T6):** the cursor records the export zip's
sha256. Re-running the pull on the SAME zip is a no-op -- ``list_items`` yields
nothing once the cursor's ``export_sha256`` matches and a prior pull succeeded.

**Zip member validation -- the FULL P7S-15 checklist** (NOT just traversal):
every member is screened BEFORE any decompression lands, and ANY violation
refuses the WHOLE export (nothing is written):

  * reject ``../`` and absolute member names (zip-slip);
  * reject symlink members (``external_attr`` high bits == ``S_IFLNK``);
  * enforce a per-member AND a total decompressed-size cap WHILE STREAMING --
    the ``ZipInfo.file_size`` declared by the archive is NEVER trusted; the cap
    is enforced against bytes actually read from the decompressor;
  * cap the member count;
  * nested archives are NOT descended into -- a ``.zip``/``.tar``/``.gz`` member
    is treated as an opaque member, subject to the same caps (here: ignored for
    transcript rendering, never recursively expanded).

A security violation surfaces as a ``refused`` result row (rc 1) -- the base's
``refused`` vocabulary covers exactly containment / zip-slip violations; an
out-of-allowlist channel surfaces as ``skipped_out_of_scope`` (rc 0).

Stdlib only. Optional floor siblings are imported defensively by the core.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

try:  # flat layout (tests put _tools on sys.path)
    from connectors.base import ConnectorContext, ConnectorError
    from connectors.remote import RemoteConnector, RemoteItem, redact, load_cursor, save_cursor
except Exception:  # pragma: no cover - package fallback
    from .base import ConnectorContext, ConnectorError  # type: ignore
    from .remote import RemoteConnector, RemoteItem, redact, load_cursor, save_cursor  # type: ignore

__all__ = ["SlackExportConnector", "ID", "SYSTEM", "build"]

ID = "slack-export"
SYSTEM = "slack"

# ---- the P7S-15 caps (decompression-bomb / member-count defenses) ---------- #
# Per-member decompressed-size ceiling (enforced WHILE streaming the member;
# ZipInfo.file_size is never trusted). A single Slack day file is tiny in
# practice; 25 MiB is a generous ceiling that still defuses a per-member bomb.
_MAX_MEMBER_BYTES = 25 * 1024 * 1024
# Total decompressed-bytes ceiling across all members of the export.
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
# Maximum number of members we will even inspect (a member-count overflow is a
# refusal, not a silent truncation).
_MAX_MEMBERS = 50_000
# Streaming chunk for the decompressor.
_CHUNK = 64 * 1024

# Suffixes that mark a member as a NESTED archive -- never recursively expanded
# (landed/treated as an opaque member; here that means: ignored for rendering).
_NESTED_ARCHIVE_SUFFIXES = (
    ".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar",
)


class ZipMemberRefused(ConnectorError):
    """A zip member failed the P7S-15 validation checklist (zip-slip, absolute
    name, symlink member, per-member/total decompression cap, member count).
    The WHOLE export is refused -- nothing is written."""


class SlackExportConnector(RemoteConnector):
    """OFFLINE Slack workspace-export connector (file_drop; no network)."""

    access_mode = "file_drop"
    # Default-deny channel allowlist: the pull refuses unless source.channels is
    # a non-empty list (I4 / P7S-13). Empty never means "everything".
    scope_allowlist_keys = ("channels",)
    # No network: the connector never makes an http_download redirect hop.
    download_host_suffixes: tuple = ()

    # ----------------------------------------------------------------------- #
    # source.path resolution
    # ----------------------------------------------------------------------- #
    def _export_path(self, ctx: ConnectorContext) -> Path:
        block = self._source_block(ctx)
        raw = block.get("path")
        if not raw or not str(raw).strip():
            raise ConnectorError(
                f"{self.id}: manifest source.path is required (the Slack export .zip)"
            )
        return Path(os.path.realpath(os.path.expanduser(str(raw))))

    def _allowed_channels(self, ctx: ConnectorContext) -> set:
        block = self._source_block(ctx)
        chans = block.get("channels")
        if not isinstance(chans, list):
            return set()
        return {str(c).strip() for c in chans if str(c).strip()}

    def _export_sha256(self, export: Path) -> str:
        h = hashlib.sha256()
        with open(export, "rb") as f:  # read-only: hashing the source export, guard-exempt
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    # ----------------------------------------------------------------------- #
    # list_items -- validate the zip, then render per-channel-per-day transcripts
    # ----------------------------------------------------------------------- #
    def list_items(self, ctx: ConnectorContext) -> Iterator[RemoteItem]:
        """Validate the export against the FULL P7S-15 checklist, then yield one
        RemoteItem per (allowlisted channel, day) carrying a rendered markdown
        transcript staged in a private temp file.

        Idempotency: if the cursor already recorded a successful pull of THIS
        export sha256, yield nothing (re-pull is a no-op).

        On ANY member-validation violation, the whole export is refused -- a
        single refusal-sentinel item is yielded (and nothing else), which the
        base lands as a ``refused`` row (rc 1, nothing written).
        """
        export = self._export_path(ctx)
        if not export.exists() or not export.is_file():
            raise ConnectorError(f"{self.id}: export path is not a file: {export}")

        export_hash = self._export_sha256(export)

        # Idempotency by export hash (no-op re-pull of the same zip).
        cur = load_cursor(ctx.root, self.id)
        if (not ctx.dry_run
                and cur.get("export_sha256") == export_hash
                and cur.get("last_success_ts")):
            return

        allowed = self._allowed_channels(ctx)

        try:
            transcripts = self._build_transcripts(export, allowed)
        except ZipMemberRefused as exc:
            # Refuse the WHOLE export: yield a single refusal sentinel. The base
            # lands it as a ``refused`` row via the _landing_name hook below.
            yield RemoteItem(
                item_id=f"refused:{export_hash[:12]}",
                name="refused",
                modified=_now_iso(),
                size=-1,
                meta={"refused": True, "refused_reason": str(exc)},
            )
            return

        for entry in transcripts:
            yield entry

    # ----------------------------------------------------------------------- #
    # fetch_item -- stage the already-rendered transcript (NO network)
    # ----------------------------------------------------------------------- #
    def fetch_item(self, ctx: ConnectorContext, item: RemoteItem) -> Path:
        """Stage the rendered markdown for ``item`` into a private temp file and
        return its Path. No network: the bytes were rendered from the local zip
        in ``list_items`` and carried on ``item.meta['markdown']``."""
        markdown = item.meta.get("markdown")
        if markdown is None:
            raise ConnectorError(f"{self.id}: no rendered transcript for {redact(item.item_id)}")
        stage_dir = Path(tempfile.mkdtemp(prefix="oracle-slack-"))
        stage = stage_dir / "transcript.md"
        data = markdown.encode("utf-8") if isinstance(markdown, str) else bytes(markdown)
        fd = os.open(str(stage), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:  # safe_paths-internal: private temp stage, fixed 0o600
            f.write(data)
        return stage

    # ----------------------------------------------------------------------- #
    # landing name -- stable per (channel, day); refusal sentinel -> refused row
    # ----------------------------------------------------------------------- #
    def _landing_name(self, sp, item: RemoteItem) -> str:
        """Stable ``<sha(item_id)[:12]>_<slug>.md`` per transcript.

        A refusal-sentinel item (``meta['refused']``) deliberately forces the
        base's ``safe_paths.contain`` to refuse, so a malicious export surfaces
        as a ``refused`` result row (rc 1) -- the base's vocabulary files
        zip-slip / containment violations under ``refused``.
        """
        if item.meta.get("refused"):
            # An unsafe segment forces _dest_for -> safe_paths.contain to raise
            # ValueError, which the base maps to a ``refused`` row carrying this
            # reason (already redacted by the base).
            raise ValueError(
                redact(item.meta.get("refused_reason") or "zip member validation refused")
            )
        h = hashlib.sha256(str(item.item_id).encode("utf-8")).hexdigest()[:12]
        try:
            slug = sp.safe_slug(item.name or "transcript")
        except ValueError:
            slug = "transcript"
        return f"{h}_{slug}.md"

    # ----------------------------------------------------------------------- #
    # cursor advance -- persist the export hash for idempotency
    # ----------------------------------------------------------------------- #
    def _advance_cursor(self, ctx: ConnectorContext, results: list) -> None:
        cur = load_cursor(ctx.root, self.id)
        ingested = [r for r in results if r.get("action") == "ingested"]
        refused = [r for r in results if r.get("action") == "refused"]
        # Only record the export hash as "done" when nothing was refused -- a
        # refused (malicious) export must NOT be marked idempotently complete.
        if not refused:
            try:
                cur["export_sha256"] = self._export_sha256(self._export_path(ctx))
            except ConnectorError:
                pass
        cur["last_success_ts"] = _now_iso()
        cur["last_ingested_count"] = len(ingested)
        save_cursor(ctx.root, self.id, cur)

    # ----------------------------------------------------------------------- #
    # zip validation + transcript rendering
    # ----------------------------------------------------------------------- #
    def _build_transcripts(self, export: Path, allowed: set) -> list:
        """Validate every member (P7S-15), read allowlisted day files streaming
        under the caps, and render markdown transcripts.

        Raises ``ZipMemberRefused`` on ANY validation violation -- the whole
        export is refused before a single transcript lands.
        """
        try:
            zf = zipfile.ZipFile(str(export), "r")
        except zipfile.BadZipFile as exc:
            raise ConnectorError(f"{self.id}: not a valid zip: {redact(str(exc))}") from exc

        with zf:
            infos = zf.infolist()
            if len(infos) > _MAX_MEMBERS:
                raise ZipMemberRefused(
                    f"member count {len(infos)} exceeds cap {_MAX_MEMBERS}"
                )

            users_map: dict = {}
            # (channel, day) -> list[message dict]
            day_files: dict = {}
            total_bytes = 0

            for info in infos:
                self._validate_member_name(info)
                if info.is_dir():
                    continue
                if self._is_symlink_member(info):
                    raise ZipMemberRefused(
                        f"symlink member refused: {info.filename!r}"
                    )

                name = info.filename
                # Decide what we need to actually read. We only read JSON we will
                # render (users.json + allowlisted day files); every OTHER member
                # is still streamed-and-counted toward the total cap WITHOUT
                # being recursively expanded (nested archives are opaque).
                channel, day = _classify_member(name)
                is_users = _basename(name) == "users.json"
                want = is_users or (channel is not None and channel in allowed)

                # Stream the member, enforcing the per-member + total caps while
                # reading (ZipInfo.file_size is NEVER trusted). We must read every
                # member to enforce the total cap honestly -- a member we don't
                # render is read and discarded, never expanded.
                data, total_bytes = self._read_member_capped(
                    zf, info, total_bytes, capture=want
                )

                if not want or data is None:
                    continue
                if is_users:
                    users_map = _parse_users(data)
                else:
                    day_files.setdefault((channel, day), [])
                    day_files[(channel, day)].extend(_parse_messages(data))

            return self._render_transcripts(day_files, users_map)

    @staticmethod
    def _validate_member_name(info: zipfile.ZipInfo) -> None:
        """Reject ``../`` traversal and absolute member names (zip-slip)."""
        name = info.filename or ""
        if not name:
            raise ZipMemberRefused("empty member name refused")
        # Absolute (POSIX or Windows-drive) members.
        if name.startswith("/") or name.startswith("\\"):
            raise ZipMemberRefused(f"absolute member name refused: {name!r}")
        if len(name) >= 2 and name[1] == ":":
            raise ZipMemberRefused(f"absolute (drive) member name refused: {name!r}")
        # Traversal in ANY path segment (normalise both separators).
        norm = name.replace("\\", "/")
        for seg in norm.split("/"):
            if seg == "..":
                raise ZipMemberRefused(f"traversal member name refused: {name!r}")
        if "\x00" in name:
            raise ZipMemberRefused("null byte in member name refused")

    @staticmethod
    def _is_symlink_member(info: zipfile.ZipInfo) -> bool:
        """True iff the member is a symlink (external_attr high bits S_IFLNK).

        The Unix mode lives in the top 16 bits of ``external_attr``; a symlink
        member has ``S_IFLNK`` set there. A crafted export can point a symlink
        member outside the tree, so symlink members are refused outright."""
        mode = (info.external_attr >> 16) & 0xFFFF
        return stat.S_ISLNK(mode)

    @staticmethod
    def _read_member_capped(zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                            total_bytes: int, *, capture: bool):
        """Stream a member through the decompressor enforcing the per-member and
        total decompressed-byte caps WHILE reading.

        ``ZipInfo.file_size`` is never trusted -- the running count of bytes
        actually read is the binding cap (a zip bomb lies about file_size).
        Returns ``(data_or_None, new_total_bytes)``; ``data`` is the captured
        bytes when ``capture`` is True, else ``None`` (read-and-discarded)."""
        member_total = 0
        buf = bytearray() if capture else None
        with zf.open(info, "r") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                member_total += len(chunk)
                if member_total > _MAX_MEMBER_BYTES:
                    raise ZipMemberRefused(
                        f"member {info.filename!r} exceeds per-member cap "
                        f"{_MAX_MEMBER_BYTES} (decompressed > {member_total}; "
                        f"declared file_size={info.file_size} not trusted)"
                    )
                if total_bytes + member_total > _MAX_TOTAL_BYTES:
                    raise ZipMemberRefused(
                        f"total decompressed bytes exceed cap {_MAX_TOTAL_BYTES} "
                        f"(running > {total_bytes + member_total})"
                    )
                if buf is not None:
                    buf.extend(chunk)
        data = bytes(buf) if buf is not None else None
        return data, total_bytes + member_total

    def _render_transcripts(self, day_files: dict, users_map: dict) -> list:
        """Render each (channel, day) into a markdown transcript RemoteItem."""
        items: list = []
        for (channel, day) in sorted(day_files.keys()):
            messages = day_files[(channel, day)]
            markdown = _render_markdown(channel, day, messages, users_map)
            item_id = f"slack:{channel}:{day}"
            name = f"{channel}_{day}"
            items.append(RemoteItem(
                item_id=item_id,
                name=name,
                modified=day,
                size=len(markdown.encode("utf-8")),
                meta={
                    "markdown": markdown,
                    "channel": channel,
                    "day": day,
                    "message_count": len(messages),
                    "provenance": f"slack-export channel={channel} day={day}",
                },
            ))
        return items

    # ----------------------------------------------------------------------- #
    # probe / health
    # ----------------------------------------------------------------------- #
    def probe(self, ctx: ConnectorContext) -> dict:
        """Cheap read: count allowlisted channels + day files in the export
        WITHOUT decompressing bodies (member names only)."""
        try:
            export = self._export_path(ctx)
        except ConnectorError as exc:
            return {"connector": self.id, "items": 0, "by_channel": {}, "error": str(exc)}
        if not export.exists() or not export.is_file():
            return {"connector": self.id, "items": 0, "by_channel": {},
                    "error": f"export not found: {export}"}
        allowed = self._allowed_channels(ctx)
        by_channel: dict = {}
        try:
            with zipfile.ZipFile(str(export), "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    channel, day = _classify_member(info.filename)
                    if channel is None or (allowed and channel not in allowed):
                        continue
                    by_channel[channel] = by_channel.get(channel, 0) + 1
        except zipfile.BadZipFile as exc:
            return {"connector": self.id, "items": 0, "by_channel": {},
                    "error": f"not a valid zip: {redact(str(exc))}"}
        return {
            "connector": self.id,
            "source": str(export),
            "items": sum(by_channel.values()),
            "channels": len(by_channel),
            "by_channel": dict(sorted(by_channel.items())),
        }

    def health(self, ctx: ConnectorContext) -> dict:
        """healthy | degraded | broken for the configured export zip."""
        try:
            self._assert_read_only()
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[str(exc)])
        try:
            export = self._export_path(ctx)
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[str(exc)])
        if not export.exists() or not export.is_file():
            return self.health_envelope(
                "broken", notes=[f"export zip does not exist or is not a file: {export}"]
            )
        if not os.access(export, os.R_OK):
            return self.health_envelope(
                "broken", notes=[f"export zip is not readable: {export}"]
            )
        try:
            self._assert_scope_allowlist(ctx)
        except ConnectorError as exc:
            return self.health_envelope("broken", notes=[str(exc)])
        notes: list = []
        probe = self.probe(ctx)
        if probe.get("error"):
            return self.health_envelope("broken", notes=[probe["error"]])
        fresh = self.freshness(ctx)
        state = "healthy"
        if probe.get("items", 0) == 0:
            state = "degraded"
            notes.append("no allowlisted channel day-files found in the export")
        if fresh.get("verdict") == "stale":
            state = "degraded"
            notes.append("export is past its freshness SLA")
        elif fresh.get("verdict") == "unknown":
            notes.append("freshness unknown (no prior successful pull)")
        return self.health_envelope(state, notes=notes, probe=probe, freshness=fresh)


# --------------------------------------------------------------------------- #
# module-level helpers (pure; no I/O)
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _basename(name: str) -> str:
    return name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _classify_member(name: str):
    """Return ``(channel, day)`` for a ``<channel>/YYYY-MM-DD.json`` member, else
    ``(None, None)``. Root files (channels.json, users.json) -> (None, None)."""
    norm = (name or "").replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if len(parts) != 2:
        return None, None
    channel, fname = parts
    if not fname.lower().endswith(".json"):
        return None, None
    day = fname[:-5]  # strip ".json"
    if not _looks_like_day(day):
        return None, None
    return channel, day


def _looks_like_day(s: str) -> bool:
    """True iff ``s`` is a YYYY-MM-DD date string."""
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _parse_users(data: bytes) -> dict:
    """Map Slack user id -> display name from a users.json member."""
    out: dict = {}
    try:
        users = json.loads(data.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return out
    if not isinstance(users, list):
        return out
    for u in users:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id") or "")
        if not uid:
            continue
        profile = u.get("profile") if isinstance(u.get("profile"), dict) else {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or u.get("real_name")
            or u.get("name")
            or uid
        )
        out[uid] = str(name)
    return out


def _parse_messages(data: bytes) -> list:
    """Parse a per-day channel JSON file into a list of message dicts."""
    try:
        payload = json.loads(data.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    return []


def _render_markdown(channel: str, day: str, messages: list, users_map: dict) -> str:
    """Render a readable markdown transcript for one channel-day."""
    lines = [f"# #{channel} -- {day}", ""]
    if not messages:
        lines.append("_(no messages)_")
        return "\n".join(lines) + "\n"
    for m in messages:
        ts = _format_ts(m.get("ts"))
        uid = str(m.get("user") or m.get("bot_id") or "")
        who = users_map.get(uid, uid or "unknown")
        text = _clean_text(str(m.get("text") or ""), users_map)
        prefix = f"**{who}**"
        if ts:
            prefix = f"`{ts}` {prefix}"
        if text:
            lines.append(f"- {prefix}: {text}")
        else:
            lines.append(f"- {prefix}:")
        for att in m.get("attachments") or []:
            if isinstance(att, dict):
                fall = att.get("fallback") or att.get("text") or ""
                if fall:
                    lines.append(f"    > {_clean_text(str(fall), users_map)}")
        for fobj in m.get("files") or []:
            if isinstance(fobj, dict):
                fname = fobj.get("name") or fobj.get("title") or "file"
                lines.append(f"    [file: {fname}]")
    return "\n".join(lines) + "\n"


def _format_ts(ts) -> str:
    """Format a Slack ``ts`` (epoch seconds as a string) as HH:MM:SS UTC."""
    if not ts:
        return ""
    try:
        epoch = float(str(ts).split(".")[0])
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return ""


def _clean_text(text: str, users_map: dict) -> str:
    """Resolve ``<@UID>`` mentions to display names; collapse to one line."""
    out = text
    for uid, name in users_map.items():
        out = out.replace(f"<@{uid}>", f"@{name}")
    return out.replace("\r", " ").replace("\n", " ").strip()


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #
def build(manifest: dict) -> SlackExportConnector:
    """Factory used by the connector registry (registered id-only + system)."""
    return SlackExportConnector(manifest)
