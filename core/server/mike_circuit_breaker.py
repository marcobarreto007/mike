# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Circuit Breaker pattern for backend health monitoring.

States: CLOSED (normal) -> OPEN (failing) -> HALF_OPEN (testing)
  - CLOSED: requests pass through normally
  - After N consecutive failures -> OPEN (requests fail fast)
  - After timeout -> HALF_OPEN (allow 1 test request)
  - If test succeeds -> CLOSED; if fails -> OPEN again
"""

import logging
import threading
import time
from enum import Enum

logger = logging.getLogger("mike.circuit_breaker")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """Raised when a circuit breaker is OPEN and the request is rejected."""

    def __init__(self, message: str = ""):
        super().__init__(message or "Circuit breaker is open — request rejected.")


class CircuitBreaker:
    """
    A thread-safe circuit breaker that protects backends from cascading failures.

    States:
      CLOSED  — normal operation, requests pass through
      OPEN    — after failure_threshold consecutive failures, requests fail fast
      HALF_OPEN — after timeout_seconds, allows one test request to probe health
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        timeout_seconds: float = 30.0,
        half_open_max_requests: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.half_open_max_requests = half_open_max_requests

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float | None = None
        self._lock = threading.Lock()
        self._half_open_requests: int = 0

    # ------------------------------------------------------------------
    # Public properties (thread-safe reads)
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """Current circuit state as a lowercase string."""
        with self._lock:
            self._transition_if_needed()
            return self._state.value

    @property
    def failure_count(self) -> int:
        """Number of consecutive failures since last success."""
        with self._lock:
            return self._failure_count

    @property
    def last_failure_time(self) -> float | None:
        """Unix timestamp of the most recent failure, or None."""
        with self._lock:
            return self._last_failure_time

    # ------------------------------------------------------------------
    # Transition logic
    # ------------------------------------------------------------------

    def _transition_if_needed(self) -> None:
        """Called inside the lock — checks if OPEN can transition to HALF_OPEN."""
        if self._state == CircuitState.OPEN:
            if (
                self._last_failure_time is not None
                and (time.time() - self._last_failure_time) >= self.timeout_seconds
            ):
                previous = self._state
                self._state = CircuitState.HALF_OPEN
                self._half_open_requests = 0
                logger.info(
                    "Circuit breaker transitioning: %s -> %s",
                    previous.value,
                    self._state.value,
                )

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def _acquire_call_slot(self) -> None:
        """Validate circuit state and reserve a HALF_OPEN probe slot."""
        with self._lock:
            self._transition_if_needed()

            if self._state == CircuitState.OPEN:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker is OPEN. "
                    f"{self._failure_count} consecutive failures, "
                    f"last failure at {self._last_failure_time:.0f}"
                )

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_requests >= self.half_open_max_requests:
                    raise CircuitBreakerOpenError(
                        "Circuit breaker is HALF_OPEN — test request capacity reached."
                    )
                self._half_open_requests += 1

    def _release_aborted_call_slot(self) -> None:
        """Release a HALF_OPEN slot when a consumer cancels without an outcome."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN and self._half_open_requests > 0:
                self._half_open_requests -= 1

    def call(self, fn, *args, **kwargs):
        """
        Wrap a callable with circuit-breaker logic.

        Returns the result of fn(*args, **kwargs) on success.
        Raises CircuitBreakerOpenError if the circuit is OPEN.
        Raises the underlying exception if fn() fails (after recording the failure).
        """
        self._acquire_call_slot()
        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception as exc:
            if getattr(exc, "counts_as_backend_failure", True):
                self.record_failure()
            else:
                self._release_aborted_call_slot()
            raise

    def call_stream(self, fn, *args, **kwargs):
        """Yield a backend stream and record its outcome after iteration.

        A generator object being created is not evidence of backend health.
        Failure is recorded if invocation or iteration raises, while success is
        recorded only when the iterator reaches normal exhaustion.
        """
        self._acquire_call_slot()
        iterator = None
        try:
            iterator = iter(fn(*args, **kwargs))
            for item in iterator:
                yield item
        except GeneratorExit:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
            self._release_aborted_call_slot()
            raise
        except BaseException as exc:
            if getattr(exc, "counts_as_backend_failure", True):
                self.record_failure()
            else:
                self._release_aborted_call_slot()
            raise
        else:
            self.record_success()

    def record_success(self) -> None:
        """Report a successful call — resets the breaker to CLOSED."""
        with self._lock:
            previous = self._state
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._half_open_requests = 0
            if previous != CircuitState.CLOSED:
                logger.info(
                    "Circuit breaker reset to CLOSED (was %s)",
                    previous.value,
                )

    def record_failure(self) -> None:
        """Report a failed call — counts toward the threshold."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    previous = self._state
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "Circuit breaker tripped: %s -> %s (%d consecutive failures)",
                        previous.value,
                        self._state.value,
                        self._failure_count,
                    )
            elif self._state == CircuitState.HALF_OPEN:
                previous = self._state
                self._state = CircuitState.OPEN
                self._half_open_requests = 0
                logger.warning(
                    "Circuit breaker tripped: %s -> %s (test request failed)",
                    previous.value,
                    self._state.value,
                )

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(state={self.state}, failures={self.failure_count}, "
            f"threshold={self.failure_threshold}, timeout={self.timeout_seconds}s)"
        )
