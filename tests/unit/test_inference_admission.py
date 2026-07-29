"""Concurrency and cleanup guarantees for local llama-server inference."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS = str(ROOT / "core" / "integrations")
if INTEGRATIONS not in sys.path:
    sys.path.insert(0, INTEGRATIONS)

from mike_llama_server_client import (  # noqa: E402
    InferenceAdmissionController,
    InferenceAdmissionError,
    MikeLlamaServerClient,
)


class _FakeResponse:
    def __init__(self, *, lines=(), payload=None, entered=None, release=None):
        self._lines = list(lines)
        self._payload = payload or {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"completion_tokens": 1},
        }
        self._entered = entered
        self._release = release
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_lines(self):
        for index, line in enumerate(self._lines):
            if index == 0 and self._entered is not None:
                self._entered.set()
            yield line
            if index == 0 and self._release is not None:
                self._release.wait(timeout=2)


class _FakeHttp:
    def __init__(self, *, post_response=None, stream_responses=()):
        self.post_response = post_response or _FakeResponse()
        self.stream_responses = list(stream_responses)
        self.closed = False

    def post(self, *_args, **_kwargs):
        response = self.post_response
        if isinstance(response, BaseException):
            raise response
        return response

    def stream(self, *_args, **_kwargs):
        return self.stream_responses.pop(0)

    def close(self):
        self.closed = True


def _client(monkeypatch, *, max_waiters="2", queue_timeout="0.05"):
    monkeypatch.setenv("MIKE_INFERENCE_MAX_WAITERS", max_waiters)
    monkeypatch.setenv("MIKE_INFERENCE_QUEUE_TIMEOUT_SECONDS", queue_timeout)
    return MikeLlamaServerClient(base_url="http://127.0.0.1:8081/v1")


def test_admission_serializes_callers_and_releases_after_success():
    controller = InferenceAdmissionController(
        parallelism=1,
        max_waiters=1,
        queue_timeout_seconds=1,
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first():
        with controller.slot():
            first_entered.set()
            release_first.wait(timeout=1)

    def second():
        first_entered.wait(timeout=1)
        with controller.slot():
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=1)
    time.sleep(0.02)
    assert not second_entered.is_set()
    assert controller.status["active"] == 1
    assert controller.status["waiting"] == 1

    release_first.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert second_entered.is_set()
    assert controller.status["active"] == 0
    assert controller.status["waiting"] == 0


def test_admission_rejects_beyond_bounded_queue():
    controller = InferenceAdmissionController(
        parallelism=1,
        max_waiters=0,
        queue_timeout_seconds=1,
    )
    with controller.slot():
        with pytest.raises(InferenceAdmissionError) as raised:
            with controller.slot():
                pass

    assert raised.value.code == "inference_queue_full"
    assert raised.value.status_code == 503
    assert controller.status["active"] == 0


def test_admission_timeout_does_not_leak_waiter_or_slot():
    controller = InferenceAdmissionController(
        parallelism=1,
        max_waiters=1,
        queue_timeout_seconds=0.01,
    )
    with controller.slot():
        with pytest.raises(InferenceAdmissionError) as raised:
            with controller.slot():
                pass
        assert raised.value.code == "inference_queue_timeout"
        assert controller.status == {
            "parallelism": 1,
            "active": 1,
            "waiting": 0,
            "max_waiters": 1,
            "queue_timeout_seconds": 0.01,
        }
    assert controller.status["active"] == 0


def test_nonstream_error_always_releases_slot(monkeypatch):
    client = _client(monkeypatch)
    client._client = _FakeHttp(post_response=RuntimeError("backend failed"))

    with pytest.raises(RuntimeError, match="backend failed"):
        client.chat_completion(messages=[{"role": "user", "content": "oi"}])

    assert client.admission_status["active"] == 0
    client._client.post_response = _FakeResponse()
    assert client.chat_completion(
        messages=[{"role": "user", "content": "tente novamente"}]
    )["choices"][0]["message"]["content"] == "ok"


def test_nonstream_cancellation_always_releases_slot(monkeypatch):
    class SimulatedCancellation(BaseException):
        pass

    client = _client(monkeypatch)
    client._client = _FakeHttp(post_response=SimulatedCancellation())

    with pytest.raises(SimulatedCancellation):
        client.chat_completion(messages=[{"role": "user", "content": "oi"}])

    assert client.admission_status["active"] == 0
    assert client.admission_status["waiting"] == 0


def test_stream_close_releases_slot_and_closes_http_response(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    first_response = _FakeResponse(
        lines=[
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: {"choices":[{"delta":{"content":"b"}}]}',
        ],
        entered=entered,
        release=release,
    )
    second_response = _FakeResponse(
        lines=[
            'data: {"choices":[{"delta":{"content":"c"}}]}',
            "data: [DONE]",
        ]
    )
    client = _client(monkeypatch)
    client._client = _FakeHttp(stream_responses=[first_response, second_response])

    stream = client.chat_completion_stream(
        messages=[{"role": "user", "content": "oi"}]
    )
    assert next(stream)["choices"][0]["delta"]["content"] == "a"
    assert entered.is_set()
    assert client.admission_status["active"] == 1

    stream.close()

    assert first_response.closed
    assert client.admission_status["active"] == 0
    assert list(
        client.chat_completion_stream(
            messages=[{"role": "user", "content": "de novo"}]
        )
    )[0]["choices"][0]["delta"]["content"] == "c"


def test_stream_iteration_error_releases_slot(monkeypatch):
    class BrokenResponse(_FakeResponse):
        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
            raise RuntimeError("stream broke")

    response = BrokenResponse()
    client = _client(monkeypatch)
    client._client = _FakeHttp(stream_responses=[response])
    stream = client.chat_completion_stream(
        messages=[{"role": "user", "content": "oi"}]
    )

    assert next(stream)["choices"][0]["delta"]["content"] == "partial"
    with pytest.raises(RuntimeError, match="stream broke"):
        next(stream)

    assert response.closed
    assert client.admission_status["active"] == 0


def test_invalid_admission_environment_uses_safe_defaults(monkeypatch):
    monkeypatch.setenv("MIKE_INFERENCE_PARALLELISM", "99")
    monkeypatch.setenv("MIKE_INFERENCE_MAX_WAITERS", "-2")
    monkeypatch.setenv("MIKE_INFERENCE_QUEUE_TIMEOUT_SECONDS", "nan")
    client = MikeLlamaServerClient(base_url="http://127.0.0.1:8081/v1")

    assert client.admission_status == {
        "parallelism": 1,
        "active": 0,
        "waiting": 0,
        "max_waiters": 2,
        "queue_timeout_seconds": 120.0,
    }
