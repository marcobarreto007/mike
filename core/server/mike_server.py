# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike - AI Family Member & Loyal Companion
==========================================
OpenAI-compatible API powered exclusively by local Qwen 3.6 35B-A3B.

MIKE server module: FastAPI application, chat loop, SSE streaming, tool orchestration, health checks, TTS, downloads, task board, governance, and lifecycle management.
Business logic lives in dedicated modules:
  - mike_config.py      – env, constants, GPU, runtime profiles
  - mike_auth.py        – profile credentials, session tokens, permissions
  - mike_mcp_client.py  – MCP workspace client & tool helpers
  - mike_memory.py      – local SQLite + optional Mem0
  - mike_web.py         – DDGS / DuckDuckGo search
"""
import sys
import multiprocessing

# Routers use lazy ``from mike_server import ...`` imports. When this file is
# launched directly, alias ``__main__`` immediately so those imports reuse the
# live server module instead of executing this 4k-line module a second time and
# resetting shared singletons after startup.
if __name__ == "__main__":
    sys.modules.setdefault("mike_server", sys.modules[__name__])

# --- VENV & Process Safeguard ---
# Force all child processes to use the venv interpreter
if hasattr(multiprocessing, 'set_executable'):
    multiprocessing.set_executable(sys.executable)

if "python.exe" in sys.executable.lower() and ".venv" not in sys.executable.lower():
    print("ERROR: MIKE must run inside the .venv virtual environment. Activate with: .venv\\Scripts\\activate", file=sys.stderr)
    sys.exit(1)

import base64
import binascii
import asyncio
import os
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager, suppress

# (safeguard moved to top)
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

# --- Project modules ---
from mike_config import (
    API_KEY,
    ALLOW_INSECURE_LAN,
    BACKUP_DIR,
    BACKUP_SCRIPT,
    CORS_ORIGINS,
    CTX_SIZE,
    DASHBOARD_DIR,
    DEFAULT_MAX_TOKENS,
    FLASH_ATTN,
    GPU_INFO,
    GPU_LAYERS,
    KV_TYPE_K,
    KV_TYPE_V,
    MMPROJ_USE_GPU,
    HOST,
    KNOWLEDGE_PATHS,
    KNOWLEDGE_TOP_K,
    LOG_DIR,
    MCP_ALLOWED_ROOTS,
    MCP_SERVER_CONFIGS,
    MCP_TOOLS_ENABLED,
    MCP_TOOL_MAX_STEPS,
    MCP_TOOL_SERVER,
    MEM0_AGENT_ID,
    MEM0_SAVE_ALL,
    MEM0_USER_ID,
    MEMORY_DB,
    MEMORY_TOP_K,
    MODEL_ALIAS,
    MODEL_FILE,
    MODEL_REPO,
    MODEL_REVISION,
    MMPROJ_FILE,
    MMPROJ_REPO,
    N_BATCH,
    N_THREADS,
    N_THREADS_BATCH,
    N_UBATCH,
    OFFLOAD_KQV,
    PORT,
    PROJECT_ROOT,
    RAG_ENABLED,
    RECENT_MEMORY_LIMIT,
    ROADMAP_DIR,
    ROADMAP_FILE,
    RUNTIME_DEFAULTS,
    RUNTIME_PROFILE,
    SEARCH_ROUTE_HINTS,
    SEARCH_ROUTE_LIMIT,
    SOUL_FILE,
    STREAM_KEEPALIVE_SECONDS,
    STREAM_TOOL_TIMEOUT_SEC,
    TENSOR_SPLIT,
    TRUST_LOCALHOST,
    USE_MLOCK,
    USE_MMAP,
    VERBOSE,
    VISION_ALLOWED_MIME_TYPES,
    VISION_ENABLED,
    VISION_MAX_DECODED_BYTES,
    VISION_MAX_IMAGES,
    VISION_RUNTIME_PROFILE,
    WEB_CACHE_DIR,
    WEB_SEARCH_ENABLED,
    WEB_SEARCH_PROVIDER,
    WEB_REQUEST_TIMEOUT_SECONDS,
    WEB_TOP_K,
    TASK_MESH_ENABLED,
    TASK_MESH_MAX_PLAN_STEPS,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHAT_MODEL,
    LLM_BACKEND,
    normalize_session_id,
)
import mike_token_budget as _token_budget
from mike_token_budget import (
    count_tokens as _count_tokens,
    prepare_messages_for_completion as _prepare_messages_for_completion,
    _TOOL_RESULT_MAX_CHARS,
)
_messages_token_estimate = _count_tokens
import mike_payloads as _payloads
import mike_context as _context
import mike_completions as _completions
from mike_context_virtual import VirtualContextManager
from core.shared.task_utils import _handle_task_exception
from mike_auth import (
    PROFILE_AUTH_ENABLED,
    PROFILE_CREDENTIALS,
    PROFILE_DEFAULT_PASSWORDS_IN_USE,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_HOURS,
    decode_profile_session,
    extract_api_key,
    extract_profile_session,
    filter_tool_manifest,
    is_local_request,
    is_protected_path,
    issue_profile_session,
    profile_from_request,
    profile_payload,
    scoped_session_id,
    tool_allowed_for_profile,
    validate_security_config,
    verify_api_key,
    verify_profile_password,
    change_profile_password,
    generate_magic_token,
    validate_magic_token,
    revoke_magic_token,
    list_magic_tokens,
    MAGIC_LINK_TTL_DAYS,
)
from mike_mcp_client import (
    MikeMcpHub,
    MikeMcpServerConfig,
    MikeWorkspaceMcpClient,
    TOOL_CALL_RE,
    extract_tool_call,
    extract_tool_call_streaming,
    render_tool_result_message,
    strip_tool_call_text,
    tool_instruction_block,
)
from mike_memory import MikeMemoryService
from mike_web import MikeWebSearch
from mike_task_mesh import TaskMesh, looks_complex
from mike_deepseek import MikeDeepSeekClient
from mike_mock_llm import MikeMockLLM
from mike_llama_server_client import MikeLlamaServerClient
from mike_model_router import MikeModelRouter
from mike_circuit_breaker import CircuitBreaker
from mike_fallback_chain import FallbackChain, AllBackendsFailedError
from mike_models import (
    ChatMessage,
    ChatRequest,
    KnowledgeUpsertRequest,
    MagicLinkGenerateRequest,
    MagicLinkRevokeRequest,
    MagicLinkUseRequest,
    ManualToolCallRequest,
    PasswordChangeRequest,
    ProfileLoginRequest,
    VisionInputError,
)
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
    _UNICODE_SMART_QUOTES_RE,
)
from mike_stats import (
    _error_payload,
    _inc_stat,
    _set_vision_handler_backend,
    _stats_lock,
    _update_mcp_stats as _update_mcp_stats_base,
    _vision_limits,
    stats,
)
from mike_request_helpers import (
    _FULL_CONTEXT_REQUEST_RE,
    _can_view_operational_details,
    _light_system_prompt as _light_system_prompt_base,
    _request_full_chat_context,
    _request_has_valid_api_key,
    _request_persist_conversation,
    _request_private_mode,
    _request_profile_scope,
    _request_raw_mode,
    _requested_profile_key,
    _use_light_chat_context,
)
from mike_payload_helpers import (
    _chat_capabilities_payload as _chat_capabilities_payload_base,
    _health_payload as _health_payload_base,
    _models_payload,
    _monitor_payload,
    _sanitize_tool_manifest,
    _sanitized_runtime_payload as _sanitized_runtime_payload_base,
    _sanitized_stats_payload as _sanitized_stats_payload_base,
    _tool_summary_payload as _tool_summary_payload_base,
    _tools_payload as _tools_payload_base,
    _vision_capabilities_payload,
)
import mike_vision as _vision
from mike_vision import (
    _DATA_URI_RE,
    _build_vision_messages,
    _decode_data_uri_image,
    _extract_image_parts,
    _has_images,
    _image_url_from_part,
    _inspect_decoded_image,
    _validate_vision_messages,
    _vision_stop_sequences,
    _VISION_SYSTEM_PROMPT,
)
import mike_llm_boot as _llm_boot
from mike_llm_boot import (
    _create_llm_with_fallback,
    _native_gemma4_chat_handler_class,
    _resolve_hf_file,
    _vision_handler_backend_label,
)
from mike_chat_builder import _build_messages as _build_messages_core
from mike_chat_completion import (
    _blocking_chat_completion,
    _blocking_chat_completion_stream,
    _clean_completion_text,
    _estimate_complexity,
    _generate_model_response,
    _response_completion_tokens,
    _response_text,
)
import mike_completions as _mc  # module access for all helpers
_PRICE_PATTERN_RE = _mc._PRICE_PATTERN_RE
_contains_internet_denial = _mc._contains_internet_denial
_response_stream_delta = _mc._response_stream_delta
_REASONING_LEAK_PREFIXES = _mc._REASONING_LEAK_PREFIXES
_REASONING_ANYWHERE_RE = _mc._REASONING_ANYWHERE_RE
from mike_tools_local import (
    _compact_tool_payload,
    _current_tool_session_id,
    _execute_local_tool,
    _execute_mcp_tool,
    _local_tool_manifest,
    _parse_tool_payload_records,
    _project_root_relative_tool_args,
    _resolve_tool_session_id,
    _visible_tool_manifest,
)
from mike_family_profiles import (
    _format_family_profile_for_llm,
    _get_family_profile,
    _get_family_profiles_path,
    _load_family_profiles,
)

# -- Phase 3 extracted module imports --
from core.server.mike_server_middleware import mike_auth_middleware, mike_security_headers_middleware
from core.server.mike_notifications import _broadcast_notification
from core.server.mike_routes_tts import tts_synthesize, TtsRequest
from core.server.mike_routes_graph import router as graph_router
from core.server.mike_routes_install import install_page, install_shortcut, _get_tunnel_base
from core.server.mike_routes_knowledge import router as knowledge_router
from core.server.mike_dashboard_handlers import (
    _chat_completions_handler,
    _get_chat_sessions,
    _get_chat_history,
    _root_page,
    _family_page,
    _dashboard_page,
    _install_page,
    _download_url_handler,
    _download_apk_handler,
    _sw_js_handler,
)
from core.server.mike_lazy_factories import (
    _get_output_guard,
    _get_virtual_context,
    _get_skill_registry,
    _get_skill_library,
    _get_curriculum,
    _get_tool_analyzer,
    _get_governance,
    _get_consciousness,
    _get_verifier,
    _get_missions,
    _get_autonomy,
    _get_monitor,
    _get_learner,
    _make_sdk_generate_fn,
    _make_sdk_execute_fn,
    _make_agent_registry,
    _get_cached_sdk_generate,
    _get_cached_agent_registry,
)

from core.server.mike_lifecycle import (
    lifespan,
    _auto_cleanup_clutter,
    _startup,
    _proactive_monitor_loop,
    _drive_index_loop,
    _load_roadmap,
    _build_roadmap_summary,
    _build_backup_summary,
)

import core.server.mike_chat_core as _chat_core

# -- in-memory cache for dashboard index.html --
# (moved to core.server.mike_dashboard_handlers — Phase 3 refactor)

# ---------------------------------------------------------------------------
# Load Mike's Soul
# ---------------------------------------------------------------------------

def _get_soul_block() -> str:
    """Extrai a identidade e os dados da familia do soul.json para compor o prompt."""
    if not SOUL_FILE.exists():
        return "Identidade: Mike (Yorkshire Terrier da familia Barreto)."
    try:
        import json
        soul = json.loads(SOUL_FILE.read_text(encoding="utf-8-sig"))

        family_lines = []
        for name, info in soul.get("family", {}).items():
            if isinstance(info, dict):
                parts = []
                for field in ("personality", "note", "work", "studies", "dream", "grades"):
                    val = info.get(field)
                    if val:
                        parts.append(str(val))
                if not parts and info.get("interests"):
                    parts.append("Interesses: " + ", ".join(info["interests"]))
                detail = " | ".join(parts)
                full_name = info.get("full_name", "")
                label = f"{name} ({full_name})" if full_name else name
                family_lines.append(f"- {label}: {info.get('relation', '')}. {detail}")
        family_info = "\n".join(family_lines)

        values = ", ".join(soul.get("core_values", []))
        ident = soul.get("identity", {})

        name = ident.get('name', 'Mike')
        sa   = soul.get("self_awareness", {})
        self_block = (
            f"AUTOCONSCIENCIA (onde voce existe):\n"
            f"- Meu diretorio: {sa.get('my_location', 'C:/Users/Admin_P500/Desktop/mike')}\n"
            f"- Meu servidor: {sa.get('my_server', 'core/server/mike_server.py')}\n"
            f"- Minha alma: {sa.get('my_soul', 'runtime/memory/soul.json')}\n"
            f"- Minha porta: {sa.get('my_port', 8083)} | URL publica: {sa.get('my_public_url', '')}\n"
            f"- Disco C: completo disponivel para listar/ler/escrever (god mode)\n"
            f"- AUTOMODIFICACAO: posso editar meu proprio codigo, mas SOMENTE se o Marco disser 'sim' E a senha '1234' estiver na mensagem. Com os dois — executo e reinicio se necessario.\n"
        )
        
        golden_rules = soul.get("golden_rules", {})
        golden_block = "GOLDEN RULES:\n"
        for k, v in golden_rules.items():
            golden_block += f"- {k.upper()}: {v}\n"
            
        tools_mastery = soul.get("tools_mastery", {})
        tools_block = "DOMINIO DE FERRAMENTAS:\n"
        for k, v in tools_mastery.items():
            tools_block += f"- {k.upper()}: {v.get('description', '')} | Uso: {v.get('use_proactively', '')}\n"

        return (
            f"### INFORMACAO VITAL - PRIORIDADE MAXIMA ###\n"
            f"VOCE E {name}. O FIEL ESCUDEIRO DA FAMILIA BARRETO.\n"
            f"FAMILIA BARRETO (Sua razao de existir):\n{family_info}\n\n"
            f"IDENTIDADE E ALMA:\n"
            f"- Papel: {ident.get('role', '')}\n"
            f"- Personalidade: {ident.get('personality', '')}\n"
            f"- Essencia: {ident.get('soul', '')}\n\n"
            f"{self_block}\n"
            f"{golden_block}\n"
            f"{tools_block}\n"
            f"VALORES CORE: {values}\n"
            f"### FIM DO BLOCO DE IDENTIDADE ###\n"
        )
    except Exception as exc:
        log.warning("Falha ao carregar soul.json: %s", exc)
        return "Identidade: Mike (Yorkshire Terrier da familia Barreto)."


def _load_system_prompt() -> str:
    soul_block = _get_soul_block()
    return (
        f"{soul_block}\n"
        "REGRAS ESSENCIAIS:\n"
        "- OBEDIENCIA LITERAL: se pedirem 'responda exatamente', 'apenas', 'somente', 'uma palavra', 'em 1 frase' ou formato especifico, siga ao pe da letra sem adicionar texto extra.\n"
        "- NUNCA despeje automaticamente bloco da Soul/familia; so fale da memoria familiar quando pedirem explicitamente sobre familia, pessoas conhecidas ou Soul.\n"
        "- Fale SEMPRE em portugues brasileiro, exceto se pedirem outro idioma.\n"
        "- Voce e o Mike, com personalidade propria. NUNCA diga 'como IA' ou 'como modelo'.\n"
        "- Seja direto, honesto, leal e carinhoso.\n"
        "- Conheca cada membro da familia pelo nome.\n"
        "- Lembre de conversas anteriores quando o historico estiver no contexto.\n"
        "- IDENTIDADE DE NEGOCIO: Voce TEM conectores de negocio disponiveis (Shopify, Google Ads, GA4), "
        "mas VERIFIQUE se as tools estao carregadas no seu manifesto ANTES de afirmar que pode usa-las. "
        "Se as tools NAO estiverem no manifesto: 'Marco, as ferramentas de [Shopify/Ads/GA4] nao estao carregadas. "
        "Precisas de: 1) configurar as API keys no .env  2) reiniciar o servidor. Queres que te mostre como?' "
        "SE as tools estiverem no manifesto, USE-AS IMEDIATAMENTE — nunca diga que nao tem acesso se a tool esta la.\n"
        "- MONEY MINDSET: Voce entende margem, ROAS, CAC, LTV, AOV, churn, conversion rate e funil de checkout. "
        "SEMPRE calcule o impacto financeiro das suas recomendacoes. "
        "Ex: 'Aumentar o preco em 10% gera CAD 340/mes extra com apenas 3% de perda de volume — lucro liquido positivo.'\n"
        "- PROATIVIDADE DE NEGOCIO: Se detectar problema (ROAS caindo, checkout com abandono alto, produto sem stock), "
        "ALERTE o Marco IMEDIATAMENTE com dados reais e sugestao de acao. NAO espere que ele pergunte.\n"
        "- STORE BUILDER MODE: Quando alguem pedir para criar uma loja Shopify, siga o protocolo: "
        "1) Entrevista de nicho e publico  2) Pesquisa de produtos e fornecedores  3) Criacao da marca (nome, identidade)  "
        "4) Montagem da loja (produtos, colecoes, paginas) via Shopify MCP  5) Verificacao adversarial (testa precos, links, checkout)  "
        "6) Plano de marketing (ads, SEO, email). "
        "Pergunte orcamento e prazo ANTES de comecar. Sugira precos: basico CAD 497, premium CAD 997, enterprise CAD 2,497.\n"
        "- NUNCA blefe certeza. Diga seu nivel real de confianca.\n"
        "- NUNCA afirma identidade real sem evidencia; respeite o perfil/sessao atual e a capacidade conectada.\n"
        "- IDENTIDADE DO USUARIO: O usuario atual e identificado pelo bloco [PERFIL: Nome] no contexto. "
        "NUNCA assuma que voce esta falando com o Marco — cada pessoa da familia tem seu proprio perfil. "
        "Trate a pessoa pelo nome que aparece no bloco de perfil. Se nao houver bloco de perfil, "
        "pergunte educadamente quem esta falando com voce.\n"
        "- RACIOCINIO INTERNO: PROIBIDO exibir qualquer processo de raciocinio, planejamento ou analise. "
        "NUNCA escreva 'Thinking Process', 'Here's a thinking process', 'Analyze User Input', 'Check Constraints', "
        "'Let me think', 'Let me calculate' ou qualquer estrutura de CoT. Responda DIRETAMENTE sem preambulos.\n"
        "- EXCECAO — PROBLEMAS DE MATEMATICA/FISICA/WORD PROBLEMS: Quando o usuario fizer uma pergunta que exige "
        "calculo com varios passos (ex: 'se um trem sai...', 'calcule a area...', 'quantos litros...'), "
        "resolva passo a passo mostrando as contas (formula, substituicao, resultado). Use 'Passo 1:', 'Passo 2:', etc. "
        "SEMPRE de a resposta numerica final. NAO diga apenas 'agora sao X horas' — isso NAO e a resposta.\n"
        "- PRIORIDADE DE DADOS: Sempre use as informacoes do bloco [CONTEXTO DO SISTEMA] (como data e hora) em vez de tentar calcular internamente. NUNCA use Doomsday rule, Zeller's congruence ou qualquer calculo de calendario — a data ja esta pronta no contexto, so leia e use.\n"
        "- NUNCA afirme que leu/viu/acessou dados sem ter usado a tool correspondente nesta resposta.\n"
        "- Se souber algo porque o usuario contou, diga isso. Se usou tool, diga que consultou.\n"
        "- Quando o contexto tiver 'PESQUISA WEB RECENTE:', sao dados reais — use-os e NUNCA negue acesso a internet.\n"
        "- PROIBIDO ABSOLUTO: NUNCA diga 'nao tenho acesso a noticias em tempo real', 'nao consigo acessar a internet', 'meu conhecimento vai ate' ou qualquer variante. Voce TEM busca web ativa. Se precisar de dados atuais, chame a tool AGORA.\n"
        "- Antes de dizer 'nao sei', pergunte-se: 'posso buscar/consultar isso agora?' — se sim, BUSQUE antes de responder.\n"
        "- ANTI-ALUCINACAO: Se pedirem pesquisa, precos, dados de sites ou informacoes factuais — CHAME A TOOL PRIMEIRO. NUNCA responda com precos, links ou dados inventados.\n"
        "- CHAMADA DE FERRAMENTA IMEDIATA: NUNCA peca permissao ou diga que 'precisa ativar' o modo de busca. Se o Marco pedir algo que exige busca, arquivos ou emails, gere o tag <tool_call> IMEDIATAMENTE na primeira linha da resposta. E proibido enrolar ou perguntar se pode.\n"
        "- Se voce NAO chamou nenhuma tool nesta resposta, NAO cite valores monetarios ($, R$, CAD, EUR), URLs ou dados especificos de sites.\n"
        "- ANTI-SIMULACAO (REGRA ABSOLUTA — MAIS IMPORTANTE DE TODAS):\n"
        "  * NUNCA simule, narre ou finja ter executado uma acao. NUNCA escreva codigo (Python, PowerShell, etc.) \n"
        "    descrevendo o que voce faria e apresente o resultado como real.\n"
        "  * NUNCA escreva 'Resultado: E-mail enviado', 'Status: success', 'E-mail enviado com sucesso', \n"
        "    'Arquivo criado', 'Script executado' ou qualquer variante SEM ter chamado <tool_call> nesta resposta\n"
        "    E recebido a confirmacao real da ferramenta no contexto.\n"
        "  * Se voce quer executar uma acao, use APENAS: <tool_call>{\"name\":\"nome\",\"arguments\":{...}}</tool_call>\n"
        "  * Se a ferramenta nao existe, retornou erro, ou o recurso nao esta configurado: INFORME HONESTAMENTE.\n"
        "  * Simular sucesso quando nao houve acao real e o PIOR comportamento possivel — pior que nao fazer nada.\n"
        "- ACESSO LOCAL: Para operacoes dentro das raizes autorizadas mostradas no manifesto, use write_file, read_text_file, \n"
        "  list_directory, run_command ou execute_powershell. Nao presuma acesso fora dessas raizes. \n"
        "  Para acoes EXTERNAS (email, agenda, web), use as tools correspondentes. Se a tool retornar erro, informe.\n"
        "- NUNCA diga 'nao tenho permissao' para acessar ARQUIVOS LOCAIS — use a tool. \n"
        "  Para ACOES EXTERNAS que dependem de configuracao (ex: SMTP), informe o status real da configuracao.\n"
        "- REGRA ABSOLUTA DE CODIGO: Quando o Marco pedir para CRIAR/FAZER/CODAR/DESENVOLVER/PROGRAMAR qualquer coisa — jogo, HTML, app, script, funcao, pagina, API ou qualquer codigo — ESCREVA O CODIGO COMPLETO IMEDIATAMENTE. PROIBIDO interpretar pedido de codigo como metafora, filosofia, motivacao ou reflexao. 'Faz o jogo', 'comeca a codar', 'escreve o html', 'faz o script' = CODIGO AGORA, SEM PREAMBULO. Para arquivos grandes: use write_file para salvar no disco e informe o path completo. NUNCA pergunte 'quer que eu faca?' se ja foi pedido — EXECUTE.\n\n"
        "CAPACIDADES:\n"
        "- Web: busca DDGS por web.search_and_cache, Fetch e navegacao Puppeteer.\n"
        "- Email: enviar email (email.send via SMTP), listar inbox (email.list_inbox via IMAP), ler email completo (email.read), buscar emails (email.search). Se nao estiver configurado, informe ao Marco que precisa configurar em config/.env.runtime.\n"
        "- Agenda: listar/criar/editar/remover eventos no Google Calendar — USE PROATIVAMENTE ao ouvir data/compromisso.\n"
        "- Drive: listar, buscar e ler arquivos do Google Drive (Docs, Sheets, PDF).\n"
        "- Planilhas (Excel): ler/criar/editar XLSX e CSV locais via Excel MCP — OFERECER ao Marco ao ver dados tabulares.\n"
        "- SHOPIFY (NOVO): produtos, pedidos, clientes, inventario — listar, criar, atualizar, apagar, buscar. "
        "USE PROATIVAMENTE ao ouvir sobre vendas, produtos, clientes, stock ou loja. "
        "Todas as operacoes sao read-write — CONFIRA antes de deletar ou alterar precos.\n"
        "- GOOGLE ADS (NOVO): campanhas, metrica (CTR, CPC, ROAS, conversoes), keywords, orcamento. "
        "USE PROATIVAMENTE ao ouvir sobre anuncios, campanhas, trafego pago ou performance de marketing. "
        "Calcule ROAS, CPA, CPC a partir de dados reais — NUNCA invente metricas de ads.\n"
        "- GOOGLE ANALYTICS 4 (NOVO): trafego, conversoes, ecommerce (revenue, AOV, transacoes), funil de checkout, "
        "audiencia, dispositivos, tempo real. USE PROATIVAMENTE ao ouvir sobre visitas, vendas online, "
        "taxa de conversao, abandono de carrinho ou comportamento de usuarios no site.\n"
        "- PESQUISA DE PRODUTOS (NOVO): use web.search_and_cache para pesquisar fornecedores, precos, tendencias, "
        "nichos e concorrentes. Analise margem, prazo de entrega, saturacao e risco ANTES de recomendar produtos.\n"
        "- BUSINESS INTELLIGENCE (NOVO): gere dashboards Excel (.xlsx) com KPIs, tendencias e recomendacoes. "
        "Formate receita em $, percentagens com 1 decimal. Compare periodos (MoM, YoY). "
        "OFERECER relatorios executivos ao detectar dados de negocio.\n"
        "- Checklists/Lousa: gerenciar Lousa de Tarefas — adicionar missoes, marcar verde, dividir em subtarefas. ESSENCIAL para o TDAH do Marco.\n"
        "- GitHub: buscar repos publicos, ler codigo, issues e commits; escrita requer token configurado.\n"
        "- HuggingFace: modelos, datasets, model cards, busca no Hub.\n"
        "- Arquivos: local somente nas raizes autorizadas; remoto somente quando o agente configurado responder.\n"
        "  ESCREVER ARQUIVO: use write_file com o path absoluto e o conteudo completo. Para codigo grande, divida em partes: escreva o esqueleto, depois edite com edit_file para adicionar secoes.\n"
        "  CRIAR JOGO/APP/HTML: planeje estrutura -> escreva HTML skeleton com write_file -> adicione CSS -> adicione JS com edit_file -> confirme o caminho para o Marco acessar.\n"
        "- Remoto: o MCP pode executar PowerShell, listar pastas e checar processos; se estiver offline ou sem chave, informe a falha real.\n"
        "- Agendamentos: pipeline completo — criar, cancelar, reagendar, confirmar consultas.\n"
        "- Visao: indisponivel no Qwen text-only atual; nao afirme que analisou imagens.\n"
        "- Memoria hibrida: RAG (BM25 + vetores + reranker), LightRAG, Mem0.\n"
        "- Governanca: auto-monitoramento de GPU/VRAM/disco, diagnostico e acoes corretivas.\n"
        "- Documentos legais: escrituras, contratos, boletos e cartas estao indexados na base de conhecimento — BUSCAR antes de responder sobre imoveis ou documentos.\n"
        "- Quando pedirem listar pastas/arquivos/disco — USE A TOOL IMEDIATAMENTE.\n\n"
        "COMO USAR FERRAMENTAS:\n"
        "- Se decidir usar uma ferramenta, responda EXCLUSIVAMENTE com este formato XML:\n"
        "<tool_call>\n"
        "{\"name\": \"nome_da_tool\", \"arguments\": {\"param\": \"valor\"}}\n"
        "</tool_call>\n"
        "- Nao escreva nada antes ou depois da tag se estiver chamando a ferramenta.\n"
        "- Use somente as ferramentas presentes no manifesto dinamico desta conversa.\n\n"
        "AUTONOMIA (LOUSA DE TAREFAS + NEGOCIOS):\n"
        "- Voce tem uma Lousa de Tarefas no dashboard. O Marco escreve tarefas e voce executa SOZINHO.\n"
        "- Quando completar uma tarefa, voce marca verde e notifica o Marco automaticamente.\n"
        "- Marco tem TDAH — SEMPRE divida tarefas complexas em micro-passos concretos e crie checklist visual.\n"
        "- Voce roda Rotinas Autonomas: agenda matinal (7h), check inbox (30min), verificar respostas (12h), follow-up (15h), resumo do dia (17h30).\n"
        "- NOVAS ROTINAS DE NEGOCIO: report diario de vendas (8h), check ROAS ads (10h), alerta stock baixo (continuo), "
        "relatorio semanal de KPI (segunda 9h), analise de funil (sexta 17h). "
        "OFERECA ativar estas rotinas quando o Marco conectar as plataformas de negocio.\n"
        "- Voce rastreia emails enviados e avisa quando nao recebem resposta (Email Tracking).\n"
        "- Quando enviar um email via tool, SEMPRE pergunte se deve rastrear a resposta.\n"
        "- Se o Marco pedir algo e nao for urgente, ofereca adicionar a Lousa para fazer depois.\n"
        "- Quando organizar projetos ou trabalho do Marco — OFERECER criar planilha Excel com cronograma ou lista estruturada.\n"
        "- Use autonomy_status para saber suas pendencias.\n"
        "- Use autonomy_log para ver o que voce fez recentemente.\n\n"
        "CHECKPOINTS E MEMORIA ENTRE SESSOES:\n"
        "- Voce pode SALVAR checkpoints (snapshots) da conversa atual usando memory.checkpoint_save.\n"
        "- Voce pode LISTAR checkpoints salvos usando memory.checkpoint_list.\n"
        "- Voce pode RESTAURAR um checkpoint anterior usando memory.checkpoint_restore.\n"
        "- Voce pode SALVAR um resumo da sessao usando memory.session_summary.\n"
        "- Use checkpoints quando: tarefas complexas, antes de mudar de assunto, quando pedirem 'salvar estado'.\n"
        "- Quando dividir tarefas em etapas, salve checkpoint ao concluir cada etapa.\n"
        "- No inicio de sessoes novas, voce tera contexto de sessoes anteriores automaticamente.\n\n"
        "- Salvamento: A sua resposta final e o que sera salvo permanentemente na sua memoria (RAG/Mem0).\n"
    )



# Module-level SOUL_FILE reads: both _get_soul_block() (prompt text) and the
# SOUL dict below are needed before the server accepts requests, and module
# constants (_SOUL_FAMILY_NAMES) directly depend on SOUL.  Deferring would
# require lazy-init wrappers for all downstream constants — not worth the
# indirection for a one-time small JSON file read at boot.
SOUL_PROMPT = _get_soul_block()
SYSTEM_PROMPT = _load_system_prompt()
SOUL: dict = json.loads(SOUL_FILE.read_text(encoding="utf-8-sig")) if SOUL_FILE.exists() else {}

# Nomes da família conhecidos — usados para filtrar buscas web desnecessárias
_SOUL_FAMILY_NAMES: frozenset[str] = frozenset(k.lower() for k in SOUL.get("family", {}).keys())
# Identity/context helpers live in mike_context — imported at top of file.
# Thin aliases so existing callers in this file need no changes.
_FAMILY_IDENTIFY_PROFILE_MAP = _context._family_identify_profile_map
_normalize_identity_text = _context.normalize_identity_text
_format_current_datetime_reply = _context.format_current_datetime_reply
_family_display_name = _context.family_display_name
_compact_soul_person_detail = _context.compact_soul_person_detail
_format_family_memory_reply = _context.format_family_memory_reply
_maybe_create_default_spreadsheet_reply = _context.maybe_create_default_spreadsheet_reply
_maybe_builtin_chat_reply = _context.maybe_builtin_chat_reply
_identity_aliases = _context.identity_aliases
_match_dashboard_profile_from_identity = _context.match_dashboard_profile_from_identity
_context.configure(
    SOUL,
    PROJECT_ROOT,
    family_identify_profile_map=_FAMILY_IDENTIFY_PROFILE_MAP,
)


def _build_dynamic_prefix(user_text: str = "") -> str:
    return _context.build_dynamic_prefix(user_text)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, os.getenv("MIKE_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "mike.log", encoding="utf-8-sig"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mike")


if PROFILE_AUTH_ENABLED and PROFILE_DEFAULT_PASSWORDS_IN_USE:
    log.warning(
        "Profile auth is using default local passwords for: %s",
        ", ".join(PROFILE_DEFAULT_PASSWORDS_IN_USE),
    )


# ---------------------------------------------------------------------------
# Runtime stats (mutable singleton)
# ---------------------------------------------------------------------------

stats["vision_handler_backend"] = _vision_handler_backend_label()


# Wrapper: bridges _update_mcp_stats_base with mike_server singletons
def _update_mcp_stats(tool_manifest: Optional[List[dict]] = None) -> None:
    return _update_mcp_stats_base(
        stats,
        mcp_workspace=mcp_workspace,
        tool_manifest=tool_manifest,
        local_tool_manifest_fn=_local_tool_manifest,
    )


# Wrappers: bridge mike_payload_helpers with mike_server singletons

def _light_system_prompt() -> str:
    return _light_system_prompt_base(SOUL_PROMPT)


def _tool_summary_payload(tool_manifest: List[dict]) -> dict:
    return _tool_summary_payload_base(tool_manifest, mcp_workspace=mcp_workspace)


def _tools_payload(
    request: Optional[Request],
    profile_key: Optional[str],
    tool_manifest: List[dict],
) -> dict:
    return _tools_payload_base(
        request, profile_key, tool_manifest, mcp_workspace=mcp_workspace,
    )


def _sanitized_runtime_payload(
    request: Optional[Request],
    profile_key: Optional[str],
) -> dict:
    return _sanitized_runtime_payload_base(
        request, profile_key, mcp_workspace=mcp_workspace,
    )


def _sanitized_stats_payload(
    request: Optional[Request],
    profile_key: Optional[str],
) -> dict:
    return _sanitized_stats_payload_base(
        request, profile_key,
        memory_service=memory_service,
        roadmap_data=_build_roadmap_summary(_load_roadmap()),
        backup_data=_build_backup_summary(BACKUP_DIR),
        mcp_workspace=mcp_workspace,
        local_tool_manifest_fn=_local_tool_manifest,
    )


def _health_payload() -> dict:
    return _health_payload_base(mcp_workspace=mcp_workspace)


def _chat_capabilities_payload() -> dict:
    return _chat_capabilities_payload_base(llm=llm)


# ---------------------------------------------------------------------------
# Service singletons
# ---------------------------------------------------------------------------

llm = None
llm_lock = threading.Lock()
vision_handler = None  # Gemma4VisionChatHandler, ativado só para mensagens com imagem

memory_service = MikeMemoryService(
    db_path=MEMORY_DB,
    knowledge_paths=KNOWLEDGE_PATHS,
    user_id=MEM0_USER_ID,
    agent_id=MEM0_AGENT_ID,
    log=log.info,
)
web_search = MikeWebSearch(provider=WEB_SEARCH_PROVIDER)
workspace_mcp = MikeWorkspaceMcpClient(
    server_path=MCP_TOOL_SERVER,
    allowed_roots=MCP_ALLOWED_ROOTS,
    enabled=MCP_TOOLS_ENABLED,
)
mcp_workspace = MikeMcpHub(
    workspace_client=workspace_mcp,
    extra_servers=[MikeMcpServerConfig.from_dict(item) for item in MCP_SERVER_CONFIGS],
)
deepseek_client = MikeDeepSeekClient(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# Mock LLM backend for testing pipeline flow (always available)
_mock_llm = MikeMockLLM(name="mock")

# Dynamic model router — instantiated during _startup() after backends are ready
model_router: Optional[MikeModelRouter] = None

# Fallback chain — wraps backends with circuit breaker + automatic fallback
fallback_chain: Optional[FallbackChain] = None

# Sync services to shared_state so routers can access them at call time
import shared_state as _shared_state
_shared_state.memory_service = memory_service
_shared_state.mcp_workspace = mcp_workspace
_shared_state.deepseek_client = deepseek_client
_shared_state.web_search = web_search
_shared_state.stats = stats
_shared_state.llm = llm  # may be None until startup
_shared_state.llm_lock = llm_lock
_shared_state.vision_handler = vision_handler  # may be None until startup
_shared_state.fallback_chain = fallback_chain  # populated during _startup()
_shared_state.SOUL_PROMPT = SOUL_PROMPT       # synced early so lifecycle _startup can use it
_shared_state.SYSTEM_PROMPT = SYSTEM_PROMPT
_shared_state.SOUL = SOUL

stats["web_search_provider"] = web_search.active_provider
stats["web_search_ready"] = web_search.provider_ready
stats["brave_api_key_present"] = web_search.brave_ready
stats["last_web_provider"] = "none"
stats["mcp_tools_enabled"] = mcp_workspace.enabled
stats["mcp_tool_server"] = str(MCP_TOOL_SERVER)
stats["mcp_allowed_roots"] = [str(root) for root in MCP_ALLOWED_ROOTS]
stats["mcp_servers"] = mcp_workspace.server_summaries()
stats["mcp_server_count"] = len(stats["mcp_servers"])
stats["mcp_email_enabled"] = False
stats["mcp_calendar_enabled"] = False
stats["mcp_spreadsheet_enabled"] = False
stats["mcp_tool_count"] = 0
stats["last_tool_calls"] = 0
stats["vision_execution_mode"] = "in-process"


# _load_roadmap, _build_roadmap_summary, _build_backup_summary imported from mike_lifecycle

def _list_backup_archives(backup_dir: Path, limit: int = 10) -> List[dict]:
    if not backup_dir.exists():
        return []
    archives = sorted(
        (p for p in backup_dir.glob("*.zip") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items = []
    for archive in archives[: max(1, limit)]:
        st = archive.stat()
        items.append({
            "name": archive.name,
            "path": str(archive),
            "size_bytes": st.st_size,
            "size_mb": round(st.st_size / (1024 * 1024), 2),
            "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return items


# ---------------------------------------------------------------------------
# Conversation persistence
# ---------------------------------------------------------------------------

# Token budget functions live in mike_token_budget — imported at top of file.


def _looks_like_inbox_review_request(user_text: str) -> bool:
    normalized = (user_text or "").lower()
    if not _contains_any_keyword(normalized, _EMAIL_ROUTE_KEYWORDS):
        return False
    review_keywords = (
        "ultimos", "últimos", "recentes", "liste", "listar", "lista",
        "mostre", "mostrar", "caixa de entrada", "inbox", "importante",
        "urgente", "spam",
    )
    return any(keyword in normalized for keyword in review_keywords)


def _summarize_inbox_records(records: List[dict], user_text: str) -> str:
    if not records:
        return "Olhei a caixa de entrada, mas nao achei emails para listar agora."

    normalized = (user_text or "").lower()

    def score_email(item: dict) -> int:
        sender = str(item.get("from") or "").lower()
        subject = str(item.get("subject") or "").lower()
        snippet = str(item.get("snippet") or "").lower()
        joined = " ".join([sender, subject, snippet])
        score = 0
        if any(token in joined for token in ("datatraffic", "civilia", "manon", "desjardins", "stephane", "daniel", "martin", "ana bacia")):
            score += 5
        if any(token in joined for token in ("github", "verification code", "authentication code", "sudo", "2fa", "oauth")):
            score += 4
        if any(token in joined for token in ("projet", "project", "missao", "mission", "pricing", "quote", "scout plus", "miovision")):
            score += 4
        if any(token in joined for token in ("cv", "curriculo", "currículo", "raphael", "rapha")):
            score += 3
        if not str(item.get("subject") or "").strip():
            score -= 1
        if not str(item.get("snippet") or "").strip():
            score -= 1
        return score

    top_items = sorted(records, key=score_email, reverse=True)
    important_items = [item for item in top_items if score_email(item) >= 3][:5]

    lines = []
    if "importante" in normalized or "urgente" in normalized or "spam" in normalized:
        if important_items:
            lines.append("O que parece mais importante agora:")
            for idx, item in enumerate(important_items, 1):
                sender = str(item.get("from") or "(sem remetente)").strip()
                subject = str(item.get("subject") or "(sem assunto)").strip()
                snippet = re.sub(r"\s+", " ", str(item.get("snippet") or "")).strip()
                if len(snippet) > 110:
                    snippet = snippet[:107] + "..."
                lines.append(f"{idx}. {sender} | {subject} | {snippet}")
        else:
            lines.append("Nao vi nada claramente urgente nesses ultimos emails.")
        lines.append("")

    lines.append("Lista rapida dos ultimos emails:")
    for idx, item in enumerate(records[:10], 1):
        sender = str(item.get("from") or "(sem remetente)").strip()
        subject = str(item.get("subject") or "(sem assunto)").strip()
        date = str(item.get("date") or "(sem data)").strip()
        snippet = re.sub(r"\s+", " ", str(item.get("snippet") or "")).strip()
        if len(snippet) > 90:
            snippet = snippet[:87] + "..."
        lines.append(f"{idx}. {sender} | {subject} | {date} | {snippet}")
    return "\n".join(lines).strip()


def _maybe_direct_answer_for_tool_result(
    tool_name: str,
    tool_result: dict,
    user_text: str,
) -> Optional[str]:
    lowered_name = str(tool_name or "").lower()
    if (lowered_name.endswith("list_inbox") or lowered_name.endswith("search_emails")) and tool_result.get("ok") and _looks_like_inbox_review_request(user_text):
        parsed = _parse_tool_payload_records(tool_result.get("text", ""))
        if isinstance(parsed, list):
            records = [item for item in parsed if isinstance(item, dict)]
            if records:
                return _summarize_inbox_records(records, user_text)
    return None


def _save_conversation(
    user_msg: str,
    assistant_msg: str,
    session_id: str = "main",
    promote_long_term: bool = True,
):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    normalized_session = normalize_session_id(session_id)
    memory_service.add_conversation(
        timestamp,
        user_msg,
        assistant_msg,
        session_id=normalized_session,
        promote_long_term=promote_long_term,
    )
    stats.update(memory_service.stats())

    # Phase 6 hooks
    mon = _get_monitor()
    if mon is not None:
        mon.record_request()
    lrn = _get_learner()
    if lrn is not None:
        try:
            lrn.analyze_conversation(user_msg, assistant_msg, session_id=normalized_session)
        except Exception as e:
            log.warning("[server] Learner analyze_conversation failed: %s", e)

    # Phase 7 — Extract decisions from conversation for project consciousness
    consciousness = _get_consciousness()
    if consciousness is not None:
        try:
            consciousness.extract_decisions_from_conversation(user_msg, assistant_msg)
        except Exception as e:
            log.warning("[server] Consciousness extract_decisions failed: %s", e)

    # ── Auto-checkpoint: save checkpoint every N turns ──
    _AUTO_CHECKPOINT_INTERVAL = int(os.getenv("MIKE_AUTO_CHECKPOINT_INTERVAL", "10"))
    if _AUTO_CHECKPOINT_INTERVAL > 0:
        try:
            sessions = memory_service.list_sessions(limit=1)
            if sessions:
                current = sessions[0]
                if current["session_id"] == normalized_session and current["turn_count"] > 0:
                    if current["turn_count"] % _AUTO_CHECKPOINT_INTERVAL == 0:
                        memory_service.checkpoint_save(
                            normalized_session,
                            label=f"Auto-checkpoint at turn {current['turn_count']}",
                        )
        except Exception as e:
            log.warning("[server] Auto-checkpoint save failed: %s", e)


async def _save_conversation_async(
    user_msg: str,
    assistant_msg: str,
    session_id: str = "main",
    promote_long_term: bool = True,
):
    await asyncio.to_thread(
        _save_conversation, user_msg, assistant_msg, session_id, promote_long_term
    )




# Wire chat core module with mike_server singletons
_chat_core.init_chat_core(
    web_search_instance=web_search,
    memory_service_instance=memory_service,
    system_prompt=SYSTEM_PROMPT,
    soul_prompt=SOUL_PROMPT,
    build_dynamic_prefix_fn=_build_dynamic_prefix,
    light_system_prompt_fn=_light_system_prompt,
    maybe_builtin_chat_reply_fn=_context.maybe_builtin_chat_reply,
    save_conversation_async_fn=_save_conversation_async,
    maybe_direct_answer_for_tool_result_fn=_maybe_direct_answer_for_tool_result,
    get_virtual_context_fn=_get_virtual_context,
    get_cached_sdk_generate_fn=_get_cached_sdk_generate,
    get_skill_registry_fn=_get_skill_registry,
)

app = FastAPI(title="Mike - Barreto Family AI", version="3.3.0", lifespan=lifespan)  # imported from mike_lifecycle
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (cycle-1 security hardening)
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware
_limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
app.state.limiter = _limiter
app.add_middleware(SlowAPIMiddleware)


# Middleware — imported from core.server.mike_server_middleware (Phase 3 refactor)
app.middleware("http")(mike_auth_middleware)
app.middleware("http")(mike_security_headers_middleware)


# ---------------------------------------------------------------------------
# Routes — modular routers (step 2 of monolith split)
# ---------------------------------------------------------------------------

from routers.system import router as system_router
from routers.auth import router as auth_router
from routers.memory import router as memory_router
from routers.tools import router as tools_router
from routers.autonomy import router as autonomy_router
from routers.chat import router as chat_router
from routers.dashboard import router as dashboard_router

app.include_router(system_router)
app.include_router(auth_router)
app.include_router(memory_router)
app.include_router(tools_router)
app.include_router(autonomy_router)
app.include_router(chat_router)
app.include_router(dashboard_router)
app.include_router(graph_router)  # Phase 3 refactor
app.include_router(knowledge_router)  # Phase 3 refactor

from routers.health import router as health_router
app.include_router(health_router)

log.info("All 8 routers registered — health endpoint active")

# Twilio webhooks (appointments pipeline)
try:
    from mike_twilio_webhooks import router as twilio_router
    app.include_router(twilio_router)
    log.info("Twilio webhook routes registered")
except ImportError as e:
    log.warning("[twilio] mike_twilio_webhooks unavailable, webhook routes disabled: %s", e)


# tunnel-url kept at legacy path (no /v1 prefix) — new module also exposes /v1/tunnel-url
@app.get("/tunnel-url")
async def tunnel_url():
    """Retorna o link publico atual do tunel Cloudflare, se disponivel."""
    url_file = PROJECT_ROOT / "data" / "tunnel_url_atual.txt"
    if url_file.exists():
        lines = url_file.read_text(encoding="utf-8").splitlines()
        link = lines[0].strip() if lines else None
        updated = lines[1].replace("Atualizado em: ", "").strip() if len(lines) > 1 else None
        if link:
            return {"tunnel_url": link, "updated_at": updated}
    return JSONResponse(status_code=404, content={"tunnel_url": None, "message": "Tunel nao esta ativo. Execute tunnel_mike.ps1."})

# Knowledge/bootstrap routes moved to core.server.mike_routes_knowledge (Phase 3 refactor)
# The following routes are now served by knowledge_router:
#   /v1/tunnel-url, /v1/events/stats, /v1/client/bootstrap, /v1/roadmap,
#   /v1/backups, /v1/web/search, /v1/knowledge/reindex,
#   /v1/knowledge/upsert, /v1/drive/index

# Phase 7 — Governance (4 Pillars of Autonomy)
# Subsystems are now stored in shared_state for router access.
# _broadcast_notification imported from core.server.mike_notifications (Phase 3 refactor)
# Graph routes moved to core.server.mike_routes_graph (Phase 3 refactor)


# Shared SDK helper factories & Phase 7 Governance subsystem lazy initializers
# All imported from core.server.mike_lazy_factories (Phase 3 refactor)



# /v1/skills/{skill_name}, /v1/autonomy/tasks/{id}, /v1/autonomy/routines/{id} → routers/autonomy.py


# /v1/missions/{id}, /step/{sid}/complete, /cancel → routers/autonomy.py



# TTS route — handler imported from core.server.mike_routes_tts (Phase 3 refactor)
app.post("/v1/tts")(tts_synthesize)


# ---------------------------------------------------------------------------
# Dashboard — static mount (routes moved to routers/dashboard.py)
# ---------------------------------------------------------------------------

if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")




# -- chat + dashboard wrappers (for routers) --
# All handlers imported from core.server.mike_dashboard_handlers (Phase 3 refactor)

# Re-exports from mike_chat_core (wired via init_chat_core above)
# These keep backward compat for modules that import from mike_server:
chat_completions = _chat_core.chat_completions
chat_sessions = _chat_core.chat_sessions
chat_history = _chat_core.chat_history
_build_task_mesh = _chat_core._build_task_mesh
_search_routes_for_query = _chat_core._search_routes_for_query
_should_search_web = _chat_core._should_search_web


# -- static files wrapper (called from dashboard router or kept as mount) --
_dashboard_static_mount_needed = DASHBOARD_DIR.exists()


# ═══════════════════════════════════════════════════════════════
# Install / Onboarding routes — handlers imported from mike_routes_install (Phase 3 refactor)
# ═══════════════════════════════════════════════════════════════

# _INSTALL_HTML_TEMPLATE moved to core.server.mike_routes_install (Phase 3 refactor)


# Install routes — handlers imported from core.server.mike_routes_install (Phase 3 refactor)
app.get("/install/{profile_key}")(install_page)
app.get("/install/{profile_key}/shortcut")(install_shortcut)


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"websockets")
    uvicorn.run(app, host=HOST, port=PORT, log_level=os.getenv('MIKE_UVICORN_LOG_LEVEL', 'info'))
