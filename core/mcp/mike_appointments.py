# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Appointments Engine
========================

State machine, SQLite persistence, and orchestration for the
appointment confirmation pipeline.

States:
    SCHEDULED → CALL_QUEUED → CONFIRMED / DECLINED / NO_ANSWER
    CONFIRMED → REMINDER_SENT → ATTENDED / NO_SHOW
    NO_SHOW → EMAIL_SENT
    All transitions are logged to Pipedrive (if enabled).

Configuration (.env):
    MIKE_APPT_ENABLED=true
    MIKE_APPT_CALL_HOURS_BEFORE=24
    MIKE_APPT_REMINDER_MINUTES_BEFORE=60
    MIKE_APPT_MAX_CALL_RETRIES=2
    MIKE_APPT_NOSHOW_GRACE_MINUTES=15
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from mike_config import env_bool, env_int

log = logging.getLogger("mike.appointments")


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------

class ApptStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    CALL_QUEUED = "CALL_QUEUED"
    CONFIRMED = "CONFIRMED"
    DECLINED = "DECLINED"
    NO_ANSWER = "NO_ANSWER"
    REMINDER_SENT = "REMINDER_SENT"
    ATTENDED = "ATTENDED"
    NO_SHOW = "NO_SHOW"
    EMAIL_SENT = "EMAIL_SENT"
    CANCELLED = "CANCELLED"


