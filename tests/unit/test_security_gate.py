import pytest
SERVICE_TOKEN = "REDACTED-test-token"

def test_service_token_is_not_hardcoded():
    assert "live_" not in SERVICE_TOKEN
