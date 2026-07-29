# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Event Bus — Async Pub/Sub for Event-Driven Autonomy
=========================================================

Lightweight event bus using asyncio.  Replaces polling loops with
immediate reactions to external events (email received, SMS reply,
calendar alert, task created, etc.).

Usage:
    from mike_event_bus import MikeEventBus
    bus = MikeEventBus()
    bus.subscribe("email.family", my_handler)
    await bus.publish("email.family", {"from": "...", "subject": "..."})
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("mike.event_bus")

# Type alias: async handler that receives (event_type, payload)
EventHandler = Callable[[dict], Awaitable[Any]]


class MikeEventBus:
    """
    Simple pub/sub event bus using asyncio.

    Events:
        email.urgent       — heartbeat detects urgent email
        email.family       — family member emails
        email.received     — any new email received
        sms.reply          — SMS reply received (from Twilio webhook)
        sms.status         — SMS delivery status update
        appointment.updated — appointment status changed
        calendar.reminder  — upcoming event reminder
        task.created       — new task on TaskBoard
        task.completed     — task completed
        system.alert       — system health alert
    """

    # Built-in event types
    EVENT_EMAIL_URGENT = "email.urgent"
    EVENT_EMAIL_FAMILY = "email.family"
    EVENT_EMAIL_RECEIVED = "email.received"
    EVENT_SMS_REPLY = "sms.reply"
    EVENT_SMS_STATUS = "sms.status"
    EVENT_APPOINTMENT_UPDATED = "appointment.updated"
    EVENT_CALENDAR_REMINDER = "calendar.reminder"
    EVENT_TASK_CREATED = "task.created"
    EVENT_TASK_COMPLETED = "task.completed"
    EVENT_SYSTEM_ALERT = "system.alert"

    def __init__(self, max_concurrent_handlers: int = 10) -> None:
        # event_type -> set of async handler callables
        self._subscribers: dict[str, set[EventHandler]] = defaultdict(set)
        # event_type -> total publish count (for stats)
        self._event_counts: dict[str, int] = defaultdict(int)
        self._max_concurrent = max_concurrent_handlers
        self._semaphore = asyncio.Semaphore(max_concurrent_handlers)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """
        Register an async handler for an event type.

        Handler signature: async def handler(payload: dict) -> Any
        """
        if not asyncio.iscoroutinefunction(handler):
            raise TypeError(
                f"Handler for '{event_type}' must be an async function, "
                f"got {type(handler).__name__}"
            )
        self._subscribers[event_type].add(handler)
        log.debug("Subscribed to '%s': %s (total: %d)",
                  event_type, handler.__name__,
                  len(self._subscribers[event_type]))

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a handler from an event type."""
        subs = self._subscribers.get(event_type)
        if subs:
            subs.discard(handler)
            log.debug("Unsubscribed from '%s': %s", event_type, handler.__name__)
            if not subs:
                del self._subscribers[event_type]

    def subscriber_count(self, event_type: Optional[str] = None) -> int:
        """Return subscriber count. If event_type is None, return total."""
        if event_type:
            return len(self._subscribers.get(event_type, set()))
        return sum(len(s) for s in self._subscribers.values())

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, event_type: str, payload: dict) -> int:
        """
        Fire an event. All registered handlers run concurrently via asyncio.gather().

        Returns the number of handlers that were invoked.

        Args:
            event_type: e.g. "email.family", "task.created"
            payload: dict with event data (must be JSON-serializable)
        """
        handlers = list(self._subscribers.get(event_type, set()))
        if not handlers:
            log.debug("Event '%s' published — no subscribers", event_type)
            return 0

        log.debug("Event '%s' firing %d handler(s)", event_type, len(handlers))

        async def _dispatch_one(handler: EventHandler) -> None:
            async with self._semaphore:
                try:
                    await handler(payload)
                except Exception as exc:
                    log.warning(
                        "Event '%s' handler '%s' failed: %s",
                        event_type, handler.__name__, exc,
                    )

        # Run all handlers concurrently
        await asyncio.gather(*[_dispatch_one(h) for h in handlers])

        async with self._lock:
            self._event_counts[event_type] += 1

        return len(handlers)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return event bus statistics."""
        return {
            "total_events_published": sum(self._event_counts.values()),
            "event_counts": dict(self._event_counts),
            "subscriber_counts": {
                event_type: len(handlers)
                for event_type, handlers in self._subscribers.items()
            },
            "total_subscribers": self.subscriber_count(),
        }

    def reset_counts(self) -> None:
        """Reset event counters (useful for testing)."""
        self._event_counts.clear()
