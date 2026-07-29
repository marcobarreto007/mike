# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Shared Google OAuth helper for Mike MCP servers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional


_mike_home = os.environ.get("MIKE_HOME", "")
PROJECT_ROOT = Path(_mike_home).resolve() if _mike_home else Path(__file__).parent.parent.parent.resolve()

GOOGLE_CREDENTIALS_ENV_NAMES = [
    "MIKE_GOOGLE_CREDENTIALS",
    "MIKE_GMAIL_CREDENTIALS",
    "MIKE_CALENDAR_CREDENTIALS",
]
GOOGLE_CREDENTIALS_DEFAULTS = [
    "config/google_credentials.json",
    "config/gmail_credentials.json",
    "google_credentials.json",
    "gmail_credentials.json",
]

GOOGLE_WORKSPACE_TOKEN_ENV_NAMES = ["MIKE_GOOGLE_TOKEN"]
GOOGLE_WORKSPACE_TOKEN_DEFAULTS = [
    "config/google_workspace_token.json",
    "google_workspace_token.json",
]

GMAIL_TOKEN_ENV_NAMES = ["MIKE_GMAIL_TOKEN", "MIKE_GOOGLE_TOKEN"]
GMAIL_TOKEN_DEFAULTS = [
    "config/google_workspace_token.json",
    "config/gmail_token.json",
    "google_workspace_token.json",
    "gmail_token.json",
]

CALENDAR_TOKEN_ENV_NAMES = ["MIKE_CALENDAR_TOKEN", "MIKE_GOOGLE_TOKEN", "MIKE_GMAIL_TOKEN"]
CALENDAR_TOKEN_DEFAULTS = [
    "config/google_workspace_token.json",
    "config/calendar_token.json",
    "config/gmail_token.json",
    "google_workspace_token.json",
    "calendar_token.json",
    "gmail_token.json",
]

DRIVE_TOKEN_ENV_NAMES = ["MIKE_DRIVE_TOKEN", "MIKE_GOOGLE_TOKEN"]
DRIVE_TOKEN_DEFAULTS = [
    "config/google_workspace_token.json",
    "config/drive_token.json",
    "google_workspace_token.json",
    "drive_token.json",
]

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
]

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

GOOGLE_WORKSPACE_SCOPES = list(dict.fromkeys(GMAIL_SCOPES + CALENDAR_SCOPES + DRIVE_SCOPES))


def _resolve_path(raw_value: str) -> Path:
    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        candidate = (PROJECT_ROOT / candidate).resolve()
    return candidate.resolve()


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def candidate_paths(env_names: list[str], defaults: list[str]) -> list[Path]:
    paths: list[Path] = []
    for env_name in env_names:
        raw_value = os.getenv(env_name, "").strip()
        if raw_value:
            paths.append(_resolve_path(raw_value))
    for default_name in defaults:
        paths.append((PROJECT_ROOT / default_name).resolve())
    return _unique_paths(paths)


def first_existing_path(env_names: list[str], defaults: list[str]) -> Optional[Path]:
    for candidate in candidate_paths(env_names, defaults):
        if candidate.exists():
            return candidate
    return None


def default_target_path(env_names: list[str], defaults: list[str]) -> Path:
    candidates = candidate_paths(env_names, defaults)
    if candidates:
        return candidates[0]
    return (PROJECT_ROOT / defaults[0]).resolve()


def oauth_status_token_path(env_names: list[str], defaults: list[str]) -> Path:
    existing = first_existing_path(env_names, defaults)
    if existing is not None:
        return existing
    return default_target_path(env_names, defaults)


def oauth_token_status(
    env_names: list[str],
    defaults: list[str],
    scopes: list[str],
) -> tuple[Path, str]:
    token_path = oauth_status_token_path(env_names, defaults)
    if not token_path.exists():
        return token_path, "missing"
    if not token_file_has_scopes(token_path, scopes):
        return token_path, "missing_scopes"
    return token_path, "ready"


def token_file_has_scopes(token_path: Path, scopes: list[str]) -> bool:
    required = {str(scope).strip() for scope in scopes if str(scope).strip()}
    if not required:
        return True

    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False

    granted = payload.get("scopes") or payload.get("scope") or []
    if isinstance(granted, str):
        granted = granted.split()
    if not isinstance(granted, list):
        return False

    granted_set = {str(scope).strip() for scope in granted if str(scope).strip()}
    return required.issubset(granted_set)


def _import_google():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Pacotes Google nao instalados. Execute: "
            "pip install google-auth google-auth-oauthlib google-api-python-client"
        ) from exc
    return Credentials, Request, build


def load_google_credentials(
    scopes: list[str],
    *,
    token_env_names: list[str],
    token_defaults: list[str],
) -> tuple[object, Path]:
    token_path = first_existing_path(token_env_names, token_defaults)
    if token_path is None:
        raise RuntimeError(
            "Token OAuth2 nao encontrado. Execute: python setup_google_workspace_oauth.py"
        )

    if not token_file_has_scopes(token_path, scopes):
        raise RuntimeError(
            "O token OAuth2 existe, mas nao tem os escopos necessarios. "
            "Execute novamente: python setup_google_workspace_oauth.py"
        )

    Credentials, Request, _ = _import_google()
    creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(
                "Token OAuth2 expirado ou invalido. "
                "Execute novamente: python setup_google_workspace_oauth.py"
            )

    return creds, token_path


def build_google_service(
    service_name: str,
    version: str,
    scopes: list[str],
    *,
    token_env_names: list[str],
    token_defaults: list[str],
) -> tuple[object, Path]:
    _, _, build = _import_google()
    creds, token_path = load_google_credentials(
        scopes,
        token_env_names=token_env_names,
        token_defaults=token_defaults,
    )
    return build(service_name, version, credentials=creds), token_path


def gmail_service() -> tuple[object, Path]:
    return build_google_service(
        "gmail",
        "v1",
        GMAIL_SCOPES,
        token_env_names=GMAIL_TOKEN_ENV_NAMES,
        token_defaults=GMAIL_TOKEN_DEFAULTS,
    )


def calendar_service() -> tuple[object, Path]:
    return build_google_service(
        "calendar",
        "v3",
        CALENDAR_SCOPES,
        token_env_names=CALENDAR_TOKEN_ENV_NAMES,
        token_defaults=CALENDAR_TOKEN_DEFAULTS,
    )


def drive_service() -> tuple[object, Path]:
    return build_google_service(
        "drive",
        "v3",
        DRIVE_SCOPES,
        token_env_names=DRIVE_TOKEN_ENV_NAMES,
        token_defaults=DRIVE_TOKEN_DEFAULTS,
    )
