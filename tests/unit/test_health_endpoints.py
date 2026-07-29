"""Focused tests for Mike's liveness and readiness probes."""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = str(ROOT / "core" / "server")
if SERVER_PATH not in sys.path:
    sys.path.insert(0, SERVER_PATH)

import shared_state
from routers import system


class _Mcp:
    enabled = True


class _Backend:
    ready = True


class _Router:
    healthy_backends = ["llama_server"]

    def __init__(self, backend=None):
        self.backend = backend if backend is not None else _Backend()

    def get_backend(self, name):
        return self.backend if name == "llama_server" else None


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_state = {
            key: getattr(shared_state, key)
            for key in (
                "ready",
                "memory_service",
                "mcp_workspace",
                "model_router",
                "llm",
                "stats",
            )
        }
        app = FastAPI()
        app.include_router(system.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        for key, value in self.original_state.items():
            setattr(shared_state, key, value)

    @staticmethod
    def _healthy_state():
        shared_state.ready = True
        shared_state.memory_service = object()
        shared_state.mcp_workspace = _Mcp()
        shared_state.model_router = _Router()
        shared_state.llm = True
        shared_state.stats = {"project_root": str(ROOT)}

    async def test_livez_stays_200_during_bootstrap(self):
        shared_state.ready = False
        with patch.object(
            system, "_probe_llm_backend", new_callable=AsyncMock
        ) as probe:
            response = await self.client.get("/livez")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "alive")
        probe.assert_not_awaited()

    async def test_readyz_is_503_while_starting_without_probe(self):
        shared_state.ready = False
        with patch.object(
            system, "_probe_llm_backend", new_callable=AsyncMock
        ) as probe:
            response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "starting")
        probe.assert_not_awaited()

    async def test_readyz_is_200_when_shared_state_and_qwen_are_ready(self):
        self._healthy_state()
        result = {
            "status": "ok",
            "backend": "llama_server",
            "detail": "http_200",
            "latency_ms": 3.0,
        }
        with patch.object(
            system, "_probe_llm_backend", new=AsyncMock(return_value=result)
        ):
            response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertTrue(response.json()["ready"])

    async def test_readyz_is_503_when_shared_dependency_is_degraded(self):
        self._healthy_state()
        shared_state.memory_service = None
        result = {
            "status": "ok",
            "backend": "llama_server",
            "detail": "http_200",
            "latency_ms": 2.0,
        }
        with patch.object(
            system, "_probe_llm_backend", new=AsyncMock(return_value=result)
        ):
            response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "degraded")
        self.assertEqual(response.json()["checks"]["memory"]["status"], "degraded")

    async def test_readyz_is_503_when_qwen_probe_fails(self):
        self._healthy_state()
        result = {
            "status": "unhealthy",
            "backend": "llama_server",
            "detail": "timeout",
            "latency_ms": 50.0,
        }
        with patch.object(
            system, "_probe_llm_backend", new=AsyncMock(return_value=result)
        ):
            response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unhealthy")
        self.assertEqual(response.json()["checks"]["llm"]["detail"], "timeout")

    async def test_qwen_probe_timeout_is_bounded(self):
        async def slow_probe(_url, _timeout):
            await asyncio.sleep(1)
            return True, "http_200"

        qwen = type("Qwen", (), {"server_root": "http://qwen.test"})()
        started = time.monotonic()
        with patch.object(system, "_probe_http_health", side_effect=slow_probe):
            result = await system._probe_llm_backend(
                "llama_server", qwen, timeout_seconds=0.05
            )
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(result["detail"], "timeout")

    async def test_health_contract_remains_compatible(self):
        self._healthy_state()
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(payload["status"], {"healthy", "degraded", "unhealthy"})
        self.assertEqual(payload["name"], "Mike")
        self.assertIn("model", payload)
        self.assertIn("checks", payload)


if __name__ == "__main__":
    unittest.main()
