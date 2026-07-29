# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

# Extracted from mike_server.py — Phase 3 refactor

"""
Mike - Middleware Module
==========================
HTTP middleware: authentication (API key / profile session) and
security headers (OWASP A05:2021 — Security Misconfiguration).
"""
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from mike_auth import (
    PROFILE_AUTH_ENABLED,
    extract_api_key,
    extract_profile_session,
    is_local_request,
    is_protected_path,
    verify_api_key,
)
from mike_config import API_KEY, TRUST_LOCALHOST

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

async def mike_auth_middleware(request: Request, call_next):
    request.state.profile_session = (
        extract_profile_session(request) if PROFILE_AUTH_ENABLED else None
    )
    if not API_KEY or not is_protected_path(request.url.path):
        return await call_next(request)
    if request.state.profile_session:
        return await call_next(request)
    if TRUST_LOCALHOST and is_local_request(request):
        return await call_next(request)
    provided_key = extract_api_key(request)
    if verify_api_key(provided_key):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={
            "error": "Mike API key required",
            "hint": (
                "Send Authorization: Bearer <MIKE_API_KEY>, X-Mike-Key, "
                "or authenticate with a Mike profile session."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Security headers middleware (OWASP A05:2021 — Security Misconfiguration)
# ---------------------------------------------------------------------------

async def mike_security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), payment=(), usb=(), "
        "microphone=(self)"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self' ws: wss:"
    )
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_https = request.url.scheme == "https" or any(
        value.strip().lower() == "https" for value in forwarded_proto.split(",")
    )
    if is_https:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response
