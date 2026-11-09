"""Send NIM chat requests: retries, streaming, failures."""

import json
import os
import random
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any

import httpx

from castor.app import (
    logger,
)
from castor.nim.config import (
    NIM_BACKOFF_BASE,
    NIM_BACKOFF_JITTER,
    NIM_BACKOFF_MAX,
    NIM_CONNECT_TIMEOUT,
    NIM_IDLE_TIMEOUT,
    NIM_KEYS_URL,
    NIM_KEY_RETIRING_KINDS,
    NIM_MAX_ATTEMPTS,
    NIM_MAX_TIMEOUTS,
    NIM_REQUEST_TIMEOUT,
    NIM_STREAM_MAX_SECONDS,
    _nim_api_keys,
    _nim_base_url,
    _nim_env,
    _nim_redact,
    _nim_url_error,
)
from castor.nim.limits import (
    _nim_admit,
    _nim_count,
    _nim_gate,
    _nim_gate_size,
    _nim_key_state,
    _nim_key_states,
    _nim_lock,
    _nim_max_wait,
    _nim_set_cooldown,
)
from castor.nim.records import (
    _nim_usage_record,
)


_NIM_CONTEXT_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "too many tokens",
    "prompt is too long",
)


_nim_http: httpx.Client | None = None  # shared, so connections are reused


def _nim_client() -> httpx.Client:
    """The shared HTTP client (lazy init), so requests reuse connections.

    Redirects are never followed and there is no default timeout: each
    request passes its own. httpx.Client is thread-safe.
    """
    global _nim_http
    with _nim_lock:
        if _nim_http is None:
            slots = _nim_gate_size()
            _nim_http = httpx.Client(
                follow_redirects=False,
                timeout=None,
                limits=httpx.Limits(
                    max_connections=slots + 2,
                    max_keepalive_connections=slots,
                ),
            )
        return _nim_http


@dataclass
class _NimFailure:
    """A classified NIM failure.

    Attributes:
        kind: auth, permission, billing, not_found, context, invalid,
            rate_limit, upstream, timeout, network or other.
        retryable: Whether retrying the same request can succeed.
        hint: Short human explanation (shown inside the error brackets).
        detail: The server's own message, redacted and truncated.
        retry_after: Seconds from a numeric Retry-After header, or None.
    """

    kind: str
    retryable: bool
    hint: str
    detail: str = ""
    retry_after: float | None = None


def _nim_error_detail(body: str) -> str:
    """Pull a short message out of an error response body."""
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    if isinstance(data, dict):
        err = data.get("error")
        for value in (
            data.get("detail"),
            data.get("message"),
            err.get("message") if isinstance(err, dict) else err,
        ):
            if value:
                text = value if isinstance(value, str) else json.dumps(value)
                return text.strip()[:300]
    return body.strip()[:300]


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a numeric Retry-After header (HTTP-date form is ignored)."""
    try:
        seconds = float((value or "").strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _classify_nim_status(
    status: int, detail: str, retry_after: float | None = None
) -> _NimFailure:
    """Classify a non-200 NIM response into a _NimFailure."""
    if status == 429:
        return _NimFailure(
            "rate_limit",
            True,
            "rate limited by NVIDIA (429)",
            detail,
            retry_after,
        )
    if status >= 500:
        return _NimFailure(
            "upstream",
            True,
            f"NVIDIA service error ({status})",
            detail,
            retry_after,
        )
    if status == 413 or (
        status in (400, 422)
        and any(m in detail.lower() for m in _NIM_CONTEXT_MARKERS)
    ):
        return _NimFailure(
            "context",
            False,
            "prompt exceeds the model's context window — inline less "
            "content or pick a larger-context model",
            detail,
        )
    fixed = {
        400: ("invalid", "invalid request (400)"),
        422: ("invalid", "invalid request (422)"),
        401: (
            "auth",
            f"API key rejected (401) — check NVIDIA_API_KEY ({NIM_KEYS_URL})",
        ),
        402: ("billing", "credits or quota exhausted (402)"),
        403: (
            "permission",
            "access denied (403) — the key may not be entitled to this model",
        ),
        404: (
            "not_found",
            "model or endpoint not found (404) — run nim_models for valid "
            "model ids",
        ),
    }
    kind, hint = fixed.get(status, ("other", f"unexpected status {status}"))
    return _NimFailure(kind, False, hint, detail)


def _format_nim_failure(failure: _NimFailure) -> str:
    """Render a failure as a bracketed error string."""
    text = f"[NVIDIA NIM error — {failure.hint}]"
    if failure.detail:
        text += f"\n{failure.detail}"
    return text


def _nim_backoff(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter; honors a Retry-After floor."""
    base = min(NIM_BACKOFF_MAX, NIM_BACKOFF_BASE * 2 ** (attempt - 1))
    delay = base + random.uniform(0, NIM_BACKOFF_JITTER)
    return max(delay, retry_after) if retry_after else delay


