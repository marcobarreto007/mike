"""
Tools routes: /v1/tools, /v1/tools/call, /v1/search/routes

Uses lazy imports from mike_server to avoid circular dependencies.
Helper functions live in mike_server.py until extracted to helpers.py.
"""
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mike_config import (
    MCP_TOOLS_ENABLED, MCP_TOOL_SERVER, MCP_ALLOWED_ROOTS,
    WEB_SEARCH_ENABLED, SEARCH_ROUTE_HINTS, RAG_ENABLED,
    MEMORY_TOP_K, KNOWLEDGE_TOP_K,
)
from mike_auth import profile_from_request, scoped_session_id, is_local_request, extract_api_key
import shared_state

router = APIRouter()


class ManualToolCallRequest(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)
    parameters: Optional[dict] = None

    def resolved_arguments(self) -> dict:
        args = self.arguments or {}
        legacy = self.parameters or {}
        if args and legacy and args != legacy:
            raise ValueError("Use apenas 'arguments' ou o legado 'parameters', nao ambos.")
        return args or legacy or {}


# ── GET /v1/tools ───────────────────────────────────────────────
@router.get("/v1/tools")
async def list_tools(request: Request):
    from mike_server import (
        _request_profile_scope, _visible_tool_manifest,
        _update_mcp_stats, _tools_payload,
    )
    profile_key = _request_profile_scope(request)
    manifest = await _visible_tool_manifest(profile_key, refresh=False)
    _update_mcp_stats(manifest)
    return _tools_payload(request, profile_key, manifest)


# ── POST /v1/tools/call ─────────────────────────────────────────
@router.post("/v1/tools/call")
async def call_tool(payload: ManualToolCallRequest, request: Request):
    from mike_server import _can_view_operational_details, _execute_mcp_tool
    profile_key = profile_from_request(request)
    if not _can_view_operational_details(request, profile_key):
        return JSONResponse(status_code=403, content={"error": "Tool runner manual disponivel apenas para owner/local."})
    try:
        arguments = payload.resolved_arguments()
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    result = await _execute_mcp_tool(payload.name, arguments, profile_key=profile_key)
    return {
        "name": payload.name,
        "arguments": arguments,
        "profile": profile_key,
        "result": result,
    }


# ── GET /v1/search/routes ───────────────────────────────────────
@router.get("/v1/search/routes")
async def search_routes(request: Request, q: str, session_id: Optional[str] = None):
    from mike_server import _search_routes_for_query, _should_search_web
    profile_key = profile_from_request(request)
    scoped = scoped_session_id(session_id, profile_key) if session_id else None
    rag_stats = {"memory_hits": 0, "knowledge_hits": 0}
    if RAG_ENABLED and q:
        try:
            _, rag_stats = shared_state.memory_service.build_context(
                q, memory_limit=MEMORY_TOP_K, knowledge_limit=KNOWLEDGE_TOP_K, session_id=scoped,
            )
        except Exception:
            pass
    routes = _search_routes_for_query(q, rag_stats, has_images=False, web_loaded=False)
    return {
        "query": q, "session_id": scoped, "routes": routes, "rag_stats": rag_stats,
        "web_should_search": WEB_SEARCH_ENABLED and _should_search_web(q, rag_stats),
        "search_route_hints_enabled": SEARCH_ROUTE_HINTS,
    }
