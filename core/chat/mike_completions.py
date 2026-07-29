"""
LLM output post-processing utilities.

All functions are pure — no global state, no server imports.
Handles think-tag stripping, reasoning-leak detection, and
response text extraction for both blocking and streaming completions.
"""
from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Output-cleaning regexes
# ---------------------------------------------------------------------------

_THINK_PATTERNS = [
    re.compile(p, re.DOTALL | re.IGNORECASE)
    for p in (
        # XML-style think / channel tags
        r"<think>.*?</think>",
        r"<\|\|channel>.*?<channel\|\|>",
        # Reasoning-step headers with optional leading numbering
        r"(?:\*{0,2}\d+\.?\s*)?Analyze User Input:.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"(?:\*{0,2}\d+\.?\s*)?Identify Key Requirements:.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"(?:\*{0,2}\d+\.?\s*)?Determine (?:Tool Usage|Response Strategy|Capabilities):.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"(?:\*{0,2}\d+\.?\s*)?Formulate Response.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"(?:\*{0,2}\d+\.?\s*)?Check Constraints:.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"(?:\*{0,2}\d+\.?\s*)?Final (?:Polish|Check|Output|Answer).*?(\n\n|\n(?=[A-Z\d\*]))",
        r"Here[''']?s a thinking process:.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"(?:\*{0,2}\d+\.?\s*)?Planning Block:.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"(?:\*{0,2}\d+\.?\s*)?Draft (?:Response|Construction|Mental).*?(\n\n|\n(?=[A-Z\d\*]))",
        r"(?:\*{0,2}\d+\.?\s*)?Mental (?:Draft|Refinement).*?(\n\n|\n(?=[A-Z\d\*]))",
        # Domain-specific leaking markers
        r"Doomsday.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"Zeller.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"Calculate Day of the Week.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"Let[''']?s calculate.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"Let[''']?s verify.*?(\n\n|\n(?=[A-Z\d\*]))",
        r"Self-?Correction.*?(\n\n|\n(?=[A-Z\d\*]))",
    )
]

_STOP_TOKEN_RE = re.compile(r"<(?:end_of|start_of)\w*>?.*$", re.DOTALL)
_STRAY_TAG_RE = re.compile(r"</?(?:bos|eos|pad|s|\|[^>]+\|)>", re.IGNORECASE)
_WRAPPING_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|text)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL
)

# ---------------------------------------------------------------------------
# Reasoning-leak detection constants
# ---------------------------------------------------------------------------

_REASONING_LEAK_PREFIXES = (
    "here's a thinking process:",
    "here’s a thinking process:",
    "heres a thinking process:",
    "analyze user input:",
    "1. analyze user input:",
    "**1. analyze user input:",
    "**2. check constraints:",
    "**2. identify",
    "**2. check",
    "2. check constraints:",
    "check constraints:",
    "1. identify",
    "identify key requirements:",
    "identify constraints",
    "determine response strategy:",
    "determine tool usage:",
    "determine capabilities",
    "formulate response",
    "draft response",
    "mental draft",
    "draft construction",
    "draft (mental",
    "doomsday",
    "zeller",
    "calculate day of the week",
    "let's calculate",
    "let’s calculate",
    "let's verify",
    "let’s verify",
    "let me calculate",
    "let me verify",
    "the user wants to",
    "the user is asking",
    "the prompt contains",
    "the provided data",
    "based on the context",
    "based on the provided",
    "i should answer",
    "i will answer",
    "i will formulate",
    "i will respond",
    "i need to answer",
    "i need to respond",
    "response structure:",
    "response draft:",
    "draft:",
    "final plan:",
    "final check:",
    "check constraints",
    "one constraint:",
    "one more thing:",
    "wait, the prompt",
    "wait, looking",
    "wait, i",
    "<think>",
)

_REASONING_FINAL_MARKERS = (
    "final output generation:",
    "output generation.",
    "final output:",
    "final answer:",
    "final polish",
    "draft response",
)

_REASONING_STOP_LINE_RE = re.compile(
    r"^\s*(?:"
    r"analy[sz]e user input|identify key requirements|determine response strategy|"
    r"determine tool usage|formulate response|check constraints|final check|"
    r"final polish|final output|output generation|self-correction|mental draft|"
    r"draft construction|draft response|ready|done|all good|proceed|checks?|"
    r"language|tone|date/time|output matches|note:"
    r")\b",
    re.IGNORECASE,
)

