"""llm/client.py -- stdlib OpenAI-compatible chat client (SPEC S2).

No SDKs, no third-party HTTP. Talks to any ``/v1/chat/completions`` endpoint
(OpenAI, OpenRouter, Anthropic's OpenAI-compatible surface, Ollama, ...) over
``urllib``.

Security-relevant behavior (STRESS C2):
  * Redirects are NEVER followed. A 3xx is raised as an error -- a loopback
    endpoint cannot 302 the confidential prompt + Authorization header off-box.
  * The API key is never placed in any exception message, repr, or log.
  * Non-loopback ``http://`` with an API key is refused at construction time --
    the key must not travel over plaintext to any off-box host.
  * Per-request guard: a client classified ``local_agent`` refuses to send to
    any URL whose host is not a literal loopback (no DNS re-check); this closes
    the TOCTOU window where classification happens at build time but the
    endpoint could have changed by request time.
  * ``Retry-After`` is honored but capped at 30 s; total retry sleep ≤ 120 s.

Errors are classified into a small, action-oriented taxonomy so the retry
layer can decide retry/backoff/fail without provider-specific branching.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# loopback literals (mirrors policy_bridge._is_literal_loopback_host)
# ---------------------------------------------------------------------------
import ipaddress as _ipaddress


def _is_literal_loopback_host(host: str) -> bool:
    """Return True iff ``host`` is a provably-loopback literal (no DNS)."""
    h = (host or "").lower().strip()
    if h == "localhost":
        return True
    h_stripped = h.strip("[]")
    try:
        return _ipaddress.ip_address(h_stripped).is_loopback
    except ValueError:
        return False


_RETRY_AFTER_CAP = 30.0       # seconds
_RETRY_BUDGET    = 120.0      # max total sleep across all retries


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect (STRESS C2): never re-send body/Authorization."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect blocked ({code})", headers, fp
        )


class LLMError(Exception):
    """Classified LLM transport/API error.

    ``kind`` in {auth, rate_limit, context_overflow, server, network,
    bad_request}; ``retryable`` true for rate_limit/server/network.
    """

    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 retryable: bool = False, retry_after: float | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as returned by the model


@dataclass
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)


_CONTEXT_MARKERS = ("context_length", "maximum context", "context window",
                    "too many tokens", "reduce the length")


def classify_error(status: int | None, body: str) -> LLMError:
    """Map an HTTP status + response body to an :class:`LLMError`."""
    low = (body or "").lower()
    if status == 400 and any(m in low for m in _CONTEXT_MARKERS):
        return LLMError("context_overflow", "context window exceeded", status=status)
    if status in (401, 403):
        return LLMError("auth", "authentication failed", status=status)
    if status == 429:
        return LLMError("rate_limit", "rate limited", status=status, retryable=True)
    if status is not None and 500 <= status < 600:
        return LLMError("server", f"server error {status}", status=status, retryable=True)
    if status == 400:
        return LLMError("bad_request", "bad request", status=status)
    if status is None:
        return LLMError("network", "network failure", status=None, retryable=True)
    return LLMError("bad_request", f"unexpected status {status}", status=status)


def _retry_after_seconds(headers) -> float | None:
    try:
        val = headers.get("Retry-After") if headers else None
    except Exception:
        return None
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 120.0, extra_headers: dict | None = None,
                 environment: str | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self._api_key = api_key  # never logged
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.environment = environment or "external"
        self._opener = urllib.request.build_opener(_NoRedirect())

        # Security check: refuse non-loopback http:// when an API key is present.
        # Plaintext transport must not carry bearer tokens off-box.
        if api_key and self.base_url:
            parsed = urllib.parse.urlsplit(self.base_url)
            if parsed.scheme == "http":
                host = (parsed.hostname or "").lower()
                if not _is_literal_loopback_host(host):
                    raise LLMError(
                        "bad_request",
                        "Refusing to send API key over plaintext http:// to "
                        f"non-loopback host {host!r}. Use https:// or a "
                        "loopback address.",
                        retryable=False,
                    )

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        h.update(self.extra_headers)
        return h

    def _check_request_host(self, url: str) -> None:
        """Per-request guard for local_agent clients (TOCTOU close, STRESS C2).

        If this client was classified ``local_agent``, the target URL's host
        must be a literal loopback address.  Any deviation (DNS rebinding,
        config swap) is refused here at send time, not just at build time.
        """
        if self.environment != "local_agent":
            return
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except Exception:
            host = ""
        if not _is_literal_loopback_host(host):
            raise LLMError(
                "bad_request",
                f"local_agent client refused to send to non-loopback host "
                f"{host!r}. Possible DNS rebinding or endpoint swap.",
                retryable=False,
            )

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None,
             max_tokens: int | None = None) -> ChatResponse:
        payload: dict = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        endpoint = self._endpoint()
        self._check_request_host(endpoint)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data,
                                     headers=self._headers(), method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            err = classify_error(exc.code, body)
            err.retry_after = _retry_after_seconds(getattr(exc, "headers", None))
            raise err from None
        except urllib.error.URLError as exc:
            # Network-level failure; key never appears in the message.
            raise LLMError("network", f"{exc.reason}", status=None, retryable=True) from None

        return self._parse(body)

    @staticmethod
    def _parse(body: str) -> ChatResponse:
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError("server", f"non-JSON response: {exc}", status=None,
                           retryable=True) from None
        choices = obj.get("choices") or []
        if not choices:
            return ChatResponse(content=None, usage=obj.get("usage", {}) or {})
        msg = (choices[0] or {}).get("message") or {}
        tcs: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            tcs.append(ToolCall(
                id=str(tc.get("id") or f"call_{len(tcs)}"),
                name=str(fn.get("name") or ""),
                arguments=fn.get("arguments") if isinstance(fn.get("arguments"), str)
                else json.dumps(fn.get("arguments") or {}),
            ))
        return ChatResponse(
            content=msg.get("content"),
            tool_calls=tcs,
            finish_reason=(choices[0] or {}).get("finish_reason"),
            usage=obj.get("usage", {}) or {},
        )


def chat_with_retry(client: LLMClient, messages: list[dict], *,
                    tools: list[dict] | None = None, max_attempts: int = 5,
                    base_delay: float = 1.0, sleep=time.sleep,
                    rng: random.Random | None = None, **kw) -> ChatResponse:
    """Call ``client.chat`` with jittered exponential backoff on retryable errors.

    Non-retryable errors (auth/bad_request/context_overflow) raise immediately.
    ``sleep`` and ``rng`` are injectable for deterministic tests.

    ``Retry-After`` is honored but capped at ``_RETRY_AFTER_CAP`` (30 s).
    Total sleep budget across all retries is capped at ``_RETRY_BUDGET`` (120 s).
    """
    rng = rng or random.Random()
    last: LLMError | None = None
    total_slept = 0.0
    for attempt in range(max_attempts):
        try:
            return client.chat(messages, tools=tools, **kw)
        except LLMError as exc:
            last = exc
            if not exc.retryable or attempt == max_attempts - 1:
                raise
            if exc.retry_after is not None:
                delay = min(exc.retry_after, _RETRY_AFTER_CAP)
            else:
                delay = base_delay * (2 ** attempt)
                delay = rng.uniform(0, delay)  # full jitter
            # Enforce total budget.
            remaining = _RETRY_BUDGET - total_slept
            if remaining <= 0:
                raise
            delay = min(delay, remaining)
            total_slept += delay
            sleep(delay)
    assert last is not None
    raise last
