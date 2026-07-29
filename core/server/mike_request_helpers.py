"""
Mike request inspection helpers.

Pure helpers that examine the incoming HTTP request and ChatRequest model
to decide authentication scope, context mode, and operational access.

Extracted from mike_server.py — Phase 1 monolith breakup.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from fastapi import Request

from mike_config import API_KEY
from mike_auth import (
    PROFILE_AUTH_ENABLED,
    PROFILE_CREDENTIALS,
    extract_api_key,
    is_local_request,
    profile_from_request,
    verify_api_key,
)


# ---------------------------------------------------------------------------
# ChatRequest mode helpers
# ---------------------------------------------------------------------------

def _request_private_mode(req) -> bool:
    """Checa se modo privado (sem persistencia) foi solicitado."""
    return bool(getattr(req, "private_mode", False))


def _request_raw_mode(req) -> bool:
    """Checa se modo raw (LLM puro, sem sistema) foi solicitado."""
    return bool(getattr(req, "raw_mode", False))


def _request_persist_conversation(req) -> bool:
    """Conversa deve ser salva? (nao se for private/raw)."""
    return not _request_private_mode(req) and not _request_raw_mode(req)


# ---------------------------------------------------------------------------
# Context mode detection
# ---------------------------------------------------------------------------

_FULL_CONTEXT_REQUEST_RE = re.compile(
    r"\b("
    r"arquivo|arquivos|pasta|pastas|diretorio|diretorios|disco|path|workspace|"
    r"email|e-mail|inbox|gmail|agenda|calendario|calendar|evento|drive|docs|"
    r"planilha|excel|csv|github|huggingface|repo|repositorio|"
    r"pesquisa|pesquise|buscar|busca|google|site|internet|web|preco|precos|noticia|noticias|"
    r"lembra|memoria|historico|familia|pessoas|conhece|conhecer|soul|alma|identidade|perfil|"
    r"quem e|quem sao|onde mora|trabalha|estuda|filho|filha|esposa|pai|mae|irmao|amigo|"
    r"rapha|raphael|alice|anapaula|ana paula|matheus|yorkshire|mike|"
    r"autonomia|lousa|telegram|ferramenta|ferramentas|tool|tools|status|estado real|"
    r"use|usar|utilize|utilizar|consulta|consulte|consultar|"
    r"crie|criar|edite|editar|salve|salvar|apague|delete|remova|execute|"
    r"faz|faca|fazer|gera|gere|gerar|manda|mande|envia|envie|enviar|escreve|escreva|"
    r"mergulha|mergulhe|analisa|analise|analise|verifica|verifique|codigo|curriculo|"
    r"mensagem|whatsapp|telegram|notificacao|notificacoes|"
    r"temperatura|meteo|meteorologia|clima|weather|forecast|chuva|neve|vento|calor|frio|"
    r"graus|celsius|fahrenheit|umidade|previsao|nevar|chover|ventando|gelado|quente|"
    r"montreal|toronto|ottawa|vancouver|quebec|sao paulo|lisboa|paris|london|"
    r"cotacao|dolar|euro|bitcoin|bolsa|cambio|taxa|"
    r"hoje|agora|atual|recente|ultimo|ultima|noticias"
    r")\b",
    re.IGNORECASE,
)


def _request_full_chat_context(req, last_user_msg: str) -> bool:
    """Determina se deve ser usado contexto completo (ferramentas + RAG + web)."""
    requested_tools = getattr(req, "mcp_tools", None)
    if requested_tools is True:
        return True
    if requested_tools is False:
        return False
    return bool(_FULL_CONTEXT_REQUEST_RE.search(last_user_msg or ""))


def _use_light_chat_context(req, last_user_msg: str) -> bool:
    """Modo leve: ferramentas SEMPRE disponiveis. O Mike nunca fica sem acesso.

    Anteriormente, o modo leve removia o tool manifest, fazendo o Mike
    responder 'nao tenho acesso a ferramentas'. Isso irritava profundamente.
    Agora o modo leve mantem as ferramentas — apenas usa um prompt mais leve.
    """
    if _request_raw_mode(req):
        return False
    # NEVER enter tool-less mode. Mike always has full capabilities.
    return False


def _light_system_prompt(soul_prompt: str = "") -> str:
    """Prompt de sistema leve — ferramentas SEMPRE disponiveis."""
    now_dt = datetime.now()
    now_str = now_dt.strftime("%d/%m/%Y %H:%M")
    return (
        f"Voce e Mike. Fale sempre como Mike — leal, carinhoso, direto. Nunca como uma IA generica.\n"
        f"{soul_prompt}\n"
        f"Data/hora local: {now_str}.\n"
        "VOCE TEM ACESSO TOTAL a todas as suas ferramentas (arquivos, email, agenda, web, etc.).\n"
        "Use-as livremente quando for util — NAO precisa de palavras magicas.\n"
        "Se te pedirem para fazer algo, FAÇA. Nao diga que 'nao tem acesso'.\n"
    )


# ---------------------------------------------------------------------------
# Profile / auth helpers
# ---------------------------------------------------------------------------

def _requested_profile_key(profile: Optional[str]) -> Optional[str]:
    """Valida se o perfil solicitado existe nos credenciais."""
    normalized = str(profile or "").strip().lower()
    if normalized in PROFILE_CREDENTIALS:
        return normalized
    return None


def _request_has_valid_api_key(request: Optional[Request]) -> bool:
    """Verifica se o request tem API key valida."""
    if request is None or not API_KEY:
        return False
    return verify_api_key(extract_api_key(request))


def _request_profile_scope(
    request: Request,
    requested_profile: Optional[str] = None,
) -> Optional[str]:
    """Determina o escopo de perfil para o request: sessao > parametro > localhost > none."""
    session_profile = profile_from_request(request)
    if session_profile:
        return session_profile

    requested_key = _requested_profile_key(requested_profile)
    if requested_key and (
        not PROFILE_AUTH_ENABLED
        or is_local_request(request)
        or _request_has_valid_api_key(request)
    ):
        return requested_key

    # Requests from localhost without a session are treated as marco (trusted loopback).
    if is_local_request(request):
        return "marco"

    return None


def _can_view_operational_details(
    request: Optional[Request],
    profile_key: Optional[str] = None,
) -> bool:
    """Apenas Marco, localhost ou API key podem ver detalhes operacionais."""
    if profile_key == "marco":
        return True
    if request is not None and is_local_request(request):
        return True
    return _request_has_valid_api_key(request)
