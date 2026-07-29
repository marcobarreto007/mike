# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
EmailTracker — Rastreamento de respostas de emails enviados.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.shared.time_utils import utc_now, utc_now_iso

_log = logging.getLogger(__name__)

EMAIL_DEFAULT_DEADLINE_HOURS = int(os.getenv("MIKE_EMAIL_DEADLINE_HOURS", "48"))


@dataclass
class TrackedEmail:
    """Email rastreado aguardando resposta."""
    id: str
    gmail_message_id: str = ""
    sent_to: str = ""
    subject: str = ""
    sent_at: str = field(default_factory=utc_now_iso)
    expected_reply_by: str = ""  # ISO timestamp deadline
    status: str = "waiting"  # waiting | replied | overdue | followup_sent | dismissed
    check_count: int = 0
    last_checked_at: Optional[str] = None
    reply_message_id: Optional[str] = None
    followup_count: int = 0
    auto_followup: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "gmail_message_id": self.gmail_message_id,
            "sent_to": self.sent_to, "subject": self.subject,
            "sent_at": self.sent_at, "expected_reply_by": self.expected_reply_by,
            "status": self.status, "check_count": self.check_count,
            "last_checked_at": self.last_checked_at,
            "reply_message_id": self.reply_message_id,
            "followup_count": self.followup_count,
            "auto_followup": self.auto_followup, "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: dict) -> "TrackedEmail":
        return TrackedEmail(
            id=str(data.get("id", "")),
            gmail_message_id=str(data.get("gmail_message_id", "")),
            sent_to=str(data.get("sent_to", "")),
            subject=str(data.get("subject", "")),
            sent_at=str(data.get("sent_at", utc_now_iso())),
            expected_reply_by=str(data.get("expected_reply_by", "")),
            status=str(data.get("status", "waiting")),
            check_count=int(data.get("check_count", 0)),
            last_checked_at=data.get("last_checked_at"),
            reply_message_id=data.get("reply_message_id"),
            followup_count=int(data.get("followup_count", 0)),
            auto_followup=bool(data.get("auto_followup", False)),
            notes=str(data.get("notes", "")),
        )

    def is_overdue(self) -> bool:
        if not self.expected_reply_by:
            return False
        try:
            deadline = datetime.fromisoformat(self.expected_reply_by)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return utc_now() > deadline
        except Exception:
            return False


class EmailTracker:
    """Rastreamento de respostas de emails enviados."""

    def __init__(
        self,
        *,
        store_dir: Path,
        lock: asyncio.Lock,
        log_fn,
        email_search_fn=None,
        notify_fn=None,
    ):
        self._store_dir = Path(store_dir)
        self._lock = lock
        self._log_fn = log_fn
        self._email_search_fn = email_search_fn
        self._notify_fn = notify_fn
        self._tracked_emails: dict[str, TrackedEmail] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _email_tracking_path(self) -> Path:
        return self._store_dir / "email_tracking.json"

    def _save_email_tracking(self) -> None:
        payload = {
            "updated_at": utc_now_iso(),
            "tracked": [e.to_dict() for e in self._tracked_emails.values()],
        }
        self._atomic_write(self._email_tracking_path(), payload)

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _notify(self, title: str, body: str, tag: str = "autonomy") -> None:
        if self._notify_fn:
            try:
                self._notify_fn(title, body, tag)
            except Exception as exc:
                _log.debug("EmailTracker notify failed: %s", exc)

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            # Load email tracking
            if self._email_tracking_path().exists():
                try:
                    data = json.loads(self._email_tracking_path().read_text(encoding="utf-8"))
                    for item in data.get("tracked", []):
                        e = TrackedEmail.from_dict(item)
                        if e.id:
                            self._tracked_emails[e.id] = e
                except Exception as exc:
                    _log.warning("Email tracking load failed: %s", exc)
            self._loaded = True

    # ------------------------------------------------------------------
    # Email Tracking API
    # ------------------------------------------------------------------

    async def track_email(
        self,
        gmail_message_id: str = "",
        sent_to: str = "",
        subject: str = "",
        deadline_hours: int = EMAIL_DEFAULT_DEADLINE_HOURS,
        auto_followup: bool = False,
    ) -> dict:
        """Track a sent email for response monitoring."""
        await self._ensure_loaded()

        now = utc_now()
        tracked = TrackedEmail(
            id=f"trk_{uuid.uuid4().hex[:8]}",
            gmail_message_id=gmail_message_id,
            sent_to=sent_to,
            subject=subject,
            sent_at=now.isoformat(),
            expected_reply_by=(now + timedelta(hours=deadline_hours)).isoformat(),
            auto_followup=auto_followup,
        )

        async with self._lock:
            self._tracked_emails[tracked.id] = tracked
            self._save_email_tracking()

        self._log_fn("email_tracked", f"Para: {sent_to} | Assunto: {subject}")
        _log.info("Email tracked: %s → %s (%dh deadline)", tracked.id, sent_to, deadline_hours)
        return tracked.to_dict()

    async def list_tracked_emails(self, status: Optional[str] = None) -> list[dict]:
        await self._ensure_loaded()
        items = list(self._tracked_emails.values())
        if status:
            items = [e for e in items if e.status == status]
        items.sort(key=lambda e: e.sent_at, reverse=True)
        return [e.to_dict() for e in items]

    async def dismiss_tracked_email(self, tracking_id: str) -> Optional[dict]:
        await self._ensure_loaded()
        async with self._lock:
            tracked = self._tracked_emails.get(tracking_id)
            if not tracked:
                return None
            tracked.status = "dismissed"
            self._save_email_tracking()
        return tracked.to_dict()

    async def check_email_responses(self) -> list[dict]:
        """Check Gmail for replies to tracked emails."""
        await self._ensure_loaded()

        if not self._email_search_fn:
            return []

        alerts: list[dict] = []
        now = utc_now()

        for tracked in list(self._tracked_emails.values()):
            if tracked.status not in ("waiting",):
                continue

            tracked.check_count += 1
            tracked.last_checked_at = now.isoformat()

            # Search for replies
            try:
                reply_query = f"from:{tracked.sent_to} subject:Re:{tracked.subject}"
                replies = await asyncio.to_thread(
                    self._email_search_fn,
                    sender=tracked.sent_to,
                    subject=f"Re:{tracked.subject}",
                    days_back=7,
                    limit=5,
                )
                if isinstance(replies, list) and replies and not replies[0].get("error"):
                    # Found a reply!
                    tracked.status = "replied"
                    tracked.reply_message_id = replies[0].get("id", "")
                    alerts.append({
                        "type": "email_replied",
                        "message": f"✅ {tracked.sent_to} respondeu: {tracked.subject}",
                        "tracking_id": tracked.id,
                    })
                    self._log_fn("email_replied", f"{tracked.sent_to} respondeu: {tracked.subject}")
                elif tracked.is_overdue():
                    tracked.status = "overdue"
                    alerts.append({
                        "type": "email_overdue",
                        "message": f"⏰ Sem resposta de {tracked.sent_to}: {tracked.subject}",
                        "tracking_id": tracked.id,
                    })
                    self._log_fn("email_overdue", f"Sem resposta: {tracked.sent_to} - {tracked.subject}")
            except Exception as exc:
                _log.warning("Email response check failed for %s: %s", tracked.id, exc)

        async with self._lock:
            self._save_email_tracking()

        return alerts
