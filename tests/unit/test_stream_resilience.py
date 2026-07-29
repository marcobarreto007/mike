"""Regression tests for SSE and streaming circuit-breaker reliability."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[2] / "core" / "server"
sys.path.insert(0, str(SERVER_DIR))

from mike_circuit_breaker import CircuitBreaker
from mike_fallback_chain import FallbackChain
from mike_sse import (
    _guard_sse_stream,
    _sse_content_chunk,
    _sse_done,
    _sse_event,
)


def _data_payload(frame: str) -> dict | None:
    for line in frame.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            return None
        return json.loads(data)
    return None


class TestSSEStreamGuard(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, source) -> list[str]:
        return [
            frame
            async for frame in _guard_sse_stream(
                source,
                "chatcmpl-test",
                error_message="stream failed",
                error_code="test_stream_failure",
            )
        ]

    async def test_exception_before_first_chunk_emits_error_and_done(self):
        async def source():
            raise RuntimeError("failed before token")
            yield ""  # pragma: no cover - makes this an async generator

        frames = await self._collect(source())

        self.assertEqual(frames[-1], _sse_done())
        self.assertEqual(sum(frame == _sse_done() for frame in frames), 1)
        self.assertIn("event: error", frames[-2])
        payload = _data_payload(frames[-2])
        self.assertEqual(payload["event"], "error")
        self.assertEqual(payload["choices"][0]["finish_reason"], "error")
        self.assertEqual(payload["error"]["code"], "test_stream_failure")
        self.assertEqual(
            payload["error"]["details"]["exception_type"],
            "RuntimeError",
        )

    async def test_exception_after_content_emits_error_and_done(self):
        content = _sse_content_chunk("chatcmpl-test", "partial")

        async def source():
            yield content
            raise ConnectionError("connection lost after token")

        frames = await self._collect(source())

        self.assertEqual(frames[0], content)
        self.assertIn("event: error", frames[-2])
        self.assertEqual(
            _data_payload(frames[-2])["choices"][0]["finish_reason"],
            "error",
        )
        self.assertEqual(frames[-1], _sse_done())

    async def test_silent_eof_is_explicit_protocol_error(self):
        async def source():
            if False:
                yield ""

        frames = await self._collect(source())

        self.assertEqual(len(frames), 2)
        payload = _data_payload(frames[0])
        self.assertEqual(payload["choices"][0]["finish_reason"], "error")
        self.assertEqual(payload["error"]["details"]["reason"], "unexpected_eof")
        self.assertEqual(frames[1], _sse_done())

    async def test_stop_frame_without_done_gets_done_without_false_error(self):
        stop = _sse_event({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        })

        async def source():
            yield stop

        frames = await self._collect(source())

        self.assertEqual(frames, [stop, _sse_done()])
        self.assertNotIn("event: error", "".join(frames))

    async def test_done_only_stream_is_not_accepted_as_empty_success(self):
        async def source():
            yield _sse_done()

        frames = await self._collect(source())

        self.assertEqual(len(frames), 2)
        self.assertEqual(
            _data_payload(frames[0])["choices"][0]["finish_reason"],
            "error",
        )
        self.assertEqual(frames[1], _sse_done())

    async def test_legacy_error_frame_is_normalized_for_frontend(self):
        legacy_error = _sse_event({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
        })

        async def source():
            yield legacy_error
            yield _sse_done()

        frames = await self._collect(source())

        self.assertEqual(len(frames), 2)
        self.assertIn("event: error", frames[0])
        payload = _data_payload(frames[0])
        self.assertIn("error", payload)
        self.assertEqual(payload["choices"][0]["finish_reason"], "error")
        self.assertEqual(frames[1], _sse_done())


class TestStreamingCircuitBreaker(unittest.TestCase):
    @staticmethod
    def _messages():
        return [{"role": "user", "content": "hello"}]

    def test_failure_during_iteration_is_recorded(self):
        cb = CircuitBreaker(failure_threshold=3)

        def stream(_messages):
            yield {"token": "first"}
            raise RuntimeError("iteration failed")

        wrapped = cb.call_stream(stream, self._messages())
        self.assertEqual(next(wrapped), {"token": "first"})
        self.assertEqual(cb.failure_count, 0)
        with self.assertRaisesRegex(RuntimeError, "iteration failed"):
            next(wrapped)
        self.assertEqual(cb.failure_count, 1)

    def test_success_is_recorded_only_after_full_exhaustion(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()

        def stream(_messages):
            yield {"token": "one"}
            yield {"token": "two"}

        wrapped = cb.call_stream(stream, self._messages())
        self.assertEqual(cb.failure_count, 1)
        self.assertEqual(next(wrapped), {"token": "one"})
        self.assertEqual(cb.failure_count, 1)
        self.assertEqual(next(wrapped), {"token": "two"})
        self.assertEqual(cb.failure_count, 1)
        with self.assertRaises(StopIteration):
            next(wrapped)
        self.assertEqual(cb.failure_count, 0)

    def test_failure_before_first_chunk_falls_back_and_trips_breaker(self):
        first_cb = CircuitBreaker(failure_threshold=3)
        second_cb = CircuitBreaker(failure_threshold=3)

        def fail_before_chunk(_messages, **_kwargs):
            raise RuntimeError("unavailable before token")
            yield  # pragma: no cover

        def healthy(_messages, **_kwargs):
            yield {"token": "fallback"}

        chain = FallbackChain([
            ("first", lambda *_args, **_kwargs: {}, first_cb, fail_before_chunk),
            ("second", lambda *_args, **_kwargs: {}, second_cb, healthy),
        ])

        frames = list(chain.execute_stream(self._messages()))
        self.assertEqual(frames[0], {"token": "fallback"})
        self.assertEqual(frames[-1]["_fallback_meta"]["backend_used"], "second")
        self.assertTrue(frames[-1]["_fallback_meta"]["fallback_attempted"])
        self.assertEqual(first_cb.failure_count, 1)
        self.assertEqual(second_cb.failure_count, 0)

    def test_failure_after_chunk_does_not_splice_fallback_stream(self):
        first_cb = CircuitBreaker(failure_threshold=3)
        second_cb = CircuitBreaker(failure_threshold=3)
        calls = {"second": 0}

        def fail_after_chunk(_messages, **_kwargs):
            yield {"token": "partial"}
            raise RuntimeError("backend died mid-stream")

        def must_not_run(_messages, **_kwargs):
            calls["second"] += 1
            yield {"token": "different answer"}

        chain = FallbackChain([
            ("first", lambda *_args, **_kwargs: {}, first_cb, fail_after_chunk),
            ("second", lambda *_args, **_kwargs: {}, second_cb, must_not_run),
        ])
        stream = chain.execute_stream(self._messages())

        self.assertEqual(next(stream), {"token": "partial"})
        with self.assertRaisesRegex(RuntimeError, "died mid-stream"):
            next(stream)
        self.assertEqual(first_cb.failure_count, 1)
        self.assertEqual(calls["second"], 0)

    def test_empty_stream_is_failure_and_uses_fallback(self):
        first_cb = CircuitBreaker(failure_threshold=3)
        second_cb = CircuitBreaker(failure_threshold=3)

        def empty(_messages, **_kwargs):
            if False:
                yield {}

        def healthy(_messages, **_kwargs):
            yield {"token": "ok"}

        chain = FallbackChain([
            ("empty", lambda *_args, **_kwargs: {}, first_cb, empty),
            ("healthy", lambda *_args, **_kwargs: {}, second_cb, healthy),
        ])

        frames = list(chain.execute_stream(self._messages()))
        self.assertEqual(frames[0], {"token": "ok"})
        self.assertEqual(first_cb.failure_count, 1)
        self.assertEqual(frames[-1]["_fallback_meta"]["backend_used"], "healthy")

    def test_admission_rejection_does_not_trip_backend_circuit(self):
        class AdmissionRejected(RuntimeError):
            counts_as_backend_failure = False

        cb = CircuitBreaker(failure_threshold=1)

        with self.assertRaises(AdmissionRejected):
            cb.call(lambda: (_ for _ in ()).throw(AdmissionRejected("busy")))

        self.assertEqual(cb.state, "closed")
        self.assertEqual(cb.failure_count, 0)

    def test_stream_admission_rejection_does_not_trip_backend_circuit(self):
        class AdmissionRejected(RuntimeError):
            counts_as_backend_failure = False

        cb = CircuitBreaker(failure_threshold=1)

        def rejected_stream():
            raise AdmissionRejected("busy")
            yield  # pragma: no cover

        wrapped = cb.call_stream(rejected_stream)
        with self.assertRaises(AdmissionRejected):
            next(wrapped)

        self.assertEqual(cb.state, "closed")
        self.assertEqual(cb.failure_count, 0)


if __name__ == "__main__":
    unittest.main()
