# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
"""
mike_email.py — envio e leitura de email via Gmail API (OAuth2).

Usa o token OAuth2 configurado em MIKE_GOOGLE_TOKEN.
Já não depende de App Passwords SMTP/IMAP.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

log = logging.getLogger("mike.email")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _get_gmail_service():
    """Obtem o servico Gmail API via OAuth2."""
    try:
        from mike_google_auth import gmail_service
        service, _ = gmail_service()
        return service, None
    except Exception as exc:
        return None, str(exc)


def _decode_header_val(value: Optional[str]) -> str:
    """Decodifica cabecalho de email (suporte a Base64/QP encodings)."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded: List[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded)


def _extract_email_address(header_value: str) -> str:
    """Extrai o endereco de email de um header From/To."""
    match = re.search(r'<([^>]+)>', header_value)
    if match:
        return match.group(1).lower().strip()
    return header_value.lower().strip()


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html: bool = False,
    from_name: Optional[str] = None,
) -> dict:
    """
    Envia um email via Gmail API (OAuth2).

    Returns:
        {"ok": True, "to": ..., "subject": ...} on success
        {"ok": False, "error": "..."} on failure
    """
    service, err = _get_gmail_service()
    if err:
        return {"ok": False, "error": f"Gmail API nao disponivel: {err}"}

    sender_name = from_name or _env("MIKE_SMTP_FROM_NAME", "Mike")
    gmail_address = _env("MIKE_GMAIL_ADDRESS", "").strip()
    if gmail_address:
        sender = gmail_address
    else:
        user = _env("MIKE_SMTP_USER", "")
        sender = f"{sender_name} <{user}>" if user else "me"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to

    mime_type = "html" if html else "plain"
    msg.attach(MIMEText(body, mime_type, "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        result = service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()
        log.info("Email enviado para %s | Assunto: %s | ID: %s", to, subject, result.get("id", "?"))
        return {"ok": True, "to": to, "subject": subject, "message_id": result.get("id")}
    except Exception as exc:
        err = f"Erro ao enviar email via Gmail API: {exc}"
        log.error(err)
        return {"ok": False, "error": err}


# ---------------------------------------------------------------------------
# Gmail API — leitura da caixa de entrada
# ---------------------------------------------------------------------------

def _msg_to_dict(msg: dict, include_body: bool = False) -> dict:
    """Converte uma mensagem da Gmail API para dict padronizado."""
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        headers[h["name"].lower()] = h["value"]

    result = {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", "(sem assunto)"),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "unread": "UNREAD" in msg.get("labelIds", []),
    }

    if include_body:
        payload = msg.get("payload", {})
        body = ""
        parts = [payload]
        if payload.get("parts"):
            parts = payload["parts"]
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    try:
                        body = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
                    except Exception:
                        body = "(corpo nao decodificado)"
                    break
        if not body and payload.get("body", {}).get("data"):
            try:
                body = base64.urlsafe_b64decode(payload["body"]["data"] + "===").decode("utf-8", errors="replace")
            except Exception:
                body = "(corpo nao decodificado)"
        result["body"] = body[:6000]

    return result


def list_inbox(
    limit: int = 10,
    folder: str = "INBOX",
    unread_only: bool = False,
) -> dict:
    """
    Lista emails da caixa de entrada via Gmail API (OAuth2).

    Args:
        limit: Numero maximo de emails (max 50).
        folder: Label do Gmail (INBOX, SENT, etc).
        unread_only: Se True, apenas nao lidos.

    Returns:
        {"ok": True, "emails": [...], "total": N}
        {"ok": False, "error": "..."}
    """
    limit = min(int(limit), 50)
    service, err = _get_gmail_service()
    if err:
        return {"ok": False, "error": f"Gmail API nao disponivel: {err}"}

    try:
        query_parts = []
        if folder.upper() != "INBOX":
            query_parts.append(f"label:{folder}")
        if unread_only:
            query_parts.append("is:unread")
        query = " ".join(query_parts) if query_parts else None

        results = service.users().messages().list(
            userId="me",
            maxResults=limit,
            q=query,
        ).execute()

        messages = results.get("messages", [])
        emails = []
        for m in messages:
            msg = service.users().messages().get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()
            emails.append(_msg_to_dict(msg))

        log.info("Gmail API list_inbox: %d emails de '%s'", len(emails), folder)
        return {"ok": True, "emails": emails, "total": len(emails), "folder": folder}
    except Exception as exc:
        log.exception("Erro Gmail API list_inbox")
        return {"ok": False, "error": str(exc)}


def read_email(email_id: str, folder: str = "INBOX") -> dict:
    """
    Le o conteudo completo de um email pelo ID (Gmail message ID).

    Args:
        email_id: ID da mensagem (obtido via list_inbox).
        folder: Ignorado (compatibilidade), a Gmail API usa IDs globais.

    Returns:
        {"ok": True, "id": ..., "from": ..., "subject": ..., "body": ...}
        {"ok": False, "error": "..."}
    """
    service, err = _get_gmail_service()
    if err:
        return {"ok": False, "error": f"Gmail API nao disponivel: {err}"}

    try:
        msg = service.users().messages().get(
            userId="me",
            id=email_id,
            format="full",
        ).execute()

        # Marcar como lido
        try:
            service.users().messages().modify(
                userId="me",
                id=email_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception:
            pass

        result = _msg_to_dict(msg, include_body=True)
        result["uid"] = email_id  # compatibilidade com API antiga
        log.info("Gmail API read_email: %s lido", email_id)
        return result
    except Exception as exc:
        log.exception("Erro Gmail API read_email")
        return {"ok": False, "error": str(exc)}


def search_emails(query: str, folder: str = "INBOX", limit: int = 10) -> dict:
    """
    Busca emails por assunto ou remetente via Gmail API.

    Args:
        query: Texto para buscar.
        folder: Ignorado (compatibilidade).
        limit: Numero maximo de resultados.

    Returns:
        {"ok": True, "emails": [...], "found": N}
        {"ok": False, "error": "..."}
    """
    limit = min(int(limit), 30)
    service, err = _get_gmail_service()
    if err:
        return {"ok": False, "error": f"Gmail API nao disponivel: {err}"}

    try:
        results = service.users().messages().list(
            userId="me",
            maxResults=limit,
            q=query,
        ).execute()

        messages = results.get("messages", [])
        emails = []
        for m in messages:
            msg = service.users().messages().get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()
            emails.append(_msg_to_dict(msg))

        log.info("Gmail API search_emails '%s': %d encontrados", query, len(emails))
        return {"ok": True, "emails": emails, "found": len(emails), "query": query}
    except Exception as exc:
        log.exception("Erro Gmail API search_emails")
        return {"ok": False, "error": str(exc)}


def _smtp_ready() -> bool:
    """Verifica se OAuth esta disponivel (substitui antiga verificacao SMTP)."""
    try:
        from mike_google_auth import gmail_service
        service, _ = gmail_service()
        return True
    except Exception:
        return False
