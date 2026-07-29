"""
System routes: /livez, /readyz, /health, /v1/models, /stats, /v1/runtime, /v1/monitor
"""
import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mike_config import (
    MODEL_ALIAS, HOST, API_KEY, DEFAULT_MAX_TOKENS,
    VISION_ENABLED, VISION_MAX_IMAGES, VISION_MAX_DECODED_BYTES,
    VISION_ALLOWED_MIME_TYPES, VISION_RUNTIME_PROFILE,
    PROJECT_ROOT,
)
from mike_auth import profile_from_request, PROFILE_AUTH_ENABLED, is_local_request
from mike_google_auth import (
    GOOGLE_WORKSPACE_SCOPES,
    GOOGLE_WORKSPACE_TOKEN_DEFAULTS,
    GOOGLE_WORKSPACE_TOKEN_ENV_NAMES,
    oauth_token_status,
)
import shared_state

router = APIRouter()

def _st(): return shared_state.stats
def _mcp(): return shared_state.mcp_workspace


def _readiness_probe_timeout() -> float:
    """Return a tightly bounded timeout for the active LLM probe."""
    try:
        configured = float(os.getenv("MIKE_READINESS_LLM_TIMEOUT_SECONDS", "0.75"))
    except ValueError:
        configured = 0.75
    return min(max(configured, 0.05), 2.0)


async def _probe_http_health(url: str, timeout_seconds: float) -> tuple[bool, str]:
    """Probe an HTTP health endpoint without using the backend's chat timeout."""
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
    return response.status_code == 200, f"http_{response.status_code}"


async def _probe_llm_backend(
    backend_name: str,
    backend: Any,
    timeout_seconds: float,
) -> dict:
    """Perform a bounded, non-inference readiness probe of the active backend."""
    started = time.monotonic()

    async def _probe() -> tuple[bool, str]:
        server_root = str(getattr(backend, "server_root", "") or "").rstrip("/")
        if backend_name == "llama_server" and server_root:
            return await _probe_http_health(f"{server_root}/health", timeout_seconds)

        # Keep potentially blocking third-party readiness properties off-loop.
        ready = await asyncio.to_thread(lambda: bool(getattr(backend, "ready", True)))
        return ready, "backend_ready" if ready else "backend_not_ready"

    try:
        available, detail = await asyncio.wait_for(_probe(), timeout=timeout_seconds)
    except TimeoutError:
        available, detail = False, "timeout"
    except Exception as exc:
        available, detail = False, f"probe_failed:{type(exc).__name__}"

    return {
        "status": "ok" if available else "unhealthy",
        "backend": backend_name or "unknown",
        "detail": detail,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    }


@router.get("/livez")
async def livez():
    """Process liveness probe: succeeds while the ASGI process can answer."""
    return {"status": "alive", "name": "Mike"}


@router.get("/readyz")
async def readyz():
    """Traffic readiness probe with a real, tightly bounded LLM check."""
    if not shared_state.ready:
        return JSONResponse(
            status_code=503,
            content={
                "status": "starting",
                "ready": False,
                "name": "Mike",
                "checks": {
                    "shared_state": {"status": "starting"},
                    "llm": {"status": "pending"},
                },
            },
        )

    try:
        base_health = await health()
        if isinstance(base_health, JSONResponse):
            base_health = {}
        base_checks = dict(base_health.get("checks") or {})
        base_checks["shared_state"] = {"status": "ok"}

        model_router = shared_state.model_router
        healthy_backends = model_router.healthy_backends if model_router else []
        backend_name = healthy_backends[0] if healthy_backends else ""
        backend = model_router.get_backend(backend_name) if backend_name else None

        if backend is None:
            llm_check = {
                "status": "unhealthy",
                "backend": backend_name or "none",
                "detail": "active_backend_missing",
                "latency_ms": 0.0,
            }
        else:
            llm_check = await _probe_llm_backend(
                backend_name,
                backend,
                _readiness_probe_timeout(),
            )
        base_checks["llm"] = llm_check

        base_status = str(base_health.get("status") or "unhealthy")
        if llm_check["status"] != "ok" or base_status == "unhealthy":
            status = "unhealthy"
        elif base_status != "healthy":
            status = "degraded"
        else:
            status = "ready"

        is_ready = status == "ready"
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": status,
                "ready": is_ready,
                "name": "Mike",
                "model": base_health.get("model", MODEL_ALIAS),
                "checks": base_checks,
            },
        )
    except Exception as exc:
        # Probes fail closed and stay machine-readable.
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "ready": False,
                "name": "Mike",
                "checks": {
                    "shared_state": {"status": "ok"},
                    "llm": {
                        "status": "unhealthy",
                        "detail": f"readiness_failed:{type(exc).__name__}",
                    },
                },
            },
        )


