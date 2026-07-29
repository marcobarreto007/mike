"""
Mike runtime stats singleton and helpers.

Extracted from mike_server.py — Phase 1 monolith breakup.
"""
from __future__ import annotations

import threading
from typing import List, Optional

from mike_config import (
    ALLOW_INSECURE_LAN,
    API_KEY,
    CORS_ORIGINS,
    CTX_SIZE,
    DEEPSEEK_CHAT_MODEL,
    DEFAULT_MAX_TOKENS,
    FLASH_ATTN,
    GPU_INFO,
    GPU_LAYERS,
    HOST,
    KV_TYPE_K,
    KV_TYPE_V,
    LLM_BACKEND,
    MEM0_SAVE_ALL,
    MODEL_ALIAS,
    MODEL_FILE,
    N_BATCH,
    N_THREADS,
    N_THREADS_BATCH,
    N_UBATCH,
    OFFLOAD_KQV,
    PROJECT_ROOT,
    RAG_ENABLED,
    RUNTIME_DEFAULTS,
    RUNTIME_PROFILE,
    SEARCH_ROUTE_HINTS,
    SEARCH_ROUTE_LIMIT,
    STREAM_KEEPALIVE_SECONDS,
    TRUST_LOCALHOST,
    VISION_ALLOWED_MIME_TYPES,
    VISION_ENABLED,
    VISION_MAX_DECODED_BYTES,
    VISION_MAX_IMAGES,
    VISION_RUNTIME_PROFILE,
    WEB_REQUEST_TIMEOUT_SECONDS,
    WEB_SEARCH_ENABLED,
)
from mike_auth import (
    PROFILE_AUTH_ENABLED,
    PROFILE_CREDENTIALS,
    PROFILE_DEFAULT_PASSWORDS_IN_USE,
)
from mike_payloads import error_payload as _error_payload_fn


# ---------------------------------------------------------------------------
# Runtime stats (mutable singleton)
# ---------------------------------------------------------------------------

stats: dict = {
    "boot_time": None,
    "total_requests": 0,
    "total_tokens_generated": 0,
    "last_request_time": None,
    "last_speed_tps": 0,
    "last_memory_hits": 0,
    "last_knowledge_hits": 0,
    "last_web_hits": 0,
    "last_search_routes": [],
    "model_name": MODEL_ALIAS,
    "model_file": MODEL_FILE,
    "llm_backend": LLM_BACKEND,
    "active_model": DEEPSEEK_CHAT_MODEL if LLM_BACKEND == "deepseek" else MODEL_FILE,
    "gpu_layers": GPU_LAYERS,
    "ctx_size": CTX_SIZE,
    "n_batch": N_BATCH,
    "n_ubatch": N_UBATCH,
    "n_threads": N_THREADS,
    "n_threads_batch": N_THREADS_BATCH,
    "flash_attn": FLASH_ATTN,
    "offload_kqv": OFFLOAD_KQV,
    "default_max_tokens": DEFAULT_MAX_TOKENS,
    "stream_keepalive_seconds": STREAM_KEEPALIVE_SECONDS,
    "web_request_timeout_seconds": WEB_REQUEST_TIMEOUT_SECONDS,
    "runtime_profile": RUNTIME_DEFAULTS["profile"],
    "runtime_profile_requested": RUNTIME_PROFILE,
    "runtime_profile_loaded": None,
    "llm_boot_profile": None,
    "llm_boot_attempts": 0,
    "boot_fallback_used": False,
    "project_root": str(PROJECT_ROOT),
    "rag_enabled": RAG_ENABLED,
    "web_search_enabled": WEB_SEARCH_ENABLED,
    "search_route_hints_enabled": SEARCH_ROUTE_HINTS,
    "search_route_limit": SEARCH_ROUTE_LIMIT,
    "mem0_save_all": MEM0_SAVE_ALL,
    "bind_host": HOST,
    "auth_enabled": bool(API_KEY),
    "trust_localhost": TRUST_LOCALHOST,
    "allow_insecure_lan": ALLOW_INSECURE_LAN,
    "profile_auth_enabled": PROFILE_AUTH_ENABLED,
    "profile_auth_profiles": sorted(PROFILE_CREDENTIALS.keys()),
    "profile_auth_default_passwords": PROFILE_DEFAULT_PASSWORDS_IN_USE,
    "cors_origins": CORS_ORIGINS,
    "vision_enabled": VISION_ENABLED,
    "vision_max_images": VISION_MAX_IMAGES,
    "vision_max_decoded_bytes": VISION_MAX_DECODED_BYTES,
    "vision_allowed_mime_types": list(VISION_ALLOWED_MIME_TYPES),
    "vision_runtime_profile": VISION_RUNTIME_PROFILE,
    "vision_handler_backend": None,
    "last_vision_image_count": 0,
    "last_vision_decoded_bytes": 0,
    **GPU_INFO,
}


_stats_lock = threading.Lock()


def _inc_stat(key: str, value: int = 1):
    with _stats_lock:
        stats[key] = stats.get(key, 0) + value


# ---------------------------------------------------------------------------
# MCP stats update
# ---------------------------------------------------------------------------

def _update_mcp_stats(
    target_stats: dict,
    *,
    mcp_workspace=None,
    tool_manifest: Optional[List[dict]] = None,
    local_tool_manifest_fn=None,
) -> None:
    """Update stats dict from MCP workspace state."""
    if mcp_workspace is None:
        return

    manifest = (
        tool_manifest
        if tool_manifest is not None
        else list(mcp_workspace.tool_manifest or []) + (local_tool_manifest_fn() if local_tool_manifest_fn else [])
    )
    summary = mcp_workspace.capability_summary(manifest)
    local_tools = [
        tool for tool in manifest
        if str(tool.get("server_name") or "").lower() == "local"
    ]
    remote_tools = [tool for tool in manifest if tool not in local_tools]
    target_stats["mcp_tools_enabled"] = mcp_workspace.enabled
    target_stats["mcp_servers"] = mcp_workspace.server_summaries()
    target_stats["mcp_server_count"] = len(target_stats["mcp_servers"])
    target_stats["mcp_tool_count"] = len(remote_tools)
    target_stats["local_tool_count"] = len(local_tools)
    target_stats["tool_count_total"] = len(manifest)
    target_stats["mcp_email_enabled"] = bool(summary.get("email"))
    target_stats["mcp_calendar_enabled"] = bool(summary.get("calendar"))
    target_stats["mcp_spreadsheet_enabled"] = bool(summary.get("spreadsheet"))


# ---------------------------------------------------------------------------
# Patch: set vision_handler_backend (called by mike_server.py after boot)
# ---------------------------------------------------------------------------

def _set_vision_handler_backend(label: str) -> None:
    stats["vision_handler_backend"] = label


# ---------------------------------------------------------------------------
# Vision limits / error payload (thin wrappers)
# ---------------------------------------------------------------------------

def _vision_limits() -> dict:
    return {
        "vision_enabled": VISION_ENABLED,
        "vision_max_images": VISION_MAX_IMAGES,
        "vision_max_decoded_bytes": VISION_MAX_DECODED_BYTES,
        "vision_allowed_mime_types": list(VISION_ALLOWED_MIME_TYPES),
        "vision_runtime_profile": VISION_RUNTIME_PROFILE,
    }


def _error_payload(message: str, code: str, details: Optional[dict] = None) -> dict:
    return _error_payload_fn(message, code, details)
