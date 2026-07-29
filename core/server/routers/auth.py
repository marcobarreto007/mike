"""
Auth routes: /v1/auth/*, login, logout, session, magic links, identify, change-password.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from slowapi import Limiter
from slowapi.util import get_remote_address

from mike_config import PROJECT_ROOT
from mike_auth import (
    PROFILE_AUTH_ENABLED, PROFILE_CREDENTIALS, SESSION_COOKIE_NAME,
    SESSION_COOKIE_SAMESITE, SESSION_COOKIE_SECURE, SESSION_TTL_HOURS,
    MAGIC_LINK_TTL_DAYS,
    extract_api_key, extract_profile_session, verify_api_key,
    issue_profile_session, decode_profile_session, profile_payload,
    verify_profile_password, change_profile_password,
    generate_magic_token, validate_magic_token, list_magic_tokens,
    revoke_magic_token,
)
from mike_context import match_dashboard_profile_from_identity as _match_dashboard_profile

router = APIRouter()

# Rate limiter -- decorator stores metadata on endpoints; the SlowAPIMiddleware
# (attached to app.state.limiter in mike_server.py) enforces the limits.
limiter = Limiter(key_func=get_remote_address)

_MAGIC_LINK_OWNER_PROFILES = frozenset({"marco", "anapaula"})


def _can_manage_magic_links(request: Request) -> bool:
    """Require an owner session or a valid API key for token administration."""
    session_payload = extract_profile_session(request)
    session_profile = str((session_payload or {}).get("profile") or "").strip().lower()
    if session_profile in _MAGIC_LINK_OWNER_PROFILES:
        return True
    return verify_api_key(extract_api_key(request))


# ── Pydantic models ─────────────────────────────────────────────
class MagicLinkGenerateRequest(BaseModel):
    profile: str
    ttl_days: Optional[int] = None


class MagicLinkUseRequest(BaseModel):
    token: str


class ProfileLoginRequest(BaseModel):
    profile: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


# ── Magic link (passwordless login via WhatsApp / QR code) ──────
@router.post("/v1/auth/magic/generate")
@limiter.limit("3/minute")
async def magic_generate(payload: MagicLinkGenerateRequest, request: Request):
    if not _can_manage_magic_links(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado. Requer sessao de owner ou MIKE_API_KEY."})
    profile_key = str(payload.profile or "").strip().lower()
    if profile_key not in PROFILE_CREDENTIALS:
        return JSONResponse(status_code=400, content={"error": f"Perfil desconhecido: {profile_key}"})
    ttl = int(payload.ttl_days or MAGIC_LINK_TTL_DAYS)
    token = generate_magic_token(profile_key, ttl_days=ttl)
    tunnel_file = PROJECT_ROOT / "data" / "tunnel_url_atual.txt"
    base = tunnel_file.read_text(encoding="utf-8-sig").splitlines()[0].strip() if tunnel_file.exists() else str(request.base_url).rstrip("/")
    magic_url = f"{base}/?magic={token}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={magic_url}"
    install_url = f"{base}/install/{profile_key}"
    return {
        "profile": profile_key,
        "magic_url": magic_url,
        "install_url": install_url,
        "qr_image_url": qr_url,
        "ttl_days": ttl,
        "token_preview": token[:8] + "...",
    }


@router.post("/v1/auth/magic/use")
async def magic_use(payload: MagicLinkUseRequest):
    profile_key = validate_magic_token((payload.token or "").strip())
    if not profile_key:
        return JSONResponse(status_code=401, content={"error": "Link invalido ou expirado. Peca um novo link."})
    magic_ttl_hours = MAGIC_LINK_TTL_DAYS * 24
    session_token = issue_profile_session(profile_key, ttl_hours=magic_ttl_hours)
    session_payload = decode_profile_session(session_token)
    response = JSONResponse({
        "status": "ok",
        "profile": profile_payload(profile_key, session_payload),
    })
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=magic_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
        path="/",
    )
    return response


@router.get("/v1/auth/magic/list")
async def magic_list(request: Request):
    if not _can_manage_magic_links(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado."})
    return {"tokens": list_magic_tokens()}


@router.delete("/v1/auth/magic/{token_id}")
async def magic_revoke(token_id: str, request: Request):
    if not _can_manage_magic_links(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado."})
    ok = revoke_magic_token(token_id)
    return {"revoked": ok}


# ── Login / Session / Logout ────────────────────────────────────
@router.post("/v1/auth/login")
@limiter.limit("5/minute")
async def auth_login(request: Request, payload: ProfileLoginRequest):
    if not PROFILE_AUTH_ENABLED:
        return JSONResponse(status_code=503, content={"error": "Profile auth is disabled"})
    profile_key = str(payload.profile or "").strip().lower()
    if not verify_profile_password(profile_key, payload.password):
        return JSONResponse(status_code=401, content={"error": "Senha incorreta para este perfil."})
    session_token = issue_profile_session(profile_key)
    session_payload = decode_profile_session(session_token)
    response = JSONResponse({
        "status": "ok",
        "ttl_hours": SESSION_TTL_HOURS,
        "profile": profile_payload(profile_key, session_payload),
    })
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite=SESSION_COOKIE_SAMESITE,
        secure=SESSION_COOKIE_SECURE,
        path="/",
    )
    return response


@router.get("/v1/auth/session")
async def auth_session(request: Request):
    session_payload = extract_profile_session(request) if PROFILE_AUTH_ENABLED else None
    if not session_payload:
        return JSONResponse(status_code=401, content={"authenticated": False})
    profile_key = str(session_payload["profile"])
    return {
        "authenticated": True,
        "ttl_hours": SESSION_TTL_HOURS,
        "profile": profile_payload(profile_key, session_payload),
    }


@router.post("/v1/auth/logout")
async def auth_logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response


# ── Identify ────────────────────────────────────────────────────
@router.post("/v1/auth/identify")
async def auth_identify(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "JSON invalido"})
    text = str(body.get("text", "")).strip().lower()
    if not text:
        return JSONResponse({"profile": None})
    dash_profile = _match_dashboard_profile(text)
    if dash_profile:
        # Identification is a UI hint, never an authentication mechanism.
        return JSONResponse({"profile": dash_profile})
    return JSONResponse({"profile": None})


# ── Change password ─────────────────────────────────────────────
@router.post("/v1/auth/change-password")
async def auth_change_password(payload: PasswordChangeRequest, request: Request):
    if not PROFILE_AUTH_ENABLED:
        return JSONResponse(status_code=503, content={"error": "Profile auth is disabled"})
    session_payload = extract_profile_session(request)
    if not session_payload:
        return JSONResponse(status_code=401, content={"error": "Nao autenticado."})
    profile_key = str(session_payload["profile"])
    ok, error = change_profile_password(profile_key, payload.old_password, payload.new_password)
    if not ok:
        return JSONResponse(status_code=400, content={"error": error})
    return {"status": "ok", "message": "Senha alterada com sucesso."}
