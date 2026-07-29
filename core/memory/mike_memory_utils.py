# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

# Extracted from mike_memory.py — Phase 3 refactor

"""
Utility functions and constants for the MIKE memory subsystem.

This module provides text extraction helpers, query sanitizers, context-detection
heuristics, and text chunking utilities used by the memory store and memory service.
"""

import json
import logging
import re
from pathlib import Path
from typing import Iterable, List

log = logging.getLogger(__name__)

from mike_extractors import extract_docx, extract_html, extract_pdf
from mike_mem0 import _normalize_snippet


TEXT_EXTENSIONS = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".pdf", ".docx", ".html", ".htm"}


def _read_text_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix in {".html", ".htm"}:
        return extract_html(path)
    if suffix in {".json", ".jsonl"}:
        try:
            if suffix == ".json":
                return json.dumps(json.loads(path.read_text(encoding="utf-8-sig")), ensure_ascii=False, indent=2)
        except Exception:
            log.warning("Failed to parse JSON document, falling back to raw text: %s", path)
    return path.read_text(encoding="utf-8-sig", errors="ignore")


def _sanitize_query(query: str) -> str:
    terms = re.findall(r"[0-9A-Za-z_]+", query.lower())
    return " ".join(terms)


_DIRECT_CHAT_PATTERNS = frozenset((
    "oi",
    "ola",
    "olá",
    "tudo bem",
    "tudo bom",
    "bom dia",
    "boa tarde",
    "boa noite",
    "obrigado",
    "obrigada",
    "valeu",
    "ok",
))

_DIRECT_GENERATION_VERBS = (
    "escreva",
    "redija",
    "crie",
    "gere",
    "monte",
    "melhore",
    "corrija",
    "reescreva",
    "traduza",
    "traduz",
    "resuma",
    "resume",
    "ajude a escrever",
    "me ajude a escrever",
)

_DIRECT_GENERATION_TARGETS = (
    "email",
    "e-mail",
    "mensagem",
    "texto",
    "resposta",
    "legenda",
    "post",
    "bio",
    "assunto",
)

_MEMORY_CONTEXT_KEYWORDS = (
    "lembra",
    "lembre",
    "historico",
    "histórico",
    "antes",
    "conversa",
    "conversa passada",
    "família",
    "familia",
    "agenda",
    "calendário",
    "calendario",
    "lembrete",
    "marco",
    "ana paula",
    "rapha",
    "raphael",
    "alice",
    "matheus",
)

_KNOWLEDGE_CONTEXT_KEYWORDS = (
    "doc",
    "docs",
    "documentacao",
    "documentação",
    "manual",
    "guia",
    "api",
    "sdk",
    "endpoint",
    "scope",
    "arquivo",
    "pasta",
    "documento",
    "pdf",
    "docx",
    "html",
    "contrato",
    "proposta",
    "knowledge",
    "base de conhecimento",
)

_SIMPLE_MATH_QUERY_RE = re.compile(
    r"^\s*(?:quanto\s+(?:é|e)\s+)?-?\d+(?:[.,]\d+)?(?:\s*[-+*/x]\s*-?\d+(?:[.,]\d+)?)+\s*\??\s*$",
    re.IGNORECASE,
)


def _contains_any_phrase(normalized: str, phrases: Iterable[str]) -> bool:
    return any(phrase in normalized for phrase in phrases)


def _looks_like_simple_math_query(query: str) -> bool:
    normalized = re.sub(r"\s+", " ", query or "").strip().lower().replace("×", "x")
    if not normalized:
        return False
    return bool(_SIMPLE_MATH_QUERY_RE.match(normalized))


def _prefers_direct_answer(query: str) -> bool:
    normalized = _normalize_snippet(query)
    if not normalized:
        return True
    if normalized in _DIRECT_CHAT_PATTERNS or _looks_like_simple_math_query(normalized):
        return True
    if _contains_any_phrase(normalized, _DIRECT_GENERATION_VERBS):
        if _contains_any_phrase(normalized, _DIRECT_GENERATION_TARGETS):
            return True
        if normalized.startswith(("traduza", "traduz", "resuma", "resume", "corrija", "reescreva", "melhore")):
            return True
    return False


def _chunk_section(text: str, max_chars: int) -> List[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return []

    chunks: List[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            end = start + max_chars
            chunks.append(paragraph[start:end].strip())
            start = end
        current = ""
    if current:
        chunks.append(current)
    return chunks


def chunk_text(title: str, text: str, max_chars: int = 1200) -> List[tuple[str, str]]:
    lines = text.splitlines()
    sections: List[tuple[str, List[str]]] = []
    current_title = title
    current_lines: List[str] = []

    for line in lines:
        if line.lstrip().startswith("#"):
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = line.lstrip("#").strip() or title
            current_lines = []
            continue
        current_lines.append(line)

    if current_lines or not sections:
        sections.append((current_title, current_lines))

    chunks: List[tuple[str, str]] = []
    for section_title, section_lines in sections:
        section_text = "\n".join(section_lines).strip()
        if not section_text:
            continue
        for part in _chunk_section(section_text, max_chars=max_chars):
            chunks.append((section_title, part))
    return chunks
