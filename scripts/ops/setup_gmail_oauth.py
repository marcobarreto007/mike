#!/usr/bin/env python3
"""
Setup Gmail OAuth2 for Mike — opens YOUR browser, you click "Allow", done.
No passwords. No Google Cloud Console. No terminal confusion.

Usage: python scripts/ops/setup_gmail_oauth.py
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "core"))
sys.path.insert(0, str(PROJECT / "core" / "integrations"))

TOKEN_PATH = PROJECT / "config" / "google_workspace_token.json"
CREDS_PATH = PROJECT / "config" / "google_credentials.json"
ENV_FILE = PROJECT / "config" / ".env.runtime"

# OAuth2 scopes needed
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

# ── Step 1: Check for client secret ──
if not CREDS_PATH.exists():
    print("\n" + "=" * 60)
    print("  PRECISAMOS DE UM FICHEIRO DE CREDENCIAIS OAuth2")
    print("=" * 60)
    print()
    print("O Marco nao tem o ficheiro 'config/google_credentials.json' ainda.")
    print()
    print("Vamos criar juntos. Abre este link no teu browser:")
    print()
    print("  https://console.cloud.google.com/apis/credentials")
    print()
    print("Passos SIMPLES (2 minutos):")
    print("  1. Cria um projeto (ou usa um existente)")
    print("  2. Vai a 'APIs & Services' > 'Credentials'")
    print("  3. Clica '+ CREATE CREDENTIALS' > 'OAuth client ID'")
    print("  4. Se pedir 'Application type', escolhe 'Desktop app'")
    print("  5. Nome: 'Mike'")
    print("  6. Clica 'CREATE'")
    print("  7. Clica 'DOWNLOAD JSON'")
    print("  8. Guarda o ficheiro como:")
    print(f"     {CREDS_PATH}")
    print()
    print("Depois corre este script outra vez.")
    print("=" * 60)
    webbrowser.open("https://console.cloud.google.com/apis/credentials")
    sys.exit(0)

# ── Step 2: Run OAuth2 flow ──
print("\nA abrir o browser para autorizares o Mike...")
print("(Se o browser nao abrir, copia o URL que vai aparecer)\n")

from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    str(CREDS_PATH),
    scopes=SCOPES,
)

# This opens a browser window — Marco just clicks "Allow"
creds = flow.run_local_server(
    host="127.0.0.1",
    port=0,  # auto-pick free port
    authorization_prompt_message="",
    success_message="[OK] Mike autorizado! Podes fechar esta pagina.",
    open_browser=True,
)

# ── Step 3: Save token ──
TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
token_data = {
    "token": creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": creds.scopes,
    "expiry": creds.expiry.isoformat() if creds.expiry else None,
}
TOKEN_PATH.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
print(f"\n[OK] Token guardado em: {TOKEN_PATH}")

# ── Step 4: Set env var ──
if ENV_FILE.exists():
    content = ENV_FILE.read_text(encoding="utf-8")
    if "MIKE_GOOGLE_TOKEN" not in content:
        with ENV_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\nMIKE_GOOGLE_TOKEN={TOKEN_PATH}\n")
        print(f"[OK] MIKE_GOOGLE_TOKEN adicionado ao .env.runtime")
    else:
        print("[OK] MIKE_GOOGLE_TOKEN ja configurado no .env.runtime")
else:
    print(f"[!] {ENV_FILE} nao encontrado. Adiciona manualmente:")
    print(f"   MIKE_GOOGLE_TOKEN={TOKEN_PATH}")

print()
print("=" * 60)
print("  CONFIGURACAO COMPLETA!")
print("=" * 60)
print()
print("Reinicia o Mike e o Gmail/Calendar/Drive vao funcionar.")
print("  powershell -File scripts/ops/launch_mike.ps1")
