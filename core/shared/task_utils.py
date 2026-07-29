"""Shared task utilities for Mike."""

import asyncio
import logging

log = logging.getLogger(__name__)


def _handle_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from background tasks so they are not silently lost."""
    try:
        task.result()
    except Exception:
        log.exception("Background task failed")
