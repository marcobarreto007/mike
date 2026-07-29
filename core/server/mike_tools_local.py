"""
Mike local tool manifest, execution, and MCP dispatch.

Defines the built-in tool catalog, resolves/executes tools (local + MCP),
compacts tool results, and handles session-aware path resolution.

Singletons (web_search, memory_service, deepseek_client, mcp_workspace,
skill_registry) are read from ``shared_state``.

Extracted from mike_server.py — Phase 2 monolith breakup.
"""
from __future__ import annotations

import json as _json_mod
import logging
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, List, Optional

import shared_state as _state

from mike_auth import (
    PROFILE_AUTH_ENABLED,
    filter_tool_manifest,
    tool_allowed_for_profile,
)
from mike_config import (
    MCP_ALLOWED_ROOTS,
    MCP_TOOLS_ENABLED,
    PROJECT_ROOT,
    WEB_CACHE_DIR,
    WEB_SEARCH_ENABLED,
    WEB_TOP_K,
)
from mike_stats import stats
from mike_token_budget import _TOOL_RESULT_MAX_CHARS

log = logging.getLogger("mike")

# ---------------------------------------------------------------------------
# Request-local tool session id (ContextVar)
# ---------------------------------------------------------------------------

_current_tool_session_id: ContextVar[str] = ContextVar(
    "_current_tool_session_id",
    default="main",
)


def _resolve_tool_session_id(arguments: dict) -> str:
    session_id = str(arguments.get("session_id") or "").strip()
    if session_id:
        return session_id
    return _current_tool_session_id.get()


# ---------------------------------------------------------------------------
# Tool result formatting helpers
# ---------------------------------------------------------------------------

def _compact_tool_payload(tool_name: str, payload: str) -> str:
    text = str(payload or "").strip()
    if not text:
        return "(sem texto)"

    parsed = _parse_tool_payload_records(text)

    lowered_name = str(tool_name or "").lower()
    if isinstance(parsed, list) and (lowered_name.endswith("list_inbox") or lowered_name.endswith("search_emails")):
        lines = []
        for idx, item in enumerate(parsed[:30], 1):
            if not isinstance(item, dict):
                continue
            sender = str(item.get("from") or "").strip() or "(sem remetente)"
            subject = str(item.get("subject") or "").strip() or "(sem assunto)"
            date = str(item.get("date") or "").strip() or "(sem data)"
            snippet = re.sub(r"\s+", " ", str(item.get("snippet") or "")).strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            lines.append(
                f"[{idx}] de={sender} | assunto={subject} | data={date} | snippet={snippet}"
            )
        if lines:
            if len(parsed) > len(lines):
                lines.append(f"... +{len(parsed) - len(lines)} email(s) omitidos")
            return "\n".join(lines)

    if len(text) <= _TOOL_RESULT_MAX_CHARS:
        return text

    clipped = text[:_TOOL_RESULT_MAX_CHARS].rstrip()
    omitted = len(text) - len(clipped)
    return f"{clipped}\n...[saida truncada, {omitted} caracteres omitidos]"


def _parse_tool_payload_records(payload: str) -> Optional[Any]:
    text = str(payload or "").strip()
    if not text:
        return None
    try:
        return _json_mod.loads(text)
    except Exception as e:
        log.warning("[tools_local] JSON parse failure in _parse_tool_payload_records: %s", e)

    try:
        decoder = _json_mod.JSONDecoder()
        idx = 0
        streamed = []
        while idx < len(text):
            while idx < len(text) and text[idx].isspace():
                idx += 1
            if idx >= len(text):
                break
            item, next_idx = decoder.raw_decode(text, idx)
            streamed.append(item)
            idx = next_idx
        if streamed:
            return streamed
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Path resolution for workspace tools
# ---------------------------------------------------------------------------

def _project_root_relative_tool_args(name: str, arguments: dict) -> dict:
    """Resolve relative filesystem tool paths against PROJECT_ROOT before MCP."""
    workspace_names = {
        "write_file", "read_text_file", "edit_file", "delete_file",
        "create_directory", "list_directory", "get_path_info", "move_path",
    }
    raw_name = str(name or "").split(".", 1)[-1]
    if raw_name not in workspace_names:
        return arguments
    normalized = dict(arguments or {})
    for key in ("path", "source", "dest"):
        value = normalized.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        else:
            candidate = candidate.resolve()
        # Verifica que o path resolvido esta dentro dos limites permitidos
        if not any(
            candidate.is_relative_to(allowed_root)
            for allowed_root in MCP_ALLOWED_ROOTS
        ):
            log.warning("Path traversal blocked: %s not within allowed roots", candidate)
            raise ValueError(f"Path fora dos limites permitidos: {candidate}")
        normalized[key] = str(candidate)
    return normalized


# ---------------------------------------------------------------------------
# Local tool manifest
# ---------------------------------------------------------------------------

