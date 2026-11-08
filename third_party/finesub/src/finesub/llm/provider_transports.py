"""Thin text-only transports for user-declared API providers (model-routing v2).

Two dialects: OpenAI-compatible (DeepSeek / OpenRouter / vLLM / Ollama and
the official API all speak it) and the Anthropic Messages API. Gemini keeps
its own hand-written path in ``llm_runtime``. Deliberately *not* litellm
(D19): these ~200 lines keep the raw response body, which is exactly what the
failure classification and usage accounting need.

Both transports normalize responses into the OpenAI-ish shape the harness
already reads everywhere (``choices[0].message.content`` +
``choices[0].finish_reason`` + nested ``usage`` details):

- usage: OpenAI nests ``*_tokens_details``; DeepSeek reports flat
  ``prompt_cache_hit_tokens``/``reasoning_tokens``; Anthropic reports
  top-level ``cache_read_input_tokens`` and folds thinking into
  ``output_tokens``. All three land in ``prompt_tokens_details.cached_tokens``
  / ``completion_tokens_details.reasoning_tokens``.
- finish reasons: Anthropic ``end_turn``→``stop``, ``max_tokens``→``length``;
  ``refusal`` and everything else pass through verbatim (an Anthropic refusal
  is HTTP 200 + stop_reason, not an error).
- errors: non-2xx raises ``RuntimeError("HTTP <code> ...")`` so the existing
  keyword-based classification applies; DeepSeek's 402 (insufficient balance)
  and OpenAI's 429 ``insufficient_quota`` both classify as ``quota`` so the
  group advances instead of retrying a dead account.

Per D18 no sampling parameters are ever sent (reasoning models reject or
ignore them; the re-roll perturbation is the prompt-tail seed text, which is
provider-agnostic). ``thinking_level`` arrives *already mapped* through the
fact's declared thinking spec (owner design 2026-08-11: catalog/user models
carry ``false`` or an explicit high,med,low -> provider-value mapping):
OpenAI-compatible sends it as ``reasoning_effort`` verbatim; Anthropic sends
it as ``output_config.effort`` (low/medium/high/xhigh). An empty value sends
no thinking parameter at all (a fact declaring ``false`` is the declared way
to keep providers that 400 on unknown fields happy — vLLM/Ollama may reject
rather than ignore them, surveyed 2026-08-11, docs/provider-adapters.md).
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from .http import StreamStalled, post_within_deadline, stream_within_deadline

ANTHROPIC_VERSION = "2023-06-01"

#: Nonoka X: hosts whose OpenAI-compatible answers are read as a stream. A
#: non-streaming answer is silent until it is complete, so a connection that
#: dies on the way looks exactly like a slow model and holds the call to its
#: hard deadline (2026-11-08: the relay finished in 236 s, the desktop waited
#: 2 h 48 min for an answer that never arrived).
STREAMED_HOSTS = frozenset({"cloud-jp.nonoka.online"})

#: Nonoka X: how long a streamed answer may go without progress.
STREAM_IDLE_SECONDS = 300.0


class ProviderHTTPError(RuntimeError):
    """Non-2xx from a user-declared provider, with the response kept.

    The message keeps the ``HTTP <code> <body>`` shape the existing
    keyword-based classification reads. Carrying the response matters for one
    thing the body cannot express: a ``Retry-After`` header. Without it the
    limiter falls back to fixed exponential backoff and can retry too early
    against a server that told us exactly how long to wait.
    """

    def __init__(self, message: str, response: httpx.Response) -> None:
        super().__init__(message)
        self.response = response
        self.status_code = response.status_code

    @property
    def retry_after_seconds(self) -> float | None:
        raw = self.response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            # Only the delta-seconds form; an HTTP-date needs clock skew
            # handling we have no reason to take on yet.
            return max(0.0, float(raw.strip()))
        except ValueError:
            return None


class ProviderStreamBroken(RuntimeError):
    """A streamed answer ended before it was complete.

    Nonoka X: the message names a 502 so the classifier retries it; a bare
    `ReadError` or `RemoteProtocolError` carries no status and would fail the
    run on the first dropped connection.
    """


def _post_json(
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Dict[str, Any]:
    """`timeout` is a deadline on the whole call (see `post_within_deadline`)."""

    response = post_within_deadline(
        url, headers=headers, json=dict(payload), deadline_seconds=timeout
    )
    return _json_answer(response)


def _json_answer(response: httpx.Response) -> Dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        snippet = response.text[:500]
        raise ProviderHTTPError(
            f"HTTP {response.status_code} {snippet}", response
        )
    try:
        data = response.json()
    except ValueError as exc:
        # Nonoka X: quote the body without its leading whitespace. The Nonoka
        # Cloud relay keeps a long answer's connection alive with one space
        # every 15 s and, when the upstream stream breaks, ends the body with a
        # marker naming a 502 that the classifier reads as retryable. Past 200
        # spaces (50 minutes) the marker fell outside the quote and the
        # failure read as a non-retryable "HTTP 200".
        raise RuntimeError(
            f"HTTP {response.status_code} returned non-JSON body: "
            f"{response.text.lstrip()[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError("Provider returned a non-object JSON response")
    return data


def _post_chat_stream(
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Dict[str, Any]:
    """One chat completion read as a stream and folded into the JSON answer
    a non-streaming call returns."""

    idle = STREAM_IDLE_SECONDS
    try:
        return stream_within_deadline(
            url,
            headers={
                **headers,
                "Accept": "text/event-stream",
                # A compressor would hold back the small writes of a stream.
                "Accept-Encoding": "identity",
            },
            json={**payload, "stream": True, "stream_options": {"include_usage": True}},
            deadline_seconds=timeout,
            idle_seconds=idle,
            consume=lambda response: _fold_chat_stream(response, idle_seconds=idle),
        )
    except httpx.ReadTimeout as exc:
        raise StreamStalled(f"stream timed out: no data for {idle:.0f}s") from exc
    except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as exc:
        raise ProviderStreamBroken(
            f"HTTP 502 connection broke before the answer was complete: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _fold_chat_stream(response: httpx.Response, *, idle_seconds: float) -> Dict[str, Any]:
    content_type = response.headers.get("Content-Type", "").lower()
    if not 200 <= response.status_code < 300 or "text/event-stream" not in content_type:
        response.read()
        return _json_answer(response)

    head: Dict[str, Any] = {}
    parts: List[str] = []
    usage: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    answered = False
    done = False
    last_progress = time.monotonic()

    def take(raw: bytes) -> None:
        nonlocal usage, finish_reason, answered, done, last_progress
        if raw.strip() == b"[DONE]":
            done = True
            return
        try:
            chunk = json.loads(raw)
        except ValueError as exc:
            raise ProviderStreamBroken(
                f"HTTP 502 malformed stream event: {raw[:200]!r}"
            ) from exc
        if not isinstance(chunk, dict):
            return
        error = chunk.get("error")
        if error:
            detail = error.get("message", error) if isinstance(error, dict) else error
            raise ProviderStreamBroken(f"HTTP 502 upstream stream failed: {str(detail)[:300]}")
        for field in ("id", "created", "model"):
            if head.get(field) is None and chunk.get(field) is not None:
                head[field] = chunk[field]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
            last_progress = time.monotonic()
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            answered = True
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if isinstance(delta.get("content"), str):
                parts.append(delta["content"])
            # Keep-alives can arrive as empty deltas: only real output counts.
            if any(delta.get(name) for name in ("content", "reasoning_content", "reasoning", "refusal", "tool_calls")):
                last_progress = time.monotonic()
            if isinstance(choice.get("finish_reason"), str):
                finish_reason = choice["finish_reason"]
                last_progress = time.monotonic()

    # Split on bytes: `iter_lines` uses `str.splitlines`, which also breaks a
    # JSON string at U+2028 and friends.
    buffer = bytearray()
    data: List[bytes] = []

    def line(text: bytes) -> None:
        nonlocal data
        if not text:
            if data:
                take(b"\n".join(data))
            data = []
        elif text.startswith(b"data:"):
            data.append(text[5:].removeprefix(b" "))

    try:
        for piece in response.iter_bytes():
            buffer += piece
            while not done and (cut := buffer.find(b"\n")) >= 0:
                line(bytes(buffer[:cut]).removesuffix(b"\r"))
                del buffer[: cut + 1]
            if done:
                break
            if time.monotonic() - last_progress > idle_seconds:
                if finish_reason is not None:
                    break
                raise StreamStalled(f"stream timed out: no progress for {idle_seconds:.0f}s")
    except httpx.TransportError:
        # Only the usage or the end marker can still be missing.
        if finish_reason is None:
            raise
    if not done:
        if buffer:
            line(bytes(buffer).removesuffix(b"\r"))
        line(b"")
    if not answered or (not done and finish_reason is None):
        raise ProviderStreamBroken("HTTP 502 stream ended before the answer was complete")

    answer: Dict[str, Any] = {
        "id": head.get("id", ""),
        "object": "chat.completion",
        "created": head.get("created", int(time.time())),
        "model": head.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(parts)},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        answer["usage"] = usage
    return answer


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    return str(content or "")


def _plain_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flatten harness messages to plain text (these backends are text-only)."""

    return [
        {"role": str(msg.get("role", "user")), "content": _as_text(msg.get("content"))}
        for msg in messages
    ]


