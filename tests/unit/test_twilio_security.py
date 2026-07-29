"""Security regression tests for the Twilio webhook perimeter."""

import base64
import hashlib
import hmac
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

for path in ("core/server", "core/comms"):
    if path not in sys.path:
        sys.path.insert(0, path)

import mike_twilio_webhooks as webhooks


def _request(*, signature=""):
    headers = [(b"host", b"mike.example.test")]
    if signature:
        headers.append((b"x-twilio-signature", signature.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/twilio/sms/status",
            "raw_path": b"/twilio/sms/status",
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.10", 12345),
            "server": ("mike.example.test", 443),
        }
    )


def _local_request():
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/twilio/sms/status",
            "raw_path": b"/twilio/sms/status",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8083")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8083),
        }
    )


def _signature(token, url, params):
    material = url + "".join(f"{key}{value}" for key, value in sorted(params.items()))
    digest = hmac.new(token.encode(), material.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def test_webhook_fails_closed_when_feature_is_absent(monkeypatch):
    monkeypatch.delenv("MIKE_TWILIO_ENABLED", raising=False)
    monkeypatch.setenv("MIKE_TWILIO_AUTH_TOKEN", "configured-test-token")
    monkeypatch.setenv("MIKE_TWILIO_SKIP_VALIDATION", "true")

    assert not webhooks._validate_twilio_signature(_request(), {})


def test_webhook_fails_closed_when_auth_token_is_absent(monkeypatch):
    monkeypatch.setenv("MIKE_TWILIO_ENABLED", "true")
    monkeypatch.delenv("MIKE_TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MIKE_TWILIO_SKIP_VALIDATION", "true")

    assert not webhooks._validate_twilio_signature(_request(), {})


def test_all_mutating_webhook_endpoints_fail_closed(monkeypatch):
    monkeypatch.delenv("MIKE_TWILIO_ENABLED", raising=False)
    monkeypatch.delenv("MIKE_TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MIKE_TWILIO_SKIP_VALIDATION", "true")
    app = FastAPI()
    app.include_router(webhooks.router)
    client = TestClient(app, base_url="https://mike.example.test")

    for path in (
        "/twilio/voice/appointment",
        "/twilio/voice/gather",
        "/twilio/voice/status",
        "/twilio/sms/status",
        "/twilio/sms/incoming",
    ):
        assert client.post(path, data={}).status_code == 403


def test_development_bypass_is_restricted_to_loopback(monkeypatch):
    monkeypatch.setenv("MIKE_TWILIO_ENABLED", "true")
    monkeypatch.setenv("MIKE_TWILIO_AUTH_TOKEN", "configured-test-token")
    monkeypatch.setenv("MIKE_TWILIO_SKIP_VALIDATION", "true")

    assert not webhooks._validate_twilio_signature(_request(), {})
    assert webhooks._validate_twilio_signature(_local_request(), {})


def test_webhook_rejects_missing_or_invalid_signature(monkeypatch):
    monkeypatch.setenv("MIKE_TWILIO_ENABLED", "true")
    monkeypatch.setenv("MIKE_TWILIO_AUTH_TOKEN", "configured-test-token")
    monkeypatch.setenv("MIKE_TWILIO_SKIP_VALIDATION", "false")

    assert not webhooks._validate_twilio_signature(_request(), {"MessageSid": "SM123"})
    assert not webhooks._validate_twilio_signature(
        _request(signature="invalid-signature"), {"MessageSid": "SM123"}
    )


def test_webhook_accepts_valid_signature(monkeypatch):
    token = "configured-test-token"
    url = "https://mike.example.test/twilio/sms/status"
    params = {"MessageStatus": "delivered", "MessageSid": "SM123"}
    signature = _signature(token, url, params)
    monkeypatch.setenv("MIKE_TWILIO_ENABLED", "true")
    monkeypatch.setenv("MIKE_TWILIO_AUTH_TOKEN", token)
    monkeypatch.setenv("MIKE_TWILIO_SKIP_VALIDATION", "false")

    assert webhooks._validate_twilio_signature(
        _request(signature=signature), params
    )
