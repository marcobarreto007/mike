# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Telegram Bot — Proactive output channel
=============================================

Sends messages, briefings and alerts to Marco and family via Telegram.

Configuration (.env):
    MIKE_TELEGRAM_BOT_TOKEN=<token from @BotFather>
    MIKE_TELEGRAM_CHAT_MARCO=<chat_id>
    MIKE_TELEGRAM_CHAT_FAMILIA=<group_id>
    MIKE_TELEGRAM_ENABLED=true
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional
from mike_config import env_bool

log = logging.getLogger("mike.telegram")





class MikeTelegram:
    """Thin async wrapper around python-telegram-bot for outbound messages."""

    def __init__(
        self,
        log_fn: Optional[Callable[[str], None]] = None,
        *,
        enabled: Optional[bool] = None,
        bot_token: Optional[str] = None,
        chat_marco: Optional[str] = None,
        chat_familia: Optional[str] = None,
    ) -> None:
        self.log = log_fn or log.info
        self.enabled = env_bool("MIKE_TELEGRAM_ENABLED", False) if enabled is None else bool(enabled)
        self.bot_token = (bot_token or os.getenv("MIKE_TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_marco = (chat_marco or os.getenv("MIKE_TELEGRAM_CHAT_MARCO", "")).strip()
        self.chat_familia = (chat_familia or os.getenv("MIKE_TELEGRAM_CHAT_FAMILIA", "")).strip()
        self._bot = None
        self._load_error: Optional[str] = None

        if self.enabled and not self.bot_token:
            self.enabled = False
            self._load_error = "MIKE_TELEGRAM_BOT_TOKEN not set"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_bot(self) -> bool:
        if self._bot is not None:
            return True
        if not self.enabled:
            return False
        if self._load_error is not None:
            return False
        try:
            from telegram import Bot
            self._bot = Bot(token=self.bot_token)
            return True
        except Exception as exc:
            self._load_error = str(exc)
            self.log(f"Telegram bot init failed: {exc}")
            return False

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_running_loop()
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            return loop

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    async def send_async(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "Markdown",
    ) -> bool:
        if not self._ensure_bot() or not chat_id:
            return False
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
            self.log(f"Telegram message sent to {chat_id}")
            return True
        except Exception as exc:
            self.log(f"Telegram send failed ({chat_id}): {exc}")
            # Retry without parse_mode in case of formatting errors
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
                return True
            except Exception:
                return False

    def send(self, chat_id: str, text: str, parse_mode: str = "Markdown") -> bool:
        """Synchronous wrapper — safe to call from non-async code."""
        if not self._ensure_bot() or not chat_id:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self.send_async(chat_id, text, parse_mode), loop
            )
            return future.result(timeout=30)
        else:
            return asyncio.run(self.send_async(chat_id, text, parse_mode))

    # ------------------------------------------------------------------
    # Convenience: send to Marco
    # ------------------------------------------------------------------

    async def send_marco_async(self, text: str, parse_mode: str = "Markdown") -> bool:
        if not self.chat_marco:
            self.log("chat_marco not configured")
            return False
        return await self.send_async(self.chat_marco, text, parse_mode)

    def send_marco(self, text: str, parse_mode: str = "Markdown") -> bool:
        if not self.chat_marco:
            self.log("chat_marco not configured")
            return False
        return self.send(self.chat_marco, text, parse_mode)

    # ------------------------------------------------------------------
    # Convenience: send to family group
    # ------------------------------------------------------------------

    async def send_familia_async(self, text: str, parse_mode: str = "Markdown") -> bool:
        if not self.chat_familia:
            self.log("chat_familia not configured")
            return False
        return await self.send_async(self.chat_familia, text, parse_mode)

    def send_familia(self, text: str, parse_mode: str = "Markdown") -> bool:
        if not self.chat_familia:
            self.log("chat_familia not configured")
            return False
        return self.send(self.chat_familia, text, parse_mode)

    # ------------------------------------------------------------------
    # Structured messages
    # ------------------------------------------------------------------

    async def send_briefing_async(self, chat_id: str, briefing_data: dict) -> bool:
        text = self._format_briefing(briefing_data)
        return await self.send_async(chat_id, text)

    def send_briefing(self, chat_id: str, briefing_data: dict) -> bool:
        text = self._format_briefing(briefing_data)
        return self.send(chat_id, text)

    async def send_alert_async(
        self,
        chat_id: str,
        alert_type: str,
        message: str,
    ) -> bool:
        icons = {
            "email": "📧",
            "calendar": "📅",
            "conflict": "⚠️",
            "system": "💻",
            "birthday": "🎂",
            "worklife": "🕐",
            "reminder": "🧠",
        }
        icon = icons.get(alert_type, "🔔")
        text = f"{icon} *{alert_type.upper()}*\n{message}"
        return await self.send_async(chat_id, text)

    def send_alert(self, chat_id: str, alert_type: str, message: str) -> bool:
        icons = {
            "email": "📧",
            "calendar": "📅",
            "conflict": "⚠️",
            "system": "💻",
            "birthday": "🎂",
            "worklife": "🕐",
            "reminder": "🧠",
        }
        icon = icons.get(alert_type, "🔔")
        text = f"{icon} *{alert_type.upper()}*\n{message}"
        return self.send(chat_id, text)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_briefing(data: dict) -> str:
        date_str = data.get("date", "")
        lines = [f"🐾 *Bom dia, Marco!* {date_str}\n"]

        # Emails
        emails = data.get("emails", [])
        if emails:
            lines.append(f"📧 *EMAILS* ({len(emails)} novos)")
            for email in emails[:10]:
                urgent = " ⚡" if email.get("urgent") else ""
                lines.append(f"  → {email.get('from', '?')}: {email.get('subject', '?')}{urgent}")
            lines.append("")

        # Agenda
        events = data.get("events", [])
        if events:
            lines.append("📅 *AGENDA*")
            for event in events[:10]:
                start = event.get("start", "")
                if "T" in str(start):
                    time_str = start.split("T")[1][:5]
                else:
                    time_str = str(start)[:10]
                lines.append(f"  → {time_str} {event.get('summary', '?')}")

            # Check for conflicts
            conflicts = data.get("conflicts", [])
            for conflict in conflicts:
                lines.append(f"  → ⚠️ Conflito: {conflict}")
            lines.append("")

        # Reminders
        reminders = data.get("reminders", [])
        if reminders:
            lines.append("🧠 *LEMBRETES*")
            for reminder in reminders[:5]:
                lines.append(f"  → {reminder}")
            lines.append("")

        # System
        system = data.get("system", {})
        if system:
            uptime = system.get("uptime", "?")
            gpu = system.get("gpu_usage", "?")
            disk = system.get("disk_free", "?")
            lines.append(f"💻 *SISTEMA*")
            lines.append(f"  → Mike: online há {uptime} | GPU: {gpu} | Disco: {disk}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "ready": self._bot is not None,
            "load_error": self._load_error,
            "chat_marco": bool(self.chat_marco),
            "chat_familia": bool(self.chat_familia),
        }
