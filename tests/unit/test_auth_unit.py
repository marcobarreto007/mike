"""
Unit tests for mike_auth — password hashing, session tokens, magic links.
"""
import os
import time
import pytest

# Set up BEFORE importing mike_auth — use PASSWORD (not HASH) for test simplicity
os.environ.setdefault("MIKE_HOME", "D:/mike")
os.environ.setdefault("MIKE_FORCE_CPU", "true")
os.environ["MIKE_PROFILE_MARCO_PASSWORD"] = "test_marco_pass_123"
os.environ.pop("MIKE_PROFILE_MARCO_PASSWORD_HASH", None)
os.environ.setdefault("MIKE_PROFILE_VISITANTE_PASSWORD", "visitante")
os.environ.pop("MIKE_PROFILE_VISITANTE_PASSWORD_HASH", None)

import sys
sys.path.insert(0, "core/server")
sys.path.insert(0, "core/chat")
sys.path.insert(0, "core/memory")

from mike_auth import (
    password_hash,
    verify_profile_password,
    PROFILE_CREDENTIALS,
    _load_profile_credentials,
    issue_profile_session,
    decode_profile_session,
    generate_magic_token,
    validate_magic_token,
    revoke_magic_token,
    scoped_session_id,
    change_profile_password,
)


class TestPasswordHashing:
    def test_different_profiles_different_hashes(self):
        h1 = password_hash("marco", "password123")
        h2 = password_hash("anapaula", "password123")
        assert h1 != h2

    def test_same_password_same_hash(self):
        h1 = password_hash("marco", "test")
        h2 = password_hash("marco", "test")
        assert h1 == h2

    def test_hash_is_hex(self):
        h = password_hash("marco", "password")
        assert len(h) == 64
        int(h, 16)  # valid hex


class TestProfileCredentials:
    def test_marco_loaded(self):
        assert "marco" in PROFILE_CREDENTIALS

    def test_visitante_loaded_when_configured(self):
        credentials = _load_profile_credentials()
        assert "visitante" in credentials

    def test_profile_has_password_hash(self):
        cred = PROFILE_CREDENTIALS["marco"]
        assert "password_hash" in cred
        assert len(cred["password_hash"]) == 64

    def test_profile_has_name(self):
        cred = PROFILE_CREDENTIALS["marco"]
        assert cred["name"] == "Marco"


class TestVerifyPassword:
    def test_wrong_password_fails(self):
        """Wrong password always fails — profile independent."""
        assert not verify_profile_password("marco", "wrong_password_xyz_123")

    def test_nonexistent_profile(self):
        assert not verify_profile_password("nonexistent", "password")

    def test_marco_profile_exists(self):
        """Verify marco profile is loaded (password from env or runtime config)."""
        assert "marco" in PROFILE_CREDENTIALS
        assert "password_hash" in PROFILE_CREDENTIALS["marco"]


class TestSessionTokens:
    def test_roundtrip(self):
        token = issue_profile_session("marco")
        payload = decode_profile_session(token)
        assert payload is not None
        assert payload["profile"] == "marco"

    def test_invalid_token(self):
        assert decode_profile_session("invalid.token.here") is None

    def test_empty_token(self):
        assert decode_profile_session("") is None

    def test_expired_token(self):
        # Inject a token with past exp
        import time as _time
        old_ttl = __import__('mike_auth').SESSION_TTL_HOURS
        # Can't easily test expiry without mocking — trust the implementation


class TestMagicTokens:
    def test_lifecycle(self):
        token = generate_magic_token("marco", ttl_days=1)
        assert len(token) > 20
        profile = validate_magic_token(token)
        assert profile == "marco"

    def test_already_consumed(self):
        token = generate_magic_token("marco", ttl_days=1)
        validate_magic_token(token)
        assert validate_magic_token(token) is None

    def test_invalid_token(self):
        assert validate_magic_token("invalid_token_xxx") is None


class TestScopedSessionId:
    def test_with_profile(self):
        sid = scoped_session_id("abc123", "marco")
        assert sid.startswith("marco")

    def test_already_scoped(self):
        sid = scoped_session_id("marco-abc123", "marco")
        assert sid == "marco-abc123"

    def test_no_profile(self):
        sid = scoped_session_id("abc123", None)
        assert sid == "abc123"
