"""
Mike chat message builder.

Constructs the messages list sent to the LLM by composing:
- System prompt (identity + rules + dynamic prefix)
- Family profile injection
- Episodic memory from SQLite / Mem0
- RAG context (local knowledge + web search)
- Search route guidance
- Conversation history
- Defensive token budget truncation

Extracted from mike_server.py — Phase 2 monolith breakup.

Dependencies still in mike_server.py are passed as callables to avoid
circular imports.  Modules already extracted are imported directly.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Callable

log = logging.getLogger("mike.chat_builder")

from mike_config import (
    CTX_SIZE,
    FORCE_TOOL_USE,
    KNOWLEDGE_TOP_K,
    MEMORY_TOP_K,
    RAG_ENABLED,
    RECENT_MEMORY_LIMIT,
    SEARCH_ROUTE_HINTS,
    WEB_SEARCH_ENABLED,
    normalize_session_id,
)
from mike_models import ChatRequest
from mike_request_helpers import (
    _request_private_mode,
    _request_raw_mode,
    _use_light_chat_context,
)
from mike_stats import stats
from mike_vision import _build_vision_messages, _has_images


# ── Type alias for callables injected by mike_server ──────────────────
# These live in mike_server.py and cannot be imported here (circular).

_ProfileFn = Callable[[str], Optional[dict]]
_FormatProfileFn = Callable[[dict], str]
_BuildPrefixFn = Callable[[str], str]
_LightSystemFn = Callable[[], str]
_ShouldSearchFn = Callable[[str, dict], bool]
_SearchWebFn = Callable[[str], List[dict]]
_FormatWebFn = Callable[[List[dict]], str]
_SearchRoutesFn = Callable[..., List[dict]]
_RenderRouteGuidanceFn = Callable[..., Optional[str]]


# ---------------------------------------------------------------------------
# Main exported function
# ---------------------------------------------------------------------------

def _build_messages(  # noqa: C901
    req: ChatRequest,
    last_user_msg: str,
    tool_instruction: Optional[str] = None,
    *,
    # ── System prompt strings (from mike_server module-level) ──
    system_prompt: str = "",
    # ── Light system prompt wrapper ──
    light_system_prompt: Optional[_LightSystemFn] = None,
    # ── Dynamic prefix builder ──
    build_dynamic_prefix: Optional[_BuildPrefixFn] = None,
    # ── Family profile functions (-> mike_family_profiles in Phase 2 module 11) ──
    get_family_profile: Optional[_ProfileFn] = None,
    format_family_profile_for_llm: Optional[_FormatProfileFn] = None,
    # ── Web search functions (still in mike_server) ──
    should_search_web: Optional[_ShouldSearchFn] = None,
    search_web: Optional[_SearchWebFn] = None,
    format_web_results: Optional[_FormatWebFn] = None,
    # ── Search route functions (still in mike_server) ──
    search_routes_for_query: Optional[_SearchRoutesFn] = None,
    render_search_route_guidance: Optional[_RenderRouteGuidanceFn] = None,
    # ── Memory service singleton ──
    memory_service: Any = None,
) -> list:
    """Build the complete messages list for an LLM completion request.

    Thin wrapper in mike_server.py injects all server-local dependencies.
    """
    session_id = normalize_session_id(req.session_id)
    private_mode = _request_private_mode(req)
    raw_mode = _request_raw_mode(req)
    light_context = False if private_mode else _use_light_chat_context(req, last_user_msg)
    has_images = _has_images(req.messages)

    stats["last_memory_hits"] = 0
    stats["last_knowledge_hits"] = 0
    stats["last_web_hits"] = 0
    stats["last_search_routes"] = []

    if raw_mode:
        return [
            {"role": message.role, "content": message.content}
            for message in req.messages
        ]

    if has_images:
        return _build_vision_messages(req)

    # Extract last user message for constraint injection
    _last_user_for_prefix = ""
    for _m in reversed(req.messages):
        if _m.role == "user" and _m.content:
            _last_user_for_prefix = _m.content[:300]
            break

    if light_context and light_system_prompt:
        base_system_prompt = light_system_prompt()
    elif build_dynamic_prefix:
        base_system_prompt = system_prompt + "\n\n" + build_dynamic_prefix(_last_user_for_prefix)
    else:
        base_system_prompt = system_prompt

    if tool_instruction:
        base_system_prompt += "\n\n" + tool_instruction
        if FORCE_TOOL_USE:
            base_system_prompt += (
                "\n\n### INSTRUÇÃO DE ALTA PRIORIDADE ###\n"
                "Você DEVE usar uma das ferramentas acima para responder a este usuário, a menos que seja uma saudação simples.\n"
                "Sempre que houver dados externos (arquivos, web, email) envolvidos, EXECUTE A FERRAMENTA PRIMEIRO.\n"
            )

    messages: list[dict] = [{"role": "system", "content": base_system_prompt}]

    # Feature 3: Rich family profile — ALWAYS inject (even in light context)
    # This is critical: without it, Mike forgets who he's talking to.
    # Uses family_profiles.json directly, NOT PROFILE_CREDENTIALS (auth vs recognition).
    _ep_profile: Optional[str] = None
    if not private_mode and not raw_mode and get_family_profile and format_family_profile_for_llm:
        _cand = ""
        if "-" in session_id:
            _cand = session_id.split("-")[0]
        else:
            _cand = session_id
        _profile_data = get_family_profile(_cand)
        if _profile_data:
            _ep_profile = _cand
            _profile_block = format_family_profile_for_llm(_profile_data)
            messages.append({"role": "user", "content": _profile_block})

    # Episodic memory hits (dynamic, from past interactions) — only in full context
    if RAG_ENABLED and not private_mode and not raw_mode and not light_context:
        if _ep_profile and _ep_profile not in ("visitante",) and get_family_profile and memory_service:
            _ep_display = _ep_profile
            _fp = get_family_profile(_ep_profile)
            if _fp:
                _ep_display = _fp.get("name", _ep_profile)
            try:
                _ep_queries = [_ep_profile]
                if last_user_msg and last_user_msg.strip():
                    _ep_queries.append(last_user_msg)
                _ep_seen: set[str] = set()
                _ep_all: list = []
                for _eq in _ep_queries:
                    for _h in memory_service.search_memories(_eq, limit=3, session_only=False):
                        _key = _h.content[:200]
                        if _key not in _ep_seen:
                            _ep_seen.add(_key)
                            _ep_all.append(_h)
                _ep_hits = _ep_all[:5]
                if _ep_hits:
                    _ep_lines = [f"- {h.content[:300]}" for h in _ep_hits]
                    _ep_block = (
                        f"[Memórias recentes sobre {_ep_display}]\n"
                        + "\n".join(_ep_lines)
                    )
                    messages.append({"role": "user", "content": _ep_block})
            except Exception as e:
                log.warning("[chat_builder] Episodic memory fetch failed: %s", e)

    rag_stats = {"memory_hits": 0, "knowledge_hits": 0}
    rag_context = ""
    web_context = ""
    if RAG_ENABLED and last_user_msg and not raw_mode and not light_context and memory_service:
        rag_context, rag_stats = memory_service.build_context(
            last_user_msg,
            memory_limit=MEMORY_TOP_K,
            knowledge_limit=KNOWLEDGE_TOP_K,
            session_id=session_id,
            include_memories=not private_mode,
        )
        if WEB_SEARCH_ENABLED and should_search_web and search_web and format_web_results:
            if should_search_web(last_user_msg, rag_stats):
                from mike_web import _clean_query as _web_clean_query
                _search_query = _web_clean_query(last_user_msg) or last_user_msg
                web_results = search_web(_search_query)
                if web_results:
                    web_context = format_web_results(web_results)
        stats["last_memory_hits"] = rag_stats["memory_hits"]
        stats["last_knowledge_hits"] = rag_stats["knowledge_hits"]

        if SEARCH_ROUTE_HINTS and search_routes_for_query and render_search_route_guidance:
            routes = search_routes_for_query(
                last_user_msg,
                rag_stats,
                has_images=_has_images(req.messages),
                web_loaded=bool(web_context),
            )
            stats["last_search_routes"] = [route["route"] for route in routes]
            route_guidance = render_search_route_guidance(routes, rag_stats, web_loaded=bool(web_context))
            if route_guidance:
                messages.append({"role": "user", "content": route_guidance})

        # Contexto local e web injetados diretamente na ultima mensagem do user.
        # Gemma-4 ignora system messages inseridas apos o historico de conversa.
    if not RAG_ENABLED and SEARCH_ROUTE_HINTS and last_user_msg and not raw_mode and not light_context:
        if search_routes_for_query and render_search_route_guidance:
            routes = search_routes_for_query(
                last_user_msg,
                rag_stats,
                has_images=_has_images(req.messages),
                web_loaded=False,
            )
            stats["last_search_routes"] = [route["route"] for route in routes]
            route_guidance = render_search_route_guidance(routes, rag_stats)
            if route_guidance:
                messages.append({"role": "user", "content": route_guidance})

    if not private_mode and not raw_mode and not light_context and memory_service:
        # Inject compact summary of the previous session so Mike remembers
        # what was discussed even when the session_id changed (new dashboard login).
        try:
            prev_summary = memory_service.last_session_summary(session_id)
            if prev_summary:
                messages.append({"role": "user", "content": prev_summary})
        except Exception as e:
            log.warning("[chat_builder] Previous session summary fetch failed: %s", e)

        # Cap recent messages to avoid blowing the context window (32k ctx supports much more)
        _effective_recent = RECENT_MEMORY_LIMIT

        # If client sends history in req.messages, we skip DB loading to avoid duplication.
        # Otherwise, load recent turns from DB.
        if len(req.messages) > 1:
            recent = []
        else:
            recent = memory_service.recent_messages(
                session_id=session_id, limit=_effective_recent
            )
        messages.extend(recent)
    # Envia as mensagens do usuário para o LLM, limpando prefixos de chip de pesquisa
    from mike_web import _clean_query as _web_clean
    cleaned_req_messages = []
    for m in req.messages:
        if m.role == "user":
            if isinstance(m.content, str):
                cleaned_req_messages.append({"role": m.role, "content": _web_clean(m.content)})
            else:
                cleaned_parts = []
                for part in m.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        cleaned_parts.append({"type": "text", "text": _web_clean(part["text"])})
                    else:
                        cleaned_parts.append(part)
                cleaned_req_messages.append({"role": m.role, "content": cleaned_parts})
        elif m.role == "system":
            # Qwen3 rejects system messages outside position 0.
            # Client-supplied system messages are merged into the user turn as context.
            cleaned_req_messages.append({"role": "user", "content": m.content})
        else:
            cleaned_req_messages.append({"role": m.role, "content": m.content})
    # Injeta contexto web/RAG diretamente na ultima mensagem do user.
    # Gemma-4 ignora system messages depois do historico — injectar no user turn garante visibilidade.
    if web_context or rag_context:
        if web_context:
            rag_extra = f"\n\n[Contexto local]\n{rag_context}" if rag_context else ""
            ctx_block = (
                f"<dados_recuperados>\n"
                f"{web_context}"
                f"{rag_extra}\n"
                f"</dados_recuperados>\n\n"
            )
        else:
            ctx_block = f"<contexto_local>\n{rag_context}\n</contexto_local>\n\n"

        # Prefixar na ultima mensagem do user
        last_user_idx = None
        for i in range(len(cleaned_req_messages) - 1, -1, -1):
            if cleaned_req_messages[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is not None:
            orig = cleaned_req_messages[last_user_idx]["content"]
            if isinstance(orig, str):
                cleaned_req_messages[last_user_idx]["content"] = ctx_block + orig
            else:
                # multipart (vision) — prepend as text part
                cleaned_req_messages[last_user_idx]["content"] = [
                    {"type": "text", "text": ctx_block}
                ] + (orig if isinstance(orig, list) else [{"type": "text", "text": str(orig)}])
        else:
            messages.append({"role": "user", "content": ctx_block})
    messages.extend(cleaned_req_messages)

    # Truncamento defensivo: se a estimativa de tokens ultrapassar 85% do ctx_size,
    # remove mensagens do histórico (recentes primeiro a sair, preservando as mais novas)
    # até caber. Preserva sempre: [0]=system, [1]=tool_instruction (se houver), últimas 2 msgs.
    _token_budget = int(CTX_SIZE * 0.85)
    def _est_tokens(m: dict) -> int:
        c = m.get("content") or ""
        if isinstance(c, list):
            return sum(len(p.get("text", "")) // 4 for p in c if isinstance(p, dict))
        return len(c) // 4

    total_est = sum(_est_tokens(m) for m in messages)
    if total_est > _token_budget:
        # Índices fixos: system msgs no início (role==system), msgs da req no final
        # Remove mensagens do meio (histórico recente) exceto as 2 últimas do cliente
        preserve_tail = 2
        head = [m for m in messages if m.get("role") == "system"]
        tail = messages[-preserve_tail:] if len(messages) > preserve_tail else messages[:]
        middle = [m for m in messages[len(head):-preserve_tail] if m.get("role") != "system"]
        # Remove do início do middle até caber
        while middle and sum(_est_tokens(m) for m in head + middle + tail) > _token_budget:
            middle.pop(0)
        messages = head + middle + tail

    return messages