def _nim_send_once(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict | None,
    key: str | None,
    read_timeout: float | None = None,
) -> tuple[Any, _NimFailure | None]:
    """Send one HTTP request. Returns (parsed JSON, None) or (None, failure).

    A streamed chat completion is rebuilt into the non-streaming shape, so
    callers never see the difference. Redirects are not followed, so the
    Authorization header can never be forwarded to another host.
    `read_timeout` raises the wait for one request above
    CASTOR_NIM_TIMEOUT (never lowers it).
    """
    streaming = bool(payload and payload.get("stream"))
    default = NIM_IDLE_TIMEOUT if streaming else NIM_REQUEST_TIMEOUT
    base = float(_nim_env("CASTOR_NIM_TIMEOUT", default))
    timeout = httpx.Timeout(
        max(base, read_timeout or 0), connect=NIM_CONNECT_TIMEOUT
    )
    try:
        if streaming and payload is not None:
            return _nim_send_stream(url, headers, payload, key, timeout)
        response = _nim_client().request(
            method, url, headers=headers, json=payload, timeout=timeout
        )
    except httpx.TimeoutException:
        return None, _NimFailure(
            "timeout", True, f"request timed out after {timeout.read:.0f}s"
        )
    except httpx.HTTPError as exc:
        return None, _NimFailure(
            "network",
            True,
            f"could not reach NVIDIA NIM ({type(exc).__name__})",
        )
    return _nim_parse_response(response, key)


def _nim_parse_response(
    response: httpx.Response, key: str | None
) -> tuple[Any, _NimFailure | None]:
    """A read response as (parsed JSON, None) or (None, failure)."""
    if response.status_code == 200:
        try:
            return response.json(), None
        except ValueError:
            return None, _NimFailure(
                "upstream", True, "response was not valid JSON"
            )
    detail = _nim_redact(_nim_error_detail(response.text), key)
    retry_after = _parse_retry_after(response.headers.get("retry-after"))
    return None, _classify_nim_status(
        response.status_code, detail, retry_after
    )


def _nim_stream_fields() -> dict[str, Any]:
    """Payload fields that turn streaming on, unless CASTOR_NIM_STREAM=0."""
    if os.environ.get("CASTOR_NIM_STREAM", "1").strip() == "0":
        return {"stream": False}
    return {"stream": True, "stream_options": {"include_usage": True}}


def _nim_send_stream(
    url: str,
    headers: dict[str, str],
    payload: dict,
    key: str | None,
    timeout: httpx.Timeout,
) -> tuple[Any, _NimFailure | None]:
    """POST a streaming chat completion and rebuild the whole response.

    An error status, or an endpoint that answers with plain JSON despite
    `stream`, is read and parsed as a normal response.
    """
    with _nim_client().stream(
        "POST", url, headers=headers, json=payload, timeout=timeout
    ) as response:
        kind = response.headers.get("content-type", "")
        if response.status_code != 200 or "text/event-stream" not in kind:
            response.read()
            return _nim_parse_response(response, key)
        return _nim_read_stream(
            response, bool(payload.get("tools")), timeout.read or 0
        )


def _nim_read_stream(
    response: httpx.Response, tools: bool, idle: float
) -> tuple[Any, _NimFailure | None]:
    """Accumulate SSE chunks until [DONE], a stall, or the wall cap."""
    stream = _NimStream()
    deadline = time.monotonic() + NIM_STREAM_MAX_SECONDS
    try:
        for line in response.iter_lines():
            if not stream.feed(line):
                break
            if time.monotonic() > deadline:
                return _nim_cut_short(
                    stream, tools, f"at the {NIM_STREAM_MAX_SECONDS:.0f}s cap"
                )
    except httpx.ReadTimeout:
        return _nim_cut_short(stream, tools, f"after a stall of {idle:.0f}s")
    except ValueError:
        return None, _NimFailure(
            "upstream", True, "a stream chunk was not valid JSON"
        )
    return stream.result(), None


def _nim_cut_short(
    stream: "_NimStream", tools: bool, why: str
) -> tuple[Any, _NimFailure | None]:
    """Handle a stream that stopped before [DONE].

    A partial answer beats a retry that spends the same time again, so an
    answer with text comes back marked; a partial tool call is useless, so
    a tool step (or an empty answer) fails as a timeout and is retried.
    """
    if tools or not stream.content:
        return None, _NimFailure("timeout", True, f"stream stopped {why}")
    note = f"\n\n[Note: response cut off {why}.]"
    return stream.result(note, "stall"), None


