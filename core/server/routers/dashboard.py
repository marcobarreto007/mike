"""
Dashboard routes: /, /family, /dashboard, /download/*, /install, /sw.js
HTML/static file serving. Delegates to mike_server handlers via lazy import.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/")
async def root_page(request: Request):
    from mike_server import _root_page
    return await _root_page(request)


@router.get("/family")
async def family_page(request: Request):
    from mike_server import _family_page
    return await _family_page(request)


@router.get("/dashboard")
async def dashboard_page(request: Request):
    from mike_server import _dashboard_page
    return await _dashboard_page(request)


@router.get("/install")
async def install_page():
    from mike_server import _install_page
    return _install_page()


@router.get("/download/mike.url")
async def download_url(request: Request):
    from mike_server import _download_url_handler
    return await _download_url_handler(request)


@router.get("/download/mike.apk")
async def download_apk():
    from mike_server import _download_apk_handler
    return _download_apk_handler()


@router.get("/sw.js")
async def service_worker():
    from mike_server import _sw_js_handler
    return _sw_js_handler()
