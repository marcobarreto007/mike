# Extracted from mike_server.py — Phase 3 refactor
# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Chat Core — chat completions, streaming, tool orchestration, and session helpers.

Extracted from mike_server.py during Phase 3 refactoring.
All functions remain in their exact original form.
Module-level singletons are injected via init_chat_core() called from mike_server.py.
"""

from __future__ import annotations

import asyncio
import binascii
import json
import logging
import os
import re
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, List, Optional

from fastapi import BackgroundTasks, Request
from fastapi.responses import JSONResponse, StreamingResponse

# --- Project config ---
from mike_config import (
    LLM_BACKEND,
    MCP_TOOL_MAX_STEPS,
    MODEL_ALIAS,
    RAG_ENABLED,
    SEARCH_ROUTE_LIMIT,
    STREAM_KEEPALIVE_SECONDS,
    STREAM_TOOL_TIMEOUT_SEC,
    TASK_MESH_ENABLED,
    TASK_MESH_MAX_PLAN_STEPS,
    WEB_CACHE_DIR,
    WEB_SEARCH_ENABLED,
    WEB_TOP_K,
)

# --- Shared state (injected by mike_server.py at startup) ---
import shared_state as _shared_state

# --- Project modules ---
from mike_models import ChatMessage, ChatRequest, VisionInputError
from mike_auth import (
    profile_from_request,
    scoped_session_id,
)
from mike_mcp_client import (
    extract_tool_call,
    extract_tool_call_streaming,
    render_tool_result_message,
    strip_tool_call_text,
    tool_instruction_block,
)
from mike_memory import MikeMemoryService
from mike_web import MikeWebSearch
from mike_task_mesh import TaskMesh, looks_complex
from mike_sse import (
    _guard_sse_stream,
    _normalize_reasoning_text,
    _request_disconnected,
    _sse_comment,
    _sse_content_chunk,
    _sse_done,
    _sse_error_event,
    _sse_event,
    _stream_error,
    _stream_headers,
)
from mike_stats import (
    _inc_stat,
    _vision_limits,
    stats,
)
from mike_tools_local import (
    _compact_tool_payload,
    _current_tool_session_id,
    _execute_mcp_tool,
    _visible_tool_manifest,
)
from mike_request_helpers import (
    _request_persist_conversation,
    _request_private_mode,
    _request_profile_scope,
    _request_raw_mode,
    _use_light_chat_context,
)
from mike_chat_builder import _build_messages as _build_messages_core
from mike_chat_completion import (
    _blocking_chat_completion_stream,
    _clean_completion_text,
    _generate_model_response,
    _response_completion_tokens,
    _response_text,
)
import mike_completions as _mc
_PRICE_PATTERN_RE = _mc._PRICE_PATTERN_RE
_contains_internet_denial = _mc._contains_internet_denial
_response_stream_delta = _mc._response_stream_delta
_REASONING_LEAK_PREFIXES = _mc._REASONING_LEAK_PREFIXES
_REASONING_ANYWHERE_RE = _mc._REASONING_ANYWHERE_RE
_looks_like_search_request = _mc._looks_like_search_request
from mike_token_budget import (
    count_tokens as _count_tokens,
)
from mike_vision import (
    _has_images,
)
import mike_context as _context
from mike_context_virtual import VirtualContextManager
from core.shared.task_utils import _handle_task_exception
from mike_family_profiles import (
    _format_family_profile_for_llm,
    _get_family_profile,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons injected by mike_server.py via init_chat_core()
# ---------------------------------------------------------------------------
web_search: Optional[MikeWebSearch] = None
memory_service: Optional[MikeMemoryService] = None
SYSTEM_PROMPT: str = ""
SOUL_PROMPT: str = ""

# Helper functions defined in mike_server.py — injected at startup
_build_dynamic_prefix = None
_light_system_prompt = None
_maybe_builtin_chat_reply = None
_save_conversation_async = None
_maybe_direct_answer_for_tool_result = None
_get_virtual_context = None
_get_cached_sdk_generate = None
_get_skill_registry = None


def init_chat_core(
    *,
    web_search_instance=None,
    memory_service_instance=None,
    system_prompt="",
    soul_prompt="",
    build_dynamic_prefix_fn=None,
    light_system_prompt_fn=None,
    maybe_builtin_chat_reply_fn=None,
    save_conversation_async_fn=None,
    maybe_direct_answer_for_tool_result_fn=None,
    get_virtual_context_fn=None,
    get_cached_sdk_generate_fn=None,
    get_skill_registry_fn=None,
):
    """Called by mike_server.py after this module is imported to inject dependencies."""
    global web_search, memory_service, SYSTEM_PROMPT, SOUL_PROMPT
    global _build_dynamic_prefix, _light_system_prompt, _maybe_builtin_chat_reply
    global _save_conversation_async, _maybe_direct_answer_for_tool_result
    global _get_virtual_context, _get_cached_sdk_generate, _get_skill_registry

    web_search = web_search_instance
    memory_service = memory_service_instance
    SYSTEM_PROMPT = system_prompt
    SOUL_PROMPT = soul_prompt
    _build_dynamic_prefix = build_dynamic_prefix_fn
    _light_system_prompt = light_system_prompt_fn
    _maybe_builtin_chat_reply = maybe_builtin_chat_reply_fn
    _save_conversation_async = save_conversation_async_fn
    _maybe_direct_answer_for_tool_result = maybe_direct_answer_for_tool_result_fn
    _get_virtual_context = get_virtual_context_fn
    _get_cached_sdk_generate = get_cached_sdk_generate_fn
    _get_skill_registry = get_skill_registry_fn


# ---------------------------------------------------------------------------
# Search keyword constants (copied from mike_server.py)
# ---------------------------------------------------------------------------

_WEB_SEARCH_KEYWORDS = frozenset((
    "hoje", "agora", "atual", "atualizado", "latest", "recent", "news",
    "noticia", "noticias", "ultimas", "internet", "web", "brave", "ddgs", "dds", "duckduckgo", "pesquisa", "pesquise",
    "procura", "verifica", "verifique", "cotacao", "preco", "preço",
    "clima", "tempo", "previsao", "jogo", "resultado", "placar",
    "quem ganhou", "o que aconteceu",
    "lançamento", "lancamento", "estreia", "novo", "nova",
    "busca", "busque", "pesquisar", "encontra", "acha",
    # meteorologia
    "meteo", "meteorologia", "temperatura", "temperatur", "chuva", "neve", "vento",
    "forecast", "umidade", "graus", "celsius", "fahrenheit", "nevar", "chover",
    "weather", "climate", "frio", "calor", "quente", "gelado",
    # cidades consultadas frequentemente (clima, noticias, eventos)
    "montreal", "toronto", "ottawa", "vancouver", "quebec",
    "paris", "lisboa", "porto", "london", "new york", "miami", "são paulo",
    # financas / noticias
    "bitcoin", "cripto", "dolar", "euro", "bolsa", "taxa", "cambio",
    "eleicao", "governo", "presidente",
    "docs", "doc", "documentacao", "manual", "guia",
    "referencia", "api", "oauth", "endpoint", "sdk", "scope",
    # geopolitica / conflitos / guerra
    "guerra", "conflito", "ataque", "bombardeio", "exercito", "militar", "tropa",
    "invasao", "invasão", "ofensiva", "cessar-fogo", "ceasefirec", "negociacao",
    "acordo", "tratado", "sancoes", "sanções", "embargo",
    # paises / líderes noticiosos
    "trump", "biden", "putin", "macron", "zelensky", "netanyahu",
    "iran", "russia", "ucrania", "ucrânia", "china", "israel", "gaza", "siria",
    "coreia", "libano", "libano", "palestina", "hamas", "hezbollah",
    # política / economia
    "politica", "político", "politico", "economia", "econômico", "economica",
    "inflacao", "inflação", "recessao", "recessão", "desemprego", "pib",
    "analise geopolitica", "situacao", "crise",
))


_SKIP_SEARCH_PATTERNS = frozenset((
    "oi", "ola", "olá", "tudo bem", "tudo bom", "como vai", "bom dia",
    "boa tarde", "boa noite", "obrigado", "obrigada", "valeu", "ok",
    "sim", "nao", "não", "pode", "claro", "certo", "entendi",
))

_NO_WEB_SEARCH_PATTERNS = (
    "nao use ferramentas",
    "não use ferramentas",
    "sem ferramentas",
    "nao use busca web",
    "não use busca web",
    "sem busca web",
    "nao pesquise",
    "não pesquise",
    "nao use a internet",
    "não use a internet",
)

_QUESTION_PREFIXES = (
    "como ", "qual ", "quais ", "quando ", "onde ", "quem ", "por que ",
    "porque ", "o que ", "me explica ", "explique ", "how ", "what ",
    "when ", "where ", "why ", "which ",
)

_LOCAL_WORK_VERBS = (
    "crie", "criar", "monte", "montar", "adicione", "adicionar", "edite",
    "editar", "atualize", "atualizar", "preencha", "preencher", "gere", "gerar",
    "salve", "salvar",
)

_LOCAL_WORK_KEYWORDS = (
    "planilha", "excel", ".xlsx", "workspace", "arquivo", "pasta", "aba", "sheet",
)

_EMAIL_ROUTE_KEYWORDS = (
    "email", "e-mail", "inbox", "caixa de entrada", "meus emails", "meu email",
    "manda email", "enviar email", "envie email", "responde email", "responder email",
    "leia email", "ler email",
)

_CALENDAR_ROUTE_KEYWORDS = (
    "agenda", "calendario", "calendário", "calendar", "evento", "eventos",
    "compromisso", "compromissos", "reuniao", "reunião", "lembrete", "lembre",
)

_SPREADSHEET_ROUTE_KEYWORDS = (
    "planilha", "excel", ".xlsx", "csv", "workbook", "sheet", "aba", "abas",
    "celula", "célula", "coluna", "linhas", "orcamento", "orçamento",
)

_WORKSPACE_ROUTE_KEYWORDS = (
    "workspace", "arquivo", "pasta", "diretorio", "diretório", "caminho", "path",
    "salve em", "edite o arquivo", "crie arquivo", "delete arquivo", "mova arquivo",
)

_MEMORY_ROUTE_KEYWORDS = (
    "meu ", "minha ", "minhas ", "meus ", "eu ", "lembra", "lembre", "historico",
    "histórico", "familia", "família", "empresa", "civilia",
    "stephane", "manon", "daniel", "caroline", "benoat", "prof", "tdah",
)

_DOC_ROUTE_KEYWORDS = (
    "doc", "docs", "documentacao", "documentação", "manual", "guia", "api", "apis",
    "oauth", "endpoint", "sdk", "scope", "referencia", "referência", "erro", "http",
)

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

_SIMPLE_MATH_QUERY_RE = re.compile(
    r"^\s*(?:quanto\s+(?:é|e)\s+)?-?\d+(?:[.,]\d+)?(?:\s*[-+*/x]\s*-?\d+(?:[.,]\d+)?)+\s*\??\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _last_user_message(req: ChatRequest) -> str:
    for message in reversed(req.messages):
        if message.role == "user":
            if isinstance(message.content, str):
                return message.content
            # Vision content (list): extract text parts only
            parts = [
                p.get("text", "") for p in message.content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return " ".join(parts)
    return ""


def _contains_any_keyword(normalized: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in normalized for keyword in keywords)


def _looks_like_simple_math_query(query: str) -> bool:
    normalized = re.sub(r"\s+", " ", query or "").strip().lower().replace("×", "x")
    if not normalized:
        return False
    return bool(_SIMPLE_MATH_QUERY_RE.match(normalized))


def _prefers_direct_model_answer(query: str) -> bool:
    normalized = query.lower().strip()
    if not normalized:
        return True
    if normalized in _SKIP_SEARCH_PATTERNS or len(normalized) < 8 or _looks_like_simple_math_query(normalized):
        return True
    if any(pattern in normalized for pattern in _NO_WEB_SEARCH_PATTERNS):
        return True
    has_generation_verb = _contains_any_keyword(normalized, _DIRECT_GENERATION_VERBS)
    if has_generation_verb and (
        _contains_any_keyword(normalized, _DIRECT_GENERATION_TARGETS)
        or normalized.startswith(("traduza", "traduz", "resuma", "resume", "corrija", "reescreva", "melhore"))
    ):
        return True
    return False


def _search_routes_for_query(
    query: str,
    rag_stats: dict,
    *,
    has_images: bool = False,
    web_loaded: bool = False,
) -> List[dict]:
    normalized = query.lower().strip()
    total_local_hits = int(rag_stats.get("memory_hits", 0)) + int(rag_stats.get("knowledge_hits", 0))
    routes: List[dict] = []
    seen: set[str] = set()

    def add_route(name: str, source: str, reason: str, priority: int) -> None:
        if name in seen:
            return
        seen.add(name)
        routes.append({
            "route": name,
            "source": source,
            "reason": reason,
            "priority": priority,
        })

    if has_images:
        add_route(
            "visao_foto",
            "vision handler",
            "Ha imagem anexada; analise a foto antes de responder ou combinar com outras fontes.",
            5,
        )

    if _contains_any_keyword(normalized, _EMAIL_ROUTE_KEYWORDS):
        add_route(
            "email_mcp",
            "MCP email.*",
            "O pedido fala de email/caixa de entrada; consulte ou aja pelas tools de Gmail.",
            10,
        )

    if _contains_any_keyword(normalized, _CALENDAR_ROUTE_KEYWORDS):
        add_route(
            "agenda_mcp",
            "MCP calendar.*",
            "O pedido envolve agenda, eventos ou lembretes; use as tools de calendario.",
            12,
        )

    if _contains_any_keyword(normalized, _SPREADSHEET_ROUTE_KEYWORDS):
        add_route(
            "planilha_mcp",
            "MCP excel.*",
            "O pedido envolve planilha/Excel/CSV; crie, leia ou edite a planilha pela tool.",
            14,
        )

    if _contains_any_keyword(normalized, _WORKSPACE_ROUTE_KEYWORDS):
        add_route(
            "workspace_mcp",
            "MCP workspace.*",
            "O pedido envolve arquivos ou pastas no workspace; use as tools de arquivos.",
            16,
        )

    if RAG_ENABLED and (
        total_local_hits > 0 or _contains_any_keyword(normalized, _MEMORY_ROUTE_KEYWORDS)
    ):
        add_route(
            "memoria_local",
            "SQLite + Mem0",
            "Ha contexto pessoal, historico ou memoria util; recupere memorias antes de inferir.",
            20,
        )

    if RAG_ENABLED and (
        int(rag_stats.get("knowledge_hits", 0)) > 0 or _contains_any_keyword(normalized, _DOC_ROUTE_KEYWORDS)
    ):
        add_route(
            "docs_locais_rag",
            "knowledge/current_docs + web_cache",
            "O pedido parece documental/tecnico; verifique docs locais, caches web e base de conhecimento.",
            24,
        )

    if WEB_SEARCH_ENABLED and _should_search_web(query, rag_stats):
        add_route(
            "ddgs_web",
            "web.search_and_cache",
            (
                "A busca DDGS ja foi executada e o resultado foi salvo no RAG local."
                if web_loaded
                else "Falta dado atual ou contexto confiavel; pesquise no DDGS e persista o resultado util."
            ),
            18 if web_loaded else 30,
        )

    if not routes and not _prefers_direct_model_answer(query):
        if RAG_ENABLED:
            add_route(
                "memoria_local",
                "SQLite + Mem0",
                "Comece pelo contexto local salvo e pelo historico recente.",
                20,
            )
            add_route(
                "docs_locais_rag",
                "knowledge/current_docs + web_cache",
                "Se a memoria nao bastar, consulte a base local e os docs salvos.",
                24,
            )
        if WEB_SEARCH_ENABLED and len(normalized.split()) >= 3:
            add_route(
                "ddgs_web",
                "web.search_and_cache",
                "Se o contexto local nao bastar, pesquise no DDGS e salve a resposta util.",
                30,
            )

    routes.sort(key=lambda item: item["priority"])
    trimmed = routes[:SEARCH_ROUTE_LIMIT]
    for item in trimmed:
        item.pop("priority", None)
    return trimmed


def _render_search_route_guidance(routes: List[dict], rag_stats: dict, web_loaded: bool = False) -> str:
    if not routes:
        return ""
    has_web = any(r["route"] in ("ddgs_web", "browse_web") for r in routes)
    lines = [
        "INSTRUCAO OBRIGATORIA — ROTAS PARA ESTA RESPOSTA:",
        (
            f"- Sinais locais: memorias={int(rag_stats.get('memory_hits', 0))}, "
            f"docs={int(rag_stats.get('knowledge_hits', 0))}."
        ),
    ]
    for index, route in enumerate(routes, start=1):
        lines.append(
            f"- {index}. [{route['route']}] via {route['source']}: {route['reason']}"
        )
    if has_web and not web_loaded:
        lines.append(
            "ACAO IMEDIATA: Gere um <tool_call> para web.search_and_cache AGORA com a query do usuario. "
            "PROIBIDO responder sem buscar primeiro. PROIBIDO dizer que nao tem internet."
        )
    elif has_web and web_loaded:
        lines.append(
            "Os dados da web ja foram buscados e estao disponíveis abaixo. "
            "Use-os diretamente para responder. NAO chame nenhuma tool de busca."
        )
    else:
        lines.append(
            "- Siga a primeira rota aplicavel e, se ela nao resolver tudo, passe para a proxima sem inventar fatos."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Web search helpers
# ---------------------------------------------------------------------------

def _should_search_web(query: str, rag_stats: dict) -> bool:
    normalized = query.lower().strip()
    # Nunca busca para mensagens triviais de conversa
    if _prefers_direct_model_answer(query):
        return False
    if (
        _contains_any_keyword(normalized, _EMAIL_ROUTE_KEYWORDS)
        or _contains_any_keyword(normalized, _CALENDAR_ROUTE_KEYWORDS)
        or _contains_any_keyword(normalized, _SPREADSHEET_ROUTE_KEYWORDS)
        or _contains_any_keyword(normalized, _WORKSPACE_ROUTE_KEYWORDS)
    ):
        return False
    if (
        any(verb in normalized for verb in _LOCAL_WORK_VERBS)
        and any(keyword in normalized for keyword in _LOCAL_WORK_KEYWORDS)
    ):
        return False
    explicit = any(kw in normalized for kw in _WEB_SEARCH_KEYWORDS)
    if explicit:
        return True
    memory_hits = int(rag_stats.get("memory_hits", 0))
    knowledge_hits = int(rag_stats.get("knowledge_hits", 0))
    total_local_hits = memory_hits + knowledge_hits
    words = [w for w in normalized.split() if len(w) > 2]
    is_substantive = len(words) >= 3
    looks_like_question = normalized.endswith("?") or any(
        normalized.startswith(prefix) for prefix in _QUESTION_PREFIXES
    )
    # Se a query menciona membros da família, Mike sabe responder com conhecimento interno
    # (soul.json cobre personalidade, relações e história da família Barreto)
    if _shared_state.SOUL and any(name in normalized for name in _shared_state.SOUL.get("family", {}).keys()):
        if not looks_like_question:
            return False
    if total_local_hits == 0 and is_substantive:
        return True
    return total_local_hits <= 1 and is_substantive and looks_like_question


def _format_web_results(results: List[dict]) -> str:
    lines = []
    for i, item in enumerate(results, 1):
        title = item.get("title", "").strip()
        url   = item.get("url", "").strip()
        desc  = item.get("description", "").strip()
        age   = item.get("age", "").strip()
        age_tag = f" [{age}]" if age else ""
        lines.append(f"[{i}] {title}{age_tag}\n    {url}\n    {desc}")
    return "\n\n".join(lines)


def _search_web(query: str) -> List[dict]:
    try:
        results = web_search.search(query, count=WEB_TOP_K)
    except Exception as exc:
        log.warning("Web search failed for query %r: %s", query, exc)
        stats["last_web_hits"] = 0
        stats["last_web_provider"] = "error"
        return []
    stats["last_web_hits"] = len(results)
    stats["last_web_provider"] = web_search.last_provider_used
    # Não cacheia dados em tempo real (clima, preço, placar) — sempre busca fresco
    if results and not web_search.is_realtime_query(query):
        memory_service.cache_web_results(
            query,
            results,
            WEB_CACHE_DIR,
            provider=web_search.last_provider_used or web_search.active_provider,
        )
        stats.update(memory_service.stats())
    return results


# Thin wrapper: bridges mike_chat_builder with mike_server singletons
def _build_messages(
    req: ChatRequest,
    last_user_msg: str,
    tool_instruction: Optional[str] = None,
):
    return _build_messages_core(
        req,
        last_user_msg,
        tool_instruction=tool_instruction,
        system_prompt=SYSTEM_PROMPT,
        light_system_prompt=_light_system_prompt,
        build_dynamic_prefix=_build_dynamic_prefix,
        get_family_profile=_get_family_profile,
        format_family_profile_for_llm=_format_family_profile_for_llm,
        should_search_web=_should_search_web,
        search_web=_search_web,
        format_web_results=_format_web_results,
        search_routes_for_query=_search_routes_for_query,
        render_search_route_guidance=_render_search_route_guidance,
        memory_service=memory_service,
    )


# _local_tool_manifest extracted to mike_tools_local
async def _generate_response_with_tools(
    messages,
    req,
    profile_key: Optional[str],
    last_user_msg: str = "",
    tool_manifest: Optional[List[dict]] = None,
    cancel_event: Optional[asyncio.Event] = None,
):
    tool_manifest = tool_manifest if tool_manifest is not None else await _visible_tool_manifest(profile_key)
    working_messages = list(messages)
    total_completion_tokens = 0
    total_elapsed = 0.0
    tool_calls: list[dict] = []

    # Native function calling DISABLED for DeepSeek — API returns 400.
    # DeepSeek v4-pro does NOT support tools/tool_choice params.
    # Use regex-based <tool_call> parsing (proven, reliable).
    openai_tools = None

    for step in range(MCP_TOOL_MAX_STEPS + 1):
        t0 = time.time()
        response = await _generate_model_response(
            working_messages,
            req,
            cancel_event=cancel_event,
            tools=openai_tools,
        )
        elapsed = time.time() - t0
        total_elapsed += elapsed

        assistant_text = _response_text(response)

        # Native OpenAI-format tool_calls: apenas relevante para backends com function
        # calling nativo. O cerebro local (Qwen via llama-server) e local-only e usa
        # tool-use explicitado no prompt, por isso native_tool_calls fica vazio.
        native_tool_calls = []

        if native_tool_calls:
            # Execute all native tool calls
            for ntc in native_tool_calls:
                tool_result = await _execute_mcp_tool(
                    ntc["name"], ntc["arguments"], profile_key=profile_key
                )
                tool_calls.append({
                    "name": ntc["name"],
                    "arguments": ntc["arguments"],
                    "ok": tool_result.get("ok", False),
                    "text": tool_result.get("text", ""),
                })
                # Feed result back as tool message
                working_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": ntc["id"],
                        "type": "function",
                        "function": {"name": ntc["name"], "arguments": json.dumps(ntc["arguments"])},
                    }],
                })
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": ntc["id"],
                    "content": tool_result.get("text", str(tool_result))[:4000],
                })
            total_completion_tokens += _response_completion_tokens(response, assistant_text)
            continue  # Let LLM process tool results and decide next action

        total_completion_tokens += _response_completion_tokens(
            response, assistant_text
        )

        tool_call = (
            extract_tool_call(assistant_text)
            if tool_manifest and step < MCP_TOOL_MAX_STEPS
            else None
        )
        if not tool_call:
            # Anti-hallucination guard: if step==0 (no tools called yet) and the
            # response contains price/currency patterns, the model is fabricating
            # data.  Force a retry with an explicit instruction to use tools.
            if (
                step == 0
                and not tool_calls
                and _PRICE_PATTERN_RE.search(assistant_text)
                and _looks_like_search_request(last_user_msg)
            ):
                log.warning(
                    "Hallucination guard: prices in response without tool call (session=%s msg=%s)",
                    req.session_id if hasattr(req, 'session_id') else '?',
                    last_user_msg[:80],
                )
                working_messages.append({"role": "assistant", "content": assistant_text})
                working_messages.append({
                    "role": "user",
                    "content": (
                        "ATENCAO: voce citou precos/valores sem ter chamado nenhuma tool. "
                        "Isso e alucinacao. Use web.search_and_cache, fetch ou puppeteer AGORA para buscar dados reais."
                    ),
                })
                continue
            # Internet-denial guard: model claimed it has no internet access.
            # Mike HAS web search — force it to actually search.
            if (
                step == 0
                and not tool_calls
                and _contains_internet_denial(assistant_text)
                and WEB_SEARCH_ENABLED
            ):
                log.warning(
                    "Internet-denial guard: model denied internet access without searching (session=%s msg=%s)",
                    req.session_id if hasattr(req, 'session_id') else '?',
                    last_user_msg[:80],
                )
                working_messages.append({"role": "assistant", "content": assistant_text})
                working_messages.append({
                    "role": "user",
                    "content": (
                        "ERRO: voce disse que nao tem acesso a noticias/internet, mas isso e FALSO. "
                        "Voce TEM busca web ativa via tool. Gere AGORA um <tool_call> com "
                        "web.search_and_cache ou uma tool fetch/puppeteer do manifesto. "
                        "PROIBIDO repetir que nao tem acesso."
                    ),
                })
                continue
            return {
                "assistant_text": assistant_text,
                "completion_tokens": total_completion_tokens,
                "elapsed": total_elapsed,
                "tool_calls": tool_calls,
            }

        tool_result = await _execute_mcp_tool(
            tool_call["name"], tool_call["arguments"], profile_key=profile_key
        )
        tool_calls.append({
            "name": tool_call["name"],
            "arguments": tool_call["arguments"],
            "ok": tool_result.get("ok", False),
            "text": tool_result.get("text", ""),
        })
        direct_answer = _maybe_direct_answer_for_tool_result(
            tool_call["name"],
            tool_result,
            last_user_msg,
        )
        if direct_answer:
            return {
                "assistant_text": direct_answer,
                "completion_tokens": total_completion_tokens + _count_tokens(direct_answer),
                "elapsed": total_elapsed,
                "tool_calls": tool_calls,
            }
        compact_tool_result = dict(tool_result)
        compact_tool_result["text"] = _compact_tool_payload(
            tool_call["name"],
            tool_result.get("text", ""),
        )
        # Strip raw <tool_call> XML and bare JSON so next LLM loop sees clean context
        clean_assistant = strip_tool_call_text(assistant_text)
        working_messages.append({"role": "assistant", "content": clean_assistant or "(chamei tool)"})
        working_messages.append({
            "role": "user",
            "content": render_tool_result_message(
                tool_call["name"], tool_call["arguments"], compact_tool_result
            ),
        })

    final_text = (
        "Eu cheguei ao limite de etapas de tool nesta tarefa. "
        "Posso continuar se voce quiser dividir em passos menores."
    )
    return {
        "assistant_text": final_text,
        "completion_tokens": total_completion_tokens + _count_tokens(final_text),
        "elapsed": total_elapsed,
        "tool_calls": tool_calls,
    }


# ---------------------------------------------------------------------------
# TaskMesh builder
# ---------------------------------------------------------------------------

def _build_task_mesh(
    profile_key: Optional[str],
    tool_manifest: Optional[List[dict]] = None,
) -> TaskMesh:
    """Create a TaskMesh instance wired to Mike's LLM and tool infrastructure.

    Includes SubAgentSpawner for intelligent step delegation to
    specialized agents (email, github, research, etc.).
    """
    async def _mesh_generate(messages, _req):
        """LLM call for the mesh — uses 4096 tokens so code/file creation has room."""
        response = await _generate_model_response(
            messages,
            ChatRequest(messages=[], max_tokens=4096, temperature=0.7),
        )
        return {
            "assistant_text": _clean_completion_text(_response_text(response)),
            "completion_tokens": _response_completion_tokens(response, _response_text(response)),
        }

    async def _mesh_execute_tool(name, arguments):
        return await _execute_mcp_tool(name, arguments, profile_key=profile_key)

    # --- Sub-Agent Spawner (with Skills) ---
    spawner = None
    try:
        from mike_agent_sdk import AgentRegistry, SubAgentSpawner
        skill_reg = _get_skill_registry()
        registry = AgentRegistry(
            generate_fn=_mesh_generate,
            execute_tool_fn=_mesh_execute_tool,
            extract_tool_call_fn=extract_tool_call,
            strip_tool_call_fn=strip_tool_call_text,
            render_tool_result_fn=render_tool_result_message,
            compact_tool_payload_fn=_compact_tool_payload,
            skill_registry=skill_reg,
        )
        spawner = SubAgentSpawner(registry)
        dyn_count = sum(1 for a in registry.list_agents() if a.get("type") == "dynamic")
        log.info("Agent SDK loaded: %d agents (%d dynamic/skill-based)", len(registry.list_agents()), dyn_count)
    except Exception as exc:
        log.warning("Agent SDK not available, using generic TaskMesh: %s", exc)

    return TaskMesh(
        generate_fn=_mesh_generate,
        execute_tool_fn=_mesh_execute_tool,
        extract_tool_call_fn=extract_tool_call,
        strip_tool_call_fn=strip_tool_call_text,
        render_tool_result_fn=render_tool_result_message,
        compact_tool_payload_fn=_compact_tool_payload,
        tool_manifest=tool_manifest or [],
        max_steps_per_subtask=MCP_TOOL_MAX_STEPS,
        max_plan_steps=TASK_MESH_MAX_PLAN_STEPS,
        sub_agent_spawner=spawner,
    )


# ---------------------------------------------------------------------------
# Chat preparation
# ---------------------------------------------------------------------------

async def _prepare_chat_response(
    req: ChatRequest,
    last_user_msg: str,
    profile_key: Optional[str],
    *,
    cancel_event: Optional[asyncio.Event] = None,
):
    _current_tool_session_id.set(getattr(req, "session_id", "main") or "main")

    builtin_reply = _maybe_builtin_chat_reply(last_user_msg, profile_key)
    if builtin_reply:
        return {
            "assistant_text": _clean_completion_text(builtin_reply),
            "completion_tokens": _count_tokens(builtin_reply),
            "elapsed": 0.0,
            "tool_calls": [],
        }

    messages, tool_manifest = await _prepare_chat_messages(req, last_user_msg, profile_key)

    # TaskMesh: detect complex tasks and use multi-step orchestration
    if (
        TASK_MESH_ENABLED
        and tool_manifest
        and last_user_msg
        and looks_complex(last_user_msg)
    ):
        log.info("TaskMesh activated for: %s", last_user_msg[:120])
        mesh = _build_task_mesh(profile_key, tool_manifest)
        system_prompt = SYSTEM_PROMPT + "\n\n" + _build_dynamic_prefix(last_user_msg)
        tool_block = tool_instruction_block(tool_manifest) or ""
        t0 = time.time()
        try:
            plan, summary = await mesh.run(last_user_msg, system_prompt, tool_block)
        except Exception as mesh_exc:
            log.error("TaskMesh non-streaming failed: %s", mesh_exc, exc_info=True)
            return {
                "assistant_text": f"Erro durante execu\u00e7\u00e3o multi-step: {mesh_exc}. Tente novamente ou reformule o pedido.",
                "completion_tokens": 0,
                "elapsed": time.time() - t0,
                "tool_calls": [],
            }
        elapsed = time.time() - t0
        all_tools = []
        for step in plan.steps:
            for tn in step.tools_used:
                all_tools.append({"name": tn, "arguments": {}, "ok": step.status.value == "done", "text": ""})
        return {
            "assistant_text": _clean_completion_text(summary),
            "completion_tokens": _count_tokens(summary),
            "elapsed": elapsed,
            "tool_calls": all_tools,
        }

    # Tree of Thoughts — structured reasoning for complex problems
    tot_path = ""
    if last_user_msg and not tool_manifest:
        try:
            from mike_tot import TreeOfThoughts
            if TreeOfThoughts.should_use_tot(last_user_msg):
                tot = TreeOfThoughts(generate_fn=_get_cached_sdk_generate(), log_fn=log.info)
                tot_result = await tot.solve(last_user_msg)
                if tot_result.success and tot_result.best_score >= 0.4:
                    tot_path = tot.format_path_for_context(tot_result)
                    # Inject as a system message before the last user message
                    if messages and tot_path:
                        messages.insert(-1, {"role": "system", "content": tot_path})
                log.info("ToT: success=%s score=%.2f thoughts=%d depth=%d",
                         tot_result.success, tot_result.best_score,
                         tot_result.total_thoughts, tot_result.depth_reached)
        except Exception as exc:
            log.debug("ToT skipped: %s", exc)

    result = await _generate_response_with_tools(
        messages,
        req,
        profile_key=profile_key,
        last_user_msg=last_user_msg,
        tool_manifest=tool_manifest,
        cancel_event=cancel_event,
    )
    return result


async def _prepare_chat_messages(
    req: ChatRequest,
    last_user_msg: str,
    profile_key: Optional[str],
):
    private_mode = _request_private_mode(req)
    raw_mode = _request_raw_mode(req)
    has_images = _has_images(req.messages)
    light_context = _use_light_chat_context(req, last_user_msg)
    tool_manifest = (
        []
        if (raw_mode or has_images or light_context)
        else await _visible_tool_manifest(profile_key, task=last_user_msg)
    )
    messages = await asyncio.to_thread(
        _build_messages,
        req,
        last_user_msg,
        tool_instruction_block(tool_manifest) if tool_manifest else None,
    )
    return messages, tool_manifest


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

async def _iter_text_stream_chunks(messages, req, stop_event: threading.Event):
    """Stream LLM output chunks via an async queue, fed by a background thread.

    Each item yielded is a ``(kind, payload)`` tuple where *kind* is one of
    ``"chunk"``, ``"done"``, ``"tool_detected"``, or ``"keepalive"``
    (the last is emitted by the caller on queue timeout, not by the worker).

    The worker thread accumulates raw text and, after every chunk, checks
    whether a tool-call pattern has appeared.  If one is detected the worker
    pushes ``"tool_detected"`` with the accumulated text and stops early.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def push(kind: str, payload: Any = None) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    def worker() -> None:
        iterator = None
        accumulated: list[str] = []
        try:
            iterator = _blocking_chat_completion_stream(messages, req)
            for chunk in iterator:
                if stop_event.is_set():
                    close = getattr(iterator, "close", None)
                    if callable(close):
                        with suppress(Exception):
                            close()
                    break

                # Accumulate text for tool-call detection
                delta = _response_stream_delta(chunk)
                if delta:
                    accumulated.append(delta)
                    acc_text = "".join(accumulated)

                    # If a complete tool call is detected, signal early stop
                    streaming_check = extract_tool_call_streaming(acc_text)
                    if streaming_check.get("complete"):
                        push("tool_detected", acc_text)
                        stop_event.set()
                        close = getattr(iterator, "close", None)
                        if callable(close):
                            with suppress(Exception):
                                close()
                        return

                    # If a partial tool-call pattern is detected (tag opened but
                    # not closed), yield the chunk but **do not** stop — keep
                    # streaming until the tag closes.
                    if streaming_check.get("partial_text"):
                        # Continue streaming: partial tool call needs more data
                        pass

                push("chunk", chunk)
            push("done")
        except BaseException as exc:  # noqa: BLE001
            push("error", exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(),
                    timeout=STREAM_KEEPALIVE_SECONDS,
                )
            except asyncio.TimeoutError:
                yield ("keepalive", None)
                continue
            if kind == "chunk":
                yield (kind, payload)
                continue
            if kind == "tool_detected":
                yield (kind, payload)
                break
            if kind == "error":
                raise payload
            break
    finally:
        stop_event.set()


