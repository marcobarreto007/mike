# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

# Extracted from mike_server.py — Phase 3 refactor

"""
Proactive notifications — SSE stream for open browser tabs (Feature 4 / PWA).

Each connected browser gets a queue; alerts are broadcast to all connected clients.
"""

import asyncio
import json
import logging
import threading

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Notification queues (shared state)
# ---------------------------------------------------------------------------

_notification_queues: list[asyncio.Queue] = []
_notification_queues_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

def _broadcast_notification(title: str, body: str, tag: str = "mike-alert") -> None:
    """Push an alert to all connected browser SSE streams."""
    payload = json.dumps({"title": title, "body": body, "tag": tag}, ensure_ascii=False)
    with _notification_queues_lock:
        for q in list(_notification_queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass
