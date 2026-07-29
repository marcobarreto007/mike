# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Unit tests for the resilience layer:
  - mike_circuit_breaker.CircuitBreaker
  - mike_fallback_chain.FallbackChain / AllBackendsFailedError
"""

import sys
import unittest
from pathlib import Path

# Ensure core/server is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "core" / "server"))

from mike_circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
)
from mike_fallback_chain import AllBackendsFailedError, FallbackChain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUCCESS = {"choices": [{"message": {"content": "hello"}}]}


def _make_succeeding_backend(name):
    """Return a callable that always succeeds."""
    def _fn(messages, **kwargs):
        return {"choices": [{"message": {"content": f"response from {name}"}}], **_SUCCESS}
    return _fn


def _make_failing_backend(name, exception_type=RuntimeError):
    """Return a callable that always raises."""
    def _fn(messages, **kwargs):
        raise exception_type(f"backend {name} is down")
    return _fn


def _make_failing_then_succeeding_backend(name, fail_count=2):
    """Return a callable that fails N times then succeeds."""
    counter = {"count": 0}

    def _fn(messages, **kwargs):
        counter["count"] += 1
        if counter["count"] <= fail_count:
            raise RuntimeError(f"backend {name} failure #{counter['count']}")
        return {"choices": [{"message": {"content": f"response from {name}"}}]}

    return _fn


def _make_streaming_backend(name, chunks=None):
    """Return a streaming callable that yields chunks."""
    if chunks is None:
        chunks = [{"chunk": f"chunk1-{name}"}, {"chunk": f"chunk2-{name}"}]

    def _fn(messages, **kwargs):
        for c in chunks:
            yield c

    return _fn


def _make_failing_streaming_backend(name):
    """Return a streaming callable that raises immediately."""
    def _fn(messages, **kwargs):
        raise RuntimeError(f"streaming backend {name} is down")
        yield  # never reached
    return _fn


# ---------------------------------------------------------------------------
# Tests: CircuitBreaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker(unittest.TestCase):
    """Test CircuitBreaker state transitions."""

    def test_initial_state_closed(self):
        cb = CircuitBreaker(failure_threshold=3, timeout_seconds=30)
        self.assertEqual(cb.state, "closed")
        self.assertEqual(cb.failure_count, 0)
        self.assertIsNone(cb.last_failure_time)

    def test_record_failure_increments_count(self):
        cb = CircuitBreaker()
        cb.record_failure()
        self.assertEqual(cb.failure_count, 1)
        cb.record_failure()
        self.assertEqual(cb.failure_count, 2)

    def test_record_success_resets_count(self):
        cb = CircuitBreaker()
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        self.assertEqual(cb.failure_count, 0)
        self.assertEqual(cb.state, "closed")

    def test_closed_to_open_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, timeout_seconds=30)
        for _ in range(3):
            cb.record_failure()
        self.assertEqual(cb.state, "open")
        self.assertEqual(cb.failure_count, 3)

    def test_open_rejects_calls(self):
        cb = CircuitBreaker(failure_threshold=2, timeout_seconds=300)
        for _ in range(2):
            cb.record_failure()
        self.assertEqual(cb.state, "open")

        with self.assertRaises(CircuitBreakerOpenError):
            cb.call(lambda: "should not run")

    def test_open_to_half_open_after_timeout(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            timeout_seconds=0.01,  # very fast timeout for testing
        )
        for _ in range(2):
            cb.record_failure()
        self.assertEqual(cb.state, "open")

        import time
        time.sleep(0.02)  # wait for timeout to elapse

        # Accessing .state triggers _transition_if_needed
        self.assertEqual(cb.state, "half_open")

    def test_half_open_success_resets_to_closed(self):
        cb = CircuitBreaker(failure_threshold=2, timeout_seconds=0.01)
        for _ in range(2):
            cb.record_failure()
        self.assertEqual(cb.state, "open")

        import time
        time.sleep(0.02)
        self.assertEqual(cb.state, "half_open")

        # Test request succeeds
        result = cb.call(lambda: "ok")
        self.assertEqual(result, "ok")
        self.assertEqual(cb.state, "closed")
        self.assertEqual(cb.failure_count, 0)

    def test_half_open_failure_returns_to_open(self):
        cb = CircuitBreaker(failure_threshold=2, timeout_seconds=0.01)
        for _ in range(2):
            cb.record_failure()
        self.assertEqual(cb.state, "open")

        import time
        time.sleep(0.02)
        self.assertEqual(cb.state, "half_open")

        # Test request fails
        cb.record_failure()  # simulate a failed call via record_failure
        self.assertEqual(cb.state, "open")

    def test_full_transition_cycle(self):
        """CLOSED -> OPEN -> HALF_OPEN -> CLOSED"""
        cb = CircuitBreaker(failure_threshold=2, timeout_seconds=0.01)
        import time

        # CLOSED -> OPEN
        for _ in range(2):
            cb.record_failure()
        self.assertEqual(cb.state, "open")

        # OPEN -> HALF_OPEN
        time.sleep(0.02)
        self.assertEqual(cb.state, "half_open")

        # HALF_OPEN -> CLOSED (success)
        cb.record_success()
        self.assertEqual(cb.state, "closed")
        self.assertEqual(cb.failure_count, 0)

    def test_call_records_success(self):
        cb = CircuitBreaker(failure_threshold=2)
        result = cb.call(lambda x: x * 2, 21)
        self.assertEqual(result, 42)
        self.assertEqual(cb.failure_count, 0)

    def test_call_records_failure_and_raises(self):
        cb = CircuitBreaker(failure_threshold=5)

        with self.assertRaises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))

        self.assertEqual(cb.failure_count, 1)


# ---------------------------------------------------------------------------
# Tests: FallbackChain
# ---------------------------------------------------------------------------

class TestFallbackChain(unittest.TestCase):
    """Test FallbackChain with multiple backends."""

    def test_first_backend_succeeds(self):
        cb1 = CircuitBreaker()
        cb2 = CircuitBreaker()
        cb3 = CircuitBreaker()

        backends = [
            ("a", _make_succeeding_backend("a"), cb1),
            ("b", _make_succeeding_backend("b"), cb2),
            ("c", _make_succeeding_backend("c"), cb3),
        ]
        chain = FallbackChain(backends)
        result = chain.execute([{"role": "user", "content": "hi"}])

        self.assertIn("_fallback_meta", result)
        self.assertEqual(result["_fallback_meta"]["backend_used"], "a")
        self.assertFalse(result["_fallback_meta"]["fallback_attempted"])

    def test_first_two_fail_third_succeeds(self):
        cb1 = CircuitBreaker(failure_threshold=10)
        cb2 = CircuitBreaker(failure_threshold=10)
        cb3 = CircuitBreaker()

        backends = [
            ("bad1", _make_failing_backend("bad1"), cb1),
            ("bad2", _make_failing_backend("bad2"), cb2),
            ("good", _make_succeeding_backend("good"), cb3),
        ]
        chain = FallbackChain(backends)
        result = chain.execute([{"role": "user", "content": "hi"}])

        self.assertEqual(result["_fallback_meta"]["backend_used"], "good")
        self.assertTrue(result["_fallback_meta"]["fallback_attempted"])
        self.assertEqual(cb1.failure_count, 1)
        self.assertEqual(cb2.failure_count, 1)
        self.assertEqual(cb3.failure_count, 0)

    def test_all_fail_raises_all_backends_failed_error(self):
        cb1 = CircuitBreaker(failure_threshold=10)
        cb2 = CircuitBreaker(failure_threshold=10)

        backends = [
            ("bad1", _make_failing_backend("bad1"), cb1),
            ("bad2", _make_failing_backend("bad2"), cb2),
        ]
        chain = FallbackChain(backends)

        with self.assertRaises(AllBackendsFailedError) as ctx:
            chain.execute([{"role": "user", "content": "hi"}])

        self.assertEqual(len(ctx.exception.errors), 2)
        self.assertIn("bad1", ctx.exception.errors[0][0])
        self.assertIn("bad2", ctx.exception.errors[1][0])

    def test_skips_open_circuit_backends(self):
        cb1 = CircuitBreaker(failure_threshold=2, timeout_seconds=300)
        cb1.record_failure()
        cb1.record_failure()
        self.assertEqual(cb1.state, "open")

        cb2 = CircuitBreaker()

        backends = [
            ("open_backend", _make_succeeding_backend("open"), cb1),
            ("good", _make_succeeding_backend("good"), cb2),
        ]
        chain = FallbackChain(backends)
        result = chain.execute([{"role": "user", "content": "hi"}])

        # Should skip the open backend and use the second
        self.assertEqual(result["_fallback_meta"]["backend_used"], "good")
        # The first was skipped, not called, so its failure count stays
        self.assertEqual(cb1.failure_count, 2)

    def test_streaming_first_succeeds(self):
        cb1 = CircuitBreaker()
        cb2 = CircuitBreaker()

        backends = [
            ("streamer",
             _make_succeeding_backend("s"),   # non-stream placeholder
             cb1,
             _make_streaming_backend("s")),    # stream callable
            ("fallback_s",
             _make_succeeding_backend("f"),
             cb2,
             _make_streaming_backend("f")),
        ]
        chain = FallbackChain(backends)

        chunks = list(chain.execute_stream([{"role": "user", "content": "hi"}]))
        self.assertGreaterEqual(len(chunks), 2)

        # Last chunk should be the metadata
        meta = chunks[-1]
        self.assertIn("_fallback_meta", meta)
        self.assertEqual(meta["_fallback_meta"]["backend_used"], "streamer")
        self.assertFalse(meta["_fallback_meta"]["fallback_attempted"])

    def test_streaming_first_fails_second_succeeds(self):
        cb1 = CircuitBreaker(failure_threshold=10)
        cb2 = CircuitBreaker()

        backends = [
            ("bad_stream",
             _make_succeeding_backend("bad"),
             cb1,
             _make_failing_streaming_backend("bad")),
            ("good_stream",
             _make_succeeding_backend("good"),
             cb2,
             _make_streaming_backend("good", [{"c": "g1"}, {"c": "g2"}])),
        ]
        chain = FallbackChain(backends)

        chunks = list(chain.execute_stream([{"role": "user", "content": "hi"}]))
        self.assertGreaterEqual(len(chunks), 2)
        meta = chunks[-1]
        self.assertEqual(meta["_fallback_meta"]["backend_used"], "good_stream")
        self.assertTrue(meta["_fallback_meta"]["fallback_attempted"])

    def test_get_status_and_properties(self):
        cb1 = CircuitBreaker()
        cb2 = CircuitBreaker(failure_threshold=2)
        cb2.record_failure()
        cb2.record_failure()
        cb3 = CircuitBreaker()

        backends = [
            ("a", _make_succeeding_backend("a"), cb1),
            ("b", _make_succeeding_backend("b"), cb2),
            ("c", _make_succeeding_backend("c"), cb3),
        ]
        chain = FallbackChain(backends)

        status = chain.get_status()
        self.assertIn("backends", status)
        self.assertIn("active_backend", status)
        self.assertIn("fallback_chain_order", status)

        self.assertEqual(status["active_backend"], "a")
        self.assertEqual(status["fallback_chain_order"], ["a", "b", "c"])
        self.assertTrue(status["backends"]["a"]["healthy"])
        self.assertFalse(status["backends"]["b"]["healthy"])
        self.assertEqual(status["backends"]["b"]["circuit"], "open")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging

    # Reduce noise during test runs
    logging.basicConfig(level=logging.WARNING)

    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if result.wasSuccessful():
        print("✅ All resilience tests passed!")
    else:
        failures = len(result.failures) + len(result.errors)
        print(f"❌ {failures} test(s) failed or errored.")
        if result.failures:
            for test, trace in result.failures:
                print(f"  FAIL: {test}")
        if result.errors:
            for test, trace in result.errors:
                print(f"  ERROR: {test}")
    sys.exit(0 if result.wasSuccessful() else 1)