def _stream_prefix_state(raw_text: str) -> str:
    """Detect whether the LLM output starts with a <tool_call> tag, bare JSON
    tool call, markdown code block, reasoning leak, or is plain text.

    Returns "pending" while there is not enough data to decide,
    "tool" when we are confident it is a tool call,
    "reasoning" when it's a thinking/CoT leak (to be filtered), and "text" otherwise.
    """
    stripped = (raw_text or "").lstrip()
    if not stripped:
        return "pending"

    # Normalized version handles Unicode curly apostrophes (e.g. Qwen3 outputs "Here's")
    lowered = _normalize_reasoning_text(raw_text)
    if not lowered:
        return "pending"

    # Qwen3 native <think> tag
    if lowered.startswith("<think>"):
        return "reasoning"
    if "<think>"[:len(lowered)] == lowered and len(lowered) < 7:
        return "pending"

    # Prefix-based reasoning detection
    for marker in _REASONING_LEAK_PREFIXES:
        if marker.startswith(lowered) and len(lowered) <= len(marker):
            return "pending"   # current text is a valid prefix of this marker — keep waiting
        if lowered.startswith(marker):
            return "reasoning"

    # Extended regex fallback (catches novel patterns and Unicode variants) when >=30 chars
    if len(lowered) >= 30 and _REASONING_ANYWHERE_RE.search(lowered[:600]):
        return "reasoning"

    # Numbered reasoning block: "2.  **Identify", "**1. Analyze", "1. Check" etc.
    if re.match(r"^\*{0,2}\s*\d+\.?\s*\*{0,2}\s*(?:check|analy|identify|determin|formulate|draft|final|mental|constraint|capabilit|provided|context|user want)", lowered):
        return "reasoning"
    # Short numbered prefix — stay pending until we can decide
    if len(lowered) <= 20 and re.match(r"^\*{0,3}\s*\d+\.?\s*$", lowered):
        return "pending"

    # --- Tool call detection ---
    tool_prefix = "<tool_call>"
    if tool_prefix.startswith(stripped):
        return "pending"
    if stripped.startswith(tool_prefix):
        return "tool"
    # XML function_calls style emitted by DeepSeek/Anthropic-family models
    fc_prefix = "<function_calls>"
    if fc_prefix.startswith(stripped) and len(stripped) < len(fc_prefix):
        return "pending"
    if stripped.startswith(fc_prefix) or stripped.startswith("<function_calls "):
        return "tool"
    if stripped[0] == "<" and len(stripped) < len(tool_prefix):
        return "pending"
    if stripped.startswith("```"):
        if len(stripped) < 20:
            return "pending"
        if '"name"' in stripped[:200]:
            return "tool"
    if stripped[0] == "{":
        if len(stripped) < 16:
            return "pending"
        if '"name"' in stripped[:200]:
            return "tool"
    return "text"


