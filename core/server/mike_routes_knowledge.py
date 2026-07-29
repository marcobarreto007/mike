# Extracted from mike_server.py — Phase 3 refactor
# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike - Knowledge & Bootstrap Routes
====================================
Routes for tunnel URL, events stats, client bootstrap, roadmap, backups,
web search, knowledge reindex/upsert, and Drive indexing.

Extracted from mike_server.py — Phase 3 refactor.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mike_config import (
    API_KEY,
    BACKUP_DIR,
    BACKUP_SCRIPT,
    PROJECT_ROOT,
    ROADMAP_FILE,
    WEB_CACHE_DIR,
    WEB_TOP_K,
)
from mike_auth import (
    PROFILE_AUTH_ENABLED,
    extract_profile_session,
    filter_tool_manifest,
    profile_from_request,
    profile_payload,
)
from mike_models import KnowledgeUpsertRequest
from mike_request_helpers import (
    _can_view_operational_details,
    _request_profile_scope,
)
from mike_tools_local import _local_tool_manifest
from mike_stats import _update_mcp_stats as _update_mcp_stats_base
from mike_payload_helpers import (
    _chat_capabilities_payload as _chat_capabilities_payload_base,
    _tool_summary_payload as _tool_summary_payload_base,
    _vision_capabilities_payload,
)
import shared_state

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Accessor helpers for shared_state singletons (resolved at call time)
# ---------------------------------------------------------------------------

def _mcp():
    return shared_state.mcp_workspace


def _ms():
    return shared_state.memory_service


def _ws():
    return shared_state.web_search


def _st():
    return shared_state.stats


# ---------------------------------------------------------------------------
# Wrappers bridging mike_payload_helpers / mike_stats to shared_state
# ---------------------------------------------------------------------------

def _update_mcp_stats(tool_manifest: Optional[List[dict]] = None) -> None:
    return _update_mcp_stats_base(
        _st(),
        mcp_workspace=_mcp(),
        tool_manifest=tool_manifest,
        local_tool_manifest_fn=_local_tool_manifest,
    )


def _tool_summary_payload(tool_manifest: List[dict]) -> dict:
    return _tool_summary_payload_base(tool_manifest, mcp_workspace=_mcp())


def _chat_capabilities_payload() -> dict:
    return _chat_capabilities_payload_base(llm=shared_state.llm)


# ---------------------------------------------------------------------------
# Roadmap & backup helpers (extracted from mike_server.py)
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
# Route: tunnel URL
# ---------------------------------------------------------------------------

@router.get("/v1/tunnel-url")
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


# ---------------------------------------------------------------------------
# Route: events stats
# ---------------------------------------------------------------------------

@router.get("/v1/events/stats")
async def events_stats():
    """Return event bus statistics — event counts and subscriber info."""
    bus = shared_state.event_bus
    if bus is None:
        return JSONResponse(status_code=503, content={"error": "Event bus nao inicializado"})
    return bus.stats()


# ---------------------------------------------------------------------------
# Route: client bootstrap
# ---------------------------------------------------------------------------

@router.get("/v1/client/bootstrap")
async def client_bootstrap(request: Request):
    profile_key = _request_profile_scope(request)
    session_payload = (
        extract_profile_session(request) if PROFILE_AUTH_ENABLED else None
    )
    # Bootstrap must never wait for MCP discovery. During cold startup the
    # manifest is populated in the background; return the currently cached
    # remote tools plus local tools so the login screen remains instant.
    remote_manifest = list(getattr(_mcp(), "tool_manifest", None) or [])
    manifest = filter_tool_manifest(remote_manifest, profile_key)
    manifest.extend(filter_tool_manifest(_local_tool_manifest(), profile_key))
    _update_mcp_stats(manifest)
    return {
        "authenticated": bool(session_payload),
        "profile": profile_payload(profile_key, session_payload) if session_payload and profile_key else None,
        "profile_auth_enabled": PROFILE_AUTH_ENABLED,
        "auth_enabled": bool(API_KEY),
        "ops_access": _can_view_operational_details(request, profile_key),
        "tool_summary": _tool_summary_payload(manifest),
        "vision": _vision_capabilities_payload(),
        "chat": _chat_capabilities_payload(),
    }


# ---------------------------------------------------------------------------
# Route: roadmap
# ---------------------------------------------------------------------------

