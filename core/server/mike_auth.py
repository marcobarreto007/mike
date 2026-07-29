# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike - Authentication Module
==============================
Profile credentials, session tokens (HMAC), password hashing,
middleware helpers, and tool permission enforcement.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import Request

from mike_config import (
    API_KEY,
    ALLOW_INSECURE_LAN,
    ALLOW_UNAUTH_HEALTHCHECK,
    HOST,
    PROJECT_ROOT,
    TRUST_LOCALHOST,
    env_bool,
    env_int,
    is_public_bind_host,
    normalize_session_id,
)

_log = logging.getLogger("mike")


# ---------------------------------------------------------------------------
# Profile auth settings
# ---------------------------------------------------------------------------

PROFILE_AUTH_ENABLED = env_bool("MIKE_PROFILE_AUTH_ENABLED", False)
SESSION_COOKIE_NAME = (
    os.getenv("MIKE_SESSION_COOKIE_NAME", "mike_session").strip() or "mike_session"
)
SESSION_TTL_HOURS = max(1, env_int("MIKE_SESSION_TTL_HOURS", 12))
SESSION_COOKIE_SECURE = env_bool("MIKE_SESSION_COOKIE_SECURE", True)
SESSION_COOKIE_SAMESITE = (
    os.getenv("MIKE_SESSION_COOKIE_SAMESITE", "strict").strip().lower() or "strict"
)


