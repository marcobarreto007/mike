"""Regression coverage for Gmail-backed local email tool formatting."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for relative in (
    "core",
    "core/server",
    "core/comms",
    "core/integrations",
    "core/autonomy",
    "core/memory",
    "core/mcp",
):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

from mike_tools_local import _execute_local_tool


def _gmail_message():
    return {
        "id": "gmail-message-id",
        "from": "Pessoa <pessoa@example.com>",
        "subject": "Assunto",
        "date": "Thu, 30 Jul 2026 12:00:00 -0400",
    }


def test_email_list_accepts_gmail_message_id(monkeypatch):
    fake = types.ModuleType("mike_email")
    fake.list_inbox = lambda **_kwargs: {"ok": True, "emails": [_gmail_message()]}
    monkeypatch.setitem(sys.modules, "mike_email", fake)

    result = asyncio.run(_execute_local_tool("email.list_inbox", {"limit": 1}))

    assert result["ok"] is True
    assert "ID:gmail-message-id" in result["text"]


def test_email_search_accepts_gmail_message_id(monkeypatch):
    fake = types.ModuleType("mike_email")
    fake.search_emails = lambda **_kwargs: {"ok": True, "emails": [_gmail_message()]}
    monkeypatch.setitem(sys.modules, "mike_email", fake)

    result = asyncio.run(_execute_local_tool("email.search", {"query": "Assunto"}))

    assert result["ok"] is True
    assert "ID:gmail-message-id" in result["text"]
