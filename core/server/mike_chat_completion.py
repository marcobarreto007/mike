"""
Mike chat completion — blocking and async wrappers around LLM inference.

Handles backend routing (local GGUF, DeepSeek API, mock), vision handler
activation, fallback chain execution, token counting, and streaming.

Singletons (llm, vision_handler, model_router, etc.) are read from
``shared_state``, populated by mike_server at startup.

Extracted from mike_server.py — Phase 2 monolith breakup.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

import shared_state as _state

from mike_config import (
    CTX_SIZE,
    LLM_BACKEND,
    MODEL_ALIAS,
    VISION_ENABLED,
)
from mike_fallback_chain import AllBackendsFailedError
from mike_models import VisionInputError
from mike_stats import _vision_limits
from mike_token_budget import (
    _TOOL_RESULT_MAX_CHARS,  # noqa: F401 — re-exported for consumer access
    count_tokens as _count_tokens,
    prepare_messages_for_completion as _prepare_messages_for_completion,
)
from mike_vision import _has_images, _vision_stop_sequences

log = logging.getLogger("mike")


# ---------------------------------------------------------------------------
# Complexity estimation
# ---------------------------------------------------------------------------

def _estimate_complexity(messages: list) -> str:
    """Estimate request complexity for model routing.

    Delegates to MikeModelRouter.estimate_complexity when the router is
    available; otherwise returns "normal".
    """
    if _state.model_router is not None:
        return _state.model_router.estimate_complexity(messages)
    return "normal"


# ---------------------------------------------------------------------------
# Blocking chat completion (non-streaming)
# ---------------------------------------------------------------------------

def _blocking_chat_completion(messages, req, tools=None):
    prepared_messages, effective_max_tokens = _prepare_messages_for_completion(
        list(messages),
        req.max_tokens,
        preserve_tail=4,
    )
    if effective_max_tokens < 64:
        log.warning(
            "Context near overflow (non-stream): effective_max_tokens=%d prompt_est=%d ctx=%d",
            effective_max_tokens,
            sum(len(str(m.get('content', ''))) // 4 for m in prepared_messages),
            CTX_SIZE,
        )

    # ── Model Router: per-request backend selection ──
    backend_name = LLM_BACKEND   # fallback
    backend_client = None

    if _state.model_router is not None:
        model_override = None
        if hasattr(req, 'model') and req.model and req.model != MODEL_ALIAS:
            model_override = req.model
        backend_name, backend_client = _state.model_router.resolve_backend(
            messages, model_override=model_override
        )

    # ── Common kwargs for all backends ──
    common_kwargs = dict(
        max_tokens=effective_max_tokens,
        temperature=req.temperature or 0.7,
        top_p=req.top_p or 0.95,
        stop=req.stop,
        top_k=64,
    )
    if tools:
        common_kwargs["tools"] = tools

    # ── Fallback chain: preferred backend first, automatic fallback on failure ──
    if _state.fallback_chain is not None:
        try:
            return _state.fallback_chain.execute(
                prepared_messages,
                preferred_backend=backend_name,
                **common_kwargs,
            )
        except AllBackendsFailedError as exc:
            log.error("All backends failed in fallback chain: %s", exc)
            raise RuntimeError("All inference backends are currently unavailable.") from exc

    # ── Legacy execution (fallback_chain not configured) ──
    # ── Mock backend ──
    if backend_name == "mock" and backend_client is not None:
        kwargs = dict(common_kwargs, messages=prepared_messages, model="mock-model")
        return backend_client.chat_completion(**kwargs)

    # (DeepSeek HTTP backend removido — o Mike e local-only. Qualquer
    #  backend_name == "deepseek" cai no path local GGUF abaixo.)

    # ── Local GGUF backend ──
    with _state.llm_lock:
        if _has_images(prepared_messages):
            if _state.vision_handler is None:
                raise VisionInputError(
                    "O modo foto ainda esta carregando. Tente novamente em alguns segundos.",
                    code="vision_disabled" if not VISION_ENABLED else "vision_unavailable",
                    status_code=503,
                    details=_vision_limits(),
                )
            # Visão: ativa o handler só durante esta chamada e desativa após
            _state.llm.chat_handler = _state.vision_handler
            try:
                return _state.llm.create_chat_completion(
                    messages=prepared_messages,
                    max_tokens=common_kwargs["max_tokens"],
                    temperature=common_kwargs["temperature"],
                    top_p=common_kwargs["top_p"],
                    top_k=common_kwargs["top_k"],
                    stop=_vision_stop_sequences(req),
                )
            finally:
                _state.llm.chat_handler = None
        else:
            # Texto puro: llm foi carregado sem handler — usa o template do GGUF normalmente
            return _state.llm.create_chat_completion(
                messages=prepared_messages,
                max_tokens=common_kwargs["max_tokens"],
                temperature=common_kwargs["temperature"],
                top_p=common_kwargs["top_p"],
                top_k=common_kwargs["top_k"],
                stop=common_kwargs["stop"],
            )


# ---------------------------------------------------------------------------
# Streaming chat completion
# ---------------------------------------------------------------------------

def _blocking_chat_completion_stream(messages, req):
    prepared_messages, effective_max_tokens = _prepare_messages_for_completion(
        list(messages),
        req.max_tokens,
        preserve_tail=4,
    )
    if effective_max_tokens < 64:
        log.warning(
            "Context near overflow (stream): effective_max_tokens=%d prompt_est=%d ctx=%d",
            effective_max_tokens,
            sum(len(str(m.get('content', ''))) // 4 for m in prepared_messages),
            CTX_SIZE,
        )

    # ── Model Router: per-request backend selection ──
    backend_name = LLM_BACKEND   # fallback
    backend_client = None

    if _state.model_router is not None:
        model_override = None
        if hasattr(req, 'model') and req.model and req.model != MODEL_ALIAS:
            model_override = req.model
        backend_name, backend_client = _state.model_router.resolve_backend(
            messages, model_override=model_override
        )

    # ── Common kwargs for all backends ──
    common_kwargs = dict(
        max_tokens=effective_max_tokens,
        temperature=req.temperature or 0.7,
        top_p=req.top_p or 0.95,
        stop=req.stop,
        top_k=64,
    )

    # ── Fallback chain: preferred backend first, automatic fallback on failure ──
    if _state.fallback_chain is not None:
        try:
            for chunk in _state.fallback_chain.execute_stream(
                prepared_messages,
                preferred_backend=backend_name,
                **common_kwargs,
            ):
                yield chunk
        except AllBackendsFailedError as exc:
            log.error("All backends failed in fallback chain (stream): %s", exc)
            raise RuntimeError("All inference backends are currently unavailable.") from exc
        return

    # ── Legacy execution (fallback_chain not configured) ──
    # ── Mock backend ──
    if backend_name == "mock" and backend_client is not None:
        for chunk in backend_client.chat_completion_stream(
            messages=prepared_messages,
            model="mock-model",
            max_tokens=common_kwargs["max_tokens"],
            temperature=common_kwargs["temperature"],
            top_p=common_kwargs["top_p"],
            stop=common_kwargs["stop"],
        ):
            yield chunk
        return

    # (DeepSeek HTTP backend removido — o Mike e local-only. Cai no path local GGUF.)

    # ── Local GGUF backend ──
    with _state.llm_lock:
        if _has_images(prepared_messages):
            raise VisionInputError(
                "Streaming de fotos nao esta disponivel neste runtime.",
                code="vision_streaming_unsupported",
                status_code=400,
                details=_vision_limits(),
            )
        response = _state.llm.create_chat_completion(
            messages=prepared_messages,
            max_tokens=common_kwargs["max_tokens"],
            temperature=common_kwargs["temperature"],
            top_p=common_kwargs["top_p"],
            top_k=common_kwargs["top_k"],
            stop=common_kwargs["stop"],
            stream=True,
        )
        if isinstance(response, dict):
            yield response
            return
        for chunk in response:
            yield chunk


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _response_text(response: dict) -> str:
    """Extract assistant text from an OpenAI-compatible response."""
    choices = response.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        return msg.get("content", "") or ""
    return ""


def _clean_completion_text(text: str) -> str:
    """Strip whitespace and thinking tags from completion text."""
    import re
    text = text.strip()
    # Remove <｜end▁of▁thinking｜>Tags from Qwen thinking models
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return text.strip()


# Token counting
# ---------------------------------------------------------------------------

def _response_completion_tokens(response: dict, text: str) -> int:
    tokens = response.get("usage", {}).get("completion_tokens")
    if isinstance(tokens, int):
        return tokens
    return _count_tokens(text)


# ---------------------------------------------------------------------------
# Async wrapper
# ---------------------------------------------------------------------------

async def _generate_model_response(
    messages,
    req,
    *,
    cancel_event: Optional[asyncio.Event] = None,
    tools: Optional[List[dict]] = None,
) -> dict:
    # Visão e texto usam in-process GPU via _blocking_chat_completion (llm_lock).
    # O subprocess CPU foi descartado: 90s+ de load vs ~20s in-process.
    return await asyncio.to_thread(_blocking_chat_completion, messages, req, tools)
