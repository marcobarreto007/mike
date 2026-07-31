"""Focused regression tests for the authentication perimeter."""
import asyncio
import os
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

os.environ.setdefault("MIKE_HOME", "D:/mike")
os.environ.setdefault("MIKE_FORCE_CPU", "true")
os.environ.setdefault("MIKE_PROFILE_MARCO_PASSWORD", "test-marco-password")
os.environ.setdefault("MIKE_PROFILE_ANAPAULA_PASSWORD", "test-ana-password")

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

import mike_auth
from routers import auth as auth_routes


def _request(*, client="127.0.0.1", host="localhost", headers=None, query=""):
    raw_headers = [(b"host", host.encode("ascii"))]
    raw_headers.extend(
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": query.encode("ascii"),
            "headers": raw_headers,
            "client": (client, 12345),
            "server": ("127.0.0.1", 8083),
        }
    )


def _auth_client():
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app, base_url="http://localhost")


def test_identify_never_creates_session_when_profile_auth_enabled(monkeypatch):
    monkeypatch.setattr(auth_routes, "PROFILE_AUTH_ENABLED", True)
    monkeypatch.setattr(auth_routes, "_match_dashboard_profile", lambda _text: "marco")
    response = _auth_client().post("/v1/auth/identify", json={"text": "sou marco"})
    assert response.status_code == 200
    assert response.json() == {"profile": "marco"}
    assert "set-cookie" not in response.headers


def test_local_request_rejects_spoofed_forwarding_and_public_tunnel():
    assert mike_auth.is_local_request(_request())
    assert not mike_auth.is_local_request(
        _request(client="203.0.113.10", headers={"x-forwarded-for": "127.0.0.1"})
    )
    assert not mike_auth.is_local_request(
        _request(host="mike.example.com", headers={"x-forwarded-for": "198.51.100.8"})
    )
    assert not mike_auth.is_local_request(
        _request(headers={"x-forwarded-for": "198.51.100.8"})
    )
    assert not mike_auth.is_local_request(
        _request(headers={"x-forwarded-host": "mike.example.com"})
    )


def test_api_key_query_parameter_is_ignored(monkeypatch):
    monkeypatch.setattr(mike_auth, "API_KEY", "expected-secret")
    assert mike_auth.extract_api_key(_request(query="api_key=expected-secret")) == ""
    assert not mike_auth.verify_api_key(
        mike_auth.extract_api_key(_request(query="api_key=expected-secret"))
    )


def test_api_key_uses_compare_digest(monkeypatch):
    monkeypatch.setattr(mike_auth, "API_KEY", "expected-secret")
    original = mike_auth.hmac.compare_digest
    calls = []

    def spy(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(mike_auth.hmac, "compare_digest", spy)
    assert mike_auth.verify_api_key("expected-secret")
    assert calls == [(b"expected-secret", b"expected-secret")]


def test_magic_admin_routes_reject_localhost_without_owner_or_key(monkeypatch):
    monkeypatch.setattr(mike_auth, "API_KEY", "expected-secret")
    monkeypatch.setattr(auth_routes, "extract_profile_session", lambda _request: None)
    client = _auth_client()
    assert client.post("/v1/auth/magic/generate", json={"profile": "marco"}).status_code == 403
    assert client.get("/v1/auth/magic/list").status_code == 403
    assert client.delete("/v1/auth/magic/token-id").status_code == 403
    assert client.get("/v1/auth/magic/list?api_key=expected-secret").status_code == 403


def test_magic_admin_accepts_owner_session_or_header_key(monkeypatch):
    monkeypatch.setattr(mike_auth, "API_KEY", "expected-secret")
    monkeypatch.setattr(auth_routes, "list_magic_tokens", lambda: [])
    monkeypatch.setattr(
        auth_routes,
        "extract_profile_session",
        lambda request: {"profile": "marco"}
        if request.headers.get("x-test-owner") == "yes"
        else None,
    )
    client = _auth_client()
    assert client.get("/v1/auth/magic/list", headers={"x-test-owner": "yes"}).status_code == 200
    assert client.get(
        "/v1/auth/magic/list",
        headers={"authorization": "Bearer expected-secret"},
    ).status_code == 200


def test_install_page_does_not_generate_token_without_owner_or_key(monkeypatch):
    import mike_server

    monkeypatch.setattr(mike_server, "extract_profile_session", lambda _request: None)
    monkeypatch.setattr(mike_server, "verify_api_key", lambda _candidate: False)

    def forbidden_generation(*_args, **_kwargs):
        raise AssertionError("unauthorized install page generated a magic token")

    monkeypatch.setattr(mike_server, "generate_magic_token", forbidden_generation)
    response = asyncio.run(mike_server.install_page("marco", _request()))
    assert response.status_code == 403
