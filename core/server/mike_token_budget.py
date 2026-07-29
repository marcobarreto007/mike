"""
Token budget management for Mike's chat context window.

Call configure(llm_instance, ctx_size, default_max_tokens) once at server startup.
All functions are safe to call before configure() — they fall back to char-based estimates.
"""
from __future__ import annotations

import logging
from typing import List, Optional

log = logging.getLogger(__name__)

# Injected at startup by mike_server via configure()
_llm = None
_ctx_size: int = 4096
_default_max_tokens: int = 1024

_CONTEXT_SAFETY_MARGIN = 2048
_MIN_COMPLETION_TOKENS = 256
_TOOL_RESULT_MAX_CHARS = 64000
_CONTEXT_OMITTED_MARKER = "\n\n[context omitted to fit the model window]"


def configure(llm_instance, ctx_size: int, default_max_tokens: int) -> None:
    global _llm, _ctx_size, _default_max_tokens
    _llm = llm_instance
    _ctx_size = ctx_size
    _default_max_tokens = default_max_tokens


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _llm is None:
        return max(1, len(text) // 3)
    try:
        return len(_llm.tokenize(text.encode("utf-8"), add_bos=False))
    except TypeError:
        return len(_llm.tokenize(text.encode("utf-8")))
    except Exception:
        return max(1, len(text) // 3)


def message_token_estimate(message: dict) -> int:
    if not isinstance(message, dict):
        return 0
    content = message.get("content") or ""
    total = 0
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += count_tokens(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                total += 256
    else:
        total += count_tokens(str(content))
    return total + 24


def messages_token_estimate(messages: List[dict]) -> int:
    return sum(message_token_estimate(m) for m in messages)


def truncate_text_to_token_budget(text: str, max_tokens: int) -> str:
    text = str(text or "")
    if max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text

    marker = _CONTEXT_OMITTED_MARKER
    marker_tokens = count_tokens(marker)
    target_tokens = max(1, max_tokens - marker_tokens)
    clipped = text[: max(64, target_tokens * 4)]

    if "\n" in clipped:
        line_clipped = clipped.rsplit("\n", 1)[0]
        if len(line_clipped) >= 64:
            clipped = line_clipped

    while clipped and count_tokens(clipped) + marker_tokens > max_tokens:
        clipped = clipped[: max(1, int(len(clipped) * 0.8))]

    return (clipped.rstrip() + marker) if clipped else marker.strip()


def shrink_message_to_token_budget(message: dict, max_tokens: int) -> dict:
    if not isinstance(message, dict):
        return message
    clone = dict(message)
    content = clone.get("content") or ""
    content_budget = max(0, max_tokens - 24)
    if isinstance(content, list):
        remaining = content_budget
        compact_parts = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                compact_parts.append(part)
                if isinstance(part, dict) and part.get("type") == "image_url":
                    remaining = max(0, remaining - 256)
                continue
            text = str(part.get("text") or "")
            compact_text = truncate_text_to_token_budget(text, max(0, remaining))
            compact_part = dict(part)
            compact_part["text"] = compact_text
            compact_parts.append(compact_part)
            remaining = max(0, remaining - count_tokens(compact_text))
        clone["content"] = compact_parts
    else:
        clone["content"] = truncate_text_to_token_budget(str(content), content_budget)
    return clone


def shrink_remaining_messages_to_budget(
    messages: List[dict],
    max_prompt_tokens: int,
    *,
    preserve_tail: int = 4,
) -> List[dict]:
    working = [dict(m) if isinstance(m, dict) else m for m in messages]
    if messages_token_estimate(working) <= max_prompt_tokens:
        return working

    min_message_tokens = 72
    tail_start = max(0, len(working) - preserve_tail)

    for _ in range(24):
        total = messages_token_estimate(working)
        if total <= max_prompt_tokens:
            break
        candidates = [
            idx for idx, m in enumerate(working)
            if message_token_estimate(m) > min_message_tokens
            and not (idx >= tail_start and idx == len(working) - 1)
        ]
        if not candidates:
            break
        idx = max(candidates, key=lambda i: message_token_estimate(working[i]))
        current = message_token_estimate(working[idx])
        excess = max(0, total - max_prompt_tokens)
        target = max(min_message_tokens, current - max(excess + 32, current // 3))
        shrunk = shrink_message_to_token_budget(working[idx], target)
        if message_token_estimate(shrunk) >= current:
            break
        working[idx] = shrunk

    return working


def trim_messages_to_budget(
    messages: List[dict],
    max_prompt_tokens: int,
    *,
    preserve_tail: int = 4,
) -> List[dict]:
    working = list(messages)
    if messages_token_estimate(working) <= max_prompt_tokens:
        return working

    head_system_count = 0
    for m in working:
        if m.get("role") == "system":
            head_system_count += 1
        else:
            break
    if head_system_count > 0:
        head_system_count = max(1, head_system_count)

    while (
        len(working) > max(head_system_count + 1, preserve_tail)
        and messages_token_estimate(working) > max_prompt_tokens
    ):
        removable_end = max(head_system_count, len(working) - preserve_tail)
        removed = False
        for idx in range(head_system_count, removable_end):
            if working[idx].get("role") != "system":
                working.pop(idx)
                removed = True
                break
        if removed:
            continue
        if removable_end > head_system_count:
            working.pop(head_system_count)
            continue
        break

    if messages_token_estimate(working) > max_prompt_tokens:
        working = shrink_remaining_messages_to_budget(
            working, max_prompt_tokens, preserve_tail=preserve_tail
        )
    return working


def prepare_messages_for_completion(
    messages: List[dict],
    requested_max_tokens: Optional[int],
    *,
    preserve_tail: int = 4,
) -> tuple[List[dict], int]:
    requested = max(_MIN_COMPLETION_TOKENS, int(requested_max_tokens or _default_max_tokens))
    prompt_budget = max(256, _ctx_size - requested - _CONTEXT_SAFETY_MARGIN)
    fitted = trim_messages_to_budget(messages, prompt_budget, preserve_tail=preserve_tail)

    prompt_tokens = messages_token_estimate(fitted)
    available = max(16, _ctx_size - prompt_tokens - _CONTEXT_SAFETY_MARGIN)
    effective_max_tokens = max(16, min(requested, available))

    log.info(
        "Chat budget: total=%d prompt=%d reserved=%d margin=%d fitted_msgs=%d",
        _ctx_size, prompt_tokens, effective_max_tokens, _CONTEXT_SAFETY_MARGIN, len(fitted),
    )

    if prompt_tokens + effective_max_tokens + _CONTEXT_SAFETY_MARGIN > _ctx_size:
        tighter_budget = max(128, _ctx_size - effective_max_tokens - _CONTEXT_SAFETY_MARGIN)
        fitted = trim_messages_to_budget(fitted, tighter_budget, preserve_tail=preserve_tail)
        prompt_tokens = messages_token_estimate(fitted)
        available = max(16, _ctx_size - prompt_tokens - _CONTEXT_SAFETY_MARGIN)
        effective_max_tokens = max(16, min(requested, available))

    return fitted, effective_max_tokens


# ---------------------------------------------------------------------------
# Back-compat aliases (used in mike_server.py via _ prefix convention)
# ---------------------------------------------------------------------------
_count_tokens = count_tokens
_message_token_estimate = message_token_estimate
_messages_token_estimate = messages_token_estimate
_truncate_text_to_token_budget = truncate_text_to_token_budget
_shrink_message_to_token_budget = shrink_message_to_token_budget
_shrink_remaining_messages_to_budget = shrink_remaining_messages_to_budget
_trim_messages_to_budget = trim_messages_to_budget
_prepare_messages_for_completion = prepare_messages_for_completion
