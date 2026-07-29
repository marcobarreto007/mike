# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Email MCP Server - OAuth2 via Gmail API
============================================

Servidor MCP para envio e leitura de email via Gmail API.

Configuracao:
    1. Crie um client OAuth Desktop no Google Cloud
    2. Salve o JSON como google_credentials.json ou gmail_credentials.json
    3. Rode: python setup_google_workspace_oauth.py
    4. Reinicie o Mike
"""

from __future__ import annotations

import base64
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from mcp.server.fastmcp import FastMCP

log = logging.getLogger("mike.email_mcp")

from mike_google_auth import (
    GMAIL_SCOPES,
    GMAIL_TOKEN_DEFAULTS,
    GMAIL_TOKEN_ENV_NAMES,
    gmail_service,
    oauth_token_status,
)


GMAIL_ADDRESS = os.getenv("MIKE_GMAIL_ADDRESS", "").strip()

mcp = FastMCP("Mike Email MCP", json_response=True)


def _get_service():
    try:
        service, _token_path = gmail_service()
        return service
    except RuntimeError as exc:
        raise RuntimeError(str(exc)) from exc


def _decode_header_value(value: str) -> str:
    import email.header as _header

    parts = _header.decode_header(value or "")
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)


def _header(msg_payload: dict, name: str) -> str:
    for item in msg_payload.get("headers", []):
        if item["name"].lower() == name.lower():
            return _decode_header_value(item["value"])
    return ""


def _body_from_parts(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            result = _body_from_parts(part)
            if result:
                return result
    return ""


@mcp.tool(description=(
    "Envia um email pelo Gmail do Mike. "
    "Informe destinatario (to), assunto (subject) e corpo (body). "
    "O campo cc e opcional."
))
def send_email(to: str, subject: str, body: str, cc: Optional[str] = None) -> str:
    try:
        service = _get_service()
    except RuntimeError as exc:
        return f"Erro de configuracao: {exc}"

    msg = MIMEMultipart("alternative")
    sender = GMAIL_ADDRESS or "me"
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    msg.attach(MIMEText(body, "plain", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    try:
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:
        return f"Erro ao enviar email: {exc}"

    cc_note = f" | cc: {cc}" if cc else ""
    return f"Email enviado com sucesso para {to}{cc_note} | assunto: {subject}"


@mcp.tool(description=(
    "Lista emails da caixa de entrada do Gmail. "
    "Parametros: limit (max emails, padrao 20, max 50), query (filtro Gmail Query Language). "
    "Exemplos de query: 'from:rapha' (emails de rapha), 'from:ana barreto' (emails de ana barreto), "
    "'from:frederico subject:reuniao' (de frederico sobre reuniao), "
    "'after:2026/04/01 before:2026/04/17' (intervalo de datas), "
    "'is:unread' (nao lidos), 'has:attachment' (com anexo). "
    "Para buscar emails de uma pessoa, USE from:nome. Para buscar por assunto, USE subject:termo. "
    "Combine filtros: 'from:marcelo after:2026/04/01'."
), structured_output=False)
def list_inbox(limit: int = 20, query: str = "") -> list[dict]:
    try:
        service = _get_service()
    except RuntimeError as exc:
        return [{"error": str(exc)}]

    limit = max(1, min(limit, 50))
    params = {"userId": "me", "labelIds": ["INBOX"], "maxResults": limit}
    if query:
        params["q"] = query

    try:
        result = service.users().messages().list(**params).execute()
        messages = result.get("messages", [])
    except Exception as exc:
        return [{"error": str(exc)}]

    if not messages:
        return []

    # Batch fetch all message metadata in a single HTTP round-trip
    emails: list[dict] = []
    email_map: dict[str, dict] = {}

    def _handle_message(request_id, response, exception):
        if exception is not None:
            log.warning("Batch email fetch failed for request_id=%s: %s", request_id, exception)
            return
        payload = response.get("payload", {})
        email_map[request_id] = {
            "id": response["id"],
            "from": _header(payload, "From"),
            "to": _header(payload, "To"),
            "subject": _header(payload, "Subject"),
            "date": _header(payload, "Date"),
            "snippet": response.get("snippet", ""),
        }

    try:
        batch = service.new_batch_http_request(callback=_handle_message)
        for i, message in enumerate(messages):
            batch.add(
                service.users().messages().get(
                    userId="me",
                    id=message["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                ),
                request_id=str(i),
            )
        batch.execute()
    except Exception as exc:
        return [{"error": f"Batch fetch failed: {exc}"}]

    # Preserve original order
    for i in range(len(messages)):
        entry = email_map.get(str(i))
        if entry:
            emails.append(entry)

    return emails


@mcp.tool(description=(
    "Le o conteudo completo de um email especifico pelo ID retornado por list_inbox."
), structured_output=False)
def read_email(email_id: str) -> dict:
    try:
        service = _get_service()
    except RuntimeError as exc:
        return {"error": str(exc)}

    try:
        message = service.users().messages().get(
            userId="me",
            id=email_id,
            format="full",
        ).execute()
    except Exception as exc:
        return {"error": str(exc)}

    payload = message.get("payload", {})
    body = _body_from_parts(payload)

    try:
        service.users().messages().modify(
            userId="me",
            id=email_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except Exception as e:
        log.warning("[email_mcp] Mark read failed for %s: %s", email_id, e)

    return {
        "id": email_id,
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "snippet": message.get("snippet", ""),
        "body": body[:4000],
    }


@mcp.tool(description=(
    "Busca emails por remetente, assunto ou termo geral. "
    "Use 'sender' para buscar por nome/email do remetente, 'subject' para assunto, "
    "'term' para busca geral. Todos sao opcionais mas informe pelo menos um. "
    "'days_back' limita a busca aos ultimos N dias (padrao 30). "
    "Retorna ate 'limit' emails (padrao 30, max 50)."
), structured_output=False)
def search_emails(
    sender: str = "",
    subject: str = "",
    term: str = "",
    days_back: int = 30,
    limit: int = 30,
) -> list[dict]:
    parts = []
    if sender:
        parts.append(f"from:{sender}")
    if subject:
        parts.append(f"subject:{subject}")
    if term:
        parts.append(term)
    if days_back > 0:
        from datetime import datetime, timedelta
        after_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")
        parts.append(f"after:{after_date}")
    query = " ".join(parts)
    if not query.strip():
        return [{"error": "Informe pelo menos sender, subject ou term para buscar."}]
    log.info("search_emails query=%r limit=%d", query, limit)
    return list_inbox(limit=limit, query=query)


@mcp.tool(description="Mostra o endereco de email configurado no Mike.")
def get_email_address() -> str:
    token_path, status = oauth_token_status(
        GMAIL_TOKEN_ENV_NAMES,
        GMAIL_TOKEN_DEFAULTS,
        GMAIL_SCOPES,
    )
    if status == "missing":
        return "Email OAuth2 nao autenticado ainda. Execute: python setup_google_workspace_oauth.py"
    if status == "missing_scopes":
        return "Email OAuth2 com token incompleto. Execute novamente: python setup_google_workspace_oauth.py"
    addr = GMAIL_ADDRESS or "configurado via OAuth2"
    return f"{addr} (OAuth2 ativo - token em {token_path.name})"


if __name__ == "__main__":
    mcp.run(transport="stdio")
