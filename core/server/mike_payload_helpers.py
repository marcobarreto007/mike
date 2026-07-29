"""
Mike API payload builders.

Constructs response dicts for REST endpoints by composing config constants,
runtime stats, MCP workspace state, and auth scopes.

Extracted from mike_server.py — Phase 1 monolith breakup.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

import mike_payloads as _payloads
from mike_config import (
    API_KEY,
    CORS_ORIGINS,
    DEFAULT_MAX_TOKENS,
    GPU_INFO,
    HOST,
    MCP_ALLOWED_ROOTS,
    MCP_TOOL_SERVER,
    MODEL_ALIAS,
    PORT,
    PROJECT_ROOT,
    SEARCH_ROUTE_HINTS,
    SEARCH_ROUTE_LIMIT,
    STREAM_KEEPALIVE_SECONDS,
    VISION_ALLOWED_MIME_TYPES,
    VISION_ENABLED,
    VISION_MAX_DECODED_BYTES,
    VISION_MAX_IMAGES,
    VISION_RUNTIME_PROFILE,
    WEB_REQUEST_TIMEOUT_SECONDS,
)
from mike_auth import (
    PROFILE_AUTH_ENABLED as _PROFILE_AUTH_ENABLED,
    profile_from_request,
)
from mike_request_helpers import _can_view_operational_details
from mike_stats import (
    _error_payload,
    _update_mcp_stats as _update_mcp_stats_base,
    _vision_limits,
    stats,
)

# Lazy references to singletons (populated by mike_server boot).
# Access via module-level name so callers see the current value at call time.
import shared_state as _shared


# ---------------------------------------------------------------------------
# Tool manifest / summary payloads
# ---------------------------------------------------------------------------

def _tool_summary_payload(tool_manifest: List[dict], *, mcp_workspace=None) -> dict:
    ws = mcp_workspace if mcp_workspace is not None else _shared.mcp_workspace
    capabilities = ws.capability_summary(tool_manifest) if ws else {}
    return {
        "tool_count": len(tool_manifest),
        "email_enabled": bool(capabilities.get("email")),
        "calendar_enabled": bool(capabilities.get("calendar")),
        "spreadsheet_enabled": bool(capabilities.get("spreadsheet")),
        "drive_enabled": bool(capabilities.get("drive")),
        "appointments_enabled": bool(capabilities.get("appointments")),
        "huggingface_enabled": bool(capabilities.get("huggingface")),
        "command_execution_enabled": bool(capabilities.get("command_execution")),
    }


def _sanitize_tool_manifest(tool_manifest: List[dict], *, include_ops: bool) -> List[dict]:
    return _payloads.sanitize_tool_manifest(tool_manifest, include_ops=include_ops)


def _tools_payload(
    request: Optional[Request],
    profile_key: Optional[str],
    tool_manifest: List[dict],
    *,
    mcp_workspace=None,
) -> dict:
    ws = mcp_workspace if mcp_workspace is not None else _shared.mcp_workspace
    include_ops = _can_view_operational_details(request, profile_key)
    return _payloads.tools_payload(
        tool_manifest=tool_manifest,
        profile_key=profile_key,
        include_ops=include_ops,
        mcp_enabled=ws.enabled if ws else False,
        tool_summary=_tool_summary_payload(tool_manifest, mcp_workspace=ws),
        mcp_tool_server=MCP_TOOL_SERVER,
        mcp_allowed_roots=MCP_ALLOWED_ROOTS,
        mcp_server_summaries=ws.server_summaries() if include_ops and ws else [],
    )


# ---------------------------------------------------------------------------
# Runtime / stats / health payloads
# ---------------------------------------------------------------------------

def _sanitized_runtime_payload(
    request: Optional[Request],
    profile_key: Optional[str],
    *,
    mcp_workspace=None,
) -> dict:
    ws = mcp_workspace if mcp_workspace is not None else _shared.mcp_workspace
    include_ops = _can_view_operational_details(request, profile_key)
    return _payloads.sanitized_runtime_payload(
        stats=stats,
        include_ops=include_ops,
        stream_keepalive_seconds=STREAM_KEEPALIVE_SECONDS,
        web_request_timeout_seconds=WEB_REQUEST_TIMEOUT_SECONDS,
        search_route_hints=SEARCH_ROUTE_HINTS,
        search_route_limit=SEARCH_ROUTE_LIMIT,
        profile_auth_enabled=_PROFILE_AUTH_ENABLED,
        gpu_info=GPU_INFO,
        vision_caps=_vision_capabilities_payload(),
        chat_caps=_chat_capabilities_payload(),
        project_root=str(PROJECT_ROOT),
        host=HOST,
        port=PORT,
        mcp_allowed_roots=MCP_ALLOWED_ROOTS,
        mcp_server_summaries=ws.server_summaries() if include_ops and ws else [],
        cors_origins=CORS_ORIGINS,
    )


def _sanitized_stats_payload(
    request: Optional[Request],
    profile_key: Optional[str],
    *,
    memory_service=None,
    roadmap_data: Optional[dict] = None,
    backup_data: Optional[dict] = None,
    mcp_workspace=None,
    local_tool_manifest_fn=None,
) -> dict:
    ws = mcp_workspace if mcp_workspace is not None else _shared.mcp_workspace
    ms = memory_service if memory_service is not None else _shared.memory_service

    _update_mcp_stats_base(
        stats,
        mcp_workspace=ws,
        local_tool_manifest_fn=local_tool_manifest_fn,
    )
    refreshed = dict(stats)
    if ms:
        refreshed.update(ms.stats())
    if roadmap_data:
        refreshed.update(roadmap_data)
    if backup_data:
        refreshed.update(backup_data)
    return _payloads.sanitized_stats_payload(
        stats=refreshed,
        include_ops=_can_view_operational_details(request, profile_key),
    )


def _health_payload(*, mcp_workspace=None) -> dict:
    ws = mcp_workspace if mcp_workspace is not None else _shared.mcp_workspace
    return _payloads.health_payload(
        model_alias=MODEL_ALIAS,
        boot_time=stats.get("boot_time"),
        stats=stats,
        host=HOST,
        api_key=API_KEY,
        profile_auth_enabled=_PROFILE_AUTH_ENABLED,
        mcp_enabled=ws.enabled if ws else False,
    )


def _models_payload() -> dict:
    return _payloads.models_payload(model_alias=MODEL_ALIAS)


def _chat_capabilities_payload(*, llm=None) -> dict:
    llm_obj = llm if llm is not None else _shared.llm
    return _payloads.chat_capabilities_payload(
        default_max_tokens=DEFAULT_MAX_TOKENS,
        llm_ready=llm_obj is not None,
    )


def _vision_capabilities_payload() -> dict:
    return _payloads.vision_capabilities_payload(
        vision_enabled=VISION_ENABLED,
        vision_max_images=VISION_MAX_IMAGES,
        vision_max_decoded_bytes=VISION_MAX_DECODED_BYTES,
        vision_allowed_mime_types=list(VISION_ALLOWED_MIME_TYPES),
        vision_runtime_profile=VISION_RUNTIME_PROFILE,
    )


# ---------------------------------------------------------------------------
# Monitor payload (operational — restricted access)
# ---------------------------------------------------------------------------

def _monitor_payload(request: Request, *, snapshot: bool) -> JSONResponse:
    profile_key = profile_from_request(request)
    if not _can_view_operational_details(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})
    mon = _shared.monitor
    if mon is None:
        return JSONResponse(status_code=503, content={"error": "Monitor nao disponivel"})
    return mon.snapshot() if snapshot else mon.status()
