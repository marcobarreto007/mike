# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.
# Extracted from mike_server.py — Phase 3 refactor

"""
Mike — Graph Knowledge routes (Phase 3 refactor).
==================================================
Routes: graph status, search, entity, and migration.
Uses lazy imports from mike_server for helper functions (avoids circular deps).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mike_auth import profile_from_request
import shared_state

log = logging.getLogger(__name__)

router = APIRouter()


def _can_view(request: Request, profile_key=None) -> bool:
    from mike_server import _can_view_operational_details as _f
    return _f(request, profile_key or profile_from_request(request))


@router.get("/v1/graph/status")
async def graph_status(request: Request):
    profile_key = profile_from_request(request)
    if not _can_view(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})
    return shared_state.memory_service.graph.status()


@router.get("/v1/graph/search")
async def graph_search(request: Request, q: str, limit: int = 10):
    profile_key = profile_from_request(request)
    if not _can_view(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})
    results = shared_state.memory_service.graph.query(q, limit=limit)
    return {"query": q, "results": results}


@router.get("/v1/graph/entity")
async def graph_entity(request: Request, name: str):
    profile_key = profile_from_request(request)
    if not _can_view(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})
    return shared_state.memory_service.graph.get_entity(name)


@router.post("/v1/graph/migrate")
async def graph_migrate(request: Request):
    profile_key = profile_from_request(request)
    if not _can_view(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})
    try:
        count = shared_state.memory_service.migrate_to_graph()
        return {"status": "ok", "migrated": count}
    except Exception as exc:
        log.warning("Graph migration failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})
