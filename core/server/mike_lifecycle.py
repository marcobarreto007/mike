# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.
# Extracted from mike_server.py — Phase 3 refactor

"""
Mike Lifecycle Module
=====================
Server startup, lifespan, proactive monitoring, drive indexing, and
auto-cleanup routines extracted from mike_server.py.

Business logic lives in dedicated modules:
  - mike_config.py          – env, constants, GPU, runtime profiles
  - mike_auth.py            – profile credentials, session tokens, permissions
  - mike_mcp_client.py      – MCP workspace client & tool helpers
  - mike_memory.py          – local SQLite + optional Mem0
  - mike_llm_boot.py        – LLM bootstrap, vision handler, HF file resolution
  - mike_model_router.py    – dynamic per-request backend selection
  - mike_fallback_chain.py  – resilience layer with circuit breakers
  - mike_heartbeat.py       – health heartbeat / alert loop
  - mike_drive_indexer.py   – Google Drive re-indexer
  - mike_event_bus.py       – event-driven autonomy bus
  - shared_state.py         – thread-safe mutable state for routers
"""

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Project modules ---
import mike_config
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

import mike_auth
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

import mike_mcp_client
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

import mike_memory
from mike_memory import MikeMemoryService

import mike_llm_boot
from mike_llm_boot import (
    _create_llm_with_fallback,
    _native_gemma4_chat_handler_class,
    _resolve_hf_file,
    _vision_handler_backend_label,
)

import mike_model_router
from mike_model_router import MikeModelRouter

import mike_fallback_chain
from mike_fallback_chain import FallbackChain, AllBackendsFailedError

import mike_heartbeat

import mike_drive_indexer

import mike_event_bus

import shared_state as _shared_state

# --- Additional imports needed by extracted functions ---
import mike_token_budget as _token_budget
import mike_context as _context
from core.shared.task_utils import _handle_task_exception
from mike_circuit_breaker import CircuitBreaker
from mike_tools_local import _local_tool_manifest
from mike_vision import _has_images
from mike_llama_server_client import MikeLlamaServerClient
from mike_mock_llm import MikeMockLLM

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service references (populated at runtime)
#   NOTE: Most of these are mirrored from _shared_state.  The architect will
#   wire the remaining singletons that are currently owned by mike_server.py.
# ---------------------------------------------------------------------------

# Global singletons set during _startup() — mirrors mike_server.py module globals
llm = None
llm_lock = threading.Lock()
vision_handler = None
model_router: Optional[MikeModelRouter] = None
fallback_chain: Optional[FallbackChain] = None


# ---------------------------------------------------------------------------
# Proactive monitoring background task constants
# ---------------------------------------------------------------------------

_PROACTIVE_INTERVAL = int(os.getenv("MIKE_PROACTIVE_INTERVAL_SECONDS", "900"))  # 15 min
_DRIVE_INDEX_INTERVAL = int(os.getenv("MIKE_DRIVE_INDEX_INTERVAL_HOURS", "24")) * 3600


# ---------------------------------------------------------------------------
# Roadmap & backup summaries (extracted alongside startup)
# ---------------------------------------------------------------------------