def _resolve_session_secret() -> str:
    env_secret = os.getenv("MIKE_SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret
    if API_KEY:
        return API_KEY
    secret_file = PROJECT_ROOT / "runtime" / "memory" / ".session_secret"
    if secret_file.exists():
        stored = secret_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    new_secret = secrets.token_urlsafe(32)
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(new_secret, encoding="utf-8")
    return new_secret


SESSION_SECRET = _resolve_session_secret()


# ---------------------------------------------------------------------------
# Magic link tokens (passwordless login via WhatsApp link / QR code)
# ---------------------------------------------------------------------------

MAGIC_LINK_TTL_DAYS = max(1, env_int("MIKE_MAGIC_LINK_TTL_DAYS", 90))

_magic_tokens: dict[str, dict] = {}


def _magic_tokens_file() -> Path:
    configured = os.getenv("MIKE_MAGIC_TOKENS_FILE", "").strip()
    if configured:
        return Path(configured)
    return PROJECT_ROOT / "runtime" / "memory" / "magic_tokens.json"


def _new_magic_token_id() -> str:
    return secrets.token_hex(8)


def _load_magic_tokens() -> None:
    path = _magic_tokens_file()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        now = int(time.time())
        changed = False
        for token, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("exp", 0) <= now:
                changed = True
                continue
            normalized = dict(entry)
            token_id = str(normalized.get("id") or "").strip()
            if not token_id:
                normalized["id"] = _new_magic_token_id()
                changed = True
            _magic_tokens[str(token).strip()] = normalized
        if changed:
            _save_magic_tokens()
    except Exception:
        _log.exception("Failed to load magic tokens from disk")


def _save_magic_tokens() -> None:
    path = _magic_tokens_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_magic_tokens, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as _e:
        _log.warning("Failed to persist magic tokens: %s", _e)


def generate_magic_token(profile_key: str, ttl_days: int = MAGIC_LINK_TTL_DAYS) -> str:
    """Generate a revocable single-use magic login token for a profile."""
    token = secrets.token_urlsafe(32)
    _magic_tokens[token] = {
        "id": _new_magic_token_id(),
        "profile": profile_key,
        "exp": int(time.time()) + ttl_days * 86400,
        "created_at": int(time.time()),
    }
    _save_magic_tokens()
    return token


def validate_magic_token(token: str) -> Optional[str]:
    """Validate and consume a magic token. Returns profile_key or None."""
    normalized_token = (token or "").strip()
    entry = _magic_tokens.get(normalized_token)
    if not entry:
        return None
    if entry.get("exp", 0) <= int(time.time()):
        _magic_tokens.pop(normalized_token, None)
        _save_magic_tokens()
        return None
    profile_key = str(entry.get("profile", "")).strip().lower()
    if profile_key not in PROFILE_CREDENTIALS:
        _magic_tokens.pop(normalized_token, None)
        _save_magic_tokens()
        return None
    _magic_tokens.pop(normalized_token, None)
    _save_magic_tokens()
    return profile_key


def revoke_magic_token(token_id: str) -> bool:
    """Revoke a magic token permanently by opaque id."""
    normalized_id = str(token_id or "").strip()
    if not normalized_id:
        return False
    for token, entry in list(_magic_tokens.items()):
        if str(entry.get("id") or "").strip() == normalized_id:
            del _magic_tokens[token]
            _save_magic_tokens()
            return True
    return False


def list_magic_tokens() -> list:
    """List all active magic tokens without exposing the raw secret."""
    now = int(time.time())
    return [
        {
            "id": v["id"],
            "token_preview": k[:8] + "...",
            "profile": v["profile"],
            "display_name": _DISPLAY_NAMES.get(v["profile"], v["profile"].capitalize()),
            "expires_at": datetime.fromtimestamp(v["exp"], tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "created_at": datetime.fromtimestamp(v.get("created_at", 0), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "valid": v["exp"] > now,
        }
        for k, v in list(_magic_tokens.items())
    ]


# Load persisted tokens on import
_load_magic_tokens()


# ---------------------------------------------------------------------------
# Password hashing / credentials
# ---------------------------------------------------------------------------

DEFAULT_PROFILE_PASSWORDS = {
    "marco": None,
    "anapaula": None,
    "raphael": None,
    "alice": None,
    "matheus": None,
    "marilene": None,
    "visitante": None,
}


def password_hash(profile_key: str, password: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        f"mike-profile::{profile_key}".encode("utf-8"),
        200_000,
    ).hex()


_DISPLAY_NAMES = {
    "marco": "Marco",
    "anapaula": "Ana Paula",
    "raphael": "Raphael",
    "alice": "Alice",
    "matheus": "Matheus",
    "marilene": "Marilene",
    "visitante": "Visitante",
}


def _load_profile_credentials() -> dict:
    credentials = {}
    for profile_key, default_password in DEFAULT_PROFILE_PASSWORDS.items():
        env_hash = os.getenv(
            f"MIKE_PROFILE_{profile_key.upper()}_PASSWORD_HASH", ""
        ).strip().lower()
        env_password = os.getenv(
            f"MIKE_PROFILE_{profile_key.upper()}_PASSWORD"
        )
        if env_hash:
            pw_hash = env_hash
        elif env_password not in (None, ""):
            pw_hash = password_hash(profile_key, env_password)
        elif default_password is not None:
            pw_hash = password_hash(profile_key, default_password)
        else:
            _log.warning(
                "Profile '%s' has no password configured. "
                "Set MIKE_PROFILE_%s_PASSWORD or MIKE_PROFILE_%s_PASSWORD_HASH to enable.",
                profile_key, profile_key.upper(), profile_key.upper(),
            )
            continue

        display = _DISPLAY_NAMES.get(profile_key, profile_key.capitalize())
        credentials[profile_key] = {
            "profile": profile_key,
            "name": display,
            "password_hash": pw_hash,
        }
    return credentials


PROFILE_CREDENTIALS = _load_profile_credentials()

PROFILE_DEFAULT_PASSWORDS_IN_USE = [
    key
    for key in DEFAULT_PROFILE_PASSWORDS
    if not os.getenv(f"MIKE_PROFILE_{key.upper()}_PASSWORD_HASH", "").strip()
    and os.getenv(f"MIKE_PROFILE_{key.upper()}_PASSWORD") in (None, "")
]


# ---------------------------------------------------------------------------
# Session tokens (HMAC-based)
# ---------------------------------------------------------------------------

def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def issue_profile_session(profile_key: str, ttl_hours: Optional[int] = None) -> str:
    effective_ttl = ttl_hours if ttl_hours is not None else SESSION_TTL_HOURS
    payload = {
        "v": 1,
        "profile": profile_key,
        "iat": int(time.time()),
        "exp": int(time.time()) + effective_ttl * 3600,
    }
    encoded_payload = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_b64url_encode(signature)}"


def decode_profile_session(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        expected_signature = _b64url_encode(
            hmac.new(
                SESSION_SECRET.encode("utf-8"),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(encoded_signature, expected_signature):
            return None
        payload = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
    except Exception:
        return None
    if payload.get("v") != 1:
        return None
    profile_key = str(payload.get("profile") or "").strip().lower()
    exp = int(payload.get("exp") or 0)
    if profile_key not in PROFILE_CREDENTIALS or exp <= int(time.time()):
        return None
    return payload


def extract_profile_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    if not token:
        token = request.headers.get("x-mike-session", "").strip()
    return decode_profile_session(token)


def verify_profile_password(profile_key: str, password: str) -> bool:
    credential = PROFILE_CREDENTIALS.get(profile_key)
    if not credential:
        return False
    expected = credential["password_hash"]
    candidate = password_hash(profile_key, password or "")
    return hmac.compare_digest(candidate, expected)


def change_profile_password(profile_key: str, old_password: str, new_password: str) -> tuple:
    """Change a profile's password. Returns (ok: bool, error: str|None)."""
    if not verify_profile_password(profile_key, old_password):
        return False, "Senha atual incorreta."
    new_password = (new_password or "").strip()
    if len(new_password) < 4:
        return False, "Nova senha deve ter pelo menos 4 caracteres."
    new_hash = password_hash(profile_key, new_password)
    PROFILE_CREDENTIALS[profile_key]["password_hash"] = new_hash
    _log.info("Password changed for profile: %s", profile_key)
    return True, None


def profile_payload(profile_key: str, session_payload: Optional[dict] = None) -> dict:
    credential = PROFILE_CREDENTIALS[profile_key]
    expires_at = None
    if session_payload:
        expires_at = (
            datetime.fromtimestamp(int(session_payload["exp"]), tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    return {
        "profile": credential["profile"],
        "name": credential["name"],
        "expires_at": expires_at,
    }


def scoped_session_id(raw_session_id: Optional[str], profile_key: Optional[str]) -> str:
    normalized = normalize_session_id(raw_session_id)
    if not profile_key:
        return normalized
    prefix = f"{profile_key}-"
    if normalized == profile_key or normalized.startswith(prefix):
        return normalized
    return normalize_session_id(f"{profile_key}-{normalized}")


# ---------------------------------------------------------------------------
# Security helpers used in middleware / routes
# ---------------------------------------------------------------------------

def validate_security_config() -> None:
    if is_public_bind_host(HOST) and not API_KEY and not ALLOW_INSECURE_LAN:
        raise RuntimeError(
            "Mike refuses to bind outside localhost without MIKE_API_KEY. "
            "Set MIKE_API_KEY or explicitly allow insecure LAN with MIKE_ALLOW_INSECURE_LAN=true."
        )


def _request_host(value: Optional[str]) -> str:
    """Normalize a host/IP value without accepting its optional port."""
    normalized = str(value or "").strip().strip('"').lower()
    if not normalized:
        return ""
    if normalized.startswith("["):
        closing = normalized.find("]")
        if closing > 0:
            return normalized[1:closing].split("%", 1)[0]
    if normalized.count(":") == 1:
        host, port = normalized.rsplit(":", 1)
        if port.isdigit():
            normalized = host
    return normalized.split("%", 1)[0]


def _is_loopback_host(value: Optional[str]) -> bool:
    normalized = _request_host(value)
    return bool(normalized) and not is_public_bind_host(normalized)


def is_local_request(request: Request) -> bool:
    """Return true only when every observable request hop is loopback."""
    peer_host = request.client.host if request.client else ""
    if not _is_loopback_host(peer_host):
        return False

    # A public Host behind a loopback proxy/tunnel is still a remote request.
    request_host = request.url.hostname or request.headers.get("host", "")
    if request_host and not _is_loopback_host(request_host):
        return False

    forwarded_hosts = request.headers.get("x-forwarded-host", "")
    if forwarded_hosts and any(
        not _is_loopback_host(part)
        for part in forwarded_hosts.split(",")
        if part.strip()
    ):
        return False

    # Forwarding headers are trusted only from the loopback peer checked above.
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        hops = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        if not hops or any(not _is_loopback_host(hop) for hop in hops):
            return False

    return True


def extract_api_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-mike-key", "").strip()


def verify_api_key(candidate: Optional[str]) -> bool:
    """Constant-time verification for an API key supplied by a caller."""
    provided = str(candidate or "").encode("utf-8")
    configured = str(API_KEY or "").encode("utf-8")
    if not provided or not configured:
        return False
    return hmac.compare_digest(provided, configured)


def profile_from_request(request: Request) -> Optional[str]:
    payload = getattr(request.state, "profile_session", None)
    if not payload:
        return None
    profile_key = str(payload.get("profile") or "").strip().lower()
    return profile_key or None


def is_protected_path(path: str) -> bool:
    if path.startswith("/v1/auth/"):
        return False
    if path == "/health" and ALLOW_UNAUTH_HEALTHCHECK:
        return False
    if path == "/stats":
        return True
    return path.startswith("/v1")


# ---------------------------------------------------------------------------
# MCP tool permissions
# ---------------------------------------------------------------------------

READ_ONLY_MCP_TOOLS = {
    "get_calendar_status",
    "get_email_address",
    "get_path_info",
    "inspect_workbook",
    "list_calendars",
    "list_allowed_directories",
    "list_directory",
    "list_events",
    "list_inbox",
    "list_sheets",
    "list_spreadsheets",
    "read_email",
    "read_range",
    "read_text_file",
    "read_sheet",
}

MUTATING_MCP_TOOLS = {
    "append_rows",
    "create_event",
    "create_directory",
    "create_sheet",
    "create_workbook",
    "delete_event",
    "delete_directory",
    "delete_file",
    "edit_file",
    "execute_powershell",
    "move_path",
    "run_command",
    "send_email",
    "update_event",
    "write_cells",
    "write_file",
}

SENSITIVE_MCP_PREFIXES = {
    "appointments",
    "calendar",
    "crawlconsole",
    "drive",
    "excel",
    "fetch",
    "filesystem",
    "github",
    "gmail",
    "puppeteer",
    "remote",
    "sqlite",
    "workspace",
}


def _default_tool_access(tool_name: str) -> Optional[str]:
    normalized_name = str(tool_name or "").strip()
    if not normalized_name:
        return None
    raw_name = normalized_name.split(".", 1)[-1]
    if raw_name in MUTATING_MCP_TOOLS:
        return "owner"
    if raw_name in READ_ONLY_MCP_TOOLS:
        return "read"
    return None


def tool_allowed_for_profile(
    tool_name: str,
    profile_key: Optional[str],
    access: Optional[str] = None,
) -> bool:
    normalized_name = str(tool_name or "").strip()
    if not normalized_name:
        return False
    # God mode: Marco, Ana Paula e Mike (sem perfil = autonomia/localhost) têm acesso total.
    raw_name = normalized_name.rsplit(".", 1)[-1]
    prefix = normalized_name.split(".", 1)[0].strip().lower()
    inferred_access = _default_tool_access(normalized_name)
    declared_access = str(access or "").strip().lower()
    is_owner = profile_key in {"marco", "anapaula"}

    # Tool sensitivity wins over permissive server-level metadata.
    if inferred_access == "owner" or prefix in SENSITIVE_MCP_PREFIXES:
        return is_owner

    if declared_access in {"owner", "ops", "write", "mutate", "private"}:
        return is_owner

    # Owners can use explicitly classified tools. Everybody else is denied
    # unless the raw tool name is on the reviewed read-only allowlist.
    if is_owner:
        return inferred_access is not None or bool(declared_access)
    if inferred_access == "read":
        return True
    if declared_access in {"any", "all", "public", "read", "readonly"}:
        return raw_name in READ_ONLY_MCP_TOOLS
    return False


def filter_tool_manifest(
    tool_manifest: List[dict], profile_key: Optional[str]
) -> List[dict]:
    return [
        tool
        for tool in tool_manifest
        if tool_allowed_for_profile(
            tool.get("name"),
            profile_key,
            access=tool.get("access"),
        )
    ]


# ---------------------------------------------------------------------------
# Natural identification — sem passwords, o Mike pergunta "quem é?"
# ---------------------------------------------------------------------------

# Patterns that detect self-identification in Portuguese
_NATURAL_ID_PATTERNS: list[tuple[str, str]] = [
    # (regex pattern, profile_key)
    (r"\b(eu\s+)?sou\s+(o\s+)?marco\b", "marco"),
    (r"\bmarco\s+(aqui|falando|presente)\b", "marco"),
    (r"\b(eu\s+)?sou\s+(a\s+)?ana\s*paula\b", "anapaula"),
    (r"\bana\s*paula\s+(aqui|falando|presente)\b", "anapaula"),
    (r"\b(eu\s+)?sou\s+(o\s+)?rapha(el)?\b", "raphael"),
    (r"\brapha(el)?\s+(aqui|falando|presente)\b", "raphael"),
    (r"\b(eu\s+)?sou\s+(a\s+)?alice\b", "alice"),
    (r"\balice\s+(aqui|falando|presente)\b", "alice"),
    (r"\b(eu\s+)?sou\s+(o\s+)?matheus\b", "matheus"),
    (r"\bmatheus\s+(aqui|falando|presente)\b", "matheus"),
    (r"\b(eu\s+)?sou\s+(a\s+)?marilene\b", "marilene"),
    (r"\bmarilene\s+(aqui|falando|presente)\b", "marilene"),
    (r"\bme\s+chamo\s+(marco|rapha(el)?|alice|matheus|marilene)\b", None),  # dynamic
    (r"\bmeu\s+nome\s+(?:eh|é)\s+(marco|rapha(el)?|alice|matheus|marilene)\b", None),  # dynamic
]

# Map common name variants to profile keys
_NAME_TO_PROFILE: dict[str, str] = {
    "marco": "marco",
    "raphael": "raphael", "rapha": "raphael",
    "alice": "alice",
    "matheus": "matheus",
    "marilene": "marilene",
    "ana paula": "anapaula", "anapaula": "anapaula",
}


def detect_profile_from_message(user_text: str) -> Optional[str]:
    """Try to identify who is speaking from their message.

    Returns profile_key if detected, None otherwise.
    Used when PROFILE_AUTH_ENABLED=False — natural identification.
    """
    if not user_text:
        return None
    text = user_text.lower().strip()
    # Check static patterns first
    for pattern, profile_key in _NATURAL_ID_PATTERNS:
        match = re.search(pattern, text)
        if match:
            if profile_key:
                return profile_key
            # Dynamic: extract name from group
            name = match.group(1)
            if name:
                return _NAME_TO_PROFILE.get(name)
    # Check if the entire message is just a known name
    for name, profile_key in _NAME_TO_PROFILE.items():
        if text == name:
            return profile_key
    return None


def natural_greeting_for_unidentified() -> str:
    """Return a friendly greeting asking who is speaking."""
    return (
        "Olá! 🐾 Antes de começarmos, me diz: quem é você? "
        "Sou o Mike, o fiel escudeiro da família Barreto. "
        "Me diz seu nome que já sei com quem estou falando!"
    )