# ── Health ──────────────────────────────────────────────────────
@router.get("/health")
async def health():
    """Health check with dependency verification. Never returns 500."""

    # During bootstrap, report "starting" so load balancers / probes
    # know not to route traffic yet. Full startup runs in background.
    if not shared_state.ready:
        return {
            "status": "starting",
            "ready": False,
            "name": "Mike",
            "model": MODEL_ALIAS,
            "bind_host": HOST,
            "auth_enabled": bool(API_KEY),
            "profile_auth_enabled": PROFILE_AUTH_ENABLED,
            "mcp_tools_enabled": shared_state.mcp_workspace.enabled if shared_state.mcp_workspace else False,
            "note": "Full startup in progress — LLM, MCP, and background tasks not yet loaded.",
        }

    ms = shared_state.memory_service
    llm = shared_state.llm
    st = _st()
    mcp = _mcp()
    mcp_enabled = mcp.enabled if mcp else False

    checks = {}

    # The model router is authoritative. This avoids reporting a configured
    # but inactive client (for example DeepSeek) as Mike's active brain.
    model_router = shared_state.model_router
    healthy_backends = model_router.healthy_backends if model_router else []
    if healthy_backends:
        checks["llm"] = {"status": "ok", "backend": healthy_backends[0]}
    elif llm is not None and llm is not True:
        checks["llm"] = {"status": "ok", "backend": "local"}
    else:
        checks["llm"] = {"status": "unhealthy", "backend": "none"}

    # Memory
    checks["memory"] = {"status": "ok" if ms else "degraded"}

    # Disk
    try:
        disk_target = st.get("project_root") or "."
        disk = shutil.disk_usage(disk_target)
        free_gb = disk.free / (1024**3)
        free_pct = (disk.free / disk.total) * 100 if disk.total else 0.0
        min_free_gb = float(os.getenv("MIKE_HEALTH_DISK_MIN_GB", "2.0"))
        min_free_pct = float(os.getenv("MIKE_HEALTH_DISK_MIN_PCT", "5.0"))
        disk_ok = free_gb >= min_free_gb and free_pct >= min_free_pct
        checks["disk"] = {
            "status": "ok" if disk_ok else "degraded",
            "path": str(disk_target),
            "free_gb": round(free_gb, 1),
            "free_pct": round(free_pct, 1),
            "min_free_gb": min_free_gb,
            "min_free_pct": min_free_pct,
        }
    except Exception:
        checks["disk"] = {"status": "unknown"}

    # MCP
    checks["mcp"] = {"status": "ok" if mcp_enabled else "degraded", "enabled": mcp_enabled}

    # Overall
    if any(c.get("status") == "unhealthy" for c in checks.values()):
        overall = "unhealthy"
    elif any(c.get("status") == "degraded" for c in checks.values()):
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "name": "Mike",
        "model": MODEL_ALIAS,
        "llm_backend": st.get("llm_backend"),
        "active_model": st.get("active_model") or st.get("model_file") or MODEL_ALIAS,
        "uptime_since": st.get("boot_time"),
        "bind_host": HOST,
        "auth_enabled": bool(API_KEY),
        "profile_auth_enabled": PROFILE_AUTH_ENABLED,
        "mcp_tools_enabled": mcp_enabled,
        "checks": checks,
    }