def openai_compat_generate(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    max_tokens: Optional[int],
    thinking_level: Optional[str],
    timeout: float,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _plain_messages(messages),
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    level = (thinking_level or "").strip()
    if level:
        # Pre-mapped provider value (the fact's thinking spec).
        payload["reasoning_effort"] = level
    post = _post_chat_stream if urlsplit(base_url).hostname in STREAMED_HOSTS else _post_json
    data = post(
        base_url.rstrip("/") + "/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout=timeout,
    )
    return _normalize_openai_response(data)


def _normalize_openai_response(data: Dict[str, Any]) -> Dict[str, Any]:
    usage = data.get("usage")
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
        if not isinstance(details, dict):
            details = {}
            usage["prompt_tokens_details"] = details
        # DeepSeek reports flat cache-hit/miss fields instead of the nested
        # details object (surveyed 2026-08-11).
        if "cached_tokens" not in details and isinstance(
            usage.get("prompt_cache_hit_tokens"), (int, float)
        ):
            details["cached_tokens"] = int(usage["prompt_cache_hit_tokens"])
        completion_details = usage.get("completion_tokens_details")
        if not isinstance(completion_details, dict):
            completion_details = {}
            usage["completion_tokens_details"] = completion_details
        if "reasoning_tokens" not in completion_details and isinstance(
            usage.get("reasoning_tokens"), (int, float)
        ):
            completion_details["reasoning_tokens"] = int(usage["reasoning_tokens"])
    choices = data.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                # DeepSeek (`reasoning_content`) / vLLM (`reasoning`) return
                # the raw chain of thought next to the answer; the harness
                # must never treat it as visible output.
                message.pop("reasoning_content", None)
                message.pop("reasoning", None)
    return data


def anthropic_generate(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    max_tokens: Optional[int],
    thinking_level: Optional[str],
    timeout: float,
) -> Dict[str, Any]:
    plain = _plain_messages(messages)
    system_text = "\n\n".join(
        msg["content"] for msg in plain if msg["role"] == "system" and msg["content"]
    )
    turns = [msg for msg in plain if msg["role"] != "system"]
    if not turns or turns[0]["role"] != "user":
        # Messages API requires the first turn to be user; consecutive
        # same-role turns are merged server-side, so this is the only fix-up.
        turns = [{"role": "user", "content": ""}] + turns
    effective_max = int(max_tokens or 8192)
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": effective_max,  # required by the Messages API
        "messages": turns,
    }
    if system_text:
        payload["system"] = system_text
    level = (thinking_level or "").strip()
    if level:
        # Pre-mapped effort word (low/medium/high/xhigh); the Anthropic
        # Messages API takes it as output_config.effort (owner decision
        # 2026-08-11 -- not thinking.budget_tokens).
        payload["output_config"] = {"effort": level}
    data = _post_json(
        base_url.rstrip("/") + "/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout=timeout,
    )
    return _normalize_anthropic_response(data)


