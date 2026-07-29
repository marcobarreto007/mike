# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mock LLM Backend for Mike -- simulates model inference without real API calls.
Used for testing the pipeline flow, tool-call parsing, and model routing.
"""

import json
import os
import random
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

MOCK_DELAY_SEC = float(os.getenv("MIKE_MOCK_LLM_DELAY_SEC", "0.05"))

# ---------------------------------------------------------------------------
# Response variety pools (Portuguese, to match Mike's persona)
# ---------------------------------------------------------------------------

_GREETINGS = [
    "Ola! Como posso ajudar voce hoje?",
    "Oi! Em que posso ser util?",
    "Ola! Estou aqui para ajudar. O que voce precisa?",
    "Oi! Conte comigo. Qual e a sua duvida?",
    "Ola! Mike pronto. Me diga o que voce precisa.",
]

_HELPERS = [
    "Claro, vou ajudar com isso! Me de um momento.",
    "Entendi. Deixe-me verificar isso para voce.",
    "Certo, vou analisar isso agora.",
    "Com certeza! Vamos ver o que encontramos.",
    "Estou processando sua solicitacao. Um instante.",
    "Vou cuidar disso para voce.",
    "Entendido. Vou trabalhar nisso agora mesmo.",
]

_SUMMARIES = [
    "Resumindo os resultados: a operacao foi concluida com sucesso. Os dados retornados estao corretos e consistentes.",
    "Baseado nos resultados da ferramenta, tudo foi executado conforme esperado. Nenhum erro foi detectado.",
    "Analisando o retorno da ferramenta: os parametros estao corretos e o resultado e positivo. Posso ajudar com mais alguma coisa?",
    "Os resultados indicam que a execucao foi bem-sucedida. Todos os passos foram completados sem problemas.",
    "De acordo com os dados recebidos, a tarefa foi realizada com exito. Esta tudo em ordem.",
]

_FALLBACKS = [
    "Sou um assistente simulado para testes. Estou funcionando corretamente!",
    "Esta e uma resposta mock do pipeline de teste do Mike. Tudo operando como esperado.",
    "Resposta automatica de teste. O fluxo do pipeline esta funcionando perfeitamente.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _approx_tokens(text: str) -> int:
    """Approximate token count: ~4 chars per token for Portuguese/English."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _extract_text(messages: List[Dict]) -> str:
    """Extract all text content from a list of chat messages."""
    parts = []
    for m in messages or []:
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
    return " ".join(parts)


def _has_tool_results(messages: List[Dict]) -> bool:
    """Check if any message contains tool results."""
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "tool":
            return True
        content = m.get("content", "") if isinstance(m, dict) else ""
        if isinstance(content, str) and "<tool_result>" in content:
            return True
    return False


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

_SEARCH_KEYWORDS = [
    "pesquisar", "buscar", "procurar", "search", "google",
    "navegar", "internet", "web", "pesquisa", "busca",
    "procure", "pesquise", "busque", "navegue", "find",
    "look up", "lookup", "search for", "browse",
    "pesquise por", "busque por", "procure por",
    "faca uma pesquisa", "faca uma busca",
]

_READ_KEYWORDS = [
    "ler", "leia", "read", "abrir arquivo", "open file",
    "mostrar arquivo", "show file", "conteudo do arquivo",
    "leia o arquivo", "le o arquivo",
]

_WRITE_KEYWORDS = [
    "escrever", "criar arquivo", "salvar", "write", "create file",
    "save", "escreva", "crie", "criar", "gravar",
    "salve", "grave",
]


def _is_search_request(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _SEARCH_KEYWORDS)


def _is_file_read_request(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _READ_KEYWORDS) and not any(
        kw in lower for kw in _WRITE_KEYWORDS
    )


def _is_file_write_request(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _WRITE_KEYWORDS)


def _extract_search_query(text: str) -> str:
    """Extract the likely search query from user text."""
    lower = text.lower()
    # Strip known prefixes
    prefixes = [
        "pesquise", "pesquisar", "buscar", "busque", "procure",
        "procurar", "search", "search for", "pesquise por",
        "busque por", "procure por", "faca uma pesquisa sobre",
        "faca uma busca por", "browse",
    ]
    for prefix in prefixes:
        idx = lower.find(prefix)
        if idx != -1:
            start = idx + len(prefix)
            query = text[start:].strip().strip('"').strip("'").strip()
            return query[:200] if query else text[:200]

    # Fallback: last sentence / question
    sentences = text.replace("?", ".").replace("!", ".").split(".")
    for s in reversed(sentences):
        s = s.strip()
        if len(s) > 3:
            return s[:200]
    return text[:200]


