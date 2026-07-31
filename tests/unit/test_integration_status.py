"""Unit coverage for credential-safe integration readiness."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[2]
for relative in (
    "core",
    "core/server",
    "core/integrations",
    "core/autonomy",
    "core/memory",
    "core/mcp",
):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

from routers import system as system_router


def _local_request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/integrations/status",
            "raw_path": b"/v1/integrations/status",
            "query_string": b"",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8083),
        }
    )


def test_gmail_api_readiness_follows_google_oauth(monkeypatch):
    monkeypatch.setattr(
        system_router,
        "oauth_token_status",
        lambda *_args, **_kwargs: (Path("token.json"), "ready"),
    )

    payload = asyncio.run(system_router.integrations_status(_local_request()))

    assert payload["integrations"]["google_workspace"]["ready"] is True
    assert payload["integrations"]["email_gmail_api"] == {
        "ready": True,
        "state": "ready",
    }
    assert "email_imap_smtp" not in payload["integrations"]