_ANTHROPIC_FINISH = {"end_turn": "stop", "max_tokens": "length"}


def _normalize_anthropic_response(data: Dict[str, Any]) -> Dict[str, Any]:
    blocks = data.get("content")
    text = ""
    if isinstance(blocks, list):
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
    stop_reason = str(data.get("stop_reason") or "")
    usage_in = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    input_tokens = int(usage_in.get("input_tokens") or 0)
    cache_read = int(usage_in.get("cache_read_input_tokens") or 0)
    cache_write = int(usage_in.get("cache_creation_input_tokens") or 0)
    output_tokens = int(usage_in.get("output_tokens") or 0)
    prompt_tokens = input_tokens + cache_read + cache_write
    return {
        "choices": [
            {
                "message": {"content": text},
                # ``refusal`` and other reasons pass through verbatim: an
                # Anthropic refusal is a 200 + stop_reason, and the harness's
                # blocked-prompt check knows the name.
                "finish_reason": _ANTHROPIC_FINISH.get(stop_reason, stop_reason),
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "prompt_tokens_details": {"cached_tokens": cache_read},
            "completion_tokens": output_tokens,
            # Anthropic folds thinking into output_tokens without a separate
            # count; reasoning_tokens stays absent rather than guessed.
            "completion_tokens_details": {},
            "total_tokens": prompt_tokens + output_tokens,
        },
        "provider_raw": data,
    }
