"""llm/client.py -- stdlib OpenAI-compatible chat client (SPEC S2).

No SDKs, no third-party HTTP. Talks to any ``/v1/chat/completions`` endpoint
(OpenAI, OpenRouter, Anthropic's OpenAI-compatible surface, Ollama, ...) over
``urllib``.

Security-relevant behavior (STRESS C2):
  * Redirects are NEVER followed. A 3xx is raised as an error -- a loopback
    endpoint cannot 302 the confidential prompt + Authorization header off-box.
  * The API key is never placed in any exception message, repr, or log.

Errors are classified into a small, action-oriented taxonomy so the retry
layer can decide retry/backoff/fail without provider-specific branching.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


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
                 timeout: float = 120.0, extra_headers: dict | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self._api_key = api_key  # never logged
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        h.update(self.extra_headers)
        return h

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

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._endpoint(), data=data,
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
    """
    rng = rng or random.Random()
    last: LLMError | None = None
    for attempt in range(max_attempts):
        try:
            return client.chat(messages, tools=tools, **kw)
        except LLMError as exc:
            last = exc
            if not exc.retryable or attempt == max_attempts - 1:
                raise
            if exc.retry_after is not None:
                delay = exc.retry_after
            else:
                delay = base_delay * (2 ** attempt)
                delay = rng.uniform(0, delay)  # full jitter
            sleep(delay)
    assert last is not None
    raise last
