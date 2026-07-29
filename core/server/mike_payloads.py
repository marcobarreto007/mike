"""
API payload builders for Mike's REST endpoints.

All functions are pure — they receive state as parameters and return dicts.
No global state is accessed directly; callers pass what's needed.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional


def health_payload(
    *,
    model_alias: str,
    boot_time: Optional[str],
    stats: dict,
    host: str,
    api_key: str,
    profile_auth_enabled: bool,
    mcp_enabled: bool,
) -> dict:
    return {
        "status": "ok",
        "name": "Mike",
        "model": model_alias,
        "llm_backend": stats.get("llm_backend"),
        "active_model": stats.get("active_model") or stats.get("model_file") or model_alias,
        "uptime_since": boot_time,
        "memory_backend": stats.get("memory_backend"),
        "web_search_provider": stats.get("web_search_provider"),
        "web_search_ready": stats.get("web_search_ready"),
        "bind_host": host,
        "auth_enabled": bool(api_key),
        "profile_auth_enabled": profile_auth_enabled,
        "mcp_tools_enabled": mcp_enabled,
        "mcp_email_enabled": stats.get("mcp_email_enabled", False),
        "mcp_calendar_enabled": stats.get("mcp_calendar_enabled", False),
        "mcp_spreadsheet_enabled": stats.get("mcp_spreadsheet_enabled", False),
    }


def models_payload(*, model_alias: str) -> dict:
    return {
        "object": "list",
        "data": [{
            "id": model_alias,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "barreto-family",
        }],
    }


def error_payload(message: str, code: str, details: Optional[dict] = None) -> dict:
    payload: dict = {"error": message, "code": code}
    if details:
        payload["details"] = details
    return payload


def sanitize_tool_manifest(tool_manifest: List[dict], *, include_ops: bool) -> List[dict]:
    visible: List[dict] = []
    for tool in tool_manifest:
        item: dict = {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "access": tool.get("access", ""),
            "capabilities": list(tool.get("capabilities") or []),
        }
        schema = tool.get("input_schema") or tool.get("inputSchema")
        if schema:
            item["input_schema"] = schema
        if include_ops:
            if tool.get("server_name"):
                item["server_name"] = tool.get("server_name")
            if tool.get("source"):
                item["source"] = tool.get("source")
        visible.append(item)
    return visible


def tools_payload(
    *,
    tool_manifest: List[dict],
    profile_key: Optional[str],
    include_ops: bool,
    mcp_enabled: bool,
    tool_summary: dict,
    mcp_tool_server: Any,
    mcp_allowed_roots: List[Any],
    mcp_server_summaries: List[dict],
) -> dict:
    payload = {
        "enabled": mcp_enabled,
        "profile": profile_key,
        "ops_access": include_ops,
        **tool_summary,
        "tools": sanitize_tool_manifest(tool_manifest, include_ops=include_ops),
    }
    if include_ops:
        payload.update({
            "server": str(mcp_tool_server),
            "allowed_roots": [str(r) for r in mcp_allowed_roots],
            "servers": mcp_server_summaries,
        })
    return payload


def chat_capabilities_payload(*, default_max_tokens: int, llm_ready: bool) -> dict:
    return {
        "default_max_tokens": default_max_tokens,
        "supports_text_streaming": llm_ready,
        "supports_vision_streaming": False,
    }


def vision_capabilities_payload(
    *,
    vision_enabled: bool,
    vision_max_images: int,
    vision_max_decoded_bytes: int,
    vision_allowed_mime_types: List[str],
    vision_runtime_profile: str,
) -> dict:
    return {
        "enabled": vision_enabled,
        "max_images": vision_max_images,
        "max_decoded_bytes": vision_max_decoded_bytes,
        "allowed_mime_types": list(vision_allowed_mime_types),
        "runtime_profile": vision_runtime_profile,
    }


def sanitized_runtime_payload(
    *,
    stats: dict,
    include_ops: bool,
    stream_keepalive_seconds: int,
    web_request_timeout_seconds: int,
    search_route_hints: bool,
    search_route_limit: int,
    profile_auth_enabled: bool,
    gpu_info: dict,
    vision_caps: dict,
    chat_caps: dict,
    project_root: str = "",
    host: str = "",
    port: int = 0,
    mcp_allowed_roots: Optional[List[Any]] = None,
    mcp_server_summaries: Optional[List[dict]] = None,
    cors_origins: Optional[List[str]] = None,
) -> dict:
    base: dict = {
        "runtime_profile": stats.get("runtime_profile"),
        "runtime_profile_requested": stats.get("runtime_profile_requested"),
        "profile_auth_enabled": profile_auth_enabled,
        "mcp_email_enabled": stats.get("mcp_email_enabled", False),
        "mcp_calendar_enabled": stats.get("mcp_calendar_enabled", False),
        "mcp_spreadsheet_enabled": stats.get("mcp_spreadsheet_enabled", False),
        "stream_keepalive_seconds": stream_keepalive_seconds,
        "web_request_timeout_seconds": web_request_timeout_seconds,
        "search_route_hints_enabled": search_route_hints,
        "search_route_limit": search_route_limit,
        "vision_enabled": vision_caps.get("enabled"),
        "vision_max_images": vision_caps.get("max_images"),
        "vision_max_decoded_bytes": vision_caps.get("max_decoded_bytes"),
        "vision_allowed_mime_types": vision_caps.get("allowed_mime_types"),
        "vision_runtime_profile": vision_caps.get("runtime_profile"),
        **chat_caps,
        **gpu_info,
    }
    if include_ops:
        base.update({
            "project_root": project_root,
            "host": host,
            "port": port,
            "mcp_allowed_roots": [str(r) for r in (mcp_allowed_roots or [])],
            "mcp_servers": mcp_server_summaries or [],
            "cors_origins": cors_origins or [],
        })
    return base


def sanitized_stats_payload(
    *,
    stats: dict,
    include_ops: bool,
) -> dict:
    if include_ops:
        return dict(stats)
    safe_keys = {
        "boot_time", "total_requests", "total_tokens_generated",
        "last_request_time", "last_speed_tps", "last_memory_hits",
        "last_knowledge_hits", "last_web_hits", "last_search_routes",
        "model_name", "model_file", "gpu_layers", "ctx_size",
        "default_max_tokens", "runtime_profile", "runtime_profile_requested",
        "runtime_profile_loaded", "llm_boot_profile", "llm_boot_attempts",
        "boot_fallback_used", "rag_enabled", "web_search_enabled",
        "profile_auth_enabled", "vision_enabled", "vision_max_images",
        "vision_max_decoded_bytes", "vision_runtime_profile",
        "memory_backend", "conversation_records", "knowledge_documents",
        "knowledge_chunks", "web_cache_documents", "mcp_tool_count",
        "mcp_email_enabled", "mcp_calendar_enabled", "mcp_spreadsheet_enabled",
        "roadmap_present", "roadmap_items_total", "roadmap_items_completed",
        "backup_archives", "latest_backup_file", "latest_backup_created_at",
        "latest_backup_size_mb",
    }
    return {k: stats[k] for k in safe_keys if k in stats}


# Back-compat aliases
_sanitize_tool_manifest = sanitize_tool_manifest
_error_payload = error_payload
_health_payload = health_payload
_models_payload = models_payload
_chat_capabilities_payload = chat_capabilities_payload
_vision_capabilities_payload = vision_capabilities_payload