# ── Models ──────────────────────────────────────────────────────
@router.get("/v1/models")
async def list_models():
    """OpenAI-compatible model listing."""
    return {
        "object": "list",
        "data": [{
            "id": MODEL_ALIAS,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "barreto-family",
        }],
    }


# ── Stats ───────────────────────────────────────────────────────
def _can_view_operational_details(request: Request, profile_key) -> bool:
    if profile_key == "marco":
        return True
    return is_local_request(request)


@router.get("/v1/integrations/status")
async def integrations_status(request: Request):
    """Credential-safe operational status for optional integrations."""
    profile_key = profile_from_request(request)
    if not _can_view_operational_details(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})

    try:
        _token_path, google_state = oauth_token_status(
            GOOGLE_WORKSPACE_TOKEN_ENV_NAMES,
            GOOGLE_WORKSPACE_TOKEN_DEFAULTS,
            GOOGLE_WORKSPACE_SCOPES,
        )
    except Exception:
        google_state = "unavailable"

    smtp_present = bool(
        os.getenv("MIKE_SMTP_USER", "").strip()
        and os.getenv("MIKE_SMTP_PASSWORD", "").strip()
    )
    telegram_configured = bool(
        os.getenv("MIKE_TELEGRAM_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and os.getenv("MIKE_TELEGRAM_BOT_TOKEN", "").strip()
        and (
            os.getenv("MIKE_TELEGRAM_CHAT_MARCO", "").strip()
            or os.getenv("MIKE_TELEGRAM_CHAT_FAMILIA", "").strip()
        )
    )
    twilio_configured = bool(
        os.getenv("MIKE_TWILIO_ACCOUNT_SID", "").strip()
        and os.getenv("MIKE_TWILIO_AUTH_TOKEN", "").strip()
        and os.getenv("MIKE_TWILIO_PHONE_FROM", "").strip()
    )

    mcp = _mcp()
    server_states = {
        str(item.get("name") or ""): item
        for item in (mcp.server_summaries() if mcp else [])
    }
    crawl = server_states.get("crawlconsole") or {}

    graph_status = {}
    memory_service = shared_state.memory_service
    if memory_service is not None and getattr(memory_service, "graph", None) is not None:
        try:
            graph_status = memory_service.graph.status()
        except Exception:
            graph_status = {"enabled": False, "ready": False}

    cloudflared_binary = (
        shutil.which("cloudflared")
        or str(PROJECT_ROOT / "bin" / "cloudflared.exe")
    )
    cloudflared_binary_present = Path(cloudflared_binary).exists()
    cloudflared_config_present = (
        Path.home() / ".cloudflared" / "config.yml"
    ).exists()
    default_profiles = list(_st().get("profile_auth_default_passwords") or [])
    github_token_present = bool(
        os.getenv("GITHUB_TOKEN", "").strip()
        or os.getenv("GH_TOKEN", "").strip()
    )
    brave_ready = bool(_st().get("brave_api_key_present"))
    remote_agent_url = os.getenv(
        "MIKE_REMOTE_AGENT_URL", "http://192.168.40.60:3000"
    ).strip()
    remote_agent_key_present = bool(
        os.getenv("MIKE_REMOTE_AGENT_KEY", "").strip()
    )

    integrations = {
        "google_workspace": {
            "ready": google_state == "ready",
            "state": google_state,
        },
        "email_imap_smtp": {
            "ready": False,
            "state": "configured_unverified" if smtp_present else "missing_credentials",
            "credentials_present": smtp_present,
        },
        "telegram": {
            "ready": telegram_configured,
            "state": "configured" if telegram_configured else "missing_credentials",
        },
        "twilio": {
            "ready": twilio_configured,
            "state": "configured" if twilio_configured else "missing_credentials",
        },
        "brave": {
            "ready": brave_ready,
            "state": "configured" if brave_ready else "missing_api_key",
            "fallback": "ddgs" if _st().get("web_search_ready") else None,
        },
        "crawlconsole": {
            "ready": bool(crawl.get("enabled")) and not crawl.get("last_error"),
            "state": (
                "ready"
                if crawl.get("enabled") and not crawl.get("last_error")
                else "authentication_failed"
            ),
            "last_error": crawl.get("last_error"),
        },
        "neo4j": {
            "ready": bool(graph_status.get("ready")),
            "state": "ready" if graph_status.get("ready") else "disabled_or_unavailable",
        },
        "cloudflare_tunnel": {
            "ready": cloudflared_binary_present and cloudflared_config_present,
            "state": (
                "configured"
                if cloudflared_binary_present and cloudflared_config_present
                else "binary_or_config_missing"
            ),
        },
        "family_profiles": {
            "ready": not default_profiles,
            "state": "ready" if not default_profiles else "passwords_missing",
            "profiles_missing_password": default_profiles,
        },
        "github_write": {
            "ready": github_token_present,
            "state": "configured" if github_token_present else "public_read_only",
        },
        "remote_agent": {
            "ready": False,
            "state": (
                "configured_unverified"
                if remote_agent_url and remote_agent_key_present
                else "missing_credentials"
            ),
            "url_configured": bool(remote_agent_url),
            "credentials_present": remote_agent_key_present,
        },
    }
    return {
        "all_ready": all(item["ready"] for item in integrations.values()),
        "integrations": integrations,
    }


@router.get("/stats")
async def get_stats(request: Request):
    """Server statistics. Restricted to owner/localhost."""
    profile_key = profile_from_request(request)
    if not _can_view_operational_details(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})
    return _st()


