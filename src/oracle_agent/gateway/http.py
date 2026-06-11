"""gateway/http.py -- local HTTP/MCP surface (Phase 4, P4-T4).

A loopback-only ``http.server`` surface with bearer-token auth, a JSON
``POST /ask {text}`` endpoint that returns a grounded reply (through
:class:`~oracle_agent.gateway.core.GatewayCore`), and a minimal MCP-shaped tool
endpoint (``POST /mcp``) so other local agents can consult the oracle. Every
authorization decision still belongs to the core / the Dispatcher chokepoint --
this module only does transport, auth, and request shaping.

Security pins (P4S-7/8/9):

  * **Fail-closed startup (P4S-7):** an unresolved or empty token => the adapter
    REFUSES to start (``RuntimeError``). There is no unauthenticated mode, ever.
  * **Auth on every route (P4S-7):** the bearer token is required on ALL routes
    including ``/mcp``, compared with ``hmac.compare_digest`` (constant time).
    The HTTP surface authenticates the TOKEN, not the OS user: loopback is
    reachable by every local UID, so anyone holding the token IS the configured
    ``principal``.
  * **Browser hardening (P4S-7):** the ``Host`` header must be in
    ``{127.0.0.1:<port>, localhost:<port>, [::1]:<port>}`` or the request is
    refused (kills DNS rebinding). No CORS headers are EVER emitted.
    ``Content-Length`` is capped; a per-request socket timeout is set.
  * **Bind validation (P4S-7):** the configured ``bind`` must parse as a literal
    loopback IP via ``ipaddress.ip_address(bind).is_loopback`` -- hostnames
    (including ``"localhost"``, which ``bind()`` would resolve via DNS) and
    ``0.0.0.0`` are refused at startup.
  * **MCP through the Dispatcher ONLY (P4S-8):** ``tools/list`` is EXACTLY
    ``tool_schemas(surface="gateway", environment)``; every ``tools/call``
    routes through ``Dispatcher.dispatch`` -- no parallel verb table, no raw
    kernel passthrough. Dropped verbs stay structurally absent and are denied
    fail-closed; ceiling forcing / ``--q=`` packing / M5 stripping / write
    provenance + rate limits all apply.
  * **No control plane (P4S-8):** no endpoint mutates allowlists, config,
    pairing, or instances.
  * **Concurrency + shutdown (P4S-9):** a single-threaded ``HTTPServer`` in its
    own listener thread. HTTP turns take the per-root lock with ``nb=True``
    (bounded retry) and return ``503 busy`` on contention. Shutdown:
    ``server.shutdown()`` from the main thread, then join the listener.

Stdlib only.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import InboundMessage

# Request body cap (P4S-7): refuse oversized POSTs before reading them.
_MAX_BODY_BYTES = 256 * 1024

# Per-request socket timeout (P4S-7): a slow-loris client can't hang the
# single-threaded listener forever.
_SOCKET_TIMEOUT = 30.0

# Bounded drain slack: when a body is over-cap we read AT MOST cap + this many
# bytes (in chunks) before replying 413, so a well-behaved client finishes its
# send and reads the status cleanly instead of being RST mid-write. The drain is
# always bounded -- never an unbounded read -- so the DoS protection holds.
_DRAIN_SLACK_BYTES = 64 * 1024
_DRAIN_CHUNK_BYTES = 64 * 1024


def validate_bind(bind: str) -> str:
    """Return ``bind`` iff it parses as a literal loopback IP, else raise (P4S-7).

    Hostnames (including ``"localhost"``) and non-loopback literals
    (``0.0.0.0``) are refused -- ``ipaddress.ip_address`` raises ``ValueError``
    on a hostname, and ``.is_loopback`` rejects ``0.0.0.0``. Consistent with
    SH-032/033's literal-loopback discipline.
    """
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError as exc:
        raise ValueError(
            f"http bind {bind!r} is not a literal IP address (hostnames, "
            f"including 'localhost', are refused -- they would resolve via DNS)"
        ) from exc
    if not ip.is_loopback:
        raise ValueError(
            f"http bind {bind!r} is not a loopback address (only 127.0.0.0/8 "
            f"and ::1 are allowed; 0.0.0.0 is refused)")
    return bind


def _allowed_hosts(bind: str, port: int) -> set[str]:
    """The exact Host header values accepted for ``(bind, port)`` (P4S-7)."""
    return {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
        f"{bind}:{port}",
    }


class HTTPAdapter:
    """Loopback HTTP/MCP surface composed over a :class:`GatewayCore`.

    ``core`` handles ``/ask`` (allowlist + ceiling + grounding + ledger). The
    ``principal`` is the single token-authenticated identity used as the
    InboundMessage ``user_id`` (the allowlist must carry it). ``dispatcher_factory``
    builds a properly-configured :class:`~oracle_agent.agentloop.verbtools.Dispatcher`
    for MCP ``tools/call`` so the same Dispatcher chokepoint (ceiling forcing,
    write provenance, rate limits) applies (P4S-8). ``environment`` is the
    classified provider environment for ``tool_schemas`` / dispatch (gateway is
    always ``"external"``).
    """

    surface = "http"

    def __init__(self, surface_cfg: dict, core, *, token: str,
                 dispatcher_factory=None, environment: str = "external",
                 logger=None, nb_lock_factory=None):
        self.surface_cfg = surface_cfg or {}
        self.core = core
        # HTTP turns take the per-root lock with nb=True and return 503 on
        # contention (P4S-9). The lock is acquired by the ADAPTER around the
        # turn; the core is composed with a no-op root lock so the (non-
        # reentrant) flock is never taken twice. ``nb_lock_factory(instance)``
        # raises ``BlockingIOError`` when the root is busy.
        self.nb_lock_factory = nb_lock_factory
        # Fail-closed startup (P4S-7): no token => no server, ever.
        if not token:
            raise RuntimeError(
                "http adapter: token_env unresolved or empty; refusing to "
                "start (there is no unauthenticated mode)")
        self._token = token
        self.principal = self.surface_cfg.get("principal", "http-operator")
        self.environment = environment
        self.dispatcher_factory = dispatcher_factory
        self.logger = logger or (lambda *a: None)

        self.bind = validate_bind(self.surface_cfg.get("bind", "127.0.0.1"))
        self.port = int(self.surface_cfg.get("port", 8765))
        self.allowed_hosts = _allowed_hosts(self.bind, self.port)

        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- protocol ----------------------------------------------------------- #
    def supports_push(self) -> bool:
        return True

    def commit(self) -> None:  # no cursor; push surface
        return None

    # -- auth (constant time; every route) ---------------------------------- #
    def check_token(self, auth_header: str | None) -> bool:
        """Constant-time bearer check (P4S-7). Required on ALL routes."""
        if not auth_header:
            return False
        prefix = "Bearer "
        if not auth_header.startswith(prefix):
            return False
        presented = auth_header[len(prefix):].strip()
        if not presented:
            return False
        return hmac.compare_digest(presented, self._token)

    def check_host(self, host_header: str | None) -> bool:
        """Host allowlist check (P4S-7): kills DNS rebinding."""
        if not host_header:
            return False
        return host_header.strip() in self.allowed_hosts

    # -- /ask --------------------------------------------------------------- #
    def handle_ask(self, text: str) -> dict:
        """Run one grounded turn through the core (token IS the principal).

        ``is_private=True``: the HTTP surface is a token-authenticated single
        principal (P4S-5 matrix). Returns a dict suitable for JSON; the caller
        maps it to an HTTP status.
        """
        msg = InboundMessage(
            surface="http",
            user_id=str(self.principal),
            channel_id=str(self.principal),
            text=text,
            is_private=True,
            meta={},
        )
        # Per-root lock taken nb=True at the ADAPTER (P4S-9): a blocking acquire
        # would pile requests up behind a 600-second harness tick. On contention
        # we return 503 busy instead of queueing.
        lock_factory = self.nb_lock_factory
        if lock_factory is not None:
            instance = self._instance_for_principal()
            if instance is not None:
                try:
                    with lock_factory(instance):
                        reply = self.core.handle(msg)
                except BlockingIOError:
                    return {"_status": 503, "error": "busy"}
            else:
                reply = self.core.handle(msg)
        else:
            reply = self.core.handle(msg)
        if reply is None:
            # Deny-by-default silence (unknown principal / empty text).
            return {"_status": 403, "error": "denied"}
        return {"_status": 200, "reply": reply.text}

    def _instance_for_principal(self) -> str | None:
        entry = (self.surface_cfg.get("allowlist") or {}).get(str(self.principal))
        if isinstance(entry, dict):
            return entry.get("instance")
        return None

    # -- /mcp (Dispatcher-only; P4S-8) -------------------------------------- #
    def handle_mcp(self, payload: dict) -> dict:
        """Minimal MCP: ``tools/list`` and ``tools/call`` via the Dispatcher.

        ``tools/list`` is EXACTLY ``tool_schemas(surface="gateway",
        environment)``. ``tools/call`` routes through ``Dispatcher.dispatch`` --
        no parallel verb table. A dropped verb is denied fail-closed by the
        Dispatcher itself (P4S-8). NO endpoint here mutates allowlists, config,
        pairing, or instances (no-control-plane invariant, P4S-8).
        """
        from ..agentloop.verbtools import tool_schemas

        method = payload.get("method")
        req_id = payload.get("id")
        if method == "tools/list":
            schemas = tool_schemas(surface="gateway", environment=self.environment)
            tools = [
                {"name": s["function"]["name"],
                 "description": s["function"]["description"],
                 "inputSchema": s["function"]["parameters"]}
                for s in schemas
            ]
            return {"_status": 200,
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {"tools": tools}}

        if method == "tools/call":
            params = payload.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not name:
                return {"_status": 400, "jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32602, "message": "missing tool name"}}
            if self.dispatcher_factory is None:
                return {"_status": 503, "jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32603,
                                  "message": "dispatch unavailable"}}
            dispatcher = self.dispatcher_factory(self.principal)
            outcome = dispatcher.dispatch(name, arguments)
            return {"_status": 200, "jsonrpc": "2.0", "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": outcome.text}],
                        "isError": outcome.rc not in (0,),
                    }}

        return {"_status": 400, "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}"}}

    # -- server lifecycle (P4S-9) ------------------------------------------- #
    def make_server(self) -> HTTPServer:
        """Build (but do not start) the single-threaded HTTPServer."""
        adapter = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # silence stdlib stderr logging
                return

            # No CORS headers are EVER emitted (P4S-7); _send writes only the
            # minimal set below.
            def _send(self, status: int, obj: dict, *, close: bool = False) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                # Connection: close lets us refuse an over-cap body without
                # honoring HTTP/1.1 keep-alive (we won't read the rest of it).
                if close:
                    self.close_connection = True
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _drain(self, length: int) -> None:
                """Read AT MOST ``min(length, cap + slack)`` bytes in chunks and
                discard them, so a well-behaved client can finish its send and
                read our status instead of being RST mid-write. Always bounded --
                never an unbounded read -- so the DoS protection holds. Best
                effort: a dead/closed peer just ends the drain early."""
                to_read = min(length, _MAX_BODY_BYTES + _DRAIN_SLACK_BYTES)
                try:
                    while to_read > 0:
                        chunk = self.rfile.read(min(to_read, _DRAIN_CHUNK_BYTES))
                        if not chunk:
                            break
                        to_read -= len(chunk)
                except OSError:
                    # Peer already gone / reset: nothing left to drain.
                    return

            def _guard(self) -> bool:
                """Host + auth guard, run on every route. Returns False (and
                replies) when the request must be refused."""
                if not adapter.check_host(self.headers.get("Host")):
                    self._send(403, {"error": "bad host"})
                    return False
                if not adapter.check_token(self.headers.get("Authorization")):
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def _read_body(self) -> bytes | None:
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    self._send(400, {"error": "bad content-length"})
                    return None
                if length > _MAX_BODY_BYTES:
                    # Bounded-drain BEFORE replying: read up to cap+slack so the
                    # client finishes its (over-cap) send and can read the 413
                    # cleanly, instead of the kernel RSTing it mid-write. Then
                    # close the connection (we won't consume the rest).
                    self._drain(length)
                    self._send(413, {"error": "request entity too large"},
                               close=True)
                    return None
                if length <= 0:
                    return b""
                return self.rfile.read(length)

            def do_POST(self):  # noqa: N802
                if not self._guard():
                    return
                raw = self._read_body()
                if raw is None:
                    return  # _read_body already replied (413/400)
                try:
                    payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                except json.JSONDecodeError:
                    self._send(400, {"error": "invalid json"})
                    return
                if not isinstance(payload, dict):
                    self._send(400, {"error": "json object required"})
                    return

                if self.path == "/ask":
                    text = payload.get("text")
                    if not isinstance(text, str) or not text.strip():
                        self._send(400, {"error": "missing 'text'"})
                        return
                    result = adapter.handle_ask(text)
                elif self.path == "/mcp":
                    result = adapter.handle_mcp(payload)
                else:
                    self._send(404, {"error": "not found"})
                    return

                status = result.pop("_status", 200)
                self._send(status, result)

            # Only POST is served; everything else is refused (no control plane).
            def do_GET(self):  # noqa: N802
                if not self._guard():
                    return
                self._send(404, {"error": "not found"})

        server = HTTPServer((self.bind, self.port), _Handler)
        server.timeout = _SOCKET_TIMEOUT
        return server

    def start(self) -> None:
        """Start the listener thread (P4S-9)."""
        if self._server is not None:
            return
        self._server = self.make_server()
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="oracle-http", daemon=True)
        self._thread.start()
        self.logger(f"gateway[http]: listening on {self.bind}:{self.port}")

    def stop(self) -> None:
        """Stop the listener (P4S-9): shutdown() from the main thread, then join."""
        if self._server is None:
            return
        try:
            self._server.shutdown()  # documented cross-thread call
            self._server.server_close()
        finally:
            if self._thread is not None:
                self._thread.join(timeout=5.0)
            self._server = None
            self._thread = None
        self.logger("gateway[http]: stopped")