@router.get("/v1/roadmap")
async def roadmap():
    payload = _load_roadmap()
    if not payload:
        return JSONResponse(status_code=404, content={"error": "Roadmap JSON not found"})
    return {"roadmap": payload, "summary": _build_roadmap_summary(payload)}


# ---------------------------------------------------------------------------
# Route: list backups
# ---------------------------------------------------------------------------

@router.get("/v1/backups")
async def list_backups(request: Request, limit: int = 10):
    profile_key = profile_from_request(request)
    items = _list_backup_archives(BACKUP_DIR, limit=max(1, min(limit, 50)))
    summary = _build_backup_summary(BACKUP_DIR)
    if not _can_view_operational_details(request, profile_key):
        items = [{k: v for k, v in item.items() if k != "path"} for item in items]
        summary = {
            key: value
            for key, value in summary.items()
            if key not in {"backup_dir", "backup_script"}
        }
    return {"items": items, "summary": summary}


# ---------------------------------------------------------------------------
# Route: web search
# ---------------------------------------------------------------------------

@router.get("/v1/web/search")
async def search_web(q: str, limit: int = WEB_TOP_K):
    try:
        results = _ws().search(q, count=limit)
    except Exception as exc:
        log.warning("Manual web search failed for query %r: %s", q, exc)
        results = []
    cached_file = None
    if results:
        cached = _ms().cache_web_results(
            q,
            results,
            WEB_CACHE_DIR,
            provider=_ws().last_provider_used or _ws().active_provider,
        )
        _st().update(_ms().stats())
        cached_file = str(cached) if cached else None
    _st()["last_web_hits"] = len(results)
    _st()["last_web_provider"] = _ws().last_provider_used if results else "error"
    return {
        "query": q,
        "provider": _ws().last_provider_used if results else _ws().active_provider,
        "web_search_ready": _ws().provider_ready,
        "brave_api_key_present": _ws().brave_ready,
        "cached_file": cached_file,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Route: reindex knowledge
# ---------------------------------------------------------------------------

@router.post("/v1/knowledge/reindex")
async def reindex_knowledge():
    indexed = _ms().reindex_knowledge(rebuild_lightrag=False)
    _st().update(_ms().stats())
    return {
        "status": "ok",
        "indexed_sources": indexed,
        "lightrag_reindexed": False,
        **_ms().stats(),
    }


# ---------------------------------------------------------------------------
# Route: upsert knowledge file
# ---------------------------------------------------------------------------

@router.post("/v1/knowledge/upsert")
async def upsert_knowledge_file(request: KnowledgeUpsertRequest):
    target = Path(request.path).expanduser()
    if not target.is_absolute():
        target = (PROJECT_ROOT / target).resolve()
    else:
        target = target.resolve()

    ms = _ms()
    previous_vector = bool(ms.local_store.embedder.enabled)
    previous_lightrag = bool(ms.lightrag.enabled)
    effective_lightrag = bool(request.enable_lightrag) and previous_lightrag

    try:
        ms.local_store.embedder.enabled = bool(request.enable_vector)
        ms.lightrag.enabled = effective_lightrag
        updated = ms.upsert_knowledge_file(target)
    finally:
        ms.local_store.embedder.enabled = previous_vector
        ms.lightrag.enabled = previous_lightrag

    if not updated:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "updated": False, "path": str(target), "error": "File not indexed"},
        )

    _st().update(ms.stats())
    return {
        "status": "ok",
        "updated": True,
        "path": str(target),
        "vector_enabled": bool(request.enable_vector),
        "lightrag_enabled": effective_lightrag,
        **ms.stats(),
    }


# ---------------------------------------------------------------------------
# Route: drive index now
# ---------------------------------------------------------------------------

@router.post("/v1/drive/index")
async def drive_index_now(request: Request):
    """Trigger Drive indexing immediately (Feature 5). Owner/local only."""
    profile_key = profile_from_request(request)
    if not _can_view_operational_details(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})
    try:
        from mike_drive_indexer import MikeDriveIndexer
        indexer = MikeDriveIndexer(log_fn=log.info)
        result = await asyncio.to_thread(indexer.run)
        if result.get("indexed", 0) > 0:
            await asyncio.to_thread(_ms().reindex_knowledge)
            _st().update(_ms().stats())
        return {"status": "ok", **result}
    except Exception as exc:
        log.warning("Drive index failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})
