# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Autoriza o Mike para Gmail + Google Calendar via OAuth2.
"""

from __future__ import annotations

import json
import os
import sys

from mike_google_auth import (
    GOOGLE_CREDENTIALS_DEFAULTS,
    GOOGLE_CREDENTIALS_ENV_NAMES,
    GOOGLE_WORKSPACE_TOKEN_DEFAULTS,
    GOOGLE_WORKSPACE_TOKEN_ENV_NAMES,
    GOOGLE_WORKSPACE_SCOPES,
    candidate_paths,
    default_target_path,
    token_file_has_scopes,
)


CREDS_ENV_NAMES = GOOGLE_CREDENTIALS_ENV_NAMES
CREDS_DEFAULTS = GOOGLE_CREDENTIALS_DEFAULTS
TOKEN_ENV_NAMES = GOOGLE_WORKSPACE_TOKEN_ENV_NAMES
TOKEN_DEFAULTS = GOOGLE_WORKSPACE_TOKEN_DEFAULTS


def _looks_like_placeholder(value: str) -> bool:
    candidate = (value or "").strip().upper()
    if not candidate:
        return True
    markers = ("PREENCHER", "YOUR_", "EXAMPLE", "CHANGE_ME", "REPLACE")
    return any(marker in candidate for marker in markers)


def _validate_credentials_file(creds_file: str) -> tuple[bool, str]:
    try:
        payload = json.loads(open(creds_file, "r", encoding="utf-8").read())
    except Exception as exc:
        return False, f"Nao foi possivel ler credenciais OAuth: {exc}"

    block = payload.get("installed") or payload.get("web")
    if not isinstance(block, dict):
        return False, "JSON invalido: esperado bloco 'installed' ou 'web'."

    client_id = str(block.get("client_id") or "")
    client_secret = str(block.get("client_secret") or "")
    project_id = str(block.get("project_id") or "")

    if (
        _looks_like_placeholder(client_id)
        or _looks_like_placeholder(client_secret)
        or _looks_like_placeholder(project_id)
    ):
        return False, "Credenciais placeholder detectadas no JSON OAuth."

    if not client_id.endswith(".apps.googleusercontent.com"):
        return False, "client_id invalido: nao parece um OAuth Client do Google."

    return True, ""


def _install_google_packages_if_needed():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google.auth.transport.requests import Request  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        return
    except ImportError:
        print("Instalando dependencias Google...")
        os.system(
            f"{sys.executable} -m pip install "
            "google-auth google-auth-oauthlib google-api-python-client"
        )


def main() -> int:
    creds_file = None
    for candidate in candidate_paths(CREDS_ENV_NAMES, CREDS_DEFAULTS):
        if candidate.exists():
            creds_file = candidate
            break

    if creds_file is None:
        target = default_target_path(CREDS_ENV_NAMES, CREDS_DEFAULTS)
        print("=" * 60)
        print("ARQUIVO DE CREDENCIAIS GOOGLE NAO ENCONTRADO")
        print("=" * 60)
        print()
        print("Passos:")
        print("1. Acesse https://console.cloud.google.com/apis/credentials")
        print("2. Crie um OAuth Client do tipo Desktop App")
        print("3. Ative estas APIs no projeto:")
        print("   - Gmail API")
        print("   - Google Calendar API")
        print("4. Baixe o JSON e salve como:")
        print(f"   {target}")
        print()
        print("Se pedir tela de consentimento OAuth:")
        print("   - Tipo: Externo")
        print("   - Nome do app: Mike")
        print("   - Usuario de teste: marcobarreto007@gmail.com")
        return 1

    ok, reason = _validate_credentials_file(str(creds_file))
    if not ok:
        print("=" * 60)
        print("CREDENCIAIS OAUTH INVALIDAS")
        print("=" * 60)
        print(reason)
        print()
        print(f"Arquivo atual: {creds_file}")
        print("Baixe um novo OAuth Client (Desktop App) no Google Cloud Console")
        print("e substitua esse arquivo. Depois rode este setup novamente.")
        return 1

    _install_google_packages_if_needed()

    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_file = default_target_path(TOKEN_ENV_NAMES, TOKEN_DEFAULTS)
    creds = None
    if token_file.exists():
        if token_file_has_scopes(token_file, GOOGLE_WORKSPACE_SCOPES):
            creds = Credentials.from_authorized_user_file(str(token_file), GOOGLE_WORKSPACE_SCOPES)
        else:
            print("Token existente sem todos os escopos de Gmail + Agenda. Reautorizando...")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Renovando token existente...")
            creds.refresh(Request())
        else:
            print("Abrindo browser para autorizar Gmail + Agenda...")
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), GOOGLE_WORKSPACE_SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    print(f"Token salvo em: {token_file}")

    try:
        gmail = build("gmail", "v1", credentials=creds)
        profile = gmail.users().getProfile(userId="me").execute()
        email_address = profile.get("emailAddress", "?")
    except Exception as exc:
        print(f"Falha ao validar Gmail: {exc}")
        return 1

    try:
        calendar = build("calendar", "v3", credentials=creds)
        calendars = calendar.calendarList().list().execute().get("items", [])
        calendars_total = len(calendars)
    except Exception as exc:
        print(f"Falha ao validar Agenda Google: {exc}")
        return 1

    print()
    print("Autorizacao OK")
    print(f"Email: {email_address}")
    print(f"Calendarios encontrados: {calendars_total}")
    print()
    print("Agora reinicie o Mike:")
    print("  .\\start_mike.ps1 -ForceRestart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