# ── Runtime ─────────────────────────────────────────────────────
@router.get("/v1/runtime")
async def get_runtime(request: Request):
    """Runtime configuration (public-safe subset)."""
    profile_key = profile_from_request(request)
    st = _st()
    payload = {
        "model_name": MODEL_ALIAS,
        "backend": st.get("llm_backend"),
        "active_model": st.get("active_model") or st.get("model_file") or MODEL_ALIAS,
        "gpu_layers": st.get("gpu_layers"),
        "ctx_size": st.get("ctx_size"),
        "n_batch": st.get("n_batch"),
        "n_ubatch": st.get("n_ubatch"),
        "n_threads": st.get("n_threads"),
        "n_threads_batch": st.get("n_threads_batch"),
        "flash_attn": st.get("flash_attn"),
        "offload_kqv": st.get("offload_kqv"),
        "default_max_tokens": DEFAULT_MAX_TOKENS,
        "runtime_profile": st.get("runtime_profile"),
        "rag_enabled": st.get("rag_enabled"),
        "web_search_enabled": st.get("web_search_enabled"),
        "bind_host": HOST,
        "auth_enabled": bool(API_KEY),
        "profile_auth_enabled": PROFILE_AUTH_ENABLED,
        "profile_auth_profiles": st.get("profile_auth_profiles", []),
        "vision_enabled": VISION_ENABLED,
        "vision_max_images": VISION_MAX_IMAGES,
        "vision_allowed_mime_types": list(VISION_ALLOWED_MIME_TYPES),
        "vision_runtime_profile": VISION_RUNTIME_PROFILE,
    }
    if _can_view_operational_details(request, profile_key):
        payload.update({
            "project_root": st.get("project_root"),
            "cors_origins": st.get("cors_origins"),
            "memory_backend": st.get("memory_backend"),
            "tensor_split": st.get("tensor_split"),
            "gpu_name": st.get("gpu_name"),
            "gpu_memory_total_mb": st.get("gpu_memory_total_mb"),
            "gpu_count": st.get("gpu_count"),
        })
    return payload


# ── Virtual Context Stats (MemGPT pattern) ────────────────────────
@router.get("/v1/context/stats")
async def get_context_stats(request: Request):
    """Virtual context manager stats (MemGPT-style infinite memory)."""
    vctx = shared_state.virtual_context
    if vctx is None:
        return JSONResponse(status_code=503, content={"error": "Virtual context not initialized"})
    return vctx.stats()
