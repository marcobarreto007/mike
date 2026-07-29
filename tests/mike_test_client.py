"""
Mike Test Client — modulo comum para todos os testes do projeto.

Uso:

    from tests.mike_test_client import MikeTestClient, check, PASS, FAIL

    client = MikeTestClient(port=8083)
    client.wait_for_health()

    status, body = client.api_post("/v1/chat/completions", {"model": "mike", "messages": [...]})
    check("chat completions endpoint", status == 200, f"got {status}")

    print(client.summary())
    sys.exit(0 if client.passed else 1)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


# ---------------------------------------------------------------------------
# CLI args (shared across all tests)
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--port", type=int, default=8083)
_parser.add_argument("--timeout", type=int, default=60)
_parser.add_argument("--base-url", type=str, default=None)
_parsed, _unknown = _parser.parse_known_args()

BASE_URL = _parsed.base_url or f"http://127.0.0.1:{_parsed.port}"
DEFAULT_TIMEOUT = _parsed.timeout

# ---------------------------------------------------------------------------
# PASS / FAIL tracking (module-level for simple scripts)
# ---------------------------------------------------------------------------
PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    """Assert-style check that tracks PASS/FAIL globally."""
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{' — ' + detail if detail else ''}")
    return ok


def summary(exit_on_fail: bool = False) -> str:
    """Return a summary string. If exit_on_fail and any test failed, sys.exit(1)."""
    total = PASS + FAIL
    msg = f"\n{'='*50}\nResults: {PASS} passed, {FAIL} failed ({total} total)"
    print(msg)
    if exit_on_fail and FAIL > 0:
        sys.exit(1)
    return msg


# ---------------------------------------------------------------------------
# HTTP helpers (standalone — no external deps)
# ---------------------------------------------------------------------------
def api_request(
    method: str,
    path: str,
    body: dict | None = None,
    timeout: int | None = None,
    base_url: str | None = None,
) -> tuple[int, dict | str]:
    """Make an HTTP request to the Mike API. Returns (status_code, parsed_body_or_raw_string)."""
    url = f"{base_url or BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout or DEFAULT_TIMEOUT)
        raw = r.read().decode("utf-8", errors="replace")
        try:
            return r.status, json.loads(raw)
        except json.JSONDecodeError:
            return r.status, raw
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def api_get(path: str, timeout: int | None = None, base_url: str | None = None) -> tuple[int, dict | str]:
    """GET request helper."""
    return api_request("GET", path, timeout=timeout, base_url=base_url)


def api_post(path: str, body: dict, timeout: int | None = None, base_url: str | None = None) -> tuple[int, dict | str]:
    """POST request helper."""
    return api_request("POST", path, body=body, timeout=timeout, base_url=base_url)


# ---------------------------------------------------------------------------
# MikeTestClient class
# ---------------------------------------------------------------------------
class MikeTestClient:
    """Stateful client with health-check and summary tracking."""

    def __init__(self, port: int | None = None, base_url: str | None = None, timeout: int | None = None):
        self.base_url = base_url or f"http://127.0.0.1:{port or _parsed.port}"
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._passed = 0
        self._failed = 0

    # -- health --
    def wait_for_health(self, max_wait_sec: int = 60, poll_interval: float = 2.0) -> bool:
        """Poll /health until 200 or timeout."""
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            try:
                status, body = self.api_get("/health")
                if status == 200 and isinstance(body, dict):
                    if body.get("status") in ("ok", "healthy"):
                        print(f"Mike is healthy on {self.base_url}")
                        return True
            except Exception:
                pass
            time.sleep(poll_interval)
        print(f"Health check timed out after {max_wait_sec}s", file=sys.stderr)
        return False

    def is_healthy(self) -> bool:
        """One-shot health check."""
        try:
            status, body = self.api_get("/health")
            return status == 200 and isinstance(body, dict) and body.get("status") in ("ok", "healthy")
        except Exception:
            return False

    # -- HTTP --
    def api_request(self, method: str, path: str, body: dict | None = None, timeout: int | None = None) -> tuple[int, dict | str]:
        return api_request(method, path, body=body, timeout=timeout or self.timeout, base_url=self.base_url)

    def api_get(self, path: str, timeout: int | None = None) -> tuple[int, dict | str]:
        return self.api_request("GET", path, timeout=timeout)

    def api_post(self, path: str, body: dict, timeout: int | None = None) -> tuple[int, dict | str]:
        return self.api_request("POST", path, body=body, timeout=timeout)

    # -- tracking --
    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self._passed += 1
            print(f"  [PASS] {name}")
        else:
            self._failed += 1
            print(f"  [FAIL] {name}{' — ' + detail if detail else ''}")
        return ok

    @property
    def passed(self) -> int:
        return self._passed

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def total(self) -> int:
        return self._passed + self._failed

    def summary(self, exit_on_fail: bool = False) -> str:
        msg = f"\n{'='*50}\nResults: {self._passed} passed, {self._failed} failed ({self.total} total)"
        print(msg)
        if exit_on_fail and self._failed > 0:
            sys.exit(1)
        return msg


# ---------------------------------------------------------------------------
# Auto-discovery: allow running any test file directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # If run directly, just show what we know
    print(f"Mike Test Client — base URL: {BASE_URL}")
    print(f"Health check: {'OK' if MikeTestClient().is_healthy() else 'UNREACHABLE'}")