@dataclass
class _NimStream:
    """One streamed chat completion, accumulated from its SSE chunks.

    Attributes:
        content: Text deltas in order (reasoning deltas are dropped).
        calls: Tool calls by index; id and name from the first delta that
            has them, arguments concatenated.
        finish: The last finish_reason seen.
        usage: Token usage from the final chunk.
    """

    content: list[str] = field(default_factory=list)
    calls: dict[int, dict] = field(default_factory=dict)
    finish: str | None = None
    usage: dict | None = None

    def feed(self, line: str) -> bool:
        """Take one SSE line; False once the stream sends [DONE]."""
        if not line.startswith("data:"):
            return True
        body = line[5:].strip()
        if body == "[DONE]":
            return False
        if body:
            self.add(json.loads(body))
        return True

    def add(self, chunk: dict) -> None:
        """Merge one parsed chunk."""
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                self.content.append(delta["content"])
            for call in delta.get("tool_calls") or []:
                self._merge_call(call)
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def _merge_call(self, call: dict) -> None:
        """Fold one tool-call delta into the call at its index."""
        slot = self.calls.setdefault(
            call.get("index", len(self.calls)),
            {"type": "function", "function": {"name": "", "arguments": ""}},
        )
        if call.get("id") and "id" not in slot:
            slot["id"] = call["id"]
        function = call.get("function") or {}
        if function.get("name") and not slot["function"]["name"]:
            slot["function"]["name"] = function["name"]
        slot["function"]["arguments"] += function.get("arguments") or ""

    def result(self, note: str = "", finish: str | None = None) -> dict:
        """The non-streaming response shape the callers already read."""
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.content) + note,
        }
        if self.calls:
            message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        choice = {"message": message, "finish_reason": finish or self.finish}
        return {"choices": [choice], "usage": self.usage}


def _nim_pause_before_retry(
    failure: _NimFailure, attempt: int, key: str | None
) -> None:
    """Back off before the next attempt.

    A 429 sets that key's cooldown (so concurrent background jobs back off
    too) and lets _nim_admit do the waiting, or move to another key;
    anything else just sleeps, capped at NIM_BACKOFF_MAX.
    """
    delay = _nim_backoff(attempt, failure.retry_after)
    if failure.kind == "rate_limit":
        _nim_set_cooldown(delay, key)
    else:
        time.sleep(min(delay, NIM_BACKOFF_MAX))


def _nim_request(
    method: str,
    path: str,
    payload: dict | None = None,
    keys: list[str] | None = None,
    read_timeout: float | None = None,
) -> tuple[Any, str | None]:
    """Admit, send and retry one NIM call.

    Requests rotate across the configured keys (`keys` pins the choice).
    Retryable failures (429, 5xx, network, a first timeout) back off and
    retry up to NIM_MAX_ATTEMPTS, a 429 moving on to another key when one
    is free; a rejected key (401/402) is retired if another remains.
    Everything else fails immediately.

    Returns:
        (parsed JSON, None) on success, or (None, bracketed error).
    """
    base = _nim_base_url()
    url_error = _nim_url_error(base)
    if url_error:
        return None, url_error
    pool: list[str | None] = list(keys or _nim_api_keys() or [None])
    slots = _nim_gate()
    timeouts = 0
    for attempt in range(1, NIM_MAX_ATTEMPTS + 1):
        key, wait_error = _nim_admit(pool)
        if wait_error:
            return None, wait_error
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if not slots.acquire(timeout=_nim_max_wait()):
            return None, "[NVIDIA NIM is busy — too many requests in flight.]"
        started = time.monotonic()
        try:
            _nim_count("requests", key)
            data, failure = _nim_send_once(
                method, f"{base}{path}", headers, payload, key, read_timeout
            )
        finally:
            slots.release()
        if path == "/chat/completions":
            _nim_usage_record(payload, data, failure, started)
        if failure is None:
            _nim_count("ok")
            return data, None
        _nim_count(
            "rate_limited" if failure.kind == "rate_limit" else "errors", key
        )
        if _nim_retire_key(failure, key, pool):
            continue
        timeouts += failure.kind == "timeout"
        logger.warning(
            "nim: attempt %d/%d failed kind=%s hint=%s",
            attempt,
            NIM_MAX_ATTEMPTS,
            failure.kind,
            failure.hint,
        )
        if (
            not failure.retryable
            or attempt == NIM_MAX_ATTEMPTS
            or timeouts >= NIM_MAX_TIMEOUTS
        ):
            return None, _format_nim_failure(failure)
        _nim_pause_before_retry(failure, attempt, key)
    return None, "[NVIDIA NIM request failed.]"  # pragma: no cover


def _reset_client() -> None:
    """Close and drop the shared HTTP client."""
    global _nim_http
    with _nim_lock:
        if _nim_http is not None:
            _nim_http.close()
        _nim_http = None


def _nim_retire_key(
    failure: "_NimFailure", key: str | None, keys: list[str | None]
) -> bool:
    """Skip a rejected key for the session if another one is still usable.

    Only an auth (401) or billing (402) failure retires a key, and never
    the last usable one, so a single-key setup keeps its normal error.
    """
    if key is None or failure.kind not in NIM_KEY_RETIRING_KINDS:
        return False
    others = [k for k in keys if k != key and not _nim_key_state(k).dead]
    if not others:
        return False
    with _nim_lock:
        _nim_key_states[key].dead = True
    logger.warning(
        "nim: key %d retired (%s); %d key(s) left",
        keys.index(key) + 1,
        failure.kind,
        len(others),
    )
    return True
