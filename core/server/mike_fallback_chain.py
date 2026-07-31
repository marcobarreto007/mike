# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Fallback chain — tries backends in priority order with circuit-breaker awareness.

If one backend fails (exception or timeout), the chain tries the next one.
Backends whose circuit breaker is OPEN are skipped automatically.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from mike_circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, CircuitState

logger = logging.getLogger("mike.fallback_chain")

# Backend tuple: (name, callable, circuit_breaker [, stream_callable])
BackendSpec = Tuple[str, Callable, CircuitBreaker]
BackendSpecFull = Tuple[str, Callable, CircuitBreaker, Optional[Callable]]


class AllBackendsFailedError(Exception):
    """Raised when every backend in the fallback chain has failed."""

    def __init__(self, errors: List[Tuple[str, str]]):
        self.errors = errors
        details = "; ".join(f"{name}: {reason}" for name, reason in errors)
        super().__init__(f"All {len(errors)} backend(s) failed: {details}")


class FallbackChain:
    """
    Tries backends in priority order.

    If one fails (exception or timeout), tries the next.
    Uses CircuitBreaker to skip known-bad backends.

    backends: list of tuples, each one of:
        (name, callable, circuit_breaker)
        (name, callable, circuit_breaker, stream_callable)

    The callable is invoked as:  fn(messages, **kwargs)
    The stream_callable is invoked the same way and must return an iterator of chunks.
    """

    def __init__(self, backends: List[BackendSpec]):
        self._backends: List[BackendSpecFull] = []
        for b in backends:
            if len(b) == 3:
                name, fn, cb = b
                self._backends.append((name, fn, cb, None))
            elif len(b) == 4:
                name, fn, cb, stream_fn = b
                self._backends.append((name, fn, cb, stream_fn))
            else:
                raise ValueError(
                    f"Invalid backend spec: expected 3 or 4 elements, got {len(b)}"
                )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def backend_names(self) -> List[str]:
        """Ordered list of backend names."""
        return [b[0] for b in self._backends]

    @property
    def status(self) -> Dict[str, Any]:
        """Per-backend health status dictionary."""
        result: Dict[str, Any] = {}
        healthy_states = {CircuitState.CLOSED.value}
        for name, _fn, cb, _stream_fn in self._backends:
            result[name] = {
                "healthy": cb.state in healthy_states,
                "circuit": cb.state,
                "failures": cb.failure_count,
            }
        return result

    @property
    def active_backend(self) -> Optional[str]:
        """Name of the first CLOSED backend, or None."""
        for name, _fn, cb, _stream_fn in self._backends:
            if cb.state == CircuitState.CLOSED.value:
                return name
        return None

    def get_status(self) -> Dict[str, Any]:
        """Full status payload suitable for health endpoints."""
        return {
            "backends": self.status,
            "active_backend": self.active_backend,
            "fallback_chain_order": self.backend_names,
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _ordered_backends(self, preferred_backend: Optional[str] = None) -> List[BackendSpecFull]:
        """
        Return backends reordered so *preferred_backend* is tried first.

        If *preferred_backend* is None or matches no backend, the original
        insertion order is preserved.
        """
        if not preferred_backend:
            return list(self._backends)

        # Find the preferred backend
        preferred_idx = None
        for idx, (name, _fn, _cb, _sfn) in enumerate(self._backends):
            if name == preferred_backend:
                preferred_idx = idx
                break

        if preferred_idx is None or preferred_idx == 0:
            return list(self._backends)

        # Reorder: preferred_backend first, rest in original order
        ordered = [self._backends[preferred_idx]]
        ordered.extend(b for i, b in enumerate(self._backends) if i != preferred_idx)
        return ordered

    def execute(
        self, messages: list, preferred_backend: Optional[str] = None, **kwargs
    ) -> dict:
        """
        Synchronous execution. Tries each backend in order.

        If *preferred_backend* is given, it is moved to the front of the list
        so it is tried first (respecting model router selection).

        Returns the result dict from the first successful backend.
        Adds ``_fallback_meta`` with ``backend_used`` and ``fallback_attempted``.

        Raises ``AllBackendsFailedError`` if every backend fails.
        """
        errors: List[Tuple[str, str]] = []
        backends = self._ordered_backends(preferred_backend)

        for name, fn, cb, _stream_fn in backends:
            if cb.state == CircuitState.OPEN:
                logger.debug(
                    "Skipping backend '%s' — circuit breaker is open", name
                )
                errors.append((name, "circuit breaker open"))
                continue

            logger.info("FallbackChain: attempting backend '%s'", name)
            t_start = time.time()
            try:
                result = cb.call(fn, messages, **kwargs)
                elapsed = time.time() - t_start
                logger.info(
                    "FallbackChain: backend '%s' succeeded in %.2fs%s",
                    name,
                    elapsed,
                    " (fallback)" if errors else "",
                )

                if isinstance(result, dict):
                    result["_fallback_meta"] = {
                        "backend_used": name,
                        "fallback_attempted": len(errors) > 0,
                        "elapsed_seconds": round(elapsed, 3),
                    }

                if errors:
                    logger.warning(
                        "FallbackChain: fallback to '%s' after %d failure(s): %s",
                        name,
                        len(errors),
                        errors,
                    )

                return result

            except CircuitBreakerOpenError:
                logger.warning(
                    "FallbackChain: backend '%s' circuit breaker is open", name
                )
                errors.append((name, "circuit breaker open"))
                continue
            except Exception as exc:
                logger.warning(
                    "FallbackChain: backend '%s' failed (%.2fs): %s",
                    name,
                    time.time() - t_start,
                    exc,
                )
                errors.append((name, str(exc)))
                continue

        raise AllBackendsFailedError(errors)

    def execute_stream(
        self, messages: list, preferred_backend: Optional[str] = None, **kwargs
    ) -> Iterator[dict]:
        """
        Synchronous streaming execution. Tries each backend in order.

        If *preferred_backend* is given, it is moved to the front of the list
        so it is tried first (respecting model router selection).

        Yields chunks from the first successful backend, then a final
        metadata chunk with ``_fallback_meta``.

        Fallback is safe only before the first chunk. Once a backend has
        emitted data, switching providers would splice two independent
        completions into one response, so an iteration failure is propagated
        to the SSE protocol boundary instead.

        Raises ``AllBackendsFailedError`` if every backend fails.
        """
        errors: List[Tuple[str, str]] = []
        backends = self._ordered_backends(preferred_backend)

        for name, _fn, cb, stream_fn in backends:
            if stream_fn is None:
                logger.debug(
                    "FallbackChain: backend '%s' has no streaming support", name
                )
                errors.append((name, "no streaming support"))
                continue

            if cb.state == CircuitState.OPEN:
                logger.debug(
                    "FallbackChain: skipping backend '%s' — circuit breaker is open",
                    name,
                )
                errors.append((name, "circuit breaker open"))
                continue

            logger.info("FallbackChain: attempting streaming backend '%s'", name)
            t_start = time.time()
            chunk_count = 0
            try:
                def _validated_stream():
                    emitted = False
                    for backend_chunk in stream_fn(messages, **kwargs):
                        emitted = True
                        yield backend_chunk
                    if not emitted:
                        raise RuntimeError("backend stream completed without chunks")

                chunks = cb.call_stream(_validated_stream)
                for chunk in chunks:
                    chunk_count += 1
                    yield chunk

                elapsed = time.time() - t_start
                logger.info(
                    "FallbackChain: backend '%s' streaming succeeded (%d chunks, %.2fs)%s",
                    name,
                    chunk_count,
                    elapsed,
                    " (fallback)" if errors else "",
                )

                if errors:
                    logger.warning(
                        "FallbackChain: stream fallback to '%s' after %d failure(s): %s",
                        name,
                        len(errors),
                        errors,
                    )

                yield {
                    "_fallback_meta": {
                        "backend_used": name,
                        "fallback_attempted": len(errors) > 0,
                        "elapsed_seconds": round(elapsed, 3),
                        "chunk_count": chunk_count,
                    }
                }
                return

            except CircuitBreakerOpenError:
                logger.warning(
                    "FallbackChain: backend '%s' circuit breaker is open", name
                )
                errors.append((name, "circuit breaker open"))
                continue
            except Exception as exc:
                logger.warning(
                    "FallbackChain: backend '%s' streaming failed (%.2fs): %s",
                    name,
                    time.time() - t_start,
                    exc,
                )
                errors.append((name, str(exc)))
                if chunk_count > 0:
                    logger.error(
                        "FallbackChain: refusing backend switch after %d emitted "
                        "chunk(s) from '%s'",
                        chunk_count,
                        name,
                    )
                    raise
                continue

        raise AllBackendsFailedError(errors)

    async def execute_async(self, messages: list, **kwargs) -> dict:
        """Async wrapper around ``execute`` — runs in a thread."""
        return await asyncio.to_thread(self.execute, messages, **kwargs)

    async def execute_stream_async(self, messages: list, **kwargs):
        """
        Async streaming wrapper around ``execute_stream``.

        Yields chunks from the background thread via a queue.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _worker() -> None:
            try:
                for chunk in self.execute_stream(messages, **kwargs):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except BaseException as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

        import threading
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        while True:
            kind, payload = await queue.get()
            if kind == "chunk":
                yield payload
            elif kind == "error":
                raise payload
            else:  # done
                break

    def __repr__(self) -> str:
        return f"FallbackChain(backends={self.backend_names})"