# ---------------------------------------------------------------------------
# MikeMockLLM
# ---------------------------------------------------------------------------

class MikeMockLLM:
    """Mock LLM backend that simulates model inference for testing.

    Implements the same interface as MikeDeepSeekClient.
    Generates varied contextual responses based on message content.
    """

    def __init__(self, name: str = "mock"):
        self.name = name
        self.delay = MOCK_DELAY_SEC

    @property
    def ready(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # chat_completion (blocking, non-streaming)
    # ------------------------------------------------------------------

    def chat_completion(
        self,
        messages: List[dict],
        model: str = "mock-model",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.95,
        stop: Optional[List[str]] = None,
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
    ) -> dict:
        """Simulate a blocking, non-streaming chat completion.

        Returns an OpenAI-compatible response dict with realistic usage.
        """
        time.sleep(self.delay)

        full_text = _extract_text(messages)
        response_text = self._generate_response(messages, full_text)
        prompt_tokens = _approx_tokens(full_text)
        completion_tokens = _approx_tokens(response_text)

        return {
            "id": f"mock-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    # ------------------------------------------------------------------
    # chat_completion_stream (blocking, yields chunks)
    # ------------------------------------------------------------------

    def chat_completion_stream(
        self,
        messages: List[dict],
        model: str = "mock-model",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.95,
        stop: Optional[List[str]] = None,
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
    ) -> Iterator[dict]:
        """Simulate a streaming chat completion.

        Yields SSE-like chunk dicts one at a time, splitting the response
        into word-level chunks.
        """
        full_text = _extract_text(messages)
        response_text = self._generate_response(messages, full_text)
        words = response_text.split()
        chunk_id = f"mock-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        for i, word in enumerate(words):
            time.sleep(self.delay * 0.3)
            delta = {"content": word + " "}
            yield {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": None,
                    }
                ],
            }

        # Final chunk with finish_reason
        time.sleep(self.delay * 0.3)
        yield {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        }

    # ------------------------------------------------------------------
    # Response generation
    # ------------------------------------------------------------------

    def _generate_response(self, messages: List[dict], full_text: str) -> str:
        """Generate a contextual mock response based on message analysis."""
        # ── Tool results → summarize ──
        if _has_tool_results(messages):
            return random.choice(_SUMMARIES)

        # ── Search request → emit <tool_call> for browse_search ──
        if _is_search_request(full_text):
            query = _extract_search_query(full_text)
            tool_call = {
                "name": "browse_search",
                "arguments": {"query": query},
            }
            return f"<tool_call>{json.dumps(tool_call, ensure_ascii=False)}</tool_call>"

        # ── File read request → emit <tool_call> for read_text_file ──
        if _is_file_read_request(full_text):
            tool_call = {
                "name": "read_text_file",
                "arguments": {"path": "/tmp/example.txt"},
            }
            return f"<tool_call>{json.dumps(tool_call, ensure_ascii=False)}</tool_call>"

        # ── File write request → emit <tool_call> for write_file ──
        if _is_file_write_request(full_text):
            tool_call = {
                "name": "write_file",
                "arguments": {
                    "path": "/tmp/example.txt",
                    "content": "Conteudo de exemplo gerado pelo mock LLM.",
                },
            }
            return f"<tool_call>{json.dumps(tool_call, ensure_ascii=False)}</tool_call>"

        # ── Greeting → friendly response ──
        lower = full_text.lower().strip()
        greeting_words = [
            "ola", "oi", "bom dia", "boa tarde", "boa noite",
            "hello", "hi", "hey", "e ai", "eai",
            "tudo bem", "como vai",
        ]
        if any(gw in lower for gw in greeting_words):
            return random.choice(_GREETINGS)

        # ── Generic helpful response ──
        # Mix in helpers and fallbacks for variety
        pool = _HELPERS + _FALLBACKS
        return random.choice(pool)
