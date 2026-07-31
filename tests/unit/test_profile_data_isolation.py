"""Regression tests for profile-scoped chat and memory data."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

from starlette.requests import Request
from starlette.responses import Response


os.environ.setdefault("MIKE_HOME", "D:/mike")
os.environ.setdefault("MIKE_FORCE_CPU", "true")
os.environ.setdefault("MIKE_PROFILE_MARCO_PASSWORD", "test-marco-password")
os.environ.setdefault("MIKE_PROFILE_VISITANTE_PASSWORD", "test-visitor-password")

for path in (
    "core/server",
    "core/chat",
    "core/memory",
    "core/orchestration",
    "core/rag",
    "core/tools",
    "core/mcp",
    "core/integrations",
    "core/autonomy",
):
    if path not in sys.path:
        sys.path.insert(0, path)

import mike_server
from routers import memory as memory_routes


def _request(profile: str | None = None) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8083),
        }
    )
    request.state.profile_session = {"profile": profile} if profile else None
    return request


class _ChatMemory:
    def __init__(self):
        self.profile = None
        self.session = None

    def list_sessions(self, profile_key=None, limit=10):
        self.profile = profile_key
        return []

    def conversation_history(self, session_id, limit=None):
        self.session = session_id
        return []


def test_chat_query_cannot_override_authenticated_profile(monkeypatch):
    fake = _ChatMemory()
    monkeypatch.setattr(mike_server._chat_core, "memory_service", fake)
    request = _request("marco")

    asyncio.run(mike_server.chat_sessions(request, profile="anapaula"))
    asyncio.run(
        mike_server.chat_history(
            request,
            session_id="anapaula-private",
            profile="anapaula",
        )
    )

    assert fake.profile == "marco"
    assert fake.session == "marco-anapaula-private"


class _SearchMemory:
    def __init__(self):
        self.args = None

    def search_memories(self, query, limit, session_id, session_only):
        self.args = (query, limit, session_id, session_only)
        return []


def test_memory_search_defaults_to_authenticated_main_session(monkeypatch):
    fake = _SearchMemory()
    monkeypatch.setattr(memory_routes.shared_state, "memory_service", fake)

    payload = asyncio.run(
        memory_routes.search_memory(_request("marco"), q="segredo", limit=5)
    )

    assert payload["session_id"] == "marco-main"
    assert fake.args == ("segredo", 5, "marco-main", True)


def test_checkpoint_restore_hides_other_profile(monkeypatch):
    fake = SimpleNamespace(
        checkpoint_restore=lambda _checkpoint_id: {
            "checkpoint_id": "cp-other",
            "session_id": "anapaula-main",
            "messages": [{"role": "assistant", "content": "private"}],
        }
    )
    monkeypatch.setattr(memory_routes.shared_state, "memory_service", fake)
    request = _request("marco")

    async def body():
        return {"checkpoint_id": "cp-other"}

    request.json = body
    response = asyncio.run(memory_routes.checkpoint_restore(request))

    assert response.status_code == 404


def test_security_headers_include_csp_and_permissions_policy():
    request = _request()

    async def call_next(_request):
        return Response("ok")

    response = asyncio.run(
        mike_server.mike_security_headers_middleware(request, call_next)
    )

    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert "microphone=(self)" in response.headers["permissions-policy"]
    assert "strict-transport-security" not in response.headers


def test_hsts_is_emitted_only_for_https_forwarded_request():
    request = _request()
    request.scope["headers"].append((b"x-forwarded-proto", b"https"))

    async def call_next(_request):
        return Response("ok")

    response = asyncio.run(
        mike_server.mike_security_headers_middleware(request, call_next)
    )

    assert response.headers["strict-transport-security"].startswith(
        "max-age=31536000"
    )
