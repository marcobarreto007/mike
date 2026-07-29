"""Shared time utilities for Mike."""
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return current UTC datetime as ISO 8601 string."""
    return utc_now().isoformat()