def _local_tool_manifest() -> List[dict]:
    tools = []
    if WEB_SEARCH_ENABLED:
        tools.append({
            "name": "web.search_and_cache",
            "raw_name": "web.search_and_cache",
            "server_name": "local",
            "description": (
                "Pesquisa na internet via DDGS, devolve resultados resumidos e salva o resultado "
                "no RAG local quando a consulta nao for de tempo real."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "title": "Query"},
                    "limit": {"type": "integer", "title": "Limit", "default": WEB_TOP_K},
                },
                "required": ["query"],
            },
            "capabilities": ["web", "rag"],
            "access": "any",
            "source": str(PROJECT_ROOT / "mike_server.py"),
        })
    # ── Memory checkpoint & session tools ──
    tools.append({
        "name": "memory.checkpoint_save",
        "raw_name": "memory.checkpoint_save",
        "server_name": "local",
        "description": (
            "Salva um checkpoint (snapshot) da sessao atual. Use ANTES de tarefas complexas, "
            "mudancas de contexto, ou entre etapas de trabalho. "
            "Permite voltar atras se algo der errado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "title": "Label", "description": "Descricao curta do checkpoint"},
            },
            "required": [],
        },
        "capabilities": ["memory", "checkpoint"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })
    tools.append({
        "name": "memory.checkpoint_list",
        "raw_name": "memory.checkpoint_list",
        "server_name": "local",
        "description": (
            "Lista checkpoints salvos da sessao atual ou do perfil. "
            "Mostra label, data, numero de turns e resumo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "title": "Limit", "default": 10},
            },
            "required": [],
        },
        "capabilities": ["memory", "checkpoint"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })
    tools.append({
        "name": "memory.checkpoint_restore",
        "raw_name": "memory.checkpoint_restore",
        "server_name": "local",
        "description": (
            "Restaura o contexto de um checkpoint anterior. "
            "Retorna o historico de mensagens ate aquele ponto. "
            "Use para voltar a um estado anterior da conversa."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "checkpoint_id": {"type": "string", "title": "Checkpoint ID", "description": "ID do checkpoint (ex: ckpt-abc123)"},
            },
            "required": ["checkpoint_id"],
        },
        "capabilities": ["memory", "checkpoint"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })
    tools.append({
        "name": "memory.session_summary",
        "raw_name": "memory.session_summary",
        "server_name": "local",
        "description": (
            "Salva um resumo permanente da sessao atual para memoria entre sessoes. "
            "Esse resumo sera usado em sessoes futuras para lembrar do que foi discutido. "
            "Inclua topicos principais e um resumo conciso."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "title": "Resumo", "description": "Resumo conciso da sessao"},
                "topics": {
                    "type": "array", "items": {"type": "string"},
                    "title": "Topicos", "description": "Lista de topicos discutidos",
                },
            },
            "required": ["summary"],
        },
        "capabilities": ["memory", "session"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })

    # ── Email tool ──
    tools.append({
        "name": "email.send",
        "raw_name": "email.send",
        "server_name": "local",
        "description": (
            "Envia um email via SMTP. Use para mandar mensagens em nome do Mike para a familia Barreto ou contatos. "
            "Sempre confirme o destinatario e o assunto antes de enviar. "
            "Se o SMTP nao estiver configurado, o resultado indicara como configurar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "title": "Destinatario", "description": "Email do destinatario (ex: ana@gmail.com)"},
                "subject": {"type": "string", "title": "Assunto", "description": "Assunto do email"},
                "body": {"type": "string", "title": "Corpo", "description": "Corpo do email em texto simples ou HTML"},
                "html": {"type": "boolean", "title": "HTML", "description": "Se true, envia como HTML. Default: false", "default": False},
            },
            "required": ["to", "subject", "body"],
        },
        "capabilities": ["email", "comms"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })
    tools.append({
        "name": "email.list_inbox",
        "raw_name": "email.list_inbox",
        "server_name": "local",
        "description": (
            "Lista os emails da caixa de entrada (INBOX) via IMAP. "
            "Retorna remetente, assunto, data e UID de cada email. "
            "Use para checar se chegou algum email, ver conversas recentes ou encontrar o UID de um email especifico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "title": "Limite", "description": "Numero de emails a retornar (padrao 10, max 50)", "default": 10},
                "folder": {"type": "string", "title": "Pasta", "description": "Pasta IMAP (padrao: INBOX)", "default": "INBOX"},
                "unread_only": {"type": "boolean", "title": "Apenas nao lidos", "description": "Se true, retorna apenas emails nao lidos", "default": False},
            },
            "required": [],
        },
        "capabilities": ["email", "comms"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })
    tools.append({
        "name": "email.read",
        "raw_name": "email.read",
        "server_name": "local",
        "description": (
            "Le o conteudo completo de um email pelo UID. "
            "Use apos email.list_inbox para abrir e ler o texto de um email especifico. "
            "Marca o email como lido automaticamente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "title": "UID", "description": "UID do email (obtido via email.list_inbox)"},
                "folder": {"type": "string", "title": "Pasta", "description": "Pasta IMAP onde o email esta (padrao: INBOX)", "default": "INBOX"},
            },
            "required": ["uid"],
        },
        "capabilities": ["email", "comms"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })
    tools.append({
        "name": "email.search",
        "raw_name": "email.search",
        "server_name": "local",
        "description": (
            "Busca emails por assunto ou remetente. "
            "Use para encontrar emails de uma pessoa especifica ou sobre um assunto. "
            "Retorna lista com UID, remetente, assunto e data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "title": "Busca", "description": "Texto para buscar no assunto ou remetente"},
                "folder": {"type": "string", "title": "Pasta", "description": "Pasta IMAP (padrao: INBOX)", "default": "INBOX"},
                "limit": {"type": "integer", "title": "Limite", "description": "Numero maximo de resultados (padrao 10)", "default": 10},
            },
            "required": ["query"],
        },
        "capabilities": ["email", "comms"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })

    # ── Autoconsciência ──
    tools.append({
        "name": "mike.introspect",
        "raw_name": "mike.introspect",
        "server_name": "local",
        "description": (
            "Retorna o mapa completo e real do codigo-fonte do Mike: estrutura de diretorios, "
            "modulos principais, paths absolutos, versao, e status do servidor. "
            "Use SEMPRE que precisar saber onde um arquivo esta, qual modulo implementa uma funcao, "
            "ou para inspecionar sua propria arquitetura antes de sugerir mudancas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "title": "Secao",
                    "description": "Opcional: 'core', 'config', 'runtime', 'scripts', 'all' (padrao: all)",
                    "default": "all"
                },
            },
            "required": [],
        },
        "capabilities": ["introspect", "self_awareness", "code_map"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })

    # ── Hot Cache ──
    tools.append({
        "name": "mike.hot_cache_list",
        "raw_name": "mike.hot_cache_list",
        "server_name": "local",
        "description": (
            "Lista os documentos/consultas no cache quente (hot cache). "
            "O cache quente armazena resumos dos documentos mais acessados para resposta instantanea."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "capabilities": ["cache", "memory"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })
    tools.append({
        "name": "mike.hot_cache_add",
        "raw_name": "mike.hot_cache_add",
        "server_name": "local",
        "description": (
            "Adiciona um documento ou consulta ao cache quente. "
            "Use para pre-carregar informacoes frequentes (documentos, precos, contatos) em memoria quente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "title": "Chave", "description": "Identificador unico (ex: 'escritura_lote7', 'contato_ana')"},
                "content": {"type": "string", "title": "Conteudo", "description": "Texto/resumo do documento a cachear"},
                "tags": {"type": "string", "title": "Tags", "description": "Tags separadas por virgula (opcional)", "default": ""},
            },
            "required": ["key", "content"],
        },
        "capabilities": ["cache", "memory"],
        "access": "any",
        "source": str(PROJECT_ROOT / "mike_server.py"),
    })

    # -- Autonomy: task board, routines, status and email follow-up --
    # These capabilities already exist in MikeAutonomy.  Exposing them here
    # makes them callable by the single Qwen brain instead of leaving the
    # corresponding skills as dashboard-only promises.
    def _autonomy_tool(
        name: str,
        description: str,
        properties: Optional[dict] = None,
        required: Optional[list[str]] = None,
        *,
        access: str = "any",
        capabilities: Optional[list[str]] = None,
    ) -> dict:
        return {
            "name": name,
            "raw_name": name,
            "server_name": "local",
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
            "capabilities": capabilities or ["autonomy"],
            "access": access,
            "source": str(PROJECT_ROOT / "core" / "autonomy" / "mike_autonomy.py"),
        }

    tools.extend([
        _autonomy_tool(
            "autonomy_status",
            "Mostra o estado real da autonomia, tarefas, rotinas e emails acompanhados.",
            capabilities=["autonomy", "status"],
        ),
        _autonomy_tool(
            "autonomy_log",
            "Lista as entradas mais recentes do log de autonomia.",
            {"limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}},
            capabilities=["autonomy", "status"],
        ),
        _autonomy_tool(
            "list_tasks",
            "Lista tarefas reais da lousa do Mike, opcionalmente por status.",
            {
                "status": {"type": "string", "description": "pending, running, done, failed ou cancelled"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
            capabilities=["autonomy", "tasks"],
        ),
        _autonomy_tool(
            "create_task",
            "Cria uma tarefa real na lousa da familia.",
            {
                "title": {"type": "string"},
                "description": {"type": "string", "default": ""},
                "priority": {"type": "integer", "default": 3, "minimum": 1, "maximum": 5},
                "notify_on_complete": {"type": "boolean", "default": True},
            },
            ["title"],
            access="any",
            capabilities=["autonomy", "tasks"],
        ),
        _autonomy_tool(
            "complete_task",
            "Marca uma tarefa da lousa como concluida e registra o resultado.",
            {
                "task_id": {"type": "string"},
                "result": {"type": "string", "default": ""},
            },
            ["task_id"],
            access="owner",
            capabilities=["autonomy", "tasks"],
        ),
        _autonomy_tool(
            "cancel_task",
            "Cancela uma tarefa real da lousa.",
            {"task_id": {"type": "string"}},
            ["task_id"],
            access="owner",
            capabilities=["autonomy", "tasks"],
        ),
        _autonomy_tool(
            "list_routines",
            "Lista todas as rotinas autonomas e seus estados.",
            capabilities=["autonomy", "routines"],
        ),
        _autonomy_tool(
            "toggle_routine",
            "Ativa, desativa ou alterna uma rotina autonoma.",
            {
                "routine_id": {"type": "string"},
                "enabled": {"type": "boolean", "description": "Omitir para alternar o estado atual"},
            },
            ["routine_id"],
            access="owner",
            capabilities=["autonomy", "routines"],
        ),
        _autonomy_tool(
            "run_routine_now",
            "Executa uma rotina autonoma imediatamente.",
            {"routine_id": {"type": "string"}},
            ["routine_id"],
            access="owner",
            capabilities=["autonomy", "routines"],
        ),
        _autonomy_tool(
            "create_routine",
            "Cria uma rotina autonoma customizada.",
            {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "schedule": {
                    "type": "object",
                    "description": "Horario da rotina; use hour/minute ou interval_minutes.",
                    "properties": {
                        "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                        "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                        "weekdays": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 6},
                        },
                        "interval_minutes": {"type": "integer", "minimum": 0},
                    },
                },
                "action_type": {"type": "string", "default": "agentic_loop"},
                "enabled": {"type": "boolean", "default": True},
                "notify": {"type": "boolean", "default": True},
            },
            ["name", "description"],
            access="owner",
            capabilities=["autonomy", "routines"],
        ),
        _autonomy_tool(
            "track_sent_email",
            "Registra um email enviado para acompanhar resposta e prazo.",
            {
                "gmail_message_id": {"type": "string", "default": ""},
                "sent_to": {"type": "string"},
                "subject": {"type": "string"},
                "deadline_hours": {"type": "integer", "default": 48, "minimum": 1},
                "auto_followup": {"type": "boolean", "default": False},
            },
            ["sent_to", "subject"],
            access="owner",
            capabilities=["autonomy", "email_tracking"],
        ),
        _autonomy_tool(
            "list_tracked_emails",
            "Lista emails acompanhados e seus estados.",
            {"status": {"type": "string"}},
            access="owner",
            capabilities=["autonomy", "email_tracking"],
        ),
        _autonomy_tool(
            "check_email_responses",
            "Verifica agora se os emails acompanhados receberam resposta.",
            access="owner",
            capabilities=["autonomy", "email_tracking"],
        ),
        _autonomy_tool(
            "dismiss_tracked_email",
            "Encerra o acompanhamento de um email.",
            {"tracking_id": {"type": "string"}},
            ["tracking_id"],
            access="owner",
            capabilities=["autonomy", "email_tracking"],
        ),
    ])

    return tools


# ---------------------------------------------------------------------------
# Visible tool manifest (filtered by profile + skill match)
# ---------------------------------------------------------------------------

async def _visible_tool_manifest(
    profile_key: Optional[str],
    *,
    task: Optional[str] = None,
    refresh: bool = False,
) -> List[dict]:
    all_tools = await _state.mcp_workspace.list_tools(refresh=refresh)
    manifest = filter_tool_manifest(all_tools, profile_key)
    manifest.extend(filter_tool_manifest(_local_tool_manifest(), profile_key))

    # Smart Match: filter tools based on task if enabled and task provided
    if task and MCP_TOOLS_ENABLED:
        skill_reg = _state.skill_registry
        if skill_reg:
            # Match skills for task (threshold 0.3 to be inclusive)
            matches = skill_reg.match(task, manifest, threshold=0.3)
            if matches:
                allowed_tool_names = set()
                for skill, _ in matches:
                    allowed_tool_names.update(
                        tool.get("name")
                        for tool in skill.filter_tools(manifest)
                        if tool.get("name")
                    )

                # Keep tools that are:
                # 1. Local (core memory/system)
                # 2. In matched skills
                # 3. Critical core tools (search/scrape)
                core_tools = {"browse_search", "web.search", "scrape_url", "google_search"}
                manifest = [
                    t for t in manifest
                    if str(t.get("server_name")).lower() == "local"
                    or t.get("name") in allowed_tool_names
                    or t.get("name") in core_tools
                ]
    return manifest


# ---------------------------------------------------------------------------
# Local tool execution
# ---------------------------------------------------------------------------

async def _execute_local_tool(name: str, arguments: dict) -> Optional[dict]:
    """Execute a built-in local tool. Returns None if the tool is not a local one."""
    log.info(f"Executing local tool: {name}")
    # ── Memory checkpoint tools ──
    if name == "memory.checkpoint_save":
        try:
            label = str(arguments.get("label") or "").strip() or None
            session_id = _resolve_tool_session_id(arguments)
            checkpoint_id = _state.memory_service.checkpoint_save(session_id, label=label)
            ckpt = _state.memory_service.checkpoint_list(session_id=session_id, limit=1)
            turn_count = ckpt[0]["turn_count"] if ckpt else 0
            return {
                "ok": True,
                "text": (
                    f"Checkpoint salvo com sucesso!\n"
                    f"- ID: {checkpoint_id}\n"
                    f"- Sessao: {session_id}\n"
                    f"- Turns: {turn_count}\n"
                    f"- Label: {label or '(auto)'}\n"
                    f"Voce pode restaurar esse estado a qualquer momento."
                ),
                "content_types": ["TextContent"],
                "server_name": "local",
            }
        except Exception as exc:
            return {"ok": False, "text": f"Erro ao salvar checkpoint: {exc}", "content_types": ["error"], "server_name": "local"}

    if name == "memory.checkpoint_list":
        try:
            limit = int(arguments.get("limit", 10))
            session_id = _resolve_tool_session_id(arguments)
            profile = session_id.split("-")[0] if "-" in session_id else None
            checkpoints = _state.memory_service.checkpoint_list(profile=profile, limit=limit)
            if not checkpoints:
                return {"ok": True, "text": "Nenhum checkpoint encontrado para este perfil.", "content_types": ["TextContent"], "server_name": "local"}
            lines = ["Checkpoints disponiveis:\n"]
            for ck in checkpoints:
                lines.append(
                    f"- **{ck['checkpoint_id']}** [{ck['label']}]\n"
                    f"  Sessao: {ck['session_id']} | Turns: {ck['turn_count']} | {ck['created_at']}\n"
                    f"  Resumo: {(ck['summary'] or '')[:200]}"
                )
            return {"ok": True, "text": "\n".join(lines), "content_types": ["TextContent"], "server_name": "local"}
        except Exception as exc:
            return {"ok": False, "text": f"Erro ao listar checkpoints: {exc}", "content_types": ["error"], "server_name": "local"}

    if name == "memory.checkpoint_restore":
        checkpoint_id = str(arguments.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            return {"ok": False, "text": "Argumento obrigatorio: checkpoint_id", "content_types": ["error"], "server_name": "local"}
        try:
            result = _state.memory_service.checkpoint_restore(checkpoint_id)
            if not result:
                return {"ok": False, "text": f"Checkpoint '{checkpoint_id}' nao encontrado.", "content_types": ["error"], "server_name": "local"}
            msg_count = len(result.get("messages", []))
            return {
                "ok": True,
                "text": (
                    f"Checkpoint restaurado!\n"
                    f"- ID: {result['checkpoint_id']}\n"
                    f"- Sessao: {result['session_id']}\n"
                    f"- Turns: {result['turn_count']}\n"
                    f"- Label: {result['label']}\n"
                    f"- Mensagens recuperadas: {msg_count}\n"
                    f"- Resumo do estado:\n{result['summary']}"
                ),
                "content_types": ["TextContent"],
                "server_name": "local",
            }
        except Exception as exc:
            return {"ok": False, "text": f"Erro ao restaurar checkpoint: {exc}", "content_types": ["error"], "server_name": "local"}

    if name == "memory.session_summary":
        summary_text = str(arguments.get("summary") or "").strip()
        if not summary_text:
            return {"ok": False, "text": "Argumento obrigatorio: summary", "content_types": ["error"], "server_name": "local"}
        try:
            topics = arguments.get("topics") or []
            if isinstance(topics, str):
                topics = [t.strip() for t in topics.split(",") if t.strip()]
            session_id = _resolve_tool_session_id(arguments)
            _state.memory_service.session_summary_save(session_id, summary_text, topics=topics)
            return {
                "ok": True,
                "text": (
                    f"Resumo da sessao salvo com sucesso!\n"
                    f"- Sessao: {session_id}\n"
                    f"- Topicos: {', '.join(topics) if topics else '(nenhum)'}\n"
                    f"- Resumo: {summary_text[:200]}\n"
                    f"Esse resumo sera usado em sessoes futuras para contexto."
                ),
                "content_types": ["TextContent"],
                "server_name": "local",
            }
        except Exception as exc:
            return {"ok": False, "text": f"Erro ao salvar resumo: {exc}", "content_types": ["error"], "server_name": "local"}

    if name == "email.send":
        from mike_email import send_email as _send_email
        to_addr = str(arguments.get("to") or "").strip()
        subject = str(arguments.get("subject") or "").strip()
        body = str(arguments.get("body") or "").strip()
        html = bool(arguments.get("html", False))
        if not to_addr or not subject or not body:
            return {"ok": False, "text": "Argumentos obrigatorios: to, subject, body", "content_types": ["error"], "server_name": "local"}
        result = _send_email(to_addr, subject, body, html=html)
        if result.get("ok"):
            return {
                "ok": True,
                "text": f"Email enviado para {to_addr} com assunto: {subject}",
                "content_types": ["TextContent"],
                "server_name": "local",
            }
        return {"ok": False, "text": result.get("error", "Erro desconhecido"), "content_types": ["error"], "server_name": "local"}

    if name == "email.list_inbox":
        from mike_email import list_inbox as _list_inbox
        limit = int(arguments.get("limit", 10))
        folder = str(arguments.get("folder", "INBOX"))
        unread_only = bool(arguments.get("unread_only", False))
        result = _list_inbox(limit=limit, folder=folder, unread_only=unread_only)
        if not result.get("ok"):
            return {"ok": False, "text": result.get("error", "Erro IMAP"), "content_types": ["error"], "server_name": "local"}
        emails = result.get("emails", [])
        if not emails:
            return {"ok": True, "text": "Caixa de entrada vazia (nenhum email encontrado).", "content_types": ["TextContent"], "server_name": "local"}
        lines = [f"\U0001f4ec {len(emails)} email(s) em {folder}:\n"]
        for i, e in enumerate(emails, 1):
            lines.append(f"{i}. UID:{e['uid']} | De: {e['from']} | Assunto: {e['subject']} | Data: {e['date']}")
        return {"ok": True, "text": "\n".join(lines), "content_types": ["TextContent"], "server_name": "local"}

    if name == "email.read":
        from mike_email import read_email as _read_email
        uid = str(arguments.get("uid") or "").strip()
        folder = str(arguments.get("folder", "INBOX"))
        if not uid:
            return {"ok": False, "text": "Argumento obrigatorio: uid", "content_types": ["error"], "server_name": "local"}
        result = _read_email(uid=uid, folder=folder)
        if not result.get("ok"):
            return {"ok": False, "text": result.get("error", "Erro IMAP"), "content_types": ["error"], "server_name": "local"}
        text = (
            f"\U0001f4e7 Email UID {uid}\n"
            f"De: {result.get('from')}\n"
            f"Para: {result.get('to')}\n"
            f"Assunto: {result.get('subject')}\n"
            f"Data: {result.get('date')}\n"
            f"{'─'*40}\n"
            f"{result.get('body', '')}"
        )
        return {"ok": True, "text": text, "content_types": ["TextContent"], "server_name": "local"}

    if name == "email.search":
        from mike_email import search_emails as _search_emails
        query = str(arguments.get("query") or "").strip()
        folder = str(arguments.get("folder", "INBOX"))
        limit = int(arguments.get("limit", 10))
        if not query:
            return {"ok": False, "text": "Argumento obrigatorio: query", "content_types": ["error"], "server_name": "local"}
        result = _search_emails(query=query, folder=folder, limit=limit)
        if not result.get("ok"):
            return {"ok": False, "text": result.get("error", "Erro IMAP"), "content_types": ["error"], "server_name": "local"}
        emails = result.get("emails", [])
        if not emails:
            return {"ok": True, "text": f"Nenhum email encontrado para '{query}'.", "content_types": ["TextContent"], "server_name": "local"}
        lines = [f"\U0001f50d {len(emails)} resultado(s) para '{query}':\n"]
        for i, e in enumerate(emails, 1):
            lines.append(f"{i}. UID:{e['uid']} | De: {e['from']} | Assunto: {e['subject']} | Data: {e['date']}")
        return {"ok": True, "text": "\n".join(lines), "content_types": ["TextContent"], "server_name": "local"}

    # ── Autoconsciência ──
    autonomy_tool_names = {
        "autonomy_status", "autonomy_log",
        "list_tasks", "create_task", "complete_task", "cancel_task",
        "list_routines", "toggle_routine", "run_routine_now", "create_routine",
        "track_sent_email", "list_tracked_emails",
        "check_email_responses", "dismiss_tracked_email",
    }
    if name in autonomy_tool_names:
        autonomy = _state.autonomy
        if autonomy is None:
            try:
                from mike_server import _get_autonomy
                autonomy = _get_autonomy()
            except Exception:
                autonomy = None
        if autonomy is None:
            return {
                "ok": False,
                "text": "Motor de autonomia nao disponivel.",
                "content_types": ["error"],
                "server_name": "local",
            }

        def _autonomy_result(payload: Any) -> dict:
            ok = payload is not None
            text = _json_mod.dumps(payload, ensure_ascii=False, indent=2, default=str)
            return {
                "ok": ok,
                "text": text if ok else "Item nao encontrado.",
                "content_types": ["StructuredContent"] if ok else ["error"],
                "server_name": "local",
            }

        try:
            if name == "autonomy_status":
                return _autonomy_result(autonomy.status())
            if name == "autonomy_log":
                limit = max(1, min(int(arguments.get("limit", 50)), 200))
                return _autonomy_result(await autonomy.get_log(limit))
            if name == "list_tasks":
                status = str(arguments.get("status") or "").strip() or None
                limit = max(1, min(int(arguments.get("limit", 50)), 200))
                return _autonomy_result(await autonomy.list_tasks(status=status, limit=limit))
            if name == "create_task":
                title = str(arguments.get("title") or "").strip()
                if not title:
                    raise ValueError("Argumento obrigatorio: title")
                priority = max(1, min(int(arguments.get("priority", 3)), 5))
                return _autonomy_result(await autonomy.create_task(
                    title=title,
                    description=str(arguments.get("description") or ""),
                    priority=priority,
                    notify_on_complete=bool(arguments.get("notify_on_complete", True)),
                ))
            if name == "complete_task":
                return _autonomy_result(await autonomy.complete_task(
                    str(arguments.get("task_id") or "").strip(),
                    result=str(arguments.get("result") or ""),
                ))
            if name == "cancel_task":
                return _autonomy_result(await autonomy.cancel_task(
                    str(arguments.get("task_id") or "").strip()
                ))
            if name == "list_routines":
                return _autonomy_result(await autonomy.list_routines())
            if name == "toggle_routine":
                enabled = arguments.get("enabled") if "enabled" in arguments else None
                return _autonomy_result(await autonomy.toggle_routine(
                    str(arguments.get("routine_id") or "").strip(),
                    enabled=enabled,
                ))
            if name == "run_routine_now":
                return _autonomy_result(await autonomy.run_routine_now(
                    str(arguments.get("routine_id") or "").strip()
                ))
            if name == "create_routine":
                data = dict(arguments)
                if not isinstance(data.get("schedule"), dict):
                    data["schedule"] = {}
                return _autonomy_result(await autonomy.create_routine(data))
            if name == "track_sent_email":
                sent_to = str(arguments.get("sent_to") or "").strip()
                subject = str(arguments.get("subject") or "").strip()
                if not sent_to or not subject:
                    raise ValueError("Argumentos obrigatorios: sent_to, subject")
                return _autonomy_result(await autonomy.track_email(
                    gmail_message_id=str(arguments.get("gmail_message_id") or ""),
                    sent_to=sent_to,
                    subject=subject,
                    deadline_hours=max(1, int(arguments.get("deadline_hours", 48))),
                    auto_followup=bool(arguments.get("auto_followup", False)),
                ))
            if name == "list_tracked_emails":
                status = str(arguments.get("status") or "").strip() or None
                return _autonomy_result(await autonomy.list_tracked_emails(status=status))
            if name == "check_email_responses":
                return _autonomy_result(await autonomy.check_email_responses())
            if name == "dismiss_tracked_email":
                return _autonomy_result(await autonomy.dismiss_tracked_email(
                    str(arguments.get("tracking_id") or "").strip()
                ))
        except Exception as exc:
            return {
                "ok": False,
                "text": f"{type(exc).__name__}: {exc}",
                "content_types": ["error"],
                "server_name": "local",
            }

    if name == "mike.introspect":
        section = str(arguments.get("section", "all")).lower()
        root = PROJECT_ROOT
        codebase = {
            "project_root": str(root),
            "server": {
                "main": str(root / "core" / "server" / "mike_server.py"),
                "config": str(root / "core" / "server" / "mike_config.py"),
                "auth": str(root / "core" / "server" / "mike_auth.py"),
                "token_budget": str(root / "core" / "server" / "mike_token_budget.py"),
                "payloads": str(root / "core" / "server" / "mike_payloads.py"),
                "completions": str(root / "core" / "chat" / "completions.py"),
                "context": str(root / "core" / "chat" / "context.py"),
                "port": 8083,
                "url": "http://127.0.0.1:8083",
            },
            "modules": {
                "memory": str(root / "core" / "memory" / "mike_memory.py"),
                "email": str(root / "core" / "comms" / "mike_email.py"),
                "autonomy": str(root / "core" / "autonomy" / "mike_autonomy.py"),
                "identity": str(root / "config" / "identity.json"),
                "utils": str(root / "core" / "utils.py"),
            },
            "config": {
                "env_runtime": str(root / "config" / ".env.runtime"),
                "mcp_servers": str(root / "config" / "mcp_servers.json"),
                "identity": str(root / "config" / "identity.json"),
            },
            "runtime": {
                "memory_db": str(root / "runtime" / "memory"),
                "hot_cache": str(root / "runtime" / "cache" / "hot_cache.json"),
                "knowledge": str(root / "runtime" / "knowledge"),
                "autonomy_routines": str(root / "runtime" / "memory" / "autonomy" / "routines.json"),
                "autonomy_log": str(root / "runtime" / "memory" / "autonomy" / "autonomy_log.jsonl"),
            },
            "scripts": {
                "start": str(root / "scripts" / "ops" / "start_mike.ps1"),
                "install_service": str(root / "scripts" / "ops" / "install_mike_service.ps1"),
            },
            "dashboard": {
                "html": str(root / "dashboard" / "index.html"),
                "js": str(root / "dashboard" / "app.js"),
                "css": str(root / "dashboard" / "style.css"),
                "url": "http://127.0.0.1:8083",
            },
        }
        if section != "all" and section in codebase:
            data = {section: codebase[section]}
        else:
            data = codebase
        lines = ["\U0001f4cd MAPA REAL DO CODIGO-FONTE DO MIKE\n"]
        for section_name, section_data in data.items():
            lines.append(f"\n[{section_name.upper()}]")
            if isinstance(section_data, dict):
                for k, v in section_data.items():
                    lines.append(f"  {k}: {v}")
            else:
                lines.append(f"  {section_data}")
        return {"ok": True, "text": "\n".join(lines), "content_types": ["TextContent"], "server_name": "local"}

    # ── Hot Cache ──
    _HOT_CACHE_PATH = PROJECT_ROOT / "runtime" / "cache" / "hot_cache.json"

    if name == "mike.hot_cache_list":
        try:
            _HOT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            cache = _json_mod.loads(_HOT_CACHE_PATH.read_text(encoding="utf-8")) if _HOT_CACHE_PATH.exists() else {}
            if not cache:
                return {"ok": True, "text": "Cache quente vazio. Use mike.hot_cache_add para adicionar documentos.", "content_types": ["TextContent"], "server_name": "local"}
            lines = [f"\U0001f525 HOT CACHE — {len(cache)} item(s):\n"]
            for key, entry in cache.items():
                tags = entry.get("tags", "")
                added = entry.get("added_at", "")[:10]
                preview = entry.get("content", "")[:100].replace("\n", " ")
                lines.append(f"• {key} [{tags}] ({added})\n  {preview}...")
            return {"ok": True, "text": "\n".join(lines), "content_types": ["TextContent"], "server_name": "local"}
        except Exception as exc:
            return {"ok": False, "text": f"Erro ao ler hot cache: {exc}", "content_types": ["error"], "server_name": "local"}

    if name == "mike.hot_cache_add":
        from datetime import datetime as _dt
        key = str(arguments.get("key") or "").strip()
        content = str(arguments.get("content") or "").strip()
        tags = str(arguments.get("tags") or "").strip()
        if not key or not content:
            return {"ok": False, "text": "Argumentos obrigatorios: key, content", "content_types": ["error"], "server_name": "local"}
        try:
            _HOT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            cache = _json_mod.loads(_HOT_CACHE_PATH.read_text(encoding="utf-8")) if _HOT_CACHE_PATH.exists() else {}
            cache[key] = {"content": content, "tags": tags, "added_at": _dt.utcnow().isoformat()}
            _HOT_CACHE_PATH.write_text(_json_mod.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"ok": True, "text": f"✅ '{key}' adicionado ao hot cache.\n{str(_HOT_CACHE_PATH)}", "content_types": ["TextContent"], "server_name": "local"}
        except Exception as exc:
            return {"ok": False, "text": f"Erro ao salvar no hot cache: {exc}", "content_types": ["error"], "server_name": "local"}

    if name not in ("web.search_and_cache",):
        return None

    query = str(arguments.get("query") or arguments.get("q") or "").strip()
    if not query:
        return {
            "ok": False,
            "text": "Argumento obrigatorio ausente: query",
            "content_types": ["error"],
            "server_name": "local",
        }

    try:
        limit = max(1, min(int(arguments.get("limit", WEB_TOP_K)), 10))
    except (TypeError, ValueError):
        limit = WEB_TOP_K

    try:
        results = _state.web_search.search(query, count=limit)
    except Exception as exc:
        log.warning("Local web search tool failed for query %r: %s", query, exc)
        stats["last_web_hits"] = 0
        stats["last_web_provider"] = "error"
        return {
            "ok": False,
            "text": f"{type(exc).__name__}: {exc}",
            "content_types": ["error"],
            "server_name": "local",
        }

    provider = _state.web_search.last_provider_used or _state.web_search.active_provider
    stats["last_web_hits"] = len(results)
    stats["last_web_provider"] = provider

    cached_path = None
    if results and not _state.web_search.is_realtime_query(query):
        cached_path = _state.memory_service.cache_web_results(
            query,
            results,
            WEB_CACHE_DIR,
            provider=provider,
        )
        try:
            stats.update(_state.memory_service.stats())
        except Exception as exc:
            log.debug("Memory stats unavailable after web cache update: %s", exc)

    # ── Format results (use injected formatter or simple fallback) ──
    def _default_format_web(results_list: List[dict]) -> str:
        lines = []
        for i, item in enumerate(results_list, 1):
            title = item.get("title", "").strip()
            url = item.get("url", "").strip()
            desc = item.get("description", "").strip()
            age = item.get("age", "").strip()
            age_tag = f" [{age}]" if age else ""
            lines.append(f"[{i}] {title}{age_tag}\n    {url}\n    {desc}")
        return "\n\n".join(lines)

    formatted = _default_format_web(results) if results else "(sem resultados relevantes)"
    cache_note = (
        f"Memoria RAG atualizada em: {cached_path}"
        if cached_path
        else "Memoria RAG nao foi atualizada porque a consulta era de tempo real ou nao retornou resultados."
    )
    return {
        "ok": True,
        "text": (
            f"Pesquisa concluida via {provider} para: {query}\n\n"
            f"{formatted}\n\n{cache_note}"
        ),
        "content_types": ["TextContent"],
        "server_name": "local",
    }


# ---------------------------------------------------------------------------
# MCP tool execution (local + external dispatch)
# ---------------------------------------------------------------------------

async def _execute_mcp_tool(
    name: str, arguments: dict, profile_key: Optional[str] = None
) -> dict:
    arguments = _project_root_relative_tool_args(name, arguments)
    local_tool = next(
        (tool for tool in _local_tool_manifest() if tool.get("name") == name),
        None,
    )
    if (
        local_tool is not None
        and PROFILE_AUTH_ENABLED
        and profile_key not in ("marco", "anapaula", None)
        and not tool_allowed_for_profile(
            local_tool.get("name"),
            profile_key,
            access=local_tool.get("access"),
        )
    ):
        return {
            "ok": False,
            "text": "Permissao negada para esta ferramenta.",
            "content_types": ["error"],
            "server_name": "local",
        }
    local_result = await _execute_local_tool(name, arguments)
    if local_result is not None:
        return local_result
    tool = await _state.mcp_workspace.resolve_tool(name)
    if tool is None:
        return {
            "ok": False,
            "text": f"Tool MCP nao encontrada: {name}",
            "content_types": ["error"],
        }
    if PROFILE_AUTH_ENABLED and profile_key not in ("marco", None) and not tool_allowed_for_profile(
        tool.get("name"),
        profile_key,
        access=tool.get("access"),
    ):
        target = "essas tools" if tool.get("server_name") != "workspace" else "tools que alteram arquivos ou pastas no workspace"
        return {
            "ok": False,
            "text": f"Permissao negada: apenas Marco pode usar {target}.",
            "content_types": ["error"],
        }
    try:
        result = await _state.mcp_workspace.call_tool(tool.get("name"), arguments)
    except Exception as exc:
        log.warning("MCP tool %s failed: %s", name, exc)
        return {
            "ok": False,
            "text": f"{type(exc).__name__}: {exc}",
            "content_types": ["error"],
        }
    log.info("MCP tool %s executed (ok=%s, result_len=%d)", name, result.get("ok"), len(result.get("text", "")))
    return result
