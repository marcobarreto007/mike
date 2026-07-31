"""Unit coverage for the deterministic autonomy inbox check."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for relative in ("core", "core/autonomy", "core/shared"):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

from mike_autonomy import MikeAutonomy


def test_inbox_check_uses_email_mcp_signature(tmp_path):
    calls = []

    def search_emails(**kwargs):
        calls.append(kwargs)
        return [{"id": "message-1", "subject": "Teste"}]

    engine = MikeAutonomy(
        email_search_fn=search_emails,
        store_dir=tmp_path,
    )
    result = asyncio.run(engine.run_routine_now("inbox_check"))

    assert result["success"] is True
    assert result["result"] == "Inbox: 1 nao lido(s)"
    assert calls == [{"term": "is:unread", "days_back": 0, "limit": 5}]


def test_inbox_check_surfaces_email_configuration_error(tmp_path):
    def search_emails(**_kwargs):
        return [{"error": "OAuth token missing"}]

    engine = MikeAutonomy(
        email_search_fn=search_emails,
        store_dir=tmp_path,
    )
    result = asyncio.run(engine.run_routine_now("inbox_check"))

    assert result["success"] is False
    assert "OAuth token missing" in result["result"]
