"""
Mike SSE (Server-Sent Events) helpers.

Pure streaming utilities with no business-logic state.
Extracted from mike_server.py — Phase 1 monolith breakup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Optional

from fastapi import Request

from mike_config import MODEL_ALIAS
from mike_payloads import error_payload as _error_payload


# ---------------------------------------------------------------------------
# SSE formatting
# ---------------------------------------------------------------------------

def _sse_event(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _sse_named_event(event: str, payload: dict) -> str:
    """Format a named SSE event while remaining compatible with data-only clients."""
    return (
        f"event: {event}\n"
        "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    )


def _sse_comment(comment: str = "keep-alive") -> str:
    return f": {comment}\n\n"


def _stream_headers() -> dict:
    return {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def _sse_content_chunk(rid: str, content: str) -> str:
    return _sse_event({
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ALIAS,
        "choices": [
            {"index": 0, "delta": {"content": content}, "finish_reason": None}
        ],
    })


def _sse_done() -> str:
    return "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Error streaming and connection helpers
# ---------------------------------------------------------------------------

def _sse_error_event(
    rid: str,
    message: str,
    code: str,
    details: Optional[dict] = None,
) -> str:
    """Return an explicit, machine-detectable terminal SSE error event."""
    return _sse_named_event("error", {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ALIAS,
        "event": "error",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
        "error": _error_payload(message, code, details),
    })


async def _stream_error(
    rid: str,
    message: str,
    code: str,
    details: Optional[dict] = None,
):
    yield _sse_event({
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ALIAS,
        "choices": [
            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
        ],
    })
    yield _sse_error_event(rid, message, code, details)
    yield _sse_done()


def _inspect_sse_frame(frame: Any) -> tuple[bool, Optional[str], bool]:
    """Return ``(is_done, finish_reason, has_error_payload)`` for an SSE frame."""
    if not isinstance(frame, str):
        return False, None, False

    finish_reason: Optional[str] = None
    has_error_payload = False
    for raw_line in frame.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return True, finish_reason, has_error_payload
        if not data or data[0] != "{":
            continue
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        has_error_payload = isinstance(payload.get("error"), dict)
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            reason = choices[0].get("finish_reason")
            if isinstance(reason, str):
                finish_reason = reason
    return False, finish_reason, has_error_payload


async def _guard_sse_stream(
    source: AsyncIterator[str],
    rid: str,
    *,
    error_message: str = "Nao consegui responder agora. Tente novamente em instantes.",
    error_code: str = "stream_generation_failed",
    logger: Optional[logging.Logger] = None,
) -> AsyncIterator[str]:
    """Guarantee a terminal SSE outcome for every server-side stream.

    Exceptions before or after the first content chunk become a named ``error``
    event containing ``finish_reason=error``, followed by ``[DONE]``. A source
    that ends silently is treated as a protocol failure too, preventing clients
    from rendering an ambiguous "(sem resposta)" EOF.
    """
    saw_done = False
    saw_terminal = False
    saw_error = False

    try:
        async for frame in source:
            is_done, finish_reason, has_error_payload = _inspect_sse_frame(frame)
            if finish_reason is not None:
                saw_terminal = True
                saw_error = saw_error or finish_reason == "error"

                # Older producers emitted finish_reason=error without a
                # structured error payload. Normalize it at the boundary so
                # the dashboard can always distinguish failure from empty text.
                if saw_error and not has_error_payload:
                    yield _sse_error_event(rid, error_message, error_code)
                    continue

            if is_done:
                saw_done = True
                # Hold the sentinel until the producer finishes. This lets us
                # reject a malformed DONE-only stream as an explicit error and
                # guarantees exactly one terminal sentinel.
                continue
            yield frame
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - protocol boundary must not leak EOF
        if logger is not None:
            logger.exception("Unhandled SSE stream failure rid=%s: %s", rid, exc)
        if not saw_done:
            yield _sse_error_event(
                rid,
                error_message,
                error_code,
                {"exception_type": type(exc).__name__},
            )
            yield _sse_done()
        return

    if not saw_terminal:
        if logger is not None:
            logger.error(
                "SSE stream ended without a terminal frame rid=%s done=%s",
                rid,
                saw_done,
            )
        yield _sse_error_event(
            rid,
            error_message,
            error_code,
            {"reason": "unexpected_eof"},
        )
    elif saw_error:
        # The normalized/error frame was already emitted; only the sentinel is
        # missing.
        pass
    yield _sse_done()


async def _request_disconnected(request: Request) -> bool:
    try:
        return await request.is_disconnected()
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Unicode / reasoning text normalization
# ---------------------------------------------------------------------------

_UNICODE_SMART_QUOTES_RE = re.compile(r"[‘’‚‛′`´]")


def _normalize_reasoning_text(text: str) -> str:
    """Strip leading non-content chars and normalize Unicode apostrophes for reasoning checks."""
    # Strip leading whitespace + BOM + zero-width chars
    stripped = re.sub(r"^[\s﻿​‌‍­]+", "", text or "")
    # Normalize curly/smart apostrophes to straight apostrophe so prefix matching works
    stripped = _UNICODE_SMART_QUOTES_RE.sub("'", stripped)
    return stripped.lower()