_REASONING_ANYWHERE_RE = re.compile(
    r"(here[''']?s a thinking process|analy[sz]e user input|identify key requirements|"
    r"identify constraints|determine tool usage|determine response strategy|determine capabilities|"
    r"formulate response|check constraints|self-?correction|mental draft|draft construction|draft response|"
    r"doomsday|zeller[''']?s|calculate day of the week|"
    r"let[''']?s calculate|let[''']?s verify|let me calculate|let me verify|"
    r"the user wants to|the user is asking|the prompt contains|the provided data|"
    r"based on the context|based on the provided|i should answer|i will formulate|"
    r"response structure:|response draft:|final plan:|final check:|"
    r"wait, the prompt|wait, looking at|wait, i need|"
    r"<think>|</think>)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Search / price / internet-denial patterns
# ---------------------------------------------------------------------------

_PRICE_PATTERN_RE = re.compile(
    r"(?:\$\s?\d[\d.,]+|R\$\s?\d[\d.,]+|CAD\s?\d[\d.,]+|USD\s?\d[\d.,]+|EUR\s?\d[\d.,]+|\d+[.,]\d{2}\s?(?:CAD|USD|BRL|EUR))",
    re.IGNORECASE,
)

_SEARCH_REQUEST_RE = re.compile(
    r"pesquis|busca|busque|preco|pre[cç]o|quanto custa|amazon|google|comprar|shop|price|search|scrape|site|loja|mercado|walmart|bestbuy|newegg",
    re.IGNORECASE,
)

_NO_INTERNET_DENIAL_RE = re.compile(
    r"(n[aã]o\s+tenho\s+acesso\s+a\s+not[ií]cias\s+em\s+tempo\s+real"
    r"|n[aã]o\s+consigo\s+acessar\s+a\s+internet"
    r"|n[aã]o\s+tenho\s+acesso\s+(?:direto\s+)?(?:a|à)\s+internet"
    r"|meu\s+conhecimento\s+(?:vai|se\s+limita|termina)\s+at[eé]"
    r"|n[aã]o\s+posso\s+navegar\s+na\s+web"
    r"|sem\s+acesso\s+(?:a|à)\s+(?:internet|web|not[ií]cias)"
    r"|n[aã]o\s+tenho\s+capacidade\s+de\s+buscar)",
    re.IGNORECASE,
)

_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "â€™", "â€œ", "â€\"", "â€”", "ðŸ")


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def contains_internet_denial(text: str) -> bool:
    return bool(_NO_INTERNET_DENIAL_RE.search(text or ""))


def looks_like_search_request(msg: str) -> bool:
    return bool(_SEARCH_REQUEST_RE.search(msg or ""))


def looks_like_reasoning_leak(text: str) -> bool:
    probe = (text or "").lstrip()[:2000].lower()
    if not probe:
        return False
    return any(probe.startswith(prefix) for prefix in _REASONING_LEAK_PREFIXES) or bool(
        _REASONING_ANYWHERE_RE.search(probe[:1200])
    )


def prune_reasoning_candidate(candidate: str) -> str:
    text = (candidate or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[\s:.\-–—]+", "", text).strip()
    text = text.strip("\"’“”'")
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines:
                break
            continue
        if _REASONING_STOP_LINE_RE.match(line):
            if lines:
                break
            continue
        if line in {"✅", "✓"} or line.endswith("✅"):
            continue
        lines.append(line.strip("\"’“”'"))
    return "\n".join(lines).strip()


def extract_final_answer_from_reasoning(raw: str) -> str:
    text = raw or ""
    if not looks_like_reasoning_leak(text):
        return text

    candidates: list[str] = []
    quoted = re.findall(r"[\"“]([^\"”\n]{8,1200})[\"”]", text)
    for item in quoted:
        if not _REASONING_ANYWHERE_RE.search(item):
            candidates.append(item)

    lowered = text.lower()
    for marker in _REASONING_FINAL_MARKERS:
        start = 0
        while True:
            idx = lowered.find(marker, start)
            if idx == -1:
                break
            candidates.append(text[idx + len(marker):])
            start = idx + len(marker)

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if paragraphs:
        candidates.append(paragraphs[-1])

    for candidate in reversed(candidates):
        pruned = prune_reasoning_candidate(candidate)
        if pruned and not _REASONING_ANYWHERE_RE.search(pruned[:600]):
            return pruned

    return ""


def _fix_mojibake(text: str) -> str:
    if not text:
        return text
    if not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    # Heurística: muitas respostas chegam como UTF-8 lido em cp1252/latin-1.
    try:
        repaired = text.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
    except Exception:
        return text
    if sum(repaired.count(m) for m in _MOJIBAKE_MARKERS) < sum(text.count(m) for m in _MOJIBAKE_MARKERS):
        return repaired
    return text


def clean_completion_text(raw: str) -> str:
    cleaned = extract_final_answer_from_reasoning(raw or "")
    cleaned = _fix_mojibake(cleaned)
    for pattern in _THINK_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = extract_final_answer_from_reasoning(cleaned)
    cleaned = _fix_mojibake(cleaned)
    cleaned = _STOP_TOKEN_RE.sub("", cleaned)
    cleaned = _STRAY_TAG_RE.sub("", cleaned)
    fence_match = _WRAPPING_CODE_FENCE_RE.match(cleaned.strip())
    if fence_match:
        inner = fence_match.group(1).strip()
        if not inner.lstrip().startswith('{"name"'):
            cleaned = inner
    return cleaned.strip()


def response_text(response: dict) -> str:
    raw = response.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    return clean_completion_text(raw)


def response_stream_delta(chunk: dict) -> str:
    choice = (chunk.get("choices") or [{}])[0]
    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    if isinstance(choice.get("text"), str):
        return choice["text"]
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


# ---------------------------------------------------------------------------
# Back-compat aliases
# ---------------------------------------------------------------------------
_contains_internet_denial = contains_internet_denial
_looks_like_search_request = looks_like_search_request
_looks_like_reasoning_leak = looks_like_reasoning_leak
_prune_reasoning_candidate = prune_reasoning_candidate
_extract_final_answer_from_reasoning = extract_final_answer_from_reasoning
_clean_completion_text = clean_completion_text
_response_text = response_text
_response_stream_delta = response_stream_delta
