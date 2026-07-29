# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Briefing — Morning briefing generator
===========================================

Collects emails, calendar events, reminders and system health
to produce a formatted daily briefing for Marco.

Used by mike_heartbeat.py and can be triggered via API.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from mike_config import nvidia_smi_path

log = logging.getLogger("mike.briefing")


class MikeBriefing:
    """Generates morning briefings from Gmail, Calendar, memory and system data."""

    def __init__(
        self,
        log_fn: Optional[Callable[[str], None]] = None,
        *,
        mike_api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        timezone_name: Optional[str] = None,
    ) -> None:
        self.log = log_fn or log.info
        self.mike_api_base = (
            mike_api_base
            or os.getenv("MIKE_API_BASE_URL", "http://127.0.0.1:8080")
        ).rstrip("/")
        self.api_key = api_key or os.getenv("MIKE_API_KEY", "").strip()
        self.timezone_name = (
            timezone_name
            or os.getenv("MIKE_CALENDAR_TIMEZONE", "America/Toronto")
        ).strip()

    # ------------------------------------------------------------------
    # Gmail helpers (direct Google API, avoids MCP for speed)
    # ------------------------------------------------------------------

    def _get_gmail_service(self):
        try:
            from mike_google_auth import gmail_service
            service, _ = gmail_service()
            return service
        except Exception as exc:
            self.log(f"Gmail service unavailable: {exc}")
            return None

    def _get_calendar_service(self):
        try:
            from mike_google_auth import calendar_service
            service, _ = calendar_service()
            return service
        except Exception as exc:
            self.log(f"Calendar service unavailable: {exc}")
            return None

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def collect_emails(self, limit: int = 10) -> list[dict]:
        service = self._get_gmail_service()
        if not service:
            return []

        try:
            result = service.users().messages().list(
                userId="me",
                labelIds=["INBOX", "UNREAD"],
                maxResults=min(limit, 50),
            ).execute()
            messages = result.get("messages", [])
        except Exception as exc:
            self.log(f"Gmail list failed: {exc}")
            return []

        emails = []
        vip_keywords = {"urgente", "urgent", "asap", "imediato", "deadline"}
        vip_senders_env = os.getenv("MIKE_VIP_SENDERS", "").strip()
        vip_senders = {s.strip().lower() for s in vip_senders_env.split(",") if s.strip()}

        for msg in messages:
            try:
                full = service.users().messages().get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                ).execute()
                payload = full.get("payload", {})
                headers = {
                    h["name"].lower(): h["value"]
                    for h in payload.get("headers", [])
                }
                sender = headers.get("from", "")
                subject = headers.get("subject", "")
                snippet = full.get("snippet", "")

                is_urgent = (
                    any(kw in subject.lower() for kw in vip_keywords)
                    or any(kw in snippet.lower() for kw in vip_keywords)
                    or any(vip in sender.lower() for vip in vip_senders)
                )

                emails.append({
                    "id": msg["id"],
                    "from": sender,
                    "subject": subject,
                    "date": headers.get("date", ""),
                    "snippet": snippet[:200],
                    "urgent": is_urgent,
                })
            except Exception:
                continue
        return emails

    def collect_events(self, hours_ahead: int = 24) -> list[dict]:
        service = self._get_calendar_service()
        if not service:
            return []

        now = datetime.now(timezone.utc)
        time_max = now + timedelta(hours=hours_ahead)

        try:
            result = service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=20,
            ).execute()
        except Exception as exc:
            self.log(f"Calendar list failed: {exc}")
            return []

        events = []
        for item in result.get("items", []):
            start_block = item.get("start", {})
            end_block = item.get("end", {})
            events.append({
                "id": item.get("id"),
                "summary": item.get("summary", ""),
                "start": start_block.get("dateTime") or start_block.get("date", ""),
                "end": end_block.get("dateTime") or end_block.get("date", ""),
                "location": item.get("location", ""),
            })
        return events

    def detect_conflicts(self, events: list[dict]) -> list[str]:
        conflicts = []
        for i, ev_a in enumerate(events):
            for ev_b in events[i + 1:]:
                start_a = ev_a.get("start", "")
                end_a = ev_a.get("end", "")
                start_b = ev_b.get("start", "")

                if not all([start_a, end_a, start_b]):
                    continue
                # Simple overlap check: B starts before A ends
                if start_b < end_a and start_a < ev_b.get("end", start_b):
                    conflicts.append(
                        f"{ev_a.get('summary', '?')} e {ev_b.get('summary', '?')} "
                        f"no mesmo horário"
                    )
        return conflicts

    def collect_system_health(self) -> dict:
        health: dict[str, Any] = {}

        # Disk
        try:
            usage = shutil.disk_usage(Path.home())
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            health["disk_free"] = f"{free_gb:.1f}GB/{total_gb:.0f}GB"
            health["disk_free_pct"] = round(100 * usage.free / usage.total, 1)
        except Exception:
            health["disk_free"] = "?"

        # GPU via nvidia-smi
        try:
            nvidia_smi = nvidia_smi_path()
            if not nvidia_smi:
                raise FileNotFoundError("nvidia-smi not found")
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=utilization.gpu,memory.free,temperature.gpu,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            rows = []
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 4:
                    continue
                try:
                    rows.append((int(parts[3]), parts))
                except ValueError:
                    rows.append((0, parts))
            if rows:
                _, parts = max(rows, key=lambda item: item[0])
                health["gpu_usage"] = f"{parts[0]}%"
                health["gpu_memory_free"] = f"{parts[1]}MB"
                health["gpu_temp"] = f"{parts[2]}°C"
        except Exception:
            health["gpu_usage"] = "?"

        # Uptime
        try:
            health["uptime"] = f"{time.monotonic() / 3600:.1f}h"
        except Exception:
            health["uptime"] = "?"

        return health

    # ------------------------------------------------------------------
    # Generate briefing data
    # ------------------------------------------------------------------

    def generate_morning(self) -> dict:
        now = datetime.now()
        date_str = now.strftime("%d/%m/%Y")

        emails = self.collect_emails(limit=10)
        events = self.collect_events(hours_ahead=24)
        conflicts = self.detect_conflicts(events)
        system = self.collect_system_health()

        return {
            "date": date_str,
            "generated_at": now.isoformat(),
            "emails": emails,
            "events": events,
            "conflicts": conflicts,
            "reminders": [],
            "system": system,
        }

    def generate_quick(self) -> dict:
        """Lightweight briefing without email (for heartbeat use)."""
        now = datetime.now()
        events = self.collect_events(hours_ahead=3)
        conflicts = self.detect_conflicts(events)
        system = self.collect_system_health()

        return {
            "date": now.strftime("%d/%m/%Y"),
            "generated_at": now.isoformat(),
            "emails": [],
            "events": events,
            "conflicts": conflicts,
            "reminders": [],
            "system": system,
        }
