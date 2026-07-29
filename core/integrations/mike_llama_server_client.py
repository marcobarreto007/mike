"""
MIKE — llama-server OpenAI-compatible client.
Connects MIKE's model router to an external llama-server instance.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

_log = logging.getLogger("mike.llama_server")


class InferenceAdmissionError(RuntimeError):
    """The local inference queue could not safely admit a request."""

    status_code = 503
    # Overload is an admission outcome, not evidence that llama-server is
    # unhealthy. Circuit breakers use this marker to avoid a false trip.
    counts_as_backend_failure = False

    def __init__(self, code: str, message: str, *, retry_after_seconds: float):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.details = {
            "code": code,
            "retryable": True,
            "retry_after_seconds": retry_after_seconds,
        }


class InferenceAdmissionController:
    """Bound active inference and queued callers without spawning workers."""

    def __init__(
        self,
        *,
        parallelism: int = 1,
        max_waiters: int = 2,
        queue_timeout_seconds: float = 120.0,
    ):
        if parallelism < 1:
            raise ValueError("parallelism must be at least 1")
        if max_waiters < 0:
            raise ValueError("max_waiters cannot be negative")
        if (
            not math.isfinite(queue_timeout_seconds)
            or queue_timeout_seconds < 0
        ):
            raise ValueError("queue_timeout_seconds must be finite and non-negative")

        self.parallelism = parallelism
        self.max_waiters = max_waiters
        self.queue_timeout_seconds = queue_timeout_seconds
        self._condition = threading.Condition()
        self._active = 0
        self._waiting = 0

    @property
    def status(self) -> Dict[str, Any]:
        with self._condition:
            return {
                "parallelism": self.parallelism,
                "active": self._active,
                "waiting": self._waiting,
                "max_waiters": self.max_waiters,
                "queue_timeout_seconds": self.queue_timeout_seconds,
            }

    def _acquire(self) -> None:
        deadline = time.monotonic() + self.queue_timeout_seconds
        with self._condition:
            if self._active < self.parallelism:
                self._active += 1
                return

            if self._waiting >= self.max_waiters:
                raise InferenceAdmissionError(
                    "inference_queue_full",
                    "Local inference is busy and its bounded queue is full. "
                    "Retry after the current request completes.",
                    retry_after_seconds=self.queue_timeout_seconds,
                )

            self._waiting += 1
            try:
                while self._active >= self.parallelism:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise InferenceAdmissionError(
                            "inference_queue_timeout",
                            "Timed out waiting for the local inference slot. "
                            "Retry when the current request has completed.",
                            retry_after_seconds=self.queue_timeout_seconds,
                        )
                    self._condition.wait(timeout=remaining)
                self._active += 1
            finally:
                self._waiting -= 1

    def _release(self) -> None:
        with self._condition:
            if self._active <= 0:
                raise RuntimeError("inference admission slot released without acquisition")
            self._active -= 1
            self._condition.notify_all()

    @contextmanager
    def slot(self):
        self._acquire()
        try:
            yield
        finally:
            self._release()


def _env_number(
    name: str,
    default: int | float,
    *,
    minimum: int | float,
    cast,
):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        _log.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default
    if not math.isfinite(float(value)) or value < minimum:
        _log.warning("Ignoring out-of-range %s=%r; using %s", name, raw, default)
        return default
    return value


class MikeLlamaServerClient:
    """OpenAI-compatible client wrapping a local llama-server instance.

    Usage:
        client = MikeLlamaServerClient(base_url="http://127.0.0.1:8081/v1")
        response = client.chat_completion(messages=[{"role": "user", "content": "Oi"}])
    """

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "not-needed",
        model: str = "mike",
    ):
        self.base_url = (base_url or os.getenv("MIKE_LLAMA_SERVER_URL", "http://127.0.0.1:8081/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("MIKE_LLAMA_SERVER_API_KEY", "not-needed")
        self.model = model or os.getenv("MIKE_LLAMA_SERVER_MODEL", "mike")
        self.enable_thinking = os.getenv(
            "MIKE_QWEN_ENABLE_THINKING", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.timeout = httpx.Timeout(300.0, connect=5.0)
        self._client: Optional[httpx.Client] = None
        self._admission = InferenceAdmissionController(
            # The launcher runs llama-server with ``--parallel 1``. Keep this
            # fixed here so an environment typo cannot oversubscribe it.
            parallelism=1,
            max_waiters=_env_number(
                "MIKE_INFERENCE_MAX_WAITERS", 2, minimum=0, cast=int
            ),
            queue_timeout_seconds=_env_number(
                "MIKE_INFERENCE_QUEUE_TIMEOUT_SECONDS",
                120.0,
                minimum=0.0,
                cast=float,
            ),
        )

    @property
    def server_root(self) -> str:
        """Return the llama-server origin without the OpenAI ``/v1`` suffix."""
        parts = urlsplit(self.base_url)
        path = parts.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")

    @property
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    @property
    def ready(self) -> bool:
        """Check that the configured local llama-server is answering."""
        try:
            r = self._http.get(f"{self.server_root}/health", timeout=httpx.Timeout(2.0))
            return r.status_code == 200
        except Exception:
            return False

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def admission_status(self) -> Dict[str, Any]:
        """Return a safe snapshot of local inference queue utilization."""
        return self._admission.status

    # ── OpenAI-compatible chat completion ──────────────────────

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.95,
        top_k: int = 40,
        stop: Optional[List[str]] = None,
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        **kwargs,
    ) -> Dict[str, Any]:
        """Blocking, non-streaming chat completion via llama-server OpenAI API."""
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        if stop:
            payload["stop"] = stop
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        # Forward any extra args (e.g. repetition_penalty)
        for k, v in kwargs.items():
            if v is not None:
                payload[k] = v

        t0 = time.time()
        try:
            with self._admission.slot():
                r = self._http.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            _log.debug(
                "llama-server chat: %d tokens in %.1fs",
                data.get("usage", {}).get("completion_tokens", 0),
                time.time() - t0,
            )
            return data
        except Exception as exc:
            _log.error("llama-server chat failed: %s", exc)
            raise

    # ── Streaming variant ──────────────────────────────────────

    def chat_completion_stream(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.95,
        top_k: int = 40,
        stop: Optional[List[str]] = None,
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        **kwargs,
    ) -> Iterator[Dict[str, Any]]:
        """Streaming chat completion. Yields SSE-like chunk dicts."""
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        if stop:
            payload["stop"] = stop
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        for k, v in kwargs.items():
            if v is not None:
                payload[k] = v

        try:
            with self._admission.slot():
                with self._http.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json=payload,
                ) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if line.startswith("data: "):
                            chunk = line[6:]
                            if chunk == "[DONE]":
                                break
                            try:
                                yield json.loads(chunk)
                            except json.JSONDecodeError:
                                continue
        except Exception as exc:
            _log.error("llama-server stream failed: %s", exc)
            raise