# ---------------------------------------------------------------------------
# Streaming chat response
# ---------------------------------------------------------------------------

async def _stream_text_chat_response(
    request: Request,
    req: ChatRequest,
    rid: str,
    last_user_msg: str,
    session_id: str,
    profile_key: Optional[str],
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

    _current_tool_session_id.set(getattr(req, "session_id", session_id) or "main")

    builtin_reply = _maybe_builtin_chat_reply(last_user_msg, profile_key)
    if builtin_reply:
        final_text = _clean_completion_text(builtin_reply)
        yield _sse_content_chunk(rid, final_text)
        if last_user_msg and final_text and _request_persist_conversation(req):
            await _save_conversation_async(
                last_user_msg,
                final_text,
                session_id=session_id,
                promote_long_term=_request_persist_conversation(req),
            )
        yield _sse_event({
            "id": rid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ALIAS,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "tool_calls": [],
        })
        yield "data: [DONE]\n\n"
        return

    messages, tool_manifest = await _prepare_chat_messages(req, last_user_msg, profile_key)

    # TaskMesh: detect complex tasks -> multi-step execution with progress streaming
    if (
        TASK_MESH_ENABLED
        and tool_manifest
        and last_user_msg
        and looks_complex(last_user_msg)
    ):
        log.info("TaskMesh (stream) activated for: %s", last_user_msg[:120])
        mesh = _build_task_mesh(profile_key, tool_manifest)
        system_prompt = SYSTEM_PROMPT + "\n\n" + _build_dynamic_prefix(last_user_msg)
        tool_block = tool_instruction_block(tool_manifest) or ""
        all_tools: list[dict] = []
        final_summary = ""

        try:
            async for event_type, data in mesh.run_streaming(last_user_msg, system_prompt, tool_block):
                if await _request_disconnected(request):
                    log.debug("Streaming client disconnected during mesh for session %s", req.session_id)
                    return

                if event_type == "plan_created":
                    steps_text = "\n".join(
                        f"  {s['id']}. {s['description']}" for s in data["steps"]
                    )
                    yield _sse_content_chunk(rid, f"\U0001f4cb Plano de execu\u00e7\u00e3o:\n{steps_text}\n\n")
                elif event_type == "step_start":
                    yield _sse_content_chunk(
                        rid,
                        f"\u23f3 [{data['step_id']}/{data['total']}] {data['description']}...\n",
                    )
                    yield _sse_comment()
                elif event_type == "step_done":
                    status_icon = "\u2705" if data["status"] == "done" else "\u274c"
                    yield _sse_content_chunk(
                        rid,
                        f"{status_icon} Passo {data['step_id']} conclu\u00eddo"
                        + (f" (tools: {', '.join(data['tools_used'])})" if data["tools_used"] else "")
                        + "\n",
                    )
                elif event_type == "complete":
                    final_summary = data.get("summary", "")
                    if final_summary:
                        yield _sse_content_chunk(rid, f"\n{final_summary}")
                elif event_type == "keepalive":
                    # TaskMesh sends keepalive during long step execution
                    # to prevent SSE client timeout (Cloudflare/browser)
                    yield _sse_comment()
        except Exception as mesh_exc:
            log.error("TaskMesh streaming failed: %s", mesh_exc, exc_info=True)
            yield _sse_error_event(
                rid,
                "Nao consegui concluir a execucao multi-step agora. Tente novamente.",
                "task_mesh_stream_failed",
                {"exception_type": type(mesh_exc).__name__},
            )
            yield _sse_done()
            return

        if last_user_msg and final_summary and _request_persist_conversation(req):
            await _save_conversation_async(
                last_user_msg,
                final_summary,
                session_id=session_id,
                promote_long_term=_request_persist_conversation(req),
            )
        yield _sse_event({
            "id": rid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ALIAS,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "tool_calls": all_tools,
        })
        yield "data: [DONE]\n\n"
        return

    working_messages = list(messages)
    total_completion_tokens = 0
    total_elapsed = 0.0
    tool_calls: list[dict] = []
    _empty_retries = 0
    _internet_denial_retries = 0

    for step in range(MCP_TOOL_MAX_STEPS + 1):
        if await _request_disconnected(request):
            log.debug("Streaming client disconnected before generation for session %s", req.session_id)
            return

        stop_event = threading.Event()
        raw_text = ""
        state = "pending"
        emitted_length = 0
        t0 = time.time()
        _reasoning_buffer = ""  # track reasoning_content for DeepSeek thinking models

        async for item_kind, payload in _iter_text_stream_chunks(working_messages, req, stop_event):
            # --- Timeout guard per streaming iteration ---
            _stream_elapsed = time.time() - t0
            if _stream_elapsed > STREAM_TOOL_TIMEOUT_SEC:
                log.warning(
                    "Streaming timeout reached (%.1fs > %.1fs) -- stopping generation for step %d",
                    _stream_elapsed,
                    STREAM_TOOL_TIMEOUT_SEC,
                    step,
                )
                stop_event.set()
                break

            if item_kind == "tool_detected":
                # Worker detected a complete tool call mid-stream.
                # Stop streaming and let the tool loop pick up.
                if isinstance(payload, str):
                    raw_text = payload
                log.debug(
                    "Tool call detected mid-stream at step %d (%d chars accumulated)",
                    step,
                    len(raw_text),
                )
                break
            if item_kind == "keepalive":
                if await _request_disconnected(request):
                    log.debug("Streaming client disconnected during generation for session %s", req.session_id)
                    stop_event.set()
                    return
                yield _sse_comment()
                continue

            # Track reasoning_content for DeepSeek thinking models (content is null during thinking phase)
            _choice = (payload.get("choices") or [{}])[0]
            _delta_raw = _choice.get("delta") or {}
            _rc = _delta_raw.get("reasoning_content")
            if isinstance(_rc, str) and _rc:
                _reasoning_buffer += _rc

            delta = _response_stream_delta(payload)
            if not delta:
                continue
            raw_text += delta

            if state == "pending":
                state = _stream_prefix_state(raw_text)
                if state != "text":
                    continue

            if state == "text":
                # Guard: if a <tool_call> tag appears mid-stream while we
                # are already emitting text, stop emitting so we don't leak
                # the raw JSON to the dashboard.
                remaining = raw_text[emitted_length:]
                if "<tool_call>" in remaining:
                    # Emit only the portion before the tag
                    tag_pos = raw_text.index("<tool_call>", emitted_length)
                    pre = raw_text[emitted_length:tag_pos]
                    if pre:
                        yield _sse_content_chunk(rid, pre)
                    emitted_length = len(raw_text)
                    state = "tool"
                    continue
                # Guard: bare JSON tool call mid-stream
                # Look for {"name":" pattern that signals a tool call
                _bare_marker = '{"name":'
                if _bare_marker in remaining:
                    marker_pos = remaining.index(_bare_marker)
                    abs_pos = emitted_length + marker_pos
                    pre = raw_text[emitted_length:abs_pos]
                    if pre:
                        yield _sse_content_chunk(rid, pre)
                    emitted_length = len(raw_text)
                    state = "tool"
                    continue
                # Guard: markdown code-block wrapping a tool call mid-stream
                _code_marker = '```'
                if _code_marker in remaining:
                    # Only treat as tool if the code block likely contains a tool call
                    code_pos = remaining.index(_code_marker)
                    after_fence = remaining[code_pos + 3:code_pos + 60]
                    if '"name"' in after_fence or after_fence.lstrip().startswith(('json', '{\n', '{"')):
                        abs_pos = emitted_length + code_pos
                        pre = raw_text[emitted_length:abs_pos]
                        if pre:
                            yield _sse_content_chunk(rid, pre)
                        emitted_length = len(raw_text)
                        state = "tool"
                        continue
                emit = raw_text[emitted_length:]
                if emit:
                    emitted_length = len(raw_text)
                    yield _sse_content_chunk(rid, emit)

        total_elapsed += time.time() - t0
        assistant_text = _clean_completion_text(raw_text)
        total_completion_tokens += _count_tokens(assistant_text)

        tool_call = (
            extract_tool_call(assistant_text)
            if tool_manifest and step < MCP_TOOL_MAX_STEPS
            else None
        )
        if not tool_call:
            final_text = strip_tool_call_text(assistant_text)
            if state != "text" and final_text:
                yield _sse_content_chunk(rid, final_text)
            # Fallback: se nenhum texto foi emitido, envia resposta minima
            if not final_text and emitted_length == 0:
                log.warning(
                    "Empty LLM response (state=%s raw_len=%d tokens=%d session=%s msg=%s)",
                    state, len(raw_text), total_completion_tokens, session_id, last_user_msg[:80],
                )
                # DeepSeek thinking models: extract conclusion from reasoning_content as fallback
                if _reasoning_buffer and _empty_retries < 1:
                    log.debug("Empty content but reasoning found (%d chars) -- retrying with explicit instruction (step %d)", len(_reasoning_buffer), step)
                    working_messages.append({
                        "role": "user",
                        "content": (
                            "ERRO INTERNO: voce raciocinou mas nao produziu nenhuma resposta de texto. "
                            "Voce DEVE responder agora em portugues, de forma direta e completa. "
                            "Nao repita o raciocinio -- apenas responda o que o Marco pediu."
                        ),
                    })
                    _reasoning_buffer = ""
                    _empty_retries += 1
                    continue
                final_text = "Desculpe, n\u00e3o consegui gerar uma resposta agora. Tente de novo ou reformule a pergunta."
                yield _sse_content_chunk(rid, final_text)
            # Internet-denial guard: model claimed it has no internet access.
            if (
                step == 0
                and not tool_calls
                and _contains_internet_denial(assistant_text)
                and WEB_SEARCH_ENABLED
                and _internet_denial_retries < 1
            ):
                log.warning(
                    "Streaming internet-denial guard triggered (session=%s msg=%s)",
                    session_id, last_user_msg[:80],
                )
                working_messages.append({"role": "assistant", "content": assistant_text})
                working_messages.append({
                    "role": "user",
                    "content": (
                        "ERRO: voce disse que nao tem acesso a noticias/internet, mas isso e FALSO. "
                        "Voce TEM busca web ativa via tool. Gere AGORA um <tool_call> com browse_search "
                        "ou web.search para buscar a informacao que o Marco pediu. PROIBIDO repetir que nao tem acesso."
                    ),
                })
                # Reset per-step state so next iteration streams cleanly
                emitted_length = 0
                _internet_denial_retries += 1
                continue
            if await _request_disconnected(request):
                log.debug("Streaming client disconnected before completion for session %s", req.session_id)
                return
            _inc_stat("total_tokens_generated", total_completion_tokens)
            stats["last_tool_calls"] = len(tool_calls)
            if total_elapsed > 0:
                stats["last_speed_tps"] = round(total_completion_tokens / total_elapsed, 2)
            if last_user_msg and final_text and _request_persist_conversation(req):
                await _save_conversation_async(
                    last_user_msg,
                    final_text,
                    session_id=session_id,
                    promote_long_term=_request_persist_conversation(req),
                )
            yield _sse_event({
                "id": rid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_ALIAS,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "tool_calls": tool_calls,
            })
            yield "data: [DONE]\n\n"
            return

        # Execute tool while sending SSE keepalives so Cloudflare/browser
        # doesn't drop the connection during long tool calls (no data = timeout).
        _tool_task = asyncio.create_task(
            _execute_mcp_tool(tool_call["name"], tool_call["arguments"], profile_key=profile_key)
        )
        while True:
            try:
                tool_result = await asyncio.wait_for(
                    asyncio.shield(_tool_task), timeout=STREAM_KEEPALIVE_SECONDS
                )
                break
            except asyncio.TimeoutError:
                yield _sse_comment()
                if await _request_disconnected(request):
                    _tool_task.cancel()
                    return
        tool_calls.append({
            "name": tool_call["name"],
            "arguments": tool_call["arguments"],
            "ok": tool_result.get("ok", False),
            "text": tool_result.get("text", ""),
        })
        direct_answer = _maybe_direct_answer_for_tool_result(
            tool_call["name"],
            tool_result,
            last_user_msg,
        )
        if direct_answer:
            total_completion_tokens += _count_tokens(direct_answer)
            yield _sse_content_chunk(rid, direct_answer)
            if await _request_disconnected(request):
                return
            _inc_stat("total_tokens_generated", total_completion_tokens)
            stats["last_tool_calls"] = len(tool_calls)
            if total_elapsed > 0:
                stats["last_speed_tps"] = round(total_completion_tokens / total_elapsed, 2)
            if last_user_msg and direct_answer and _request_persist_conversation(req):
                await _save_conversation_async(
                    last_user_msg,
                    direct_answer,
                    session_id=session_id,
                    promote_long_term=_request_persist_conversation(req),
                )
            yield _sse_event({
                "id": rid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_ALIAS,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "tool_calls": tool_calls,
            })
            yield "data: [DONE]\n\n"
            return

        compact_tool_result = dict(tool_result)
        compact_tool_result["text"] = _compact_tool_payload(
            tool_call["name"],
            tool_result.get("text", ""),
        )
        # Strip raw <tool_call> XML and bare JSON so next LLM loop sees clean context
        clean_assistant = strip_tool_call_text(assistant_text)
        working_messages.append({"role": "assistant", "content": clean_assistant or "(chamei tool)"})
        working_messages.append({
            "role": "user",
            "content": render_tool_result_message(
                tool_call["name"], tool_call["arguments"], compact_tool_result
            ),
        })

    final_text = (
        "Eu cheguei ao limite de etapas de tool nesta tarefa. "
        "Posso continuar se voce quiser dividir em passos menores."
    )
    total_completion_tokens += _count_tokens(final_text)
    _inc_stat("total_tokens_generated", total_completion_tokens)
    stats["last_tool_calls"] = len(tool_calls)
    if total_elapsed > 0:
        stats["last_speed_tps"] = round(total_completion_tokens / total_elapsed, 2)
    yield _sse_content_chunk(rid, final_text)
    if last_user_msg and final_text and _request_persist_conversation(req):
        await _save_conversation_async(
            last_user_msg,
            final_text,
            session_id=session_id,
            promote_long_term=_request_persist_conversation(req),
        )
    yield _sse_event({
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ALIAS,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "tool_calls": tool_calls,
    })
    yield "data: [DONE]\n\n"


async def _stream(
    request: Request,
    assistant_msg: str,
    req,
    rid: str,
    last_user_msg: str,
    tokens: int,
    elapsed: float,
    tool_calls: List[dict],
    emit_role: bool = True,
):
    disconnected = False
    try:
        if emit_role:
            yield _sse_event({
                "id": rid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_ALIAS,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            })
        chunk_size = 18
        for idx in range(0, len(assistant_msg), chunk_size):
            if await _request_disconnected(request):
                disconnected = True
                log.debug("Streaming client disconnected for session %s", req.session_id)
                break
            content = assistant_msg[idx : idx + chunk_size]
            yield _sse_event({
                "id": rid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_ALIAS,
                "choices": [
                    {"index": 0, "delta": {"content": content}, "finish_reason": None}
                ],
            })
            await asyncio.sleep(0)
        if not disconnected:
            yield _sse_event({
                "id": rid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_ALIAS,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "tool_calls": tool_calls,
            })
    except Exception as exc:
        log.exception("Streaming failed for session %s: %s", req.session_id, exc)
        yield _sse_error_event(
            rid,
            "Nao consegui transmitir a resposta agora. Tente novamente em instantes.",
            "stream_delivery_failed",
            {"exception_type": type(exc).__name__},
        )
    finally:
        yield _sse_done()


async def _stream_chat_response(
    request: Request,
    req: ChatRequest,
    rid: str,
    last_user_msg: str,
    session_id: str,
    profile_key: Optional[str],
    has_images: bool,
):
    if not has_images:
        async for chunk in _stream_text_chat_response(
            request,
            req,
            rid,
            last_user_msg,
            session_id,
            profile_key,
        ):
            yield chunk
        return

    yield _sse_event({
        "id": rid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ALIAS,
        "choices": [
            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
        ],
    })

    cancel_event = asyncio.Event()
    response_task = asyncio.create_task(
        _prepare_chat_response(
            req,
            last_user_msg,
            profile_key,
            cancel_event=cancel_event,
        )
    )

    while not response_task.done():
        if await _request_disconnected(request):
            log.debug("Streaming client disconnected before completion for session %s", req.session_id)
            cancel_event.set()
            response_task.cancel()
            return
        yield _sse_comment()
        await asyncio.sleep(STREAM_KEEPALIVE_SECONDS)

    try:
        result = await response_task
        assistant_msg = result["assistant_text"]
        tokens = result["completion_tokens"]
        elapsed = result["elapsed"]
        _inc_stat("total_tokens_generated", tokens)
        stats["last_tool_calls"] = len(result["tool_calls"])
        if elapsed > 0:
            stats["last_speed_tps"] = round(tokens / elapsed, 2)

        async for chunk in _stream(
            request,
            assistant_msg,
            req,
            rid,
            last_user_msg,
            tokens,
            elapsed,
            result["tool_calls"],
            emit_role=False,
        ):
            yield chunk

        if (
            last_user_msg
            and assistant_msg
            and _request_persist_conversation(req)
            and not await _request_disconnected(request)
        ):
            task = asyncio.create_task(
                _save_conversation_async(
                    last_user_msg,
                    assistant_msg,
                    session_id=session_id,
                    promote_long_term=_request_persist_conversation(req),
                )
            )
            task.add_done_callback(_handle_task_exception)
            # MemGPT-style virtual context
            try:
                vctx = _get_virtual_context()
                if vctx:
                    vctx.add_conversation_turn(last_user_msg, assistant_msg)
            except Exception as e:
                log.warning("[server] Virtual context turn save failed: %s", e)
    except asyncio.CancelledError:
        cancel_event.set()
        return
    except VisionInputError as exc:
        yield _sse_error_event(rid, exc.message, exc.code, exc.details)
        yield _sse_done()
    except Exception as exc:
        log.exception("Chat generation failed for session %s: %s", req.session_id, exc)
        message = (
            "Nao consegui processar essa foto agora. Tente novamente com uma imagem menor."
            if has_images
            else "Nao consegui responder agora. Tente novamente em instantes."
        )
        code = "vision_generation_failed" if has_images else "chat_generation_failed"
        details = _vision_limits() if has_images else None
        error_details = dict(details or {})
        error_details["exception_type"] = type(exc).__name__
        yield _sse_error_event(rid, message, code, error_details)
        yield _sse_done()


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

async def chat_completions(req: ChatRequest, request: Request, background_tasks: BackgroundTasks):
    """Core logic for chat completions. Orchestrates preparation, execution, and streaming."""
    last_user_msg = ""
    if req.messages:
        last_msg = req.messages[-1].content
        if isinstance(last_msg, str):
            last_user_msg = last_msg
        elif isinstance(last_msg, list):
            # Extract text from content list if needed (vision)
            for part in last_msg:
                if isinstance(part, dict) and part.get("type") == "text":
                    last_user_msg += part.get("text", "")

    profile_key = profile_from_request(request)
    # A signed profile session owns its conversation namespace. Never trust a
    # client-supplied session id to select another profile's history.
    req.session_id = scoped_session_id(req.session_id or "main", profile_key)

    if req.stream:
        rid = f"chatcmpl-{binascii.hexlify(os.urandom(12)).decode()}"
        session_id = req.session_id
        has_images = _has_images(req.messages)
        stream = _stream_chat_response(
            request,
            req,
            rid,
            last_user_msg,
            session_id,
            profile_key,
            has_images,
        )
        return StreamingResponse(
            _guard_sse_stream(stream, rid, logger=log),
            media_type="text/event-stream",
            headers=_stream_headers(),
        )
    else:
        result = await _prepare_chat_response(req, last_user_msg, profile_key)

        # Output Guard -- detect simulation BEFORE user sees it
        assistant_text = result.get("assistant_text", "")
        tool_calls = result.get("tool_calls", [])
        if assistant_text:
            try:
                from mike_output_guard import detect_simulation
                is_sim, confidence, patterns = detect_simulation(assistant_text, bool(tool_calls))
                if is_sim and confidence >= 0.65:
                    log.warning(
                        "[GUARD] Simulation detected: confidence=%.2f patterns=%s snippet=%s",
                        confidence, patterns, assistant_text[:150]
                    )
                    if _shared_state.output_guard:
                        _shared_state.output_guard.check(
                            assistant_text, tool_calls,
                            session_id=getattr(req, "session_id", "main"),
                            original_request=last_user_msg,
                        )
                    # Tag result for analytics
                    result["guard_simulation_detected"] = True
                    result["guard_confidence"] = confidence
                    result["guard_patterns"] = patterns
            except Exception as exc:
                log.debug("Output guard skipped: %s", exc)

        # Chain-of-Verification -- post-process to reduce hallucination
        if assistant_text and not tool_calls:
            try:
                from mike_verify_chain import ChainOfVerification
                cove = ChainOfVerification(generate_fn=_get_cached_sdk_generate(), log_fn=log.info)
                cove_result = await cove.verify(assistant_text)
                if cove_result.corrected:
                    result["assistant_text"] = cove_result.verified_response
                    result["completion_tokens"] = _count_tokens(cove_result.verified_response)
            except Exception as exc:
                log.debug("CoVe skipped: %s", exc)

        # Record stats
        _inc_stat("total_requests")
        _inc_stat("total_tokens_generated", result.get("completion_tokens", 0))

        # Persist immediately here because this handler is called via router wrapper
        # and BackgroundTasks attached manually would not be executed automatically.
        if last_user_msg and result.get("assistant_text") and _request_persist_conversation(req):
            await _save_conversation_async(
                last_user_msg,
                result["assistant_text"],
                getattr(req, "session_id", "main"),
                promote_long_term=_request_persist_conversation(req),
            )
            # MemGPT-style virtual context -- infinite memory beyond context window
            try:
                vctx = _get_virtual_context()
                if vctx:
                    vctx.add_conversation_turn(last_user_msg, result["assistant_text"])
            except Exception as e:
                log.warning("[server] Virtual context turn save failed (non-stream): %s", e)
        return result


async def chat_sessions(request: Request, profile: Optional[str] = None, limit: int = 10):
    """Implementation for listing sessions."""
    if memory_service is None:
        return JSONResponse(status_code=503, content={"error": "Memory service not available"})
    authenticated_profile = profile_from_request(request)
    effective_profile = authenticated_profile or profile
    return memory_service.list_sessions(profile_key=effective_profile, limit=limit)


async def chat_history(request: Request, session_id: str = "main", profile: Optional[str] = None, limit: Optional[int] = 50):
    """Implementation for getting session history."""
    if memory_service is None:
        return JSONResponse(status_code=503, content={"error": "Memory service not available"})
    authenticated_profile = profile_from_request(request)
    effective_session = scoped_session_id(session_id, authenticated_profile)
    return memory_service.conversation_history(session_id=effective_session, limit=limit)