def _load_roadmap() -> Optional[dict]:
    if not ROADMAP_FILE.exists():
        return None
    try:
        return json.loads(ROADMAP_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        log.warning("Failed to read roadmap JSON: %s", exc)
        return None


def _build_roadmap_summary(roadmap: Optional[dict]) -> dict:
    items = roadmap.get("items", []) if isinstance(roadmap, dict) else []
    total = len(items)
    counts: dict[str, int] = {}
    current_focus_id = current_focus_title = next_focus_id = None

    for item in items:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1

    if isinstance(roadmap, dict):
        summary = roadmap.get("summary", {}) or {}
        current_focus_id = summary.get("current_focus_id")
        next_focus_id = summary.get("next_focus_id")

    if current_focus_id:
        current = next((i for i in items if i.get("id") == current_focus_id), None)
        if current:
            current_focus_title = current.get("title")

    completed = counts.get("completed", 0)
    percent = round((completed / total) * 100, 2) if total else 0.0
    return {
        "roadmap_file": str(ROADMAP_FILE),
        "roadmap_present": bool(roadmap),
        "roadmap_items_total": total,
        "roadmap_items_completed": completed,
        "roadmap_completion_percent": percent,
        "roadmap_status_counts": counts,
        "roadmap_current_focus_id": current_focus_id,
        "roadmap_current_focus_title": current_focus_title,
        "roadmap_next_focus_id": next_focus_id,
    }


def _build_backup_summary(backup_dir: Path) -> dict:
    if not backup_dir.exists():
        return {
            "backup_dir": str(backup_dir),
            "backup_script": str(BACKUP_SCRIPT),
            "backup_archives": 0,
            "latest_backup_file": None,
            "latest_backup_created_at": None,
            "latest_backup_size_mb": None,
        }
    all_zips = sorted(
        (p for p in backup_dir.glob("*.zip") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    latest_info = None
    if all_zips:
        st = all_zips[0].stat()
        latest_info = {
            "name": all_zips[0].name,
            "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size_mb": round(st.st_size / (1024 * 1024), 2),
        }
    return {
        "backup_dir": str(backup_dir),
        "backup_script": str(BACKUP_SCRIPT),
        "backup_archives": len(all_zips),
        "latest_backup_file": latest_info["name"] if latest_info else None,
        "latest_backup_created_at": latest_info["created_at"] if latest_info else None,
        "latest_backup_size_mb": latest_info["size_mb"] if latest_info else None,
    }


# ---------------------------------------------------------------------------
# _update_mcp_stats wrapper (mirrored from mike_server.py)
# ---------------------------------------------------------------------------

# NOTE: The original wrapper references mike_server singletons (stats, mcp_workspace).
# After extraction, these will be resolved through _shared_state.
# The architect will route calls back through mike_server or update callers.

import mike_stats as _mike_stats


# ---------------------------------------------------------------------------
# Auto-cleanup
# ---------------------------------------------------------------------------

def _auto_cleanup_clutter(root: Path) -> None:
    """Remove stale/generated clutter on every server startup.
    Safe: only deletes regenerable artifacts (cache, pycache, temp files).

    Scans only project directories (core/, tests/, config/, agents/, dashboard/,
    carregadores/).  Skips .venv, node_modules, llm_cache, llama.cpp, .git to
    avoid wasteful I/O on large trees.
    """
    import shutil

    # Directories that are safe to scan (project-owned, regenerable artifacts).
    _CLEANUP_SCAN_ROOTS = [
        root / "core",
        root / "tests",
        root / "config",
        root / "agents",
        root / "dashboard",
        root / "carregadores",
    ]

    # Glob patterns that should never be scanned (large vendored/cache trees).
    _CLEANUP_EXCLUDE_FRAGMENTS = (
        ".venv", "node_modules", "llm_cache", "llama.cpp", ".git",
    )

    def _is_excluded(path: Path) -> bool:
        p = str(path)
        return any(frag in p for frag in _CLEANUP_EXCLUDE_FRAGMENTS)

    cleaned = 0

    # Stale __pycache__ directories
    for scan_root in _CLEANUP_SCAN_ROOTS:
        if not scan_root.exists():
            continue
        for pycache in scan_root.rglob("__pycache__"):
            if _is_excluded(pycache):
                continue
            try:
                shutil.rmtree(pycache, ignore_errors=True)
                cleaned += 1
            except Exception as e:
                log.warning("[cleanup] Failed to remove __pycache__ %s: %s", pycache, e)

    # Orphaned SQLite WAL/SHM files
    for scan_root in _CLEANUP_SCAN_ROOTS:
        if not scan_root.exists():
            continue
        for pattern in ("*.db-shm", "*.db-wal"):
            for f in scan_root.rglob(pattern):
                if _is_excluded(f):
                    continue
                try:
                    f.unlink(missing_ok=True)
                    cleaned += 1
                except Exception as e:
                    log.warning("[cleanup] Failed to remove WAL/SHM %s: %s", f, e)

    # Root-level database.db (stray artifact from curriculum tasks)
    stray_db = root / "database.db"
    if stray_db.exists():
        try:
            stray_db.unlink()
            cleaned += 1
        except Exception as e:
            log.warning("[cleanup] Failed to remove stray database.db: %s", e)

    # Legacy file migrations kept for backward compatibility with older layouts.
    migrations = (
        (
            root / "p106_status.json",
            root / "runtime" / "cache" / "hardware" / "p106_status.json",
        ),
        (
            root / "MikeTyson_PunchOut.spec",
            root / "dashboard" / "games" / "packaging" / "MikeTyson_PunchOut.spec",
        ),
        (
            root / "scripts" / "test_flux.py",
            root / "tests" / "manual" / "test_flux.py",
        ),
    )
    for src, dst in migrations:
        if not src.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                src.unlink(missing_ok=True)
            else:
                shutil.move(str(src), str(dst))
            cleaned += 1
        except Exception as e:
            log.warning("[cleanup] Legacy file migration failed for %s: %s", src, e)

    # Known throwaway test artifacts that should never stay tracked.
    stale_files = (
        root / "read_test.json",
        root / "write_test.json",
        root / "ps_test.json",
        root / "tool_test.json",
        root / "help.txt",
        root / "setup_comfy.sh",
        root / "logs" / "test_out.txt",
        root / "logs" / "test_err.txt",
        root / "logs" / "test_import.py",
        root / "data" / "teste_sucesso.txt",
    )
    for stale_file in stale_files:
        if stale_file.exists():
            try:
                stale_file.unlink(missing_ok=True)
                cleaned += 1
            except Exception as e:
                log.warning("[cleanup] Failed to remove stale file %s: %s", stale_file, e)

    # Stale mike/ alternative root
    stale_mike = root / "mike"
    if stale_mike.exists():
        try:
            shutil.rmtree(stale_mike, ignore_errors=True)
            cleaned += 1
        except Exception as e:
            log.warning("[cleanup] Failed to remove stale mike/ root: %s", e)

    if cleaned:
        log.info("[cleanup] Removed %d stale artifact(s)", cleaned)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def _startup():
    # Log Soul status
    if _shared_state.SOUL_PROMPT and "FAMILIA" in _shared_state.SOUL_PROMPT:
        log.info("Mike's Soul loaded successfully (%d chars)", len(_shared_state.SOUL_PROMPT))
    else:
        log.warning("Mike's Soul file not found or incomplete!")

    global llm, vision_handler

    # Only import llama.cpp for local backend
    if LLM_BACKEND == "local":
        try:
            from llama_cpp import Llama
        except ImportError:
            log.error("llama-cpp-python not installed. Install with: pip install llama-cpp-python")
            log.error("Or set MIKE_LLM_BACKEND=deepseek to use cloud API.")
            raise
    else:
        Llama = None  # deepseek cloud backend — no local model needed

    validate_security_config()
    (PROJECT_ROOT / "runtime" / "knowledge").mkdir(parents=True, exist_ok=True)
    WEB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ROADMAP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    _shared_state.memory_service.cleanup_web_cache(
        WEB_CACHE_DIR,
        max_age_hours=int(os.getenv("MIKE_WEB_CACHE_MAX_HOURS", "24")),
    )
    if os.getenv("MIKE_CLEANUP_ON_BOOT", "").lower() in ("1", "true", "yes"):
        asyncio.create_task(asyncio.to_thread(_auto_cleanup_clutter, PROJECT_ROOT))

    log.info("=" * 60)
    log.info("  MIKE - Waking up...")
    log.info("=" * 60)
    log.info(
        "Runtime: profile=%s requested=%s gpu_layers=%s ctx=%s "
        "n_batch=%s n_ubatch=%s threads=%s/%s flash_attn=%s offload_kqv=%s",
        RUNTIME_DEFAULTS["profile"], RUNTIME_PROFILE, GPU_LAYERS,
        CTX_SIZE, N_BATCH, N_UBATCH, N_THREADS, N_THREADS_BATCH,
        FLASH_ATTN, OFFLOAD_KQV,
    )
    if GPU_INFO["cuda_detected"]:
        log.info(
            "CUDA: %s | free %s MiB / total %s MiB | driver %s",
            GPU_INFO["gpu_name"], GPU_INFO["gpu_memory_free_mb"],
            GPU_INFO["gpu_memory_total_mb"], GPU_INFO["cuda_driver"],
        )
    else:
        log.warning("CUDA/GPU not detected via nvidia-smi. Mike may run CPU-only.")
    log.info(
        "Web search: enabled=%s provider=%s ready=%s",
        WEB_SEARCH_ENABLED, _shared_state.web_search.active_provider,
        "yes" if _shared_state.web_search.provider_ready else "no",
    )
    log.info(
        "Profile auth: enabled=%s cookie=%s ttl_hours=%s defaults=%s",
        PROFILE_AUTH_ENABLED, SESSION_COOKIE_NAME, SESSION_TTL_HOURS,
        ",".join(PROFILE_DEFAULT_PASSWORDS_IN_USE) if PROFILE_DEFAULT_PASSWORDS_IN_USE else "custom",
    )
    log.info(
        "Vision: enabled=%s runtime=%s max_images=%s max_decoded_mb=%.2f mimes=%s",
        VISION_ENABLED,
        VISION_RUNTIME_PROFILE,
        VISION_MAX_IMAGES,
        VISION_MAX_DECODED_BYTES / (1024 * 1024),
        ",".join(VISION_ALLOWED_MIME_TYPES),
    )

    remote_tool_manifest = await _shared_state.mcp_workspace.list_tools(refresh=True)
    log.info("MCP workspace tools loaded: %d remote tools", len(remote_tool_manifest))
    tool_manifest = list(remote_tool_manifest) + _local_tool_manifest()
    # _update_mcp_stats called directly with _shared_state singletons
    _mike_stats._update_mcp_stats(
        _shared_state.stats,
        mcp_workspace=_shared_state.mcp_workspace,
        tool_manifest=tool_manifest,
        local_tool_manifest_fn=_local_tool_manifest,
    )
    capability_summary = _shared_state.mcp_workspace.capability_summary(remote_tool_manifest)
    log.info(
        "MCP tools: enabled=%s servers=%s tools=%s email=%s calendar=%s spreadsheet=%s",
        _shared_state.mcp_workspace.enabled,
        ",".join(server["name"] for server in _shared_state.stats["mcp_servers"]) or "none",
        len(tool_manifest),
        capability_summary["email"],
        capability_summary["calendar"],
        capability_summary["spreadsheet"],
    )
    log.info(
        "Security: host=%s auth=%s trust_localhost=%s allow_insecure_lan=%s cors=%s",
        HOST, bool(API_KEY), TRUST_LOCALHOST, ALLOW_INSECURE_LAN,
        ",".join(CORS_ORIGINS),
    )

    await asyncio.to_thread(_shared_state.memory_service.initialize)
    _shared_state.stats.update(_shared_state.memory_service.stats())
    _shared_state.stats.update(_build_roadmap_summary(_load_roadmap()))
    _shared_state.stats.update(_build_backup_summary(BACKUP_DIR))

    # External OpenAI-compatible backends own model loading. Mike's production
    # runtime uses llama-server so the 18 GB Qwen model is loaded exactly once.
    if LLM_BACKEND in ("llama_server", "deepseek", "mock"):
        external_model = (
            "Qwen3.6-35B-A3B"
            if LLM_BACKEND == "llama_server"
            else (DEEPSEEK_CHAT_MODEL or "mock")
        )
        model_path = f"[{LLM_BACKEND}-api:{external_model}]"
        mmproj_path = None
    else:
        model_path = _resolve_hf_file(
            repo_id=MODEL_REPO,
            configured_file=MODEL_FILE,
            revision=MODEL_REVISION,
            cache_dir=PROJECT_ROOT / "llm_cache",
            label="Model",
        )
        mmproj_path = None
        if VISION_ENABLED and MMPROJ_FILE:
            try:
                mmproj_path = _resolve_hf_file(
                    repo_id=MMPROJ_REPO,
                    configured_file=MMPROJ_FILE,
                    revision=MODEL_REVISION,
                    cache_dir=PROJECT_ROOT / "llm_cache",
                    label="Vision projector",
                )
            except Exception as exc:
                log.warning(
                    "Vision projector unavailable for repo=%s file=%s; continuing with vision disabled: %s",
                    MMPROJ_REPO,
                    MMPROJ_FILE,
                    exc,
                )
    log.info("Brain: %s", model_path)
    if mmproj_path:
        log.info("Vision projector: %s", mmproj_path)
    else:
        log.info("Vision projector: disabled")
    t0 = time.time()
    # Vision handler: carregado separado, ativado só durante chamadas com imagem via llm_lock.
    # mmproj roda em CPU/RAM (use_gpu=False) para preservar ~1.1 GB de VRAM para o modelo principal.
    vision_handler = None
    if VISION_ENABLED and mmproj_path:
        # PHASE3-NOTE: _create_vision_handler is not yet extracted. The architect
        # will wire this via a shared helper or leave it in mike_server.
        # For now, import from its home module when available.
        try:
            from mike_server import _create_vision_handler
        except ImportError:
            # Fallback: vision stays disabled if the handler factory is unavailable
            _create_vision_handler = None
            log.warning("_create_vision_handler not importable — vision disabled")
        if _create_vision_handler is not None:
            vision_handler, handler_backend = _create_vision_handler(mmproj_path)
            _shared_state.stats["vision_handler_backend"] = handler_backend
            mode_label = "GPU" if MMPROJ_USE_GPU else "CPU/RAM"
            log.info("Vision handler: loaded backend=%s mode=%s", handler_backend, mode_label)
    _shared_state.vision_handler = vision_handler

    if LLM_BACKEND in ("llama_server", "deepseek", "mock"):
        # Skip in-process GGUF loading for external backends.
        llm = True  # sentinela: qualquer valor truthy faz llm_ready=True
        _token_budget.configure(None, CTX_SIZE, DEFAULT_MAX_TOKENS)
        if LLM_BACKEND == "llama_server":
            log.info("LLM backend: local Qwen 3.6 via llama-server")
        else:
            log.info("LLM backend: Mock (no GPU required, instant replies)")
    else:
        llm = _create_llm_with_fallback(Llama, model_path)
        _token_budget.configure(llm, CTX_SIZE, DEFAULT_MAX_TOKENS)

    # --- Model Router: dynamic per-request backend selection ---
    global model_router
    # Register exactly the configured brain. Production is intentionally
    # single-brain: no silent switch from Qwen to mock or a cloud model.
    _llama_srv = MikeLlamaServerClient()
    _mock_mllm = MikeMockLLM(name="mock")  # injected from mike_server; lifecycle needs its own instance
    _router_backends = {}
    if LLM_BACKEND == "llama_server":
        if not _llama_srv.ready:
            raise RuntimeError(
                f"Qwen llama-server is required but unavailable at {_llama_srv.base_url}"
            )
        _router_backends = {"llama_server": _llama_srv}
        log.info("Single brain locked: Qwen 3.6 at %s", _llama_srv.base_url)
    elif LLM_BACKEND == "mock":
        _router_backends["mock"] = _mock_mllm
    elif LLM_BACKEND == "local" and llm is not None and llm is not True:
        _router_backends["local"] = llm
    else:
        raise RuntimeError(f"Unsupported or unavailable MIKE_LLM_BACKEND={LLM_BACKEND!r}")
    model_router = MikeModelRouter(backends=_router_backends)
    _shared_state.model_router = model_router
    log.info(
        "Model router: backends=%s healthy=%s",
        list(model_router.backends.keys()),
        model_router.healthy_backends,
    )

    # --- Fallback Chain: resilience layer with circuit breakers ---
    global fallback_chain
    _fallback_backends = []

    # Qwen 3.6 via local llama-server.
    if LLM_BACKEND == "llama_server":
        _cb_llama_srv = CircuitBreaker()
        def _llama_srv_chat(messages, **kwargs):
            return _llama_srv.chat_completion(messages=messages, model="mike", **kwargs)
        def _llama_srv_chat_stream(messages, **kwargs):
            for chunk in _llama_srv.chat_completion_stream(messages=messages, model="mike", **kwargs):
                yield chunk
        _fallback_backends.append(("llama_server", _llama_srv_chat, _cb_llama_srv, _llama_srv_chat_stream))

    # Mock is only a deliberately selected test backend, never a production
    # fallback for the family assistant.
    if LLM_BACKEND == "mock":
        _cb_mock = CircuitBreaker()
        def _mock_chat(messages, **kwargs):
            return _mock_mllm.chat_completion(messages=messages, model="mock-model", **kwargs)

        def _mock_chat_stream(messages, **kwargs):
            for chunk in _mock_mllm.chat_completion_stream(messages=messages, model="mock-model", **kwargs):
                yield chunk

        _fallback_backends.append(("mock", _mock_chat, _cb_mock, _mock_chat_stream))

    # (DeepSeek cloud backend removido — o Mike e estritamente local-only.
    #  Se MIKE_LLM_BACKEND=deepseek, o router cai no else e levanta RuntimeError.)

    # Local GGUF backend
    if LLM_BACKEND == "local" and llm is not None and llm is not True:
        _cb_local = CircuitBreaker()

        def _local_chat(messages, **kwargs):
            with llm_lock:
                if _has_images(messages):
                    if vision_handler is None:
                        from mike_models import VisionInputError
                        from mike_stats import _vision_limits
                        raise VisionInputError(
                            "Vision handler not available.",
                            code="vision_unavailable",
                            status_code=503,
                            details=_vision_limits(),
                        )
                    llm.chat_handler = vision_handler
                    try:
                        return llm.create_chat_completion(messages=messages, **kwargs)
                    finally:
                        llm.chat_handler = None
                else:
                    return llm.create_chat_completion(messages=messages, **kwargs)

        def _local_chat_stream(messages, **kwargs):
            kwargs["stream"] = True
            with llm_lock:
                if _has_images(messages):
                    from mike_models import VisionInputError
                    from mike_stats import _vision_limits
                    raise VisionInputError(
                        "Streaming de fotos nao esta disponivel neste runtime.",
                        code="vision_streaming_unsupported",
                        status_code=400,
                        details=_vision_limits(),
                    )
                response = llm.create_chat_completion(messages=messages, **kwargs)
                if isinstance(response, dict):
                    yield response
                    return
                for chunk in response:
                    yield chunk

        _fallback_backends.append(("local", _local_chat, _cb_local, _local_chat_stream))

    fallback_chain = FallbackChain(_fallback_backends)
    _shared_state.fallback_chain = fallback_chain
    log.info(
        "Fallback chain: backends=%s active=%s",
        fallback_chain.backend_names,
        fallback_chain.active_backend,
    )

    # PHASE3-NOTE: _FAMILY_IDENTIFY_PROFILE_MAP was defined in mike_server as
    # ``_context._family_identify_profile_map``.  After extraction we build it
    # directly from the SOUL dict loaded via shared_state.
    _family_map = _context._family_identify_profile_map
    _context.configure(
        _shared_state.SOUL,
        PROJECT_ROOT,
        get_monitor=_get_monitor,
        get_consciousness=_get_consciousness,
        get_verifier=_get_verifier,
        get_governance=_get_governance,
        get_autonomy=_get_autonomy,
        family_identify_profile_map=_family_map,
    )
    log.info("Mike is awake! Loaded in %.1fs", time.time() - t0)
    _shared_state.stats["boot_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared_state.stats.update(_build_backup_summary(BACKUP_DIR))

    # Create event bus for event-driven autonomy
    from mike_event_bus import MikeEventBus
    event_bus = MikeEventBus()
    _shared_state.event_bus = event_bus
    log.info("Event bus initialized — event-driven autonomy active")

    # Wire event bus into Twilio webhooks
    try:
        from mike_twilio_webhooks import set_event_bus as _twilio_set_event_bus
        _twilio_set_event_bus(event_bus)
        log.info("Event bus wired into Twilio webhooks")
    except ImportError as e:
        log.warning("[twilio] mike_twilio_webhooks unavailable, event bus wiring disabled: %s", e)

    # Sync runtime state to shared_state for routers
    _shared_state.llm = llm
    _shared_state.stats = _shared_state.stats
    _shared_state.SYSTEM_PROMPT = _shared_state.SYSTEM_PROMPT
    _shared_state.SOUL_PROMPT = _shared_state.SOUL_PROMPT
    _shared_state.SOUL = _shared_state.SOUL


# ---------------------------------------------------------------------------
# Proactive monitoring background task (Feature 2)
# ---------------------------------------------------------------------------

async def _proactive_monitor_loop() -> None:
    """Background task: runs heartbeat checks every 15 min, sends Telegram alerts."""
    await asyncio.sleep(90)  # cooldown after boot so everything else can initialise
    while True:
        try:
            from mike_heartbeat import MikeHeartbeat
            hb = MikeHeartbeat(event_bus=_shared_state.event_bus)
            alerts = await asyncio.to_thread(hb.run)
            if alerts:
                log.info("[PROATIVO] %d alerta(s) disparado(s) via Telegram", len(alerts))
                for a in alerts[:3]:
                    # Broadcast to open browser tabs (PWA push Feature 4)
                    try:
                        # PHASE3-NOTE: _broadcast_notification lives in mike_server.py.
                        # Import lazily to avoid circular dependency.
                        from mike_server import _broadcast_notification
                        _broadcast_notification(
                            title=f"Mike — {str(a.get('type', 'Alerta')).capitalize()}",
                            body=a.get("message", ""),
                            tag=f"proativo-{a.get('type', 'alert')}",
                        )
                    except Exception as exc:
                        log.warning('Telegram notifications unavailable: %s', exc)
            else:
                log.debug("[PROATIVO] heartbeat: sem alertas")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[PROATIVO] Heartbeat loop error: %s", exc)
        try:
            await asyncio.sleep(_PROACTIVE_INTERVAL)
        except asyncio.CancelledError:
            break


async def _drive_index_loop() -> None:
    """Background task: re-indexes Google Drive documents every 24h (configurable)."""
    await asyncio.sleep(120)  # cooldown — let server finish booting first
    while True:
        try:
            from mike_drive_indexer import MikeDriveIndexer
            indexer = MikeDriveIndexer(log_fn=log.info)
            result = await asyncio.to_thread(indexer.run)
            indexed = result.get("indexed", 0)
            if indexed > 0:
                log.info("[DRIVE] %d arquivo(s) novos indexados — reindexando RAG", indexed)
                await asyncio.to_thread(_shared_state.memory_service.reindex_knowledge)
                _shared_state.stats.update(_shared_state.memory_service.stats())
            else:
                log.debug("[DRIVE] Nenhum arquivo novo no Drive")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[DRIVE] Drive index loop error: %s", exc)
        try:
            await asyncio.sleep(_DRIVE_INDEX_INTERVAL)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# Lifespan (FastAPI asynccontextmanager)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app):
    """Minimal-bootstrap lifespan: server accepts requests immediately.

    Full startup (LLM, MCP, Vision, model router) and background tasks
    (autonomy, governance, missions, proactive monitor, drive indexer)
    run asynchronously after the server starts accepting connections.
    Health check reports "starting" until _shared_state.ready is True.
    """
    _shared_state.ready = False
    validate_security_config()
    (PROJECT_ROOT / "runtime" / "knowledge").mkdir(parents=True, exist_ok=True)
    WEB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ROADMAP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("  MIKE - Minimal bootstrap complete. Server online.")
    log.info("  Full startup (LLM, MCP, Vision) running in background...")
    log.info("=" * 60)

    # Background bootstrap: runs full _startup() then spawns background tasks.
    # All tasks are tracked so the CancelledError handler can shut them down.
    _autonomy_obj = None
    _missions_obj = None
    _gov_obj = None
    _bg_tasks: list = []

    async def _bootstrap_full():
        nonlocal _autonomy_obj, _missions_obj, _gov_obj, _bg_tasks
        try:
            try:
                await _startup()
                log.info("Bootstrap: _startup() completed OK")
            except Exception as _ex:
                log.exception("Bootstrap: _startup() FAILED: %s", _ex)
                raise

            # Background tasks — only spawn after heavy startup completes
            _bg_tasks.append(asyncio.create_task(_proactive_monitor_loop()))
            _bg_tasks.append(asyncio.create_task(_drive_index_loop()))

            _missions_obj = _get_missions()
            if _missions_obj:
                _bg_tasks.append(asyncio.create_task(_missions_obj.start_supervised()))
                log.info("Mission engine started as supervised background task")

            _gov_obj = _get_governance()
            if _gov_obj:
                _bg_tasks.append(asyncio.create_task(_gov_obj.start()))
                log.info("Governance loop started as background task")

            _autonomy_obj = _get_autonomy()
            if _autonomy_obj:
                _bg_tasks.append(asyncio.create_task(_autonomy_obj.start()))
                log.info("Autonomy engine started as background task")

            _shared_state.ready = True
            log.info("=" * 60)
            log.info("  MIKE - Fully operational. Ready for requests.")
            log.info("=" * 60)

            # Keep alive until cancelled
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Graceful shutdown of background subsystems
            if _autonomy_obj:
                _autonomy_obj.stop()
            if _missions_obj:
                _missions_obj.stop()
            if _gov_obj:
                _gov_obj.stop()
            for t in _bg_tasks:
                t.cancel()
            for t in _bg_tasks:
                with suppress(asyncio.CancelledError):
                    await t
            raise

    _bootstrap_task = asyncio.create_task(_bootstrap_full())

    try:
        yield  # <- Server starts accepting requests NOW
    finally:
        _shared_state.ready = False
        _bootstrap_task.cancel()
        with suppress(asyncio.CancelledError):
            await _bootstrap_task
        await _shared_state.deepseek_client.close()
        _shared_state.memory_service.close()


# ---------------------------------------------------------------------------
# Lazy-initialized subsystems (mirrored from mike_server.py)
#   PHASE3-NOTE: These are exact copies of the _get_* helpers from mike_server.
#   The architect will decide whether to keep them here or import from mike_server.
# ---------------------------------------------------------------------------

def _get_skill_registry():
    if _shared_state.skill_registry is None:
        try:
            from mike_skills import SkillRegistry
            skills_dir = str(PROJECT_ROOT / "skills")
            _shared_state.skill_registry = SkillRegistry(skills_dir)
            count = _shared_state.skill_registry.load_all()
            log.info("SkillRegistry: %d skills loaded from %s", count, skills_dir)
        except Exception as exc:
            log.warning("SkillRegistry not available: %s", exc)
    return _shared_state.skill_registry


def _get_skill_library():
    """Lazy-init SkillLibrary (Voyager pattern) for executable validated skills."""
    if _shared_state.skill_library is None:
        try:
            from mike_skill_library import SkillLibrary
            _shared_state.skill_library = SkillLibrary(
                store_dir=PROJECT_ROOT / "runtime" / "memory" / "skills_lib",
                log_fn=log.info,
            )
            bootstrapped = _shared_state.skill_library.bootstrap_defaults()
            if bootstrapped:
                log.info("SkillLibrary bootstrapped with %d default skills", bootstrapped)
            else:
                log.info("SkillLibrary initialized (Voyager pattern)")
        except Exception as exc:
            log.warning("SkillLibrary init failed: %s", exc)
    return _shared_state.skill_library


def _get_curriculum():
    if _shared_state.curriculum is None:
        try:
            from mike_curriculum import AutoCurriculum
            _shared_state.curriculum = AutoCurriculum(
                store_dir=PROJECT_ROOT / "runtime" / "memory" / "curriculum",
                reflection_store=_shared_state.reflection_store,
                skill_library=_shared_state.skill_library,
                log_fn=log.info,
            )
            log.info("Auto-curriculum initialized")
        except Exception as exc:
            log.warning("Curriculum init failed: %s", exc)
    return _shared_state.curriculum


def _get_tool_analyzer():
    if _shared_state.tool_analyzer is None:
        try:
            from mike_tool_analyzer import ToolFailureAnalyzer
            _shared_state.tool_analyzer = ToolFailureAnalyzer(
                store_dir=PROJECT_ROOT / "runtime" / "memory" / "tool_analyzer",
                reflection_store=_shared_state.reflection_store,
                log_fn=log.info,
            )
            log.info("Tool failure analyzer initialized")
        except Exception as exc:
            log.warning("Tool analyzer init failed: %s", exc)
    return _shared_state.tool_analyzer


def _get_governance():
    if _shared_state.governance is None:
        try:
            from mike_governance import GovernanceLoop
            tg_fn = None
            try:
                from mike_telegram import MikeTelegram
                tg = MikeTelegram(log_fn=log.info)
                if tg.enabled and tg.chat_marco:
                    tg_fn = lambda msg: tg.send_marco(msg)
            except Exception as e:
                log.warning("[governance] MikeTelegram unavailable, Telegram alerts disabled: %s", e)
            _shared_state.governance = GovernanceLoop(
                monitor=_get_monitor(),
                telegram_fn=tg_fn,
                stats=_shared_state.stats,
            )
        except Exception as exc:
            log.warning("Governance init failed: %s", exc)
    return _shared_state.governance


def _get_consciousness():
    if _shared_state.consciousness is None:
        try:
            from mike_project_memory import ProjectConsciousness
            _shared_state.consciousness = ProjectConsciousness()
        except Exception as exc:
            log.warning("ProjectConsciousness init failed: %s", exc)
    return _shared_state.consciousness


def _get_verifier():
    if _shared_state.verifier is None:
        try:
            from mike_verifier import OutputVerifier
            _shared_state.verifier = OutputVerifier(consciousness=_get_consciousness())
        except Exception as exc:
            log.warning("Verifier init failed: %s", exc)
    return _shared_state.verifier


def _get_missions():
    if _shared_state.missions is None:
        try:
            from mike_agent_sdk import AgentRegistry
            from mike_mission_engine import MissionEngine

            async def _mission_planner(goal: str, profile_key: str, max_steps: int) -> list[dict]:
                manifest = await _shared_state.mcp_workspace.list_tools() if _shared_state.mcp_workspace else []
                # PHASE3-NOTE: _build_task_mesh is not yet extracted — import lazily
                from mike_server import _build_task_mesh
                mesh = _build_task_mesh(profile_key, manifest)
                system_prompt = _shared_state.SYSTEM_PROMPT + "\n\n" + _context.build_dynamic_prefix(goal)
                tool_block = tool_instruction_block(manifest) or ""
                plan = await mesh.create_plan(goal, system_prompt, tool_block)
                return [
                    {
                        "id": str(step.id),
                        "description": step.description,
                    }
                    for step in plan.steps[:max(1, min(max_steps, TASK_MESH_MAX_PLAN_STEPS))]
                ]

            def _mission_registry_factory(profile_key: str):
                from mike_server import _make_agent_registry
                return _make_agent_registry(profile_key)


            async def _mission_tool_manifest(profile_key: str) -> list[dict]:
                all_tools = await _shared_state.mcp_workspace.list_tools() if _shared_state.mcp_workspace else []
                return filter_tool_manifest(all_tools, profile_key)

            def _mission_system_prompt(goal: str) -> str:
                return _shared_state.SYSTEM_PROMPT + "\n\n" + _context.build_dynamic_prefix(goal)

            async def _mission_calendar_event(
                title: str,
                start_iso: str,
                goal: str,
                profile_key: str,
            ) -> dict:
                start_text = (start_iso or "").strip()
                if not start_text:
                    return {"error": "start_iso is required"}
                if start_text.endswith("Z"):
                    start_text = start_text[:-1] + "+00:00"
                start_dt = datetime.fromisoformat(start_text)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                end_dt = start_dt + timedelta(minutes=30)
                from mike_server import _execute_mcp_tool
                return await _execute_mcp_tool(
                    "calendar.create_event",
                    {
                        "title": title,
                        "start": start_dt.astimezone(timezone.utc).isoformat(),
                        "end": end_dt.astimezone(timezone.utc).isoformat(),
                        "description": f"Mission: {goal}",
                    },
                    profile_key=profile_key,
                )

            from mike_server import _broadcast_notification

            _shared_state.missions = MissionEngine(
                planner_fn=_mission_planner,
                registry_factory=_mission_registry_factory,
                tool_manifest_fn=_mission_tool_manifest,
                system_prompt_fn=_mission_system_prompt,
                notify_fn=_broadcast_notification,
                calendar_event_fn=_mission_calendar_event,
                store_path=PROJECT_ROOT / "runtime" / "memory" / "missions" / "missions.json",
            )
        except Exception as exc:
            log.warning("Mission engine init failed: %s", exc)
    return _shared_state.missions


def _get_autonomy():
    if _shared_state.autonomy is not None:
        return _shared_state.autonomy
    try:
        from mike_autonomy import MikeAutonomy

        # Initialize Reflection Store (Reflexion pattern)
        if _shared_state.reflection_store is None:
            try:
                from mike_reflection import EpisodicReflectionStore
                _shared_state.reflection_store = EpisodicReflectionStore(
                    db_path=PROJECT_ROOT / "runtime" / "memory" / "reflections.db",
                    embedder=_shared_state.memory_service.local_store.embedder if _shared_state.memory_service else None,
                    log_fn=log.info,
                )
                _shared_state.reflection_store.initialize()
                log.info("Reflection store initialized")
            except Exception as exc:
                log.warning("Reflection store init failed: %s", exc)

        async def _auto_tool_block():
            manifest = await _shared_state.mcp_workspace.list_tools() if _shared_state.mcp_workspace else []
            return tool_instruction_block(manifest) or ""

        # Email search function for response tracking
        email_search_fn = None
        try:
            from mike_email_mcp import search_emails
            email_search_fn = search_emails
        except Exception as e:
            log.warning("[server] Failed to import email search for auto-reply: %s", e)

        # Wire auto-reply function for family email events
        auto_reply_fn = None
        try:
            from mike_auto_reply import auto_reply_to_family, build_llm_fn
            from mike_email_mcp import (
                list_inbox as _gmail_list_inbox,
                read_email as _gmail_read_email,
                send_email as _gmail_send_email,
            )

            def _auto_reply_send(to: str, subject: str, body: str) -> dict:
                response = _gmail_send_email(to, subject, body)
                ok = isinstance(response, str) and not response.lower().startswith("erro")
                return {"ok": ok, "text": response, "error": "" if ok else response}

            auto_reply_fn = lambda: auto_reply_to_family(
                list_inbox_fn=_gmail_list_inbox,
                read_email_fn=_gmail_read_email,
                send_email_fn=_auto_reply_send,
                llm_fn=build_llm_fn(_shared_state),
            )
        except Exception as exc:
            log.warning("Family auto-reply wiring unavailable: %s", exc)

        from mike_server import _get_cached_sdk_generate, _make_sdk_execute_fn, _broadcast_notification

        _shared_state.autonomy = MikeAutonomy(
            generate_fn=_get_cached_sdk_generate(),
            execute_tool_fn=_make_sdk_execute_fn("marco"),
            extract_tool_call_fn=mike_mcp_client.extract_tool_call,
            strip_tool_call_fn=mike_mcp_client.strip_tool_call_text,
            render_tool_result_fn=mike_mcp_client.render_tool_result_message,
            compact_tool_payload_fn=_get_compact_tool_payload(),
            tool_block_fn=_auto_tool_block,
            system_prompt=_shared_state.SYSTEM_PROMPT,
            notify_fn=_broadcast_notification,
            email_search_fn=email_search_fn,
            reflection_store=_shared_state.reflection_store,
            curriculum=_shared_state.curriculum,
            tool_analyzer=_shared_state.tool_analyzer,
            event_bus=_shared_state.event_bus,
            auto_reply_fn=auto_reply_fn,
            store_dir=PROJECT_ROOT / "runtime" / "memory" / "autonomy",
        )
        log.info("Autonomy engine initialized")
    except Exception as exc:
        log.warning("Autonomy engine init failed: %s", exc)
    return _shared_state.autonomy


def _get_monitor():
    if _shared_state.monitor is None:
        try:
            from mike_monitor import MikeMonitor
            _shared_state.monitor = MikeMonitor(log_fn=log.info)
        except Exception as exc:
            log.warning("Monitor init failed: %s", exc)
    return _shared_state.monitor


def _get_learner():
    if _shared_state.learner is None:
        try:
            from mike_learner import MikeLearner
            _shared_state.learner = MikeLearner(
                log_fn=log.info,
                memory=_shared_state.memory_service,
                graph=_shared_state.memory_service.graph if hasattr(_shared_state.memory_service, "graph") else None,
            )
        except Exception as exc:
            log.warning("Learner init failed: %s", exc)
    return _shared_state.learner


# PHASE3-NOTE: The following helpers are referenced by _get_autonomy above.
# They are exact copies of mike_server.py helpers the architect will deduplicate.

def _get_compact_tool_payload():
    """Lazy import to avoid circular dependency at module level."""
    from mike_tools_local import _compact_tool_payload
    return _compact_tool_payload
