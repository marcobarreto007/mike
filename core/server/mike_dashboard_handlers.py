# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.
# Extracted from mike_server.py — Phase 3 refactor

"""
Mike - Dashboard & Chat Handler Wrappers
========================================
Thin handler wrappers called by routers/chat.py and routers/dashboard.py.
These delegate to the core functions defined in mike_server.py.

Functions:
  - _chat_completions_handler  (router: /v1/chat/completions)
  - _get_chat_sessions         (router: /v1/chat/sessions)
  - _get_chat_history          (router: /v1/chat/history)
  - _root_page                 (router: GET /)
  - _family_page               (router: GET /family)
  - _dashboard_page            (router: GET /dashboard)
  - _install_page              (router: GET /install)
  - _download_url_handler      (router: GET /download/mike.url)
  - _download_apk_handler      (router: GET /download/mike.apk)
  - _sw_js_handler             (router: GET /sw.js)
"""

import logging

from fastapi import BackgroundTasks, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from mike_config import DASHBOARD_DIR, PROJECT_ROOT

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory cache for dashboard index.html
# (mirrors the singleton in mike_server.py — shared across handler calls)
# ---------------------------------------------------------------------------

_index_html_cache = None
_index_html_mtime: float = 0


# -- chat wrappers (for routers/chat.py) --

async def _chat_completions_handler(request: Request):
    """Wrapper for routers/chat.py POST /v1/chat/completions"""
    from mike_server import chat_completions
    from mike_models import ChatRequest
    body = await request.json()
    req = ChatRequest(**{k: v for k, v in body.items() if k in ChatRequest.model_fields})
    bg = BackgroundTasks()
    return await chat_completions(req, request, bg)


async def _get_chat_sessions(request: Request):
    """Wrapper for routers/chat.py GET /v1/chat/sessions"""
    from mike_server import chat_sessions
    profile = request.query_params.get("profile")
    limit = int(request.query_params.get("limit", 10))
    return await chat_sessions(request, profile=profile, limit=limit)


async def _get_chat_history(request: Request):
    """Wrapper for routers/chat.py GET /v1/chat/history"""
    from mike_server import chat_history
    session_id = request.query_params.get("session_id", "main")
    profile = request.query_params.get("profile")
    limit = request.query_params.get("limit")
    limit = int(limit) if limit else None
    return await chat_history(request, session_id=session_id, profile=profile, limit=limit)


# -- dashboard wrappers --

async def _root_page(request: Request):
    """Wrapper for routers/dashboard.py GET /"""
    global _index_html_cache, _index_html_mtime
    if not DASHBOARD_DIR.exists():
        return HTMLResponse(content="<h1>Mike is alive!</h1>")
    index_file = DASHBOARD_DIR / "index.html"
    if index_file.exists():
        current_mtime = index_file.stat().st_mtime
        if _index_html_cache is None or current_mtime != _index_html_mtime:
            _index_html_cache = index_file.read_text(encoding="utf-8-sig")
            _index_html_mtime = current_mtime
        return HTMLResponse(
            content=_index_html_cache,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    return HTMLResponse(content="<h1>Mike is alive!</h1>")


async def _dashboard_page(request: Request):
    """Wrapper for routers/dashboard.py GET /dashboard (serves SPA index)"""
    return await _root_page(request)


async def _family_page(request: Request):
    """Wrapper for routers/dashboard.py GET /family"""
    family_file = DASHBOARD_DIR / "family.html"
    if family_file.exists():
        return HTMLResponse(
            content=family_file.read_text(encoding="utf-8-sig"),
            headers={"Cache-Control": "public, max-age=3600"},
        )
    return HTMLResponse(content="<h1>Familia Barreto</h1>")


def _install_page():
    """Wrapper for routers/dashboard.py GET /install"""
    if not DASHBOARD_DIR.exists():
        return HTMLResponse(content="<h1>Pagina de instalacao nao encontrada</h1>", status_code=404)
    install_file = DASHBOARD_DIR / "install.html"
    if install_file.exists():
        return HTMLResponse(
            content=install_file.read_text(encoding="utf-8-sig"),
            headers={"Cache-Control": "public, max-age=3600"},
        )
    return HTMLResponse(content="<h1>Pagina de instalacao nao encontrada</h1>", status_code=404)


async def _download_url_handler(request: Request):
    """Wrapper for routers/dashboard.py GET /download/mike.url"""
    target_url = str(request.base_url).rstrip("/") + "/"
    payload = "[InternetShortcut]\r\n" f"URL={target_url}\r\n"
    return PlainTextResponse(
        content=payload, media_type="application/internet-shortcut",
        headers={
            "Content-Disposition": 'attachment; filename="Mike da Mamae.url"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


def _download_apk_handler():
    """Wrapper for routers/dashboard.py GET /download/mike.apk"""
    apk_path = PROJECT_ROOT / "mobile" / "mike-debug.apk"
    if not apk_path.exists():
        return PlainTextResponse("APK nao disponivel. Execute o build primeiro.", status_code=404)
    from starlette.responses import FileResponse
    return FileResponse(
        path=str(apk_path), media_type="application/vnd.android.package-archive",
        filename="Mike.apk",
    )


def _sw_js_handler():
    """Wrapper for routers/dashboard.py GET /sw.js"""
    if not DASHBOARD_DIR.exists():
        return PlainTextResponse("Service worker nao encontrado.", status_code=404)
    sw_file = DASHBOARD_DIR / "sw.js"
    if not sw_file.exists():
        return PlainTextResponse("Service worker nao encontrado.", status_code=404)
    return Response(
        content=sw_file.read_bytes(),
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )
