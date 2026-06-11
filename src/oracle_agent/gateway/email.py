"""gateway/email.py -- email messaging surface (Phase 4, P4-T3).

The email *adapter* (:class:`EmailAdapter`) translates inbound IMAP messages
to/from the normalized gateway types and composes with the landed
:class:`~oracle_agent.gateway.core.GatewayCore`, which owns every authorization
decision. The adapter owns ONLY adapter-row responsibilities (P4S-3): wire
parsing, identity extraction, the ``is_private`` assertion, layered
fail-closed sensitivity unlocking, the per-sender hourly turn cap, reply
discipline, loop protection, MIME/HTML handling, the persisted
``(UIDVALIDITY, last_UID)`` cursor, and transport (IMAP4_SSL / SMTP STARTTLS
with explicit socket timeouts).

Identity is layered fail-closed (P4S-10). Inbound ``From`` is attacker-writable
and DKIM is unverifiable in stdlib over IMAP, so:

  * The surface is HARD-CAPPED at ``public`` by default. ``is_private`` is set
    to ``False`` unless the message clears the unlock below, so
    :class:`GatewayCore` builds the loop at the ``public`` ceiling (its own
    non-private public-cap rule, P4S-5) -- even when config names a higher
    ``max_sensitivity``.
  * Raising the effective ceiling to ``internal`` requires BOTH (a) the
    operator configuring a trusted ``authserv_id`` AND (b) an
    ``Authentication-Results`` header FROM exactly that authserv-id carrying
    ``dmarc=pass`` (or ``spf=pass`` where DMARC is absent) on THIS message.
    Any of: no header, wrong authserv-id, or a fail verdict => the message is
    served at ``public`` at most (``is_private=False``).
  * A per-sender hourly turn cap (``per_sender_turns_per_hour``) is ALWAYS on,
    enforced in the adapter BEFORE any InboundMessage reaches the core (so the
    refusal precedes any model call).

Reply discipline (P4S-10/11): the reply goes to the exact header ``From`` --
``Reply-To`` is read and IGNORED (Reply-To redirection is the one path that
converts forgery into direct disclosure). Envelope recipient == header From ==
the allowlisted address. Never reply-all, never a list address, never an
address parsed from content. The reply body is the model output ONLY -- the
inbound message and thread are NEVER quoted (no Re:-chain re-emission of
confidential text through a capped surface). Outbound replies set
``Auto-Submitted: auto-replied``.

Loop protection (P4S-11): never reply to messages with ``Auto-Submitted`` !=
``no``, ``Precedence: bulk/list/auto_reply``, or our own ``Message-ID`` in
``References``.

Mailbox + cursor (P4S-12): a DEDICATED mailbox is required (doctor-checked
elsewhere -- a shared human mailbox races ``\\Seen``). The persisted
``(UIDVALIDITY, last_UID)`` cursor lives in the profile dir (atomic write,
P4S-20 naming); cursor corruption or a UIDVALIDITY change => log + start from
the mailbox's current ``UIDNEXT`` (never replay the mailbox unbounded).
``commit()`` persists the cursor AFTER the batch is handled (at-least-once,
P4S-4).

Stdlib only.
"""
from __future__ import annotations

import email
import email.message
import email.utils
import html.parser
import json
import os
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path

from .core import InboundMessage, OutboundReply

# Default timeouts (P4S-11/18): imaplib/smtplib default to NO timeout; a
# black-holed host would hang the daemon forever.
_IMAP_TIMEOUT = 30.0
_SMTP_TIMEOUT = 30.0

# Inbound size cap (P4S-11): refuse before parsing anything oversized.
_MAX_INBOUND_BYTES = 256 * 1024

# Outbound size cap (defensive): never blast an unbounded model reply.
_MAX_OUTBOUND_BYTES = 64 * 1024


# --------------------------------------------------------------------------- #
# HTML -> text extraction (kernel extractor discipline; stdlib only)
# --------------------------------------------------------------------------- #
class _HTMLTextExtractor(html.parser.HTMLParser):
    """Collect visible text, dropping script/style and tags entirely."""

    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):  # noqa: ARG002
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self._chunks).split())


