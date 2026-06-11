#!/usr/bin/env python3
"""connectors/remote.py -- the shared safety core for every remote connector.

A remote connector (Google Drive, Microsoft Graph, Notion, an IMAP mailbox, a
Slack export) is a thin, dumb adapter over the safety primitives in THIS module.
The base class ``RemoteConnector`` owns everything that must be identical across
systems, and it owns it as a FINAL template method ``pull`` that subclasses must
not override:

    gate-first authorization (gated pulls authorize BEFORE any network call)
      -> default-deny scope allowlist enforcement
      -> _assert_read_only
      -> list_items (subclass: metadata WITHIN the scope allowlist)
      -> max_files cap PLUS a running landed-byte counter that ABORTS at max_bytes
      -> content-based sensitivity classification (intake_classify.classify_file,
         the manifest floor as the FLOOR; UP on ambiguity)
      -> policy.check_processing (deny = SKIP)
      -> safe_paths-contained landing under _INPUT/<id>/<sha[:12]>_<slug><suffix>
         (STABLE per item across pulls, UNIQUE per item)
      -> atomic cursor advance

Subclasses implement only ``list_items`` and ``fetch_item``. An adapter bug can
drop a document but it can NOT widen access, skip classification, escape
containment, follow a redirect off-host, or exceed the byte cap -- because the
primitives that could do those things (``http_json``, ``http_download``,
``persist_rotated_token``, the cursor + landing helpers) live ONLY here.

Network discipline (I5):
  * ``http_json``      -- the ONLY JSON primitive; https-only; NEVER follows a
    redirect (any 3xx raises); bounded retry/backoff on 429/5xx.
  * ``http_download``  -- the ONLY byte primitive; https-only; streams to a
    private stage enforcing ``max_bytes`` WHILE reading (Content-Length never
    trusted); follows AT MOST ONE redirect, only to https, only to the calling
    connector's enumerated download-host suffixes, ALWAYS stripping the
    Authorization header on a cross-host hop.

Secret discipline (P7S-2/-9): ``persist_rotated_token`` is the ONE sanctioned
kernel-side secret write (contained, atomic, 0o600, no-bypass-marked). Every
string that leaves ``pull`` passes through ``redact``.

Stdlib only. Floor siblings (safe_paths, policy, intake_classify, actions) are
imported defensively so this works flat (tests inject ``_tools`` on sys.path) OR
as a package, and degrades gracefully when an optional sibling is absent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

try:  # flat layout (tests put _tools on sys.path)
    from connectors.base import Connector, ConnectorContext, ConnectorError
except Exception:  # pragma: no cover - package fallback
    from .base import Connector, ConnectorContext, ConnectorError  # type: ignore

__all__ = [
    "RemoteItem",
    "RemoteConnector",
    "http_json",
    "http_download",
    "device_flow",
    "loopback_flow",
    "resolve_auth",
    "persist_rotated_token",
    "redact",
    "load_cursor",
    "save_cursor",
    "RedirectRefused",
    "ByteCapExceeded",
]

# Intake lane within Workproduct.nosync. We land pulled files under
# _INPUT/<connector-id>/ via safe_paths.contain(base="Workproduct.nosync").
_INTAKE_BASE = "Workproduct.nosync"
_INTAKE_PREFIX = "_INPUT"

# Sensitivity ladder (mirrors intake_classify.LABELS / policy.SENSITIVITY_ORDER).
_SENS_ORDER = ("public", "internal", "confidential", "restricted", "secret")

# A sane file ceiling so a misconfigured pull cannot land an unbounded set.
_DEFAULT_MAX_FILES = 500
# Fail-closed byte ceiling used when neither ctx nor manifest prices a cap and
# the gate is not the one enforcing it (a direct/ungated pull). The gate's
# blast_radius_caps.max_bytes is the binding ceiling for a gated pull.
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB

# The canonical gated-pull loop id (P7S-20). Every admin allowlist entry and the
# _planned_pull_scope declaration use THIS id; it is deliberately NOT a member of
# actions.DETERMINISTIC_LOOPS (credentialed network egress is not level-1).
CONNECTOR_PULL_LOOP = "connector-pull"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class RedirectRefused(ConnectorError):
    """An HTTP redirect was refused (http_json never redirects; http_download
    follows at most one same-policy hop)."""


class ByteCapExceeded(ConnectorError):
    """A streamed download exceeded ``max_bytes`` mid-read (Content-Length is
    never trusted; the cap is enforced WHILE reading)."""


# --------------------------------------------------------------------------- #
# sibling-import shims (work flat OR as a package; optional ones may be absent)
# --------------------------------------------------------------------------- #
def _import_safe_paths():
    try:
        import safe_paths  # type: ignore
        return safe_paths
    except Exception:  # pragma: no cover - package fallback
        from .. import safe_paths  # type: ignore
        return safe_paths


def _import_policy():
    try:
        import policy  # type: ignore
        return policy
    except Exception:  # pragma: no cover - optional / package fallback
        try:
            from .. import policy  # type: ignore
            return policy
        except Exception:
            return None


def _import_intake_classify():
    try:
        import intake_classify  # type: ignore
        return intake_classify
    except Exception:  # pragma: no cover - optional / package fallback
        try:
            from .. import intake_classify  # type: ignore
            return intake_classify
        except Exception:
            return None


def _import_actions():
    try:
        import actions  # type: ignore
        return actions
    except Exception:  # pragma: no cover - optional / package fallback
        try:
            from .. import actions  # type: ignore
            return actions
        except Exception:
            return None


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
# RemoteItem
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RemoteItem:
    """Metadata for ONE remote item -- never its body.

    ``item_id`` is the upstream system's STABLE identifier; it keys BOTH the
    landing filename (``<sha256(item_id)[:12]>_<slug><suffix>``) and the cursor,
    so a re-pull of a changed item reuses its name (supersession), while two
    items with the same display name land separately (no cross-item overwrite).
    ``size`` may be ``-1`` (unknown).
    """

    item_id: str
    name: str
    modified: str
    size: int
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# redaction (P7S-9)
# --------------------------------------------------------------------------- #
# A query string on any scheme -- pre-signed download URLs (Graph
# @microsoft.graph.downloadUrl, Drive export links) carry credentials there.
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#'\"]+)\?[^\s'\"]*")
# Bearer / token-shaped substrings.
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_TOKEN_KV_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|client_secret|api[_-]?key|token|"
    r"password|authorization|code)\b\s*[=:]\s*[^\s,&;'\"]+"
)
# Long opaque token-ish runs (JWTs, OAuth tokens) standing alone.
_OPAQUE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{10,})?\b")


def redact(text) -> str:
    """Strip URL query strings and Bearer/token-shaped substrings.

    EVERY result/error/exception string that leaves ``pull`` (results, CLI
    payloads, action_event reasons) MUST pass through this. Pre-signed download
    URLs carry credentials in their query strings; OAuth responses carry bearer
    tokens. Idempotent and total -- never raises on odd input.
    """
    if text is None:
        return ""
    s = str(text)
    s = _URL_QUERY_RE.sub(r"\1?<redacted>", s)
    s = _BEARER_RE.sub("Bearer <redacted>", s)
    s = _TOKEN_KV_RE.sub(lambda m: f"{m.group(1)}=<redacted>", s)
    s = _OPAQUE_TOKEN_RE.sub("<redacted>", s)
    return s


# --------------------------------------------------------------------------- #
# HTTP primitives -- the ONLY network egress in the connector layer
# --------------------------------------------------------------------------- #
_NO_REDIRECT_CODES = (301, 302, 303, 307, 308)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that REFUSES every redirect by raising.

    Token endpoints and JSON APIs never legitimately redirect; a 3xx is treated
    as a hard error so a compromised endpoint cannot bounce a credentialed
    request off-host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        raise RedirectRefused(f"redirect refused ({code}) to {redact(newurl)}")


class _CaptureRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that SURFACES a 3xx as an HTTPError (does not follow it).

    ``http_download`` inspects the surfaced redirect and applies its one-hop,
    enumerated-host, strip-Authorization-cross-host policy itself, so urllib
    must never silently follow.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None  # do not follow; the HTTPError propagates to http_download


def _require_https(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ConnectorError(f"refusing non-https URL (scheme={parsed.scheme!r})")
    if not parsed.hostname:
        raise ConnectorError("refusing URL with no host")
    return parsed.hostname


def http_json(method, url, *, headers=None, body=None, timeout=30,
              _max_retries: int = 3) -> dict:
    """Make an HTTPS JSON request. https-only; NO redirects, ever.

    Any 3xx raises ``RedirectRefused``. Bounded retry/backoff on 429 (honoring
    Retry-After) and 5xx. Returns the parsed JSON object (or ``{}`` on an empty
    2xx body).
    """
    _require_https(url)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = bytes(body)

    opener = urllib.request.build_opener(_NoRedirect())
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=str(method).upper())
        try:
            with opener.open(req, timeout=timeout) as resp:
                code = resp.getcode()
                if code in _NO_REDIRECT_CODES:  # pragma: no cover - handler raises first
                    raise RedirectRefused(f"redirect refused ({code})")
                raw = resp.read()
            text = raw.decode("utf-8") if raw else ""
            return json.loads(text) if text.strip() else {}
        except RedirectRefused:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in _NO_REDIRECT_CODES:
                raise RedirectRefused(f"redirect refused ({exc.code})") from exc
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            if exc.code == 429 or 500 <= exc.code < 600:
                attempt += 1
                if attempt > _max_retries:
                    raise ConnectorError(
                        f"http_json {method} failed after {attempt} attempts: HTTP {exc.code}"
                    ) from exc
                time.sleep(retry_after if retry_after is not None else min(2 ** attempt, 30))
                continue
            detail = ""
            try:
                detail = redact(exc.read().decode("utf-8", "replace")[:200])
            except Exception:
                pass
            raise ConnectorError(f"http_json {method} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            attempt += 1
            if attempt > _max_retries:
                raise ConnectorError(f"http_json {method} transport error: {redact(exc.reason)}") from exc
            time.sleep(min(2 ** attempt, 30))
            continue


def _parse_retry_after(value) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _host_allowed(host: str, allowed_suffixes) -> bool:
    """True iff ``host`` ends with one of the enumerated download-host suffixes.

    A suffix like ``googleusercontent.com`` matches ``x.googleusercontent.com``
    and the bare host; a leading-dot or ``*.`` form is normalized first.
    """
    h = (host or "").lower().rstrip(".")
    for suf in allowed_suffixes or ():
        s = str(suf).lower().lstrip("*").lstrip(".").rstrip(".")
        if not s:
            continue
        if h == s or h.endswith("." + s):
            return True
    return False


def http_download(url, dest_stage, *, headers=None, max_bytes,
                  allowed_host_suffixes=None, timeout=60) -> Path:
    """THE one byte-fetch primitive. Subclasses NEVER import urllib (P7S-8).

    Streams the body to ``dest_stage`` enforcing ``max_bytes`` WHILE reading --
    Content-Length is never trusted (P7S-15/17). Redirect policy (P7S-7):
    follows AT MOST ONE redirect, only to https, only to a host matching
    ``allowed_host_suffixes``, and ALWAYS strips the Authorization header on a
    cross-host hop. Everything else raises.

    Returns the staged ``Path``. The stage is the caller's private temp file.
    """
    if max_bytes is None or int(max_bytes) <= 0:
        raise ConnectorError("http_download requires a positive max_bytes")
    max_bytes = int(max_bytes)
    origin_host = _require_https(url)
    hdrs = dict(headers or {})

    current_url = url
    current_host = origin_host
    redirects_left = 1
    # Capturing opener: urllib surfaces a 3xx as HTTPError (never follows it) so
    # we apply the single-hop, enumerated-host, strip-Authorization policy here.
    opener = urllib.request.build_opener(_CaptureRedirect())

    while True:
        req = urllib.request.Request(current_url, headers=hdrs, method="GET")
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in _NO_REDIRECT_CODES:
                location = exc.headers.get("Location") if exc.headers else None
                if not location or redirects_left <= 0:
                    raise RedirectRefused(
                        f"download redirect refused ({exc.code})"
                    ) from exc
                redirects_left -= 1
                next_url = urllib.parse.urljoin(current_url, location)
                next_host = _require_https(next_url)
                if not _host_allowed(next_host, allowed_host_suffixes):
                    raise RedirectRefused(
                        f"download redirect to non-enumerated host "
                        f"{next_host!r} refused"
                    ) from exc
                if next_host.lower() != current_host.lower():
                    # Cross-host hop: STRIP Authorization.
                    hdrs = {k: v for k, v in hdrs.items() if k.lower() != "authorization"}
                current_url = next_url
                current_host = next_host
                continue
            raise ConnectorError(f"http_download HTTP {exc.code}") from exc
        # 2xx: stream to the stage enforcing the cap while reading.
        try:
            written = _stream_to_stage(resp, dest_stage, max_bytes)
        finally:
            resp.close()
        return dest_stage


def _stream_to_stage(resp, dest_stage, max_bytes: int) -> int:
    """Stream ``resp`` to ``dest_stage`` aborting at ``max_bytes`` while reading.

    Content-Length is never trusted -- the running byte count is the binding
    cap. On overflow the partial stage is unlinked and ByteCapExceeded raised.
    """
    dest_stage = Path(dest_stage)
    dest_stage.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    chunk = 64 * 1024
    fd = os.open(str(dest_stage), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:  # safe_paths-internal: private temp stage, fixed 0o600
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                total += len(buf)
                if total > max_bytes:
                    raise ByteCapExceeded(
                        f"download exceeded max_bytes={max_bytes} (read>{total})"
                    )
                out.write(buf)
    except BaseException:
        try:
            dest_stage.unlink()
        except OSError:
            pass
        raise
    return total


# --------------------------------------------------------------------------- #
# OAuth flows (stdlib only)
# --------------------------------------------------------------------------- #
def device_flow(endpoints, client_id, *, client_secret=None, out=print) -> dict:
    """stdlib OAuth2 device-code flow (msgraph: public client, no secret).

    Prints the user_code + verification URL via ``out``, polls the token
    endpoint, returns the token dict. ``client_secret`` is accepted for
    providers that require it (P7S-1). ``endpoints`` carries
    ``device_authorization``, ``token``, and ``scope``.
    """
    device_url = endpoints["device_authorization"]
    token_url = endpoints["token"]
    scope = endpoints.get("scope", "")

    body = {"client_id": client_id, "scope": scope}
    if client_secret:
        body["client_secret"] = client_secret
    start = http_json("POST", device_url, body=urllib.parse.urlencode(body),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    user_code = start.get("user_code", "")
    verification = start.get("verification_uri") or start.get("verification_url") or ""
    out(f"To authorize, visit {verification} and enter code: {user_code}")
    device_code = start["device_code"]
    interval = int(start.get("interval", 5) or 5)
    deadline = time.time() + int(start.get("expires_in", 900) or 900)

    poll = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }
    if client_secret:
        poll["client_secret"] = client_secret
    while time.time() < deadline:
        time.sleep(interval)
        try:
            tok = http_json("POST", token_url, body=urllib.parse.urlencode(poll),
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        except ConnectorError:
            # authorization_pending / slow_down surface as 400 in real life; the
            # http_json error already redacts -- keep polling until the deadline.
            continue
        if tok.get("access_token"):
            return tok
    raise ConnectorError("device_flow timed out waiting for authorization")


def loopback_flow(endpoints, client_id, client_secret, *, scopes, out=print) -> dict:
    """stdlib installed-app flow (gdrive: Google's device flow cannot grant Drive
    read scopes -- P7S-1).

    Binds a one-shot ``http.server`` to literal ``127.0.0.1`` on an ephemeral
    port (refusing any non-loopback bind), uses state + PKCE, prints the browser
    URL via ``out``, captures the code, exchanges it, returns the token dict.
    """
    import base64
    import http.server
    import secrets
    import threading

    auth_url = endpoints["authorization"]
    token_url = endpoints["token"]

    # PKCE + state.
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)

    captured: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            captured["code"] = (q.get("code") or [None])[0]
            captured["state"] = (q.get("state") or [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You may close this window.")

        def log_message(self, *a):  # silence
            return

    # Literal-loopback discipline: bind 127.0.0.1 only.
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    bound_host, port = server.server_address[0], server.server_address[1]
    if bound_host != "127.0.0.1":  # pragma: no cover - defensive
        server.server_close()
        raise ConnectorError(f"loopback_flow refused non-loopback bind {bound_host!r}")
    redirect_uri = f"http://127.0.0.1:{port}/"

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes) if isinstance(scopes, (list, tuple)) else str(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    out(f"To authorize, open: {auth_url}?{urllib.parse.urlencode(params)}")

    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    t.join(timeout=300)
    server.server_close()

    if captured.get("state") != state:
        raise ConnectorError("loopback_flow state mismatch (possible CSRF)")
    code = captured.get("code")
    if not code:
        raise ConnectorError("loopback_flow received no authorization code")

    exchange = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    tok = http_json("POST", token_url, body=urllib.parse.urlencode(exchange),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    if not tok.get("access_token"):
        raise ConnectorError("loopback_flow token exchange returned no access_token")
    return tok


# --------------------------------------------------------------------------- #
# auth resolution + the one sanctioned secret write
# --------------------------------------------------------------------------- #
def _env_nosync_path(root: Path) -> Path:
    return Path(root) / ".env.nosync"


def _load_env_nosync(root: Path) -> dict:
    p = _env_nosync_path(root)
    out: dict = {}
    if not p.exists():
        return out
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def resolve_auth(root, manifest) -> dict:
    """Resolve manifest ``auth.vars`` NAMES to values from ``os.environ`` then
    ``<root>/.env.nosync`` (0o600). Raises ConnectorError if any is unresolved.
    Values are never logged.
    """
    auth = manifest.get("auth") if isinstance(manifest, dict) else None
    auth = auth if isinstance(auth, dict) else {}
    names = auth.get("vars")
    names = [str(n) for n in names if n] if isinstance(names, list) else []
    env_file = _load_env_nosync(Path(root))
    resolved: dict = {}
    missing: list = []
    for name in names:
        val = os.environ.get(name) or env_file.get(name)
        if val:
            resolved[name] = val
        else:
            missing.append(name)
    if missing:
        raise ConnectorError(
            f"unresolved auth vars: {', '.join(sorted(missing))} "
            f"(set them in <root>/.env.nosync or the process env)"
        )
    return resolved


def persist_rotated_token(root, var_name, value) -> None:
    """The ONE sanctioned kernel-side secret write (P7S-2).

    When a provider rotates a refresh token mid-pull (Microsoft does), the new
    value is upserted into ``<root>/.env.nosync`` via a contained, atomic,
    0o600 temp+rename writer carrying the no-bypass marker. The value is never
    logged, never echoed, never placed in a result dict. An explicitly
    documented exception, mirroring the kernel's backup exception pattern.
    """
    if not re.match(r"^[A-Z][A-Z0-9_]*$", str(var_name) or ""):
        raise ConnectorError(f"invalid env var name: {var_name!r}")
    if value is None or "\n" in str(value) or "\r" in str(value):
        raise ConnectorError("rotated token value must be a single-line non-empty string")

    root = Path(root)
    dst = _env_nosync_path(root)
    existing = _load_env_nosync(root)
    existing[str(var_name)] = str(value)
    lines = ["# Oracle connector secrets -- 0600, never committed, never logged."]
    for k in sorted(existing):
        lines.append(f"{k}={existing[k]}")
    text = "\n".join(lines) + "\n"

    dst.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".env.nosync.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:  # safe_paths-internal: contained root file, 0o600 atomic
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(dst))  # safe_paths-internal: atomic secret swap on the root's own .env.nosync
            os.chmod(dst, 0o600)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        os.umask(old_umask)


# --------------------------------------------------------------------------- #
# cursor: Connectors/<id>/state.json (atomic; torn => {} + warning)
# --------------------------------------------------------------------------- #
def _cursor_path(root: Path, cid: str) -> Path:
    sp = _import_safe_paths()
    # Contain under base="Connectors" -> Connectors/<id>/state.json.
    return sp.contain(Path(root), f"{cid}/state.json", base="Connectors")


def load_cursor(root, cid) -> dict:
    """Load ``Connectors/<id>/state.json``. An unparseable/torn cursor loads as
    ``{}`` with a logged warning -- fail-closed means a full re-pull, which is
    safe (idempotent landing names) if quota-expensive (P7S-23).
    """
    try:
        path = _cursor_path(Path(root), str(cid))
    except ValueError:
        return {}
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        import warnings
        warnings.warn(f"connector cursor {path} is torn/unparseable; treating as empty (full re-pull)")
        return {}
    return data if isinstance(data, dict) else {}


def save_cursor(root, cid, cur) -> None:
    """Atomically write ``Connectors/<id>/state.json`` (temp + os.replace)."""
    path = _cursor_path(Path(root), str(cid))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(cur or {}), indent=2, sort_keys=True, default=str) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="state.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:  # safe_paths-internal: contained (safe_paths.contain) cursor path
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))  # safe_paths-internal: atomic cursor swap on a safe_paths.contain()-ed path
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# the base connector
# --------------------------------------------------------------------------- #
class RemoteConnector(Connector):
    """Shared safety core. Subclasses implement ``list_items`` + ``fetch_item``
    only; ``pull`` is a FINAL template method (overriding it fails a test).

    A subclass declares its enumerated download-host suffixes (for the single
    ``http_download`` redirect hop) via ``download_host_suffixes``.
    """

    #: Subclasses set this to the enumerated download-host suffixes that
    #: http_download is permitted to make its single redirect hop to.
    download_host_suffixes: tuple = ()

    # -- subclass hooks ------------------------------------------------------ #
    def list_items(self, ctx: ConnectorContext) -> Iterable[RemoteItem]:
        """Subclass: yield RemoteItem metadata for items WITHIN the scope
        allowlist. Items the API returns outside the allowlist must NOT be
        yielded (or are yielded with ``meta['out_of_scope']=True`` to record an
        expected skip)."""
        raise NotImplementedError(f"{self.id}: list_items not implemented")

    def fetch_item(self, ctx: ConnectorContext, item: RemoteItem) -> Path:
        """Subclass: fetch ONE item's bytes to a private temp stage via
        ``http_download`` ONLY, and return the staged Path."""
        raise NotImplementedError(f"{self.id}: fetch_item not implemented")

    # -- scope allowlist (default-deny) -------------------------------------- #
    def _source_block(self, ctx: ConnectorContext) -> dict:
        src = ctx.manifest.get("source") or self.manifest.get("source") or {}
        return src if isinstance(src, dict) else {}

    #: Subclasses name the source.* allowlist key(s) that gate scope. A pull is
    #: refused unless at least one named key holds a non-empty list.
    scope_allowlist_keys: tuple = ()

    def _assert_scope_allowlist(self, ctx: ConnectorContext) -> None:
        """Default-deny: None / missing / [] / non-list ALL refuse (I4, P7S-13).
        Empty never means "everything"."""
        block = self._source_block(ctx)
        keys = self.scope_allowlist_keys or ()
        if not keys:  # pragma: no cover - subclasses always set this
            raise ConnectorError(f"{self.id}: no scope_allowlist_keys declared")
        ok = False
        for key in keys:
            val = block.get(key)
            if isinstance(val, list) and len(val) > 0:
                ok = True
                break
        if not ok:
            raise ConnectorError(
                f"{self.id}: scope allowlist is empty -- set a non-empty list under "
                f"source.{ ' / source.'.join(keys) } (empty never means 'everything')"
            )

    def _assert_read_only(self) -> None:
        perms = str(self.manifest.get("permissions") or "unknown")
        if perms == "read_write":
            raise ConnectorError(
                f"{self.id}: refusing to run a read_write connector as a read-only "
                f"pull (manifest permissions=read_write)"
            )

    def _connector_floor(self, ctx: ConnectorContext) -> str:
        if ctx.sensitivity_override in _SENS_ORDER:
            return ctx.sensitivity_override
        cand = self._source_block(ctx).get("default_sensitivity")
        return cand if cand in _SENS_ORDER else "internal"

    def _max_files(self, ctx: ConnectorContext) -> int:
        if isinstance(ctx.max_files, int) and ctx.max_files > 0:
            return ctx.max_files
        cand = self._source_block(ctx).get("max_files")
        if isinstance(cand, int) and cand > 0:
            return cand
        return _DEFAULT_MAX_FILES

    def _max_bytes(self, ctx: ConnectorContext) -> int:
        """The per-pull landed-byte ceiling. The autonomy gate's
        blast_radius_caps.max_bytes is the binding ceiling for a gated pull;
        for a direct pull we use the manifest/default fail-closed ceiling."""
        cand = self._source_block(ctx).get("max_bytes")
        if isinstance(cand, int) and cand > 0:
            return cand
        if ctx.gated:
            actions = _import_actions()
            if actions is not None:
                try:
                    autonomy = actions.Autonomy.load(ctx.root)
                    if autonomy.max_bytes > 0:
                        return autonomy.max_bytes
                except Exception:
                    pass
        return _DEFAULT_MAX_BYTES

    # -- landing name (stable + unique per item; P7S-14) --------------------- #
    def _landing_name(self, sp, item: RemoteItem) -> str:
        """``<sha256(item_id)[:12]>_<slug><suffix>`` -- STABLE across pulls
        (supersession keys on origin_filename) and UNIQUE per item."""
        h = hashlib.sha256(str(item.item_id).encode("utf-8")).hexdigest()[:12]
        stem = Path(item.name or "item").stem or "item"
        try:
            slug = sp.safe_slug(stem)
        except ValueError:
            slug = "item"
        suffix = Path(item.name or "").suffix
        clean_suffix = ""
        if suffix:
            tail = suffix.lstrip(".").lower()
            if tail and tail.isalnum():
                clean_suffix = "." + tail
        return f"{h}_{slug}{clean_suffix}"

    def _dest_for(self, sp, root: Path, item: RemoteItem) -> Path:
        rel = f"{_INTAKE_PREFIX}/{self.id}/{self._landing_name(sp, item)}"
        return sp.contain(root, rel, base=_INTAKE_BASE)

    # -- classification ------------------------------------------------------ #
    def _classify(self, path: Path, floor: str) -> str:
        """Classify intake sensitivity from CONTENT signals at pull time.

        Uses ``intake_classify.classify_file(path, connector_default=floor)``
        (NOT localfolder's dead ``classify(path, ...)`` call; P7S-16). The
        manifest floor is the classification FLOOR; ambiguity classifies UP.
        """
        ic = _import_intake_classify()
        if ic is not None and hasattr(ic, "classify_file"):
            try:
                result = ic.classify_file(path, connector_default=floor)
                label = result.get("label") if isinstance(result, dict) else result
                if label in _SENS_ORDER:
                    return label
            except Exception:
                pass
        return floor if floor in _SENS_ORDER else "internal"

    # -- the FINAL template method ------------------------------------------ #
    def pull(self, ctx: ConnectorContext) -> list:
        """FINAL: gate-first authorize -> scope allowlist -> list -> cap+byte
        counter -> classify -> policy -> contained stable landing -> cursor.

        Subclasses MUST NOT override this; the no-override invariant is enforced
        by ``tests/test_connectors_remote.py``.
        """
        # (0) GATE FIRST -- for a gated pull NO network call happens before the
        # grant. We authorize with a cap-derived declared scope (P7S-18); the
        # declared files/bytes are the caps themselves (unknown never declares
        # 0 -- fail closed, P7S-17). This module authorizes; the runtime wrapper
        # (__init__._guarded_pull) is the other enforcement site, but a direct
        # call to pull(gated=True) must STILL not touch the network on deny.
        if ctx.gated:
            self._authorize_before_network(ctx)

        # (1) scope allowlist (default-deny) + read-only.
        self._assert_read_only()
        self._assert_scope_allowlist(ctx)

        sp = _import_safe_paths()
        if sp is None:  # pragma: no cover - floor always ships safe_paths
            raise ConnectorError(f"{self.id}: safe_paths is required to pull")
        policy = _import_policy()

        floor = self._connector_floor(ctx)
        cap = self._max_files(ctx)
        max_bytes = self._max_bytes(ctx)

        results: list = []
        landed_bytes = 0
        ingested = 0

        for item in self.list_items(ctx):
            if not isinstance(item, RemoteItem):  # pragma: no cover - subclass contract
                continue
            # Expected out-of-scope skip (rc 0, never a failure signal; P7S-12).
            if item.meta.get("out_of_scope"):
                results.append({
                    "action": "skipped_out_of_scope",
                    "item_id": redact(item.item_id),
                    "reason": redact(item.meta.get("scope_reason") or "outside scope allowlist"),
                })
                continue

            if ingested >= cap:
                results.append({
                    "action": "skipped",
                    "reason": "max_files cap reached",
                    "item_id": redact(item.item_id),
                })
                continue

            # contained, stable destination under _INPUT/<id>/.
            try:
                dest = self._dest_for(sp, ctx.root, item)
            except ValueError as exc:
                # Containment / zip-slip / unsafe name = a security violation.
                results.append({
                    "action": "refused",
                    "reason": redact(f"destination containment refused: {exc}"),
                    "item_id": redact(item.item_id),
                })
                continue

            if ctx.dry_run:
                results.append({
                    "action": "planned",
                    "item_id": redact(item.item_id),
                    "dst": str(dest),
                })
                ingested += 1
                continue

            # fetch bytes to a private stage via http_download ONLY.
            try:
                stage = self.fetch_item(ctx, item)
            except ByteCapExceeded as exc:
                self._log_byte_abort(ctx, item, exc)
                results.append({
                    "action": "failed",
                    "reason": redact(f"byte cap exceeded: {exc}"),
                    "item_id": redact(item.item_id),
                })
                break  # abort the pull -- the cap is a hard ceiling
            except RedirectRefused as exc:
                results.append({
                    "action": "refused",
                    "reason": redact(f"redirect refused: {exc}"),
                    "item_id": redact(item.item_id),
                })
                continue
            except ConnectorError as exc:
                results.append({
                    "action": "failed",
                    "reason": redact(str(exc)),
                    "item_id": redact(item.item_id),
                })
                continue

            stage = Path(stage)
            try:
                stage_size = stage.stat().st_size
            except OSError:
                stage_size = 0

            # Running landed-byte counter -- ABORT the pull at max_bytes (runtime
            # enforcement, not just plan-time; P7S-17).
            if landed_bytes + stage_size > max_bytes:
                self._cleanup_stage(stage)
                exc = ByteCapExceeded(
                    f"cumulative landed bytes {landed_bytes + stage_size} exceeds "
                    f"max_bytes={max_bytes}"
                )
                self._log_byte_abort(ctx, item, exc)
                results.append({
                    "action": "failed",
                    "reason": redact(str(exc)),
                    "item_id": redact(item.item_id),
                })
                break

            # classify from CONTENT at pull time.
            sensitivity = self._classify(stage, floor)

            # policy gate (deny = SKIP, expected, rc 0).
            if policy is not None:
                try:
                    verdict = policy.check_processing(sensitivity, "local_agent")
                except Exception:
                    verdict = "deny"
                if verdict == "deny":
                    self._cleanup_stage(stage)
                    results.append({
                        "action": "skipped_policy",
                        "reason": f"processing denied for sensitivity={sensitivity}",
                        "item_id": redact(item.item_id),
                        "sensitivity": sensitivity,
                    })
                    continue

            # land it: stage -> _INPUT via safe_copy_verify_delete (consumes the
            # stage, never the upstream).
            try:
                sha = sp.safe_copy_verify_delete(stage, dest)
            except Exception as exc:
                self._cleanup_stage(stage)
                results.append({
                    "action": "failed",
                    "reason": redact(str(exc)),
                    "item_id": redact(item.item_id),
                    "dst": str(dest),
                })
                continue

            landed_bytes += stage_size
            ingested += 1
            results.append({
                "action": "ingested",
                "item_id": redact(item.item_id),
                "dst": str(dest),
                "origin_filename": dest.name,
                "sensitivity": sensitivity,
                "sha256_12": sha,
                "connector": self.id,
            })

        # cursor advance (atomic). dry_run reports the plan only -- no cursor
        # write (steps 0-5 done; step 6 skipped).
        if not ctx.dry_run:
            self._advance_cursor(ctx, results)

        return results

    # -- gate-first authorization (P7S-18) ----------------------------------- #
    def _authorize_before_network(self, ctx: ConnectorContext) -> None:
        """Authorize a gated pull with a cap-derived declared scope BEFORE any
        network call. Raises ConnectorError on deny -- NO list/fetch happens."""
        actions = _import_actions()
        if actions is None or not hasattr(actions, "authorize"):
            return  # the runtime wrapper handles the gate when actions exists
        scope = planned_pull_scope(self, ctx)
        decision = actions.authorize(CONNECTOR_PULL_LOOP, scope, root=ctx.root)
        if not decision.get("granted"):
            raise ConnectorError(
                f"connector pull refused by action gate: {redact(decision.get('reason'))}"
            )

    def _log_byte_abort(self, ctx: ConnectorContext, item: RemoteItem, exc) -> None:
        cap = _import_capture()
        if cap is None or not hasattr(cap, "failure_event"):
            return
        try:
            cap.failure_event(
                ctx.root,
                target=f"connector:{self.id}",
                severity="high",
                failure_mode="byte-cap-abort",
                excerpt=redact(str(exc)),
                actor=getattr(ctx, "actor", "connector-runtime"),
            )
        except Exception:
            pass

    def _cleanup_stage(self, stage: Path) -> None:
        try:
            stage = Path(stage)
            if stage.exists():
                stage.unlink()
            parent = stage.parent
            if parent.name.startswith("oracle-") and parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    pass
        except OSError:
            pass

    # -- cursor / freshness -------------------------------------------------- #
    def _advance_cursor(self, ctx: ConnectorContext, results: list) -> None:
        cur = load_cursor(ctx.root, self.id)
        ingested = [r for r in results if r.get("action") == "ingested"]
        cur["last_success_ts"] = datetime.now().isoformat(timespec="seconds")
        cur["last_ingested_count"] = len(ingested)
        save_cursor(ctx.root, self.id, cur)

    def freshness(self, ctx: ConnectorContext) -> dict:
        """Derive freshness from the cursor's ``last_success_ts``, NOT from the
        manifest ``freshness.last_verified`` -- a pull never rewrites its own
        manifest (P7S-23). Without this a scheduled connector reports stale
        forever.
        """
        from datetime import datetime as _dt

        cur = load_cursor(ctx.root, self.id)
        last = cur.get("last_success_ts")
        fblock = ctx.manifest.get("freshness") or self.manifest.get("freshness") or {}
        fblock = fblock if isinstance(fblock, dict) else {}
        decay = fblock.get("expected_decay_days")
        fclass = fblock.get("class")

        verdict = "unknown"
        age_days = None
        if last:
            try:
                last_dt = _dt.fromisoformat(str(last).replace("Z", "+00:00"))
                now = ctx.now
                if last_dt.tzinfo is not None and now.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=None)
                age_days = max(0.0, (now - last_dt).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                age_days = None
        if age_days is not None and isinstance(decay, int):
            verdict = "fresh" if age_days <= decay else "stale"
        return {
            "connector": self.id,
            "class": fclass,
            "verdict": verdict,
            "age_days": round(age_days, 2) if age_days is not None else None,
            "expected_decay_days": decay if isinstance(decay, int) else None,
            "last_success_ts": last,
        }


# --------------------------------------------------------------------------- #
# planned scope (consumed by __init__._planned_pull_scope; fail-closed pricing)
# --------------------------------------------------------------------------- #
def planned_pull_scope(connector: "RemoteConnector", ctx: ConnectorContext) -> dict:
    """Declare a gated pull's blast radius for the autonomy gate.

    Loop id is the canonical ``connector-pull`` (P7S-20). Declared files/bytes
    are the CAPS themselves when probe data is absent -- unknown never declares
    0 (fail closed, P7S-17).
    """
    cap_files = _DEFAULT_MAX_FILES
    if isinstance(ctx.max_files, int) and ctx.max_files > 0:
        cap_files = ctx.max_files
    else:
        src = ctx.manifest.get("source") or {}
        if isinstance(src, dict) and isinstance(src.get("max_files"), int) and src["max_files"] > 0:
            cap_files = src["max_files"]

    # Byte ceiling: gate cap if gated, else manifest/default. Fail-closed: never 0.
    cap_bytes = _DEFAULT_MAX_BYTES
    src = ctx.manifest.get("source") or {}
    if isinstance(src, dict) and isinstance(src.get("max_bytes"), int) and src["max_bytes"] > 0:
        cap_bytes = src["max_bytes"]
    else:
        actions = _import_actions()
        if actions is not None:
            try:
                autonomy = actions.Autonomy.load(ctx.root)
                if autonomy.max_bytes > 0:
                    cap_bytes = autonomy.max_bytes
            except Exception:
                pass

    return {
        "loop": CONNECTOR_PULL_LOOP,
        "connectors": [connector.id],
        "lanes": ["_INPUT"],
        "files": cap_files,
        "bytes": cap_bytes,
        "actor": ctx.actor,
        "role": ctx.role,
    }