# Valid state transitions
_VALID_TRANSITIONS: dict[ApptStatus, set[ApptStatus]] = {
    ApptStatus.SCHEDULED:      {ApptStatus.CALL_QUEUED, ApptStatus.CONFIRMED, ApptStatus.CANCELLED},
    ApptStatus.CALL_QUEUED:    {ApptStatus.CONFIRMED, ApptStatus.DECLINED, ApptStatus.NO_ANSWER, ApptStatus.CANCELLED},
    ApptStatus.CONFIRMED:      {ApptStatus.REMINDER_SENT, ApptStatus.CANCELLED, ApptStatus.DECLINED},
    ApptStatus.DECLINED:       {ApptStatus.SCHEDULED, ApptStatus.CANCELLED},
    ApptStatus.NO_ANSWER:      {ApptStatus.CALL_QUEUED, ApptStatus.CANCELLED},  # retry
    ApptStatus.REMINDER_SENT:  {ApptStatus.ATTENDED, ApptStatus.NO_SHOW, ApptStatus.CANCELLED},
    ApptStatus.ATTENDED:       set(),  # terminal
    ApptStatus.NO_SHOW:        {ApptStatus.EMAIL_SENT, ApptStatus.SCHEDULED},
    ApptStatus.EMAIL_SENT:     {ApptStatus.SCHEDULED},  # can reschedule
    ApptStatus.CANCELLED:      {ApptStatus.SCHEDULED},  # can reactivate
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Appointment:
    id: int = 0
    external_id: str = ""          # Pipedrive deal ID
    contact_name: str = ""
    contact_phone: str = ""        # E.164
    contact_email: str = ""
    appointment_at: str = ""       # ISO 8601
    duration_min: int = 60
    service_type: str = ""
    status: str = "SCHEDULED"
    call_sid: str = ""
    call_attempts: int = 0
    call_result: str = ""
    sms_sid: str = ""
    sms_sent_at: str = ""
    email_sent_at: str = ""
    pipedrive_deal_id: int = 0
    pipedrive_synced: int = 0
    notes: str = "[]"              # JSON array
    created_at: str = ""
    updated_at: str = ""

    def notes_list(self) -> list[dict]:
        try:
            return json.loads(self.notes or "[]")
        except (json.JSONDecodeError, TypeError):
            return []

    def add_note(self, event_type: str, text: str) -> None:
        notes = self.notes_list()
        notes.append({
            "type": event_type,
            "text": text,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        self.notes = json.dumps(notes, ensure_ascii=False)

    def appointment_datetime(self) -> Optional[datetime]:
        if not self.appointment_at:
            return None
        raw = self.appointment_at
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

APPT_ENABLED = env_bool("MIKE_APPT_ENABLED", True)
CALL_HOURS_BEFORE = env_int("MIKE_APPT_CALL_HOURS_BEFORE", 24)
REMINDER_MINUTES_BEFORE = env_int("MIKE_APPT_REMINDER_MINUTES_BEFORE", 60)
MAX_CALL_RETRIES = env_int("MIKE_APPT_MAX_CALL_RETRIES", 2)
NOSHOW_GRACE_MINUTES = env_int("MIKE_APPT_NOSHOW_GRACE_MINUTES", 15)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id     TEXT DEFAULT '',
    contact_name    TEXT NOT NULL,
    contact_phone   TEXT NOT NULL,
    contact_email   TEXT DEFAULT '',
    appointment_at  TEXT NOT NULL,
    duration_min    INTEGER DEFAULT 60,
    service_type    TEXT DEFAULT '',
    status          TEXT DEFAULT 'SCHEDULED',
    call_sid        TEXT DEFAULT '',
    call_attempts   INTEGER DEFAULT 0,
    call_result     TEXT DEFAULT '',
    sms_sid         TEXT DEFAULT '',
    sms_sent_at     TEXT DEFAULT '',
    email_sent_at   TEXT DEFAULT '',
    pipedrive_deal_id INTEGER DEFAULT 0,
    pipedrive_synced INTEGER DEFAULT 0,
    notes           TEXT DEFAULT '[]',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_appt_status ON appointments(status);
CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(appointment_at);
CREATE INDEX IF NOT EXISTS idx_appt_phone ON appointments(contact_phone);
"""


class AppointmentDB:
    """SQLite persistence for appointments."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        if db_path is None:
            from mike_config import MEMORY_DB
            db_path = MEMORY_DB
        self.db_path = str(db_path)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _connection(self):
        """Yield a transactional connection and always close it on Windows."""
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        """Explicitly close any cached connections (useful for tests on Windows)."""
        pass  # connections are opened per-call, no caching

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

    # -- CRUD --

    def create(self, appt: Appointment) -> Appointment:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """INSERT INTO appointments
                   (external_id, contact_name, contact_phone, contact_email,
                    appointment_at, duration_min, service_type, status,
                    pipedrive_deal_id, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    appt.external_id, appt.contact_name, appt.contact_phone,
                    appt.contact_email, appt.appointment_at, appt.duration_min,
                    appt.service_type, appt.status, appt.pipedrive_deal_id,
                    appt.notes, now, now,
                ),
            )
            appt.id = cursor.lastrowid  # type: ignore[assignment]
            appt.created_at = now
            appt.updated_at = now
        return appt

    def get(self, appt_id: int) -> Optional[Appointment]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM appointments WHERE id = ?", (appt_id,)
            ).fetchone()
        return self._row_to_appt(row) if row else None

    def get_by_phone_and_date(self, phone: str, date_str: str) -> Optional[Appointment]:
        with self._connection() as conn:
            row = conn.execute(
                """SELECT * FROM appointments
                   WHERE contact_phone = ? AND appointment_at LIKE ?
                   ORDER BY appointment_at ASC LIMIT 1""",
                (phone, f"{date_str}%"),
            ).fetchone()
        return self._row_to_appt(row) if row else None

    def list_by_status(self, *statuses: str) -> list[Appointment]:
        placeholders = ",".join("?" for _ in statuses)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM appointments WHERE status IN ({placeholders}) ORDER BY appointment_at ASC",
                statuses,
            ).fetchall()
        return [self._row_to_appt(r) for r in rows]

    def list_between(
        self,
        start: datetime,
        end: datetime,
        status: Optional[str] = None,
    ) -> list[Appointment]:
        start_str = start.isoformat()
        end_str = end.isoformat()
        if status:
            with self._connection() as conn:
                rows = conn.execute(
                    """SELECT * FROM appointments
                       WHERE appointment_at >= ? AND appointment_at <= ? AND status = ?
                       ORDER BY appointment_at ASC""",
                    (start_str, end_str, status),
                ).fetchall()
        else:
            with self._connection() as conn:
                rows = conn.execute(
                    """SELECT * FROM appointments
                       WHERE appointment_at >= ? AND appointment_at <= ?
                       ORDER BY appointment_at ASC""",
                    (start_str, end_str),
                ).fetchall()
        return [self._row_to_appt(r) for r in rows]

    def list_today(self, tz_offset_hours: int = -3) -> list[Appointment]:
        now = datetime.now(timezone(timedelta(hours=tz_offset_hours)))
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return self.list_between(day_start, day_end)

    def list_upcoming(self, days: int = 7, tz_offset_hours: int = -3) -> list[Appointment]:
        now = datetime.now(timezone(timedelta(hours=tz_offset_hours)))
        end = now + timedelta(days=days)
        return self.list_between(now, end)

    def update(self, appt: Appointment) -> None:
        appt.updated_at = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                """UPDATE appointments SET
                   external_id=?, contact_name=?, contact_phone=?,
                   contact_email=?, appointment_at=?, duration_min=?,
                   service_type=?, status=?, call_sid=?, call_attempts=?,
                   call_result=?, sms_sid=?, sms_sent_at=?, email_sent_at=?,
                   pipedrive_deal_id=?, pipedrive_synced=?, notes=?, updated_at=?
                   WHERE id=?""",
                (
                    appt.external_id, appt.contact_name, appt.contact_phone,
                    appt.contact_email, appt.appointment_at, appt.duration_min,
                    appt.service_type, appt.status, appt.call_sid,
                    appt.call_attempts, appt.call_result, appt.sms_sid,
                    appt.sms_sent_at, appt.email_sent_at, appt.pipedrive_deal_id,
                    appt.pipedrive_synced, appt.notes, appt.updated_at, appt.id,
                ),
            )

    def delete(self, appt_id: int) -> bool:
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM appointments WHERE id = ?", (appt_id,))
            return cursor.rowcount > 0

    def count_by_status(self) -> dict[str, int]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM appointments GROUP BY status"
            ).fetchall()
        return {row["status"]: row["cnt"] for row in rows}

    @staticmethod
    def _row_to_appt(row: sqlite3.Row) -> Appointment:
        return Appointment(**{k: row[k] for k in row.keys()})


# ---------------------------------------------------------------------------
# State Machine / Engine
# ---------------------------------------------------------------------------

class AppointmentEngine:
    """Orchestrates state transitions and side effects."""

    def __init__(
        self,
        db: Optional[AppointmentDB] = None,
        pipedrive: Optional[object] = None,
        telegram: Optional[object] = None,
    ) -> None:
        self.db = db or AppointmentDB()
        self.pipedrive = pipedrive       # MikePipedrive instance or None
        self.telegram = telegram         # MikeTelegram instance or None

    # -- Create --

    def schedule(
        self,
        contact_name: str,
        contact_phone: str,
        appointment_at: str,
        service_type: str = "",
        contact_email: str = "",
        duration_min: int = 60,
    ) -> Appointment:
        appt = Appointment(
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            appointment_at=appointment_at,
            duration_min=duration_min,
            service_type=service_type,
            status=ApptStatus.SCHEDULED.value,
        )
        appt.add_note("created", f"Agendamento criado: {service_type}")
        self.db.create(appt)

        # Sync to Pipedrive
        self._sync_pipedrive_create(appt)

        # Notify via Telegram
        self._notify_telegram(
            f"📅 *Novo agendamento*\n"
            f"👤 {contact_name}\n"
            f"📞 {contact_phone}\n"
            f"🕐 {appointment_at}\n"
            f"📋 {service_type}"
        )
        log.info(f"Appointment #{appt.id} scheduled: {contact_name} @ {appointment_at}")
        return appt

    # -- Transitions --

    def transition(self, appt_id: int, new_status: str, note: str = "") -> Appointment:
        appt = self.db.get(appt_id)
        if not appt:
            raise ValueError(f"Appointment #{appt_id} not found")

        current = ApptStatus(appt.status)
        target = ApptStatus(new_status)

        if target not in _VALID_TRANSITIONS.get(current, set()):
            raise ValueError(
                f"Invalid transition: {current.value} → {target.value} "
                f"(allowed: {[s.value for s in _VALID_TRANSITIONS.get(current, set())]})"
            )

        old_status = appt.status
        appt.status = target.value
        appt.add_note("transition", f"{old_status} → {target.value}" + (f": {note}" if note else ""))
        self.db.update(appt)

        # Pipedrive side effects
        self._sync_pipedrive_transition(appt, old_status, target.value)

        log.info(f"Appointment #{appt_id}: {old_status} → {target.value}")
        return appt

    def cancel(self, appt_id: int, reason: str = "") -> Appointment:
        return self.transition(appt_id, ApptStatus.CANCELLED.value, reason or "Cancelado")

    def confirm(self, appt_id: int, note: str = "") -> Appointment:
        return self.transition(appt_id, ApptStatus.CONFIRMED.value, note or "Confirmado pelo contato")

    def mark_attended(self, appt_id: int) -> Appointment:
        return self.transition(appt_id, ApptStatus.ATTENDED.value, "Paciente compareceu")

    def mark_no_show(self, appt_id: int) -> Appointment:
        return self.transition(appt_id, ApptStatus.NO_SHOW.value, "Paciente não compareceu")

    def reschedule(self, appt_id: int, new_datetime: str) -> Appointment:
        appt = self.db.get(appt_id)
        if not appt:
            raise ValueError(f"Appointment #{appt_id} not found")
        old_dt = appt.appointment_at
        appt.appointment_at = new_datetime
        appt.status = ApptStatus.SCHEDULED.value
        appt.call_attempts = 0
        appt.call_sid = ""
        appt.call_result = ""
        appt.sms_sid = ""
        appt.sms_sent_at = ""
        appt.add_note("rescheduled", f"Reagendado de {old_dt} para {new_datetime}")
        self.db.update(appt)
        self._notify_telegram(f"🔄 *Reagendado*\n👤 {appt.contact_name}\n🕐 {old_dt} → {new_datetime}")
        log.info(f"Appointment #{appt_id} rescheduled to {new_datetime}")
        return appt

    # -- Queries --

    def list_today(self) -> list[Appointment]:
        return self.db.list_today()

    def list_upcoming(self, days: int = 7) -> list[Appointment]:
        return self.db.list_upcoming(days=days)

    def status_summary(self) -> dict:
        counts = self.db.count_by_status()
        today = self.db.list_today()
        return {
            "total_today": len(today),
            "by_status": counts,
            "today": [
                {
                    "id": a.id,
                    "name": a.contact_name,
                    "time": a.appointment_at,
                    "status": a.status,
                    "service": a.service_type,
                }
                for a in today
            ],
        }

    # -- Heartbeat checks --

    def get_pending_calls(self, now: Optional[datetime] = None) -> list[Appointment]:
        """Get appointments that need a confirmation call (T-24h window)."""
        now = now or datetime.now(timezone.utc)
        window_start = now + timedelta(hours=CALL_HOURS_BEFORE - 1)
        window_end = now + timedelta(hours=CALL_HOURS_BEFORE + 1)
        scheduled = self.db.list_between(window_start, window_end, ApptStatus.SCHEDULED.value)
        return [a for a in scheduled if a.call_attempts < MAX_CALL_RETRIES]

    def get_retry_calls(self) -> list[Appointment]:
        """Get appointments that had no answer and need retry."""
        no_answer = self.db.list_by_status(ApptStatus.NO_ANSWER.value)
        return [a for a in no_answer if a.call_attempts < MAX_CALL_RETRIES]

    def get_pending_reminders(self, now: Optional[datetime] = None) -> list[Appointment]:
        """Get confirmed appointments needing a reminder (T-1h)."""
        now = now or datetime.now(timezone.utc)
        window_start = now + timedelta(minutes=REMINDER_MINUTES_BEFORE - 10)
        window_end = now + timedelta(minutes=REMINDER_MINUTES_BEFORE + 10)
        confirmed = self.db.list_between(window_start, window_end, ApptStatus.CONFIRMED.value)
        return confirmed

    def get_potential_no_shows(self, now: Optional[datetime] = None) -> list[Appointment]:
        """Get appointments past their time + grace that weren't marked attended."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=NOSHOW_GRACE_MINUTES)
        # Look at all REMINDER_SENT whose appointment_at < cutoff
        reminder_sent = self.db.list_by_status(ApptStatus.REMINDER_SENT.value)
        result = []
        for appt in reminder_sent:
            dt = appt.appointment_datetime()
            if dt and dt < cutoff:
                result.append(appt)
        return result

    # -- Pipedrive sync --

    def _sync_pipedrive_create(self, appt: Appointment) -> None:
        if not self.pipedrive or not getattr(self.pipedrive, "enabled", False):
            return
        try:
            person = self.pipedrive.find_or_create_person(
                appt.contact_name, appt.contact_phone, appt.contact_email
            )
            deal = self.pipedrive.create_deal(
                title=f"{appt.service_type} - {appt.contact_name}",
                person_id=person["id"],
            )
            appt.pipedrive_deal_id = deal["id"]
            appt.pipedrive_synced = 1
            self.db.update(appt)
            self.pipedrive.add_activity(
                deal_id=deal["id"],
                subject=f"{appt.service_type} @ {appt.appointment_at}",
                activity_type="meeting",
                due_date=appt.appointment_at[:10] if len(appt.appointment_at) >= 10 else None,
            )
        except Exception as exc:
            log.warning(f"Pipedrive sync failed for #{appt.id}: {exc}")

    def _sync_pipedrive_transition(self, appt: Appointment, old: str, new: str) -> None:
        if not self.pipedrive or not appt.pipedrive_deal_id:
            return
        try:
            self.pipedrive.log_appointment_event(
                appt.pipedrive_deal_id, "transition", f"{old} → {new}"
            )
            if new == ApptStatus.CONFIRMED.value:
                self.pipedrive.update_deal_stage(
                    appt.pipedrive_deal_id, self.pipedrive.stage_confirmed
                )
            elif new == ApptStatus.ATTENDED.value:
                self.pipedrive.update_deal_stage(
                    appt.pipedrive_deal_id, self.pipedrive.stage_done
                )
                self.pipedrive.update_deal_status(appt.pipedrive_deal_id, "won")
            elif new == ApptStatus.DECLINED.value:
                self.pipedrive.update_deal_status(appt.pipedrive_deal_id, "lost")
        except Exception as exc:
            log.warning(f"Pipedrive transition sync failed for #{appt.id}: {exc}")

    # -- Telegram --

    def _notify_telegram(self, text: str) -> None:
        if not self.telegram:
            return
        try:
            if hasattr(self.telegram, "send_marco"):
                self.telegram.send_marco(text)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning(f"Telegram notification failed: {exc}")