def _html_to_text(raw_html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return ""
    return parser.text()


# --------------------------------------------------------------------------- #
# Transport wrappers (injectable for tests)
# --------------------------------------------------------------------------- #
class IMAPClient:
    """Thin IMAP4_SSL wrapper (injectable base for tests).

    Wraps the stdlib ``imaplib.IMAP4_SSL`` with an explicit socket timeout and
    exposes only the small surface the adapter needs. Tests inject a fake with
    the same method shape, so NONE of the adapter's security logic depends on a
    live network.
    """

    def __init__(self, host: str, user: str, password: str, *,
                 mailbox: str = "INBOX", timeout: float = _IMAP_TIMEOUT):
        import imaplib
        self._conn = imaplib.IMAP4_SSL(host, timeout=timeout)
        self._conn.login(user, password)
        self.mailbox = mailbox

    def select(self) -> dict:
        """Select the dedicated mailbox; return {uidvalidity, uidnext}."""
        typ, _ = self._conn.select(self.mailbox)
        if typ != "OK":
            raise OSError(f"IMAP select {self.mailbox!r} failed: {typ}")
        uidvalidity = self._status_int("UIDVALIDITY")
        uidnext = self._status_int("UIDNEXT")
        return {"uidvalidity": uidvalidity, "uidnext": uidnext}

    def _status_int(self, name: str) -> int:
        typ, data = self._conn.status(self.mailbox, f"({name})")
        if typ != "OK" or not data:
            raise OSError(f"IMAP status {name} failed: {typ}")
        line = data[0].decode("ascii", "replace") if isinstance(data[0], bytes) else str(data[0])
        # e.g. 'INBOX (UIDVALIDITY 12345)'
        token = line.split(name, 1)[1] if name in line else ""
        digits = "".join(ch for ch in token if ch.isdigit())
        return int(digits) if digits else 0

    def search_uids_since(self, last_uid: int) -> list[int]:
        """Return UIDs strictly greater than ``last_uid`` (sorted ascending)."""
        typ, data = self._conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return []
        raw = data[0].decode("ascii", "replace") if isinstance(data[0], bytes) else str(data[0])
        out = []
        for tok in raw.split():
            try:
                u = int(tok)
            except ValueError:
                continue
            if u > last_uid:
                out.append(u)
        return sorted(set(out))

    def fetch_rfc822(self, uid: int) -> bytes:
        typ, data = self._conn.uid("FETCH", str(uid), "(RFC822)")
        if typ != "OK" or not data:
            return b""
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                return bytes(part[1])
        return b""

    def logout(self) -> None:
        try:
            self._conn.logout()
        except Exception:
            pass


class SMTPClient:
    """Thin SMTP STARTTLS wrapper (injectable base for tests)."""

    def __init__(self, host: str, user: str, password: str, *,
                 port: int = 587, timeout: float = _SMTP_TIMEOUT):
        self._host = host
        self._user = user
        self._password = password
        self._port = port
        self._timeout = timeout

    def send(self, msg: EmailMessage) -> None:
        import smtplib
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as s:
            s.starttls()
            s.login(self._user, self._password)
            s.send_message(msg)


# --------------------------------------------------------------------------- #
# EmailAdapter (P4S-3 adapter rows)
# --------------------------------------------------------------------------- #
class EmailAdapter:
    """Translate IMAP/SMTP wire format to/from the normalized gateway types.

    ``surface_cfg`` is the ``gateway.email`` config block (allowlist,
    ``authserv_id``, ``per_sender_turns_per_hour``, etc). ``own_address`` is the
    oracle's own single mailbox address (lowercased). ``imap`` and ``smtp`` are
    injected transport objects (see :class:`IMAPClient` / :class:`SMTPClient`).
    """

    surface = "email"

    def __init__(self, surface_cfg: dict, own_address: str, imap, smtp, *,
                 instances: dict | None = None, clock=time.time, logger=None,
                 profile_dir=None, scope: str = "default"):
        self.surface_cfg = surface_cfg or {}
        self.own_address = (own_address or "").strip().lower()
        self.imap = imap
        self.smtp = smtp
        self.instances = instances or {}
        self.clock = clock
        self.logger = logger or (lambda *a: None)
        self._profile_dir = profile_dir
        self._scope = scope
        # Cursor: (uidvalidity, last_uid). Loaded lazily on first fetch.
        self._uidvalidity = 0
        self._last_uid = 0
        self._cursor_loaded = False
        self._cursor_dirty = False
        self._cursor_file: Path | None = None
        # Per-sender hourly turn cap window (ALWAYS on).
        self._sender_times: dict[str, list[float]] = {}
        # Message-IDs we have emitted (for our-own References loop guard).
        self._own_message_ids: set[str] = set()
        self.next_poll_not_before = 0.0

    # -- protocol ----------------------------------------------------------- #
    def supports_push(self) -> bool:
        return False

    # -- cursor persistence (P4S-12/20) ------------------------------------- #
    def _cursor_path(self) -> Path | None:
        if self._cursor_file is not None:
            return self._cursor_file
        if self._profile_dir is not None:
            base = Path(self._profile_dir)
        else:
            try:
                from .. import config as _cfg
                base = _cfg.profile_dir()
            except Exception:
                return None
        # P4S-20 naming: <surface>_<kind>_<scope>.json
        self._cursor_file = base / f"email_cursor_{self._scope}.json"
        return self._cursor_file

    def _load_cursor(self) -> None:
        if self._cursor_loaded:
            return
        self._cursor_loaded = True
        p = self._cursor_path()
        if p is None or not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            uv = int(data["uidvalidity"])
            lu = int(data["last_uid"])
            if uv >= 0 and lu >= 0:
                self._uidvalidity = uv
                self._last_uid = lu
        except Exception as exc:
            # Corruption => log + leave cursor at zero; the fetch path resets to
            # the mailbox's current UIDNEXT so we never replay unbounded (P4S-12).
            self.logger(
                f"gateway[email]: cursor file corrupt ({type(exc).__name__}); "
                "will start from current UIDNEXT (no mailbox replay)")
            self._uidvalidity = 0
            self._last_uid = -1  # sentinel: force reset-to-UIDNEXT in fetch()

    def commit(self) -> None:
        """Atomically persist the cursor AFTER the batch is handled (P4S-4)."""
        if not self._cursor_dirty:
            return
        p = self._cursor_path()
        if p is None:
            return
        payload = json.dumps(
            {"uidvalidity": self._uidvalidity, "last_uid": self._last_uid}) + "\n"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent),
                                       prefix=".emcur-", suffix="~")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, p)
                self._cursor_dirty = False
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            self.logger(f"gateway[email]: cursor save failed: {type(exc).__name__}")

    # -- polling ------------------------------------------------------------ #
    def fetch(self) -> list[tuple[int, bytes]]:
        """Select the mailbox + return [(uid, rfc822_bytes), ...] new messages.

        Handles UIDVALIDITY change / cursor corruption by resetting to the
        mailbox's current ``UIDNEXT`` (no unbounded replay, P4S-12).
        """
        self._load_cursor()
        try:
            sel = self.imap.select()
        except Exception as exc:
            self.logger(f"gateway[email]: IMAP select failed: {type(exc).__name__}")
            return []

        cur_uidvalidity = int(sel.get("uidvalidity", 0) or 0)
        cur_uidnext = int(sel.get("uidnext", 0) or 0)

        # UIDVALIDITY change OR a corruption sentinel => reset to UIDNEXT.
        if self._last_uid < 0 or (
                self._uidvalidity != 0 and cur_uidvalidity != self._uidvalidity):
            if self._uidvalidity != 0 and cur_uidvalidity != self._uidvalidity:
                self.logger(
                    f"gateway[email]: UIDVALIDITY changed "
                    f"({self._uidvalidity} -> {cur_uidvalidity}); resetting to "
                    f"UIDNEXT {cur_uidnext} (no mailbox replay)")
            self._uidvalidity = cur_uidvalidity
            self._last_uid = max(0, cur_uidnext - 1)
            self._cursor_dirty = True
            # Nothing strictly newer than the reset point on this pass.
            return []

        # First-ever select: adopt UIDVALIDITY, but DO scan from last_uid (0).
        if self._uidvalidity == 0:
            self._uidvalidity = cur_uidvalidity
            self._cursor_dirty = True

        try:
            uids = self.imap.search_uids_since(self._last_uid)
        except Exception as exc:
            self.logger(f"gateway[email]: IMAP search failed: {type(exc).__name__}")
            return []

        out: list[tuple[int, bytes]] = []
        for uid in uids:
            try:
                raw = self.imap.fetch_rfc822(uid)
            except Exception as exc:
                self.logger(
                    f"gateway[email]: fetch uid {uid} failed: {type(exc).__name__}")
                raw = b""
            out.append((uid, raw))
        return out

    def advance(self, uid: int) -> None:
        """Advance the cursor past a handled UID (sequencing, P4S-4)."""
        u = int(uid)
        if u > self._last_uid:
            self._last_uid = u
            self._cursor_dirty = True

    # -- parsing + identity (P4S-10/11) ------------------------------------- #
    def parse(self, raw: bytes) -> InboundMessage | None:
        """Parse one RFC822 message into an InboundMessage, or drop it.

        Returns ``None`` (silent drop, no reply, no model call) for: oversized
        mail, loop-marker mail (Auto-Submitted/Precedence/own-References),
        unknown/multiple senders, list/cc'd targeting (above-public served as
        non-private via is_private=False), unparseable bodies, and over the
        per-sender hourly cap.
        """
        if not raw or len(raw) > _MAX_INBOUND_BYTES:
            if raw:
                self.logger("gateway[email]: dropped oversized inbound message")
            return None

        try:
            msg = email.message_from_bytes(raw)
        except Exception:
            self.logger("gateway[email]: dropped unparseable message")
            return None

        # --- Loop protection (P4S-11) -------------------------------------- #
        auto = (msg.get("Auto-Submitted") or "").strip().lower()
        if auto and auto != "no":
            self.logger("gateway[email]: dropped auto-submitted message (loop guard)")
            return None
        prec = (msg.get("Precedence") or "").strip().lower()
        if prec in ("bulk", "list", "auto_reply"):
            self.logger(f"gateway[email]: dropped Precedence:{prec} (loop guard)")
            return None
        references = (msg.get("References") or "")
        if any(mid and mid in references for mid in self._own_message_ids):
            self.logger("gateway[email]: dropped our-own References (loop guard)")
            return None

        # --- Sender identity: exactly one From (Reply-To IGNORED) ---------- #
        from_addrs = email.utils.getaddresses(msg.get_all("From", []))
        sender_addrs = [a.strip().lower() for _, a in from_addrs if a.strip()]
        if len(sender_addrs) != 1:
            self.logger(
                f"gateway[email]: dropped message with {len(sender_addrs)} From "
                "addresses (need exactly one)")
            return None
        sender = sender_addrs[0]

        # --- Per-sender hourly turn cap (ALWAYS on; before any model call) -- #
        if not self._allow_sender_turn(sender):
            self.logger(f"gateway[email]: per-sender hourly cap hit for {sender}")
            return None

        # --- Recipient targeting -> is_private (P4S-11) -------------------- #
        # is_private ⟺ exactly one To == our own address AND empty Cc. Anything
        # broader is SERVED but capped at public (is_private=False); the core
        # enforces the public cap (P4S-5). Reply still goes only to the From.
        to_addrs = [a.strip().lower()
                    for _, a in email.utils.getaddresses(msg.get_all("To", []))
                    if a.strip()]
        cc_addrs = [a.strip().lower()
                    for _, a in email.utils.getaddresses(msg.get_all("Cc", []))
                    if a.strip()]
        sole_to_self = (to_addrs == [self.own_address]) and not cc_addrs

        # --- Layered fail-closed sensitivity unlock (P4S-10) --------------- #
        dmarc_ok = self._auth_verified(msg)
        is_private = bool(sole_to_self and dmarc_ok)

        # --- Body extraction (text/plain preferred, HTML stripped) --------- #
        body = self._extract_body(msg)
        if not body or not body.strip():
            self.logger("gateway[email]: dropped message with no usable text body")
            return None

        return InboundMessage(
            surface="email",
            user_id=sender,                # full address lowercased (P4S-17 key)
            channel_id=sender,             # reply goes to the exact header From
            text=body,
            is_private=is_private,
            meta={
                "uidvalidity": int(self._uidvalidity),
                "subject_len": len(msg.get("Subject", "") or ""),
                "dmarc_ok": bool(dmarc_ok),
            },
        )

    def _allow_sender_turn(self, sender: str) -> bool:
        cap = self.surface_cfg.get("per_sender_turns_per_hour", 10)
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            cap = 10
        if cap <= 0:
            cap = 10
        now = self.clock()
        window = [t for t in self._sender_times.get(sender, []) if now - t < 3600]
        if len(window) >= cap:
            self._sender_times[sender] = window
            return False
        window.append(now)
        self._sender_times[sender] = window
        return True

    def _auth_verified(self, msg: email.message.Message) -> bool:
        """Verify Authentication-Results from the configured authserv-id (P4S-10).

        Returns True ONLY when ``authserv_id`` is configured AND an
        ``Authentication-Results`` header FROM exactly that authserv-id carries
        ``dmarc=pass`` (or ``spf=pass`` where DMARC is absent). No header, wrong
        authserv-id, or any fail => False (public cap).
        """
        authserv = self.surface_cfg.get("authserv_id")
        if not authserv:
            return False  # hard public cap by default
        authserv = str(authserv).strip().lower()

        for header in msg.get_all("Authentication-Results", []):
            line = str(header)
            # The authserv-id is the first token before the first ';'.
            first = line.split(";", 1)[0].strip().lower()
            # Strip an optional version suffix (e.g. "mx.co.com 1").
            servid = first.split()[0] if first.split() else ""
            if servid != authserv:
                continue
            rest = line.lower()
            if "dmarc=pass" in rest:
                return True
            if "dmarc=" in rest:
                # DMARC present but not pass => fail closed for this header.
                continue
            if "spf=pass" in rest:
                return True
        return False

    def _extract_body(self, msg: email.message.Message) -> str:
        """Prefer text/plain; fall back to HTML-extracted text (P4S-11).

        Attachments are ignored. Returns "" when no usable text is found.
        """
        plain_parts: list[str] = []
        html_parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.is_multipart():
                    continue
                disp = (part.get("Content-Disposition") or "").lower()
                if "attachment" in disp:
                    continue
                ctype = part.get_content_type()
                payload = self._decode_part(part)
                if not payload:
                    continue
                if ctype == "text/plain":
                    plain_parts.append(payload)
                elif ctype == "text/html":
                    html_parts.append(payload)
        else:
            ctype = msg.get_content_type()
            payload = self._decode_part(msg)
            if payload:
                if ctype == "text/plain":
                    plain_parts.append(payload)
                elif ctype == "text/html":
                    html_parts.append(payload)
                else:
                    plain_parts.append(payload)
        if plain_parts:
            return "\n".join(p for p in plain_parts if p).strip()
        if html_parts:
            extracted = " ".join(_html_to_text(h) for h in html_parts).strip()
            return extracted
        return ""

    @staticmethod
    def _decode_part(part: email.message.Message) -> str:
        try:
            raw = part.get_payload(decode=True)
        except Exception:
            return ""
        if raw is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, "replace")
        except (LookupError, ValueError):
            return raw.decode("utf-8", "replace")

    # -- sending (P4S-10/11) ------------------------------------------------ #
    def send(self, reply: OutboundReply) -> None:
        """Send the reply to the exact header From ONLY.

        Body is the model output only -- NEVER quotes inbound text. Sets
        ``Auto-Submitted: auto-replied`` (our outbound loop marker). Envelope
        recipient == header From == ``reply.channel_id`` (the allowlisted
        address that produced the InboundMessage). Never reply-all.
        """
        recipient = (reply.channel_id or "").strip().lower()
        if not recipient:
            self.logger("gateway[email]: refusing to send with empty recipient")
            return
        body = reply.text or ""
        if len(body.encode("utf-8", "replace")) > _MAX_OUTBOUND_BYTES:
            body = body.encode("utf-8", "replace")[:_MAX_OUTBOUND_BYTES].decode(
                "utf-8", "replace")

        out = EmailMessage()
        out["From"] = self.own_address
        out["To"] = recipient            # exact From only; never reply-all/list
        out["Subject"] = "Re: your message"
        out["Auto-Submitted"] = "auto-replied"
        mid = email.utils.make_msgid()
        out["Message-ID"] = mid
        out["Date"] = email.utils.formatdate(localtime=False)
        out.set_content(body)            # model output ONLY, no quoted inbound
        self._own_message_ids.add(mid)
        try:
            self.smtp.send(out)
        except Exception as exc:
            self.logger(f"gateway[email]: send failed: {type(exc).__name__}")
