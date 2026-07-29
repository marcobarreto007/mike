# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Heartbeat — Periodic proactive monitoring
===============================================

Runs every 30 min via Task Scheduler.  Checks Gmail, Calendar
and system health, then sends Telegram alerts when needed.

Also provides MikeHeartbeatV2 — LLM-powered analysis layer with
email urgency classification, smart reply generation, SMS intent
analysis, and family profile matching.  Falls back to keyword
matching when no LLM is available.

Usage:
    python mike_heartbeat.py          # single run
    python mike_heartbeat.py --once   # alias
    python mike_heartbeat.py --loop   # internal loop (30 min)

    from mike_heartbeat import MikeHeartbeat    # keyword-based
    from mike_heartbeat import MikeHeartbeatV2   # LLM + keyword fallback
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# Ensure src/ is on path when called directly
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

_PROJECT_ROOT = _SRC_DIR.parent.parent  # core/autonomy -> core -> mike (project root)

# Load env files
from mike_config import load_env_file, env_bool
from core.shared.task_utils import _handle_task_exception
load_env_file(_PROJECT_ROOT / "config" / ".env")


LOG_DIR = _PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("mike.heartbeat")


# ------------------------------------------------------------------
# V2 Config
# ------------------------------------------------------------------

MIKE_HEARTBEAT_USE_LLM = env_bool("MIKE_HEARTBEAT_USE_LLM", False)
MIKE_HEARTBEAT_LLM_MODEL = os.getenv("MIKE_HEARTBEAT_LLM_MODEL", "mock")


# ------------------------------------------------------------------
# V2 Helper Functions
# ------------------------------------------------------------------

def _load_family_profiles() -> dict:
    """Load family_profiles.json for rich personality context."""
    path = _PROJECT_ROOT / "config" / "family_profiles.json"
    if not path.exists():
        log.debug("family_profiles.json not found at %s", path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data.get("profiles", {})
    except Exception as exc:
        log.warning("Failed to load family_profiles.json: %s", exc)
        return {}


def _load_identity() -> dict:
    """Load identity.json for email contacts."""
    path = _PROJECT_ROOT / "config" / "identity.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# ==================================================================
# MikeHeartbeat (keyword-based)
# ==================================================================

class MikeHeartbeat:
    """Checks emails, calendar, system and sends Telegram alerts."""

    INTERVAL_MINUTES = 30
    EVENT_WARN_MINUTES = 30

    def __init__(self, event_bus: Any = None) -> None:
        from mike_telegram import MikeTelegram
        from mike_briefing import MikeBriefing

        self.telegram = MikeTelegram(log_fn=log.info)
        self.briefing = MikeBriefing(log_fn=log.info)
        self.log_file = LOG_DIR / "heartbeat_history.jsonl"
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Alert checks
    # ------------------------------------------------------------------

    def check_urgent_emails(self) -> list[dict]:
        """Return list of alert dicts for urgent unread emails."""
        alerts = []
        try:
            emails = self.briefing.collect_emails(limit=10)
            for email in emails:
                if email.get("urgent"):
                    alerts.append({
                        "type": "email",
                        "message": f"{email.get('from', '?')}: {email.get('subject', '?')}",
                        "data": email,
                    })
                    # Publish to event bus if configured
                    if self._event_bus:
                        try:
                            import asyncio as _asyncio
                            task = _asyncio.create_task(
                                self._event_bus.publish(
                                    self._event_bus.EVENT_EMAIL_URGENT
                                    if hasattr(self._event_bus, 'EVENT_EMAIL_URGENT')
                                    else "email.urgent",
                                    {"email": email},
                                )
                            )
                            task.add_done_callback(_handle_task_exception)
                        except Exception:
                            pass
        except Exception as exc:
            log.warning(f"Email check failed: {exc}")
        return alerts

    def check_upcoming_events(self) -> list[dict]:
        """Return alerts for events happening within WARN minutes."""
        alerts = []
        try:
            events = self.briefing.collect_events(hours_ahead=2)
            now = datetime.now(timezone.utc)

            for event in events:
                start_str = event.get("start", "")
                if not start_str or "T" not in start_str:
                    continue
                try:
                    start_raw = start_str
                    if start_raw.endswith("Z"):
                        start_raw = start_raw[:-1] + "+00:00"
                    event_time = datetime.fromisoformat(start_raw)
                    if event_time.tzinfo is None:
                        continue
                    delta = (event_time - now).total_seconds() / 60
                    if 0 < delta <= self.EVENT_WARN_MINUTES:
                        alerts.append({
                            "type": "calendar",
                            "message": f"{event.get('summary', '?')} em {int(delta)} min",
                            "data": event,
                        })
                except (ValueError, TypeError):
                    continue

            # Conflict detection
            conflicts = self.briefing.detect_conflicts(events)
            for conflict in conflicts:
                alerts.append({
                    "type": "conflict",
                    "message": conflict,
                })
        except Exception as exc:
            log.warning(f"Calendar check failed: {exc}")
        return alerts

    def check_system_health(self) -> list[dict]:
        """Return alerts for system issues."""
        alerts = []
        try:
            health = self.briefing.collect_system_health()

            # Disk warning: < 10%
            disk_pct = health.get("disk_free_pct", 100)
            if isinstance(disk_pct, (int, float)) and disk_pct < 10:
                alerts.append({
                    "type": "system",
                    "message": f"Disco quase cheio! Apenas {disk_pct}% livre",
                    "data": health,
                })

            # GPU temp warning: > 85°C
            gpu_temp_raw = health.get("gpu_temp", "")
            if gpu_temp_raw and gpu_temp_raw != "?":
                try:
                    temp_val = int(str(gpu_temp_raw).replace("°C", ""))
                    if temp_val > 85:
                        alerts.append({
                            "type": "system",
                            "message": f"GPU quente! {temp_val}°C",
                            "data": health,
                        })
                except (ValueError, TypeError):
                    pass

            # LLM server (Qwen) health check — port 8081
            try:
                req = urllib.request.Request('http://127.0.0.1:8081/health')
                urllib.request.urlopen(req, timeout=3)
                log.debug("LLM server (Qwen) health check OK")
            except Exception:
                alerts.append({
                    "type": "system",
                    "message": "LLM server (Qwen) nao esta respondendo",
                })
        except Exception as exc:
            log.warning(f"System health check failed: {exc}")
        return alerts

    def check_worklife_balance(self) -> list[dict]:
        """Warn if it's after 21h and Mike is being used."""
        alerts = []
        now = datetime.now()
        if now.hour >= 21:
            alerts.append({
                "type": "worklife",
                "message": f"Marco, {now.hour}h — hora da familia 🐾",
            })
        return alerts

    def check_appointments(self) -> list[dict]:
        """Check appointment pipeline: send reminders, detect no-shows, queue calls."""
        alerts = []
        try:
            from mike_config import APPT_ENABLED
            if not APPT_ENABLED:
                return alerts

            from mike_appointments import AppointmentDB, AppointmentEngine, ApptStatus
            from mike_twilio import MikeTwilio

            db = AppointmentDB()
            twilio = MikeTwilio()
            engine = AppointmentEngine(db=db)
            now = datetime.now(timezone.utc)

            # 1) Send SMS reminders for confirmed appointments ~1h before
            pending_reminders = engine.get_pending_reminders(now)
            for appt in pending_reminders:
                if twilio.enabled:
                    try:
                        result = twilio.send_reminder_sms(
                            appt.contact_phone, appt.contact_name,
                            appt.appointment_at, appt.service_type,
                        )
                        appt.sms_sid = result.get("sid", "")
                        appt.sms_sent_at = now.isoformat()
                        engine.transition(appt.id, ApptStatus.REMINDER_SENT.value, f"SMS SID={appt.sms_sid}")
                        alerts.append({
                            "type": "appointment",
                            "message": f"📱 Lembrete SMS enviado: {appt.contact_name} ({appt.appointment_at})",
                        })
                    except Exception as exc:
                        log.warning(f"SMS reminder failed for #{appt.id}: {exc}")
                        alerts.append({
                            "type": "appointment",
                            "message": f"⚠️ Falha SMS lembrete: {appt.contact_name} - {exc}",
                        })
                else:
                    # No Twilio — just transition and alert
                    engine.transition(appt.id, ApptStatus.REMINDER_SENT.value, "Twilio disabled, manual reminder")
                    alerts.append({
                        "type": "appointment",
                        "message": f"⏰ Lembrete pendente (sem Twilio): {appt.contact_name} ({appt.appointment_at})",
                    })

            # 2) Detect no-shows
            no_shows = engine.get_potential_no_shows(now)
            for appt in no_shows:
                try:
                    engine.mark_no_show(appt.id)
                    if twilio.enabled:
                        twilio.send_noshow_sms(appt.contact_phone, appt.contact_name)
                    alerts.append({
                        "type": "appointment",
                        "message": f"❌ No-show: {appt.contact_name} ({appt.appointment_at})",
                    })
                except Exception as exc:
                    log.warning(f"No-show processing failed for #{appt.id}: {exc}")

            # 3) Queue confirmation calls for appointments ~24h away
            pending_calls = engine.get_pending_calls(now)
            for appt in pending_calls:
                try:
                    engine.transition(appt.id, ApptStatus.CALL_QUEUED.value, "Queued by heartbeat")
                    if twilio.enabled:
                        result = twilio.initiate_confirmation_call(
                            appt.contact_phone, appt.contact_name,
                            appt.appointment_at, appt.service_type,
                            appt.id,
                        )
                        appt = db.get(appt.id)
                        appt.call_sid = result.get("sid", "")
                        appt.call_attempts += 1
                        db.update(appt)
                    alerts.append({
                        "type": "appointment",
                        "message": f"📞 Ligação confirmação: {appt.contact_name} ({appt.appointment_at})",
                    })
                except Exception as exc:
                    log.warning(f"Confirmation call failed for #{appt.id}: {exc}")

            # 4) Retry calls that had no answer
            retry_calls = engine.get_retry_calls()
            for appt in retry_calls:
                try:
                    engine.transition(appt.id, ApptStatus.CALL_QUEUED.value, "Retry by heartbeat")
                    if twilio.enabled:
                        result = twilio.initiate_confirmation_call(
                            appt.contact_phone, appt.contact_name,
                            appt.appointment_at, appt.service_type,
                            appt.id,
                        )
                        appt = db.get(appt.id)
                        appt.call_sid = result.get("sid", "")
                        appt.call_attempts += 1
                        db.update(appt)
                    alerts.append({
                        "type": "appointment",
                        "message": f"🔄 Retry ligação: {appt.contact_name} (tentativa {appt.call_attempts})",
                    })
                except Exception as exc:
                    log.warning(f"Call retry failed for #{appt.id}: {exc}")

        except ImportError:
            pass  # Appointments module not available
        except Exception as exc:
            log.warning(f"Appointments check failed: {exc}")
        return alerts

    def check_special_dates(self) -> list[dict]:
        """Check for upcoming birthdays/anniversaries from soul.json."""
        alerts = []
        soul_file = _PROJECT_ROOT / "runtime" / "memory" / "soul.json"
        if not soul_file.exists():
            return alerts

        try:
            soul = json.loads(soul_file.read_text(encoding="utf-8-sig"))
            family = soul.get("family", {})
            today = datetime.now()

            for name, info in family.items():
                if not isinstance(info, dict):
                    continue
                birthday_raw = info.get("birthday") or info.get("aniversario") or ""
                if not birthday_raw:
                    continue
                try:
                    # Expect MM-DD or YYYY-MM-DD
                    parts = str(birthday_raw).split("-")
                    if len(parts) >= 2:
                        month = int(parts[-2])
                        day = int(parts[-1])
                        bday_this_year = today.replace(month=month, day=day)
                        delta_days = (bday_this_year - today).days
                        if 0 <= delta_days <= 3:
                            full_name = info.get("full_name", name)
                            if delta_days == 0:
                                alerts.append({
                                    "type": "birthday",
                                    "message": f"Hoje é aniversário de {full_name}! 🎂",
                                })
                            else:
                                alerts.append({
                                    "type": "birthday",
                                    "message": f"Aniversário de {full_name} em {delta_days} dia(s)!",
                                })
                except (ValueError, TypeError):
                    continue
        except Exception as exc:
            log.warning(f"Special dates check failed: {exc}")
        return alerts

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def run(self) -> list[dict]:
        """Execute a single heartbeat cycle.  Returns list of alerts sent."""
        log.info("Heartbeat starting...")
        all_alerts = []

        all_alerts.extend(self.check_urgent_emails())
        all_alerts.extend(self.check_upcoming_events())
        all_alerts.extend(self.check_system_health())
        all_alerts.extend(self.check_worklife_balance())
        all_alerts.extend(self.check_appointments())
        all_alerts.extend(self.check_special_dates())

        if all_alerts and self.telegram.enabled and self.telegram.chat_marco:
            for alert in all_alerts:
                self.telegram.send_alert(
                    self.telegram.chat_marco,
                    alert["type"],
                    alert["message"],
                )

        self._log_heartbeat(all_alerts)
        log.info(f"Heartbeat done. {len(all_alerts)} alert(s).")
        return all_alerts

    def run_morning_briefing(self) -> bool:
        """Generate and send the full morning briefing."""
        log.info("Generating morning briefing...")
        data = self.briefing.generate_morning()
        if self.telegram.enabled and self.telegram.chat_marco:
            return self.telegram.send_briefing(self.telegram.chat_marco, data)
        log.warning("Telegram not configured — briefing not sent.")
        return False

    def _log_heartbeat(self, alerts: list[dict]) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "alert_count": len(alerts),
            "alerts": [
                {"type": a["type"], "message": a["message"]}
                for a in alerts
            ],
        }
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning(f"Failed to log heartbeat: {exc}")


# ==================================================================
# MikeHeartbeatV2 — LLM-powered analysis layer
# ==================================================================

class MikeHeartbeatV2:
    """
    Enhanced heartbeat that supports LLM-powered analysis.
    Falls back to keyword matching when no LLM is available.

    Args:
        generate_fn: Optional async callable(messages, tools) -> dict
                     If None, falls back to keyword matching for all analysis.
        execute_tool_fn: Optional async callable(tool_name, args) -> dict
        event_bus: Optional MikeEventBus instance for publishing events.
    """

    def __init__(
        self,
        generate_fn: Optional[Callable] = None,
        execute_tool_fn: Optional[Callable] = None,
        event_bus: Optional[Any] = None,
    ) -> None:
        self._generate_fn = generate_fn
        self._execute_tool_fn = execute_tool_fn
        self._event_bus = event_bus
        self._use_llm = MIKE_HEARTBEAT_USE_LLM and generate_fn is not None

        # Load family profiles for context-aware replies
        self._family_profiles = _load_family_profiles()
        self._identity = _load_identity()
        self._family_emails: dict[str, str] = {}
        contacts = self._identity.get("email_contacts", {})
        for profile_key, email_addr in contacts.items():
            if email_addr:
                self._family_emails[email_addr.lower().strip()] = profile_key

        # Wrapped heartbeat
        self._heartbeat: Optional[Any] = None
        try:
            self._heartbeat = MikeHeartbeat()
        except Exception as exc:
            log.debug("MikeHeartbeat not available (limited env): %s", exc)

    # ------------------------------------------------------------------
    # Property: whether LLM is available
    # ------------------------------------------------------------------

    @property
    def has_llm(self) -> bool:
        """True if generate_fn is configured and LLM mode is enabled."""
        return self._use_llm

    # ------------------------------------------------------------------
    # Email Urgency Analysis
    # ------------------------------------------------------------------

    async def analyze_email_urgency(self, email: dict) -> dict:
        """
        Classify email urgency (1-5) and extract intent.

        If generate_fn is available, uses LLM with a focused prompt.
        Otherwise falls back to keyword matching.

        Returns:
            {"urgency": int 1-5, "intent": str, "reasoning": str, "method": str}
        """
        if self._use_llm and self._generate_fn:
            return await self._llm_analyze_urgency(email)

        return self._keyword_analyze_urgency(email)

    async def _llm_analyze_urgency(self, email: dict) -> dict:
        """Use LLM to classify email urgency."""
        sender = email.get("from", email.get("sender", "unknown"))
        subject = email.get("subject", "")
        snippet = email.get("snippet", email.get("body", ""))
        body = email.get("body", "")

        # Load family context for the sender
        family_context = ""
        profile = self._match_family_profile(sender)
        if profile:
            family_context = profile.get("memory_summary_for_mike", "")[:500]

        prompt = f"""Analise a urgencia deste email em uma escala de 1 a 5:

De: {sender}
Assunto: {subject}
Conteudo: {body or snippet[:300]}
Contexto Familiar: {family_context}

Classifique:
1 = Nao urgente / newsletter / spam
2 = Baixa urgencia / informativo
3 = Moderada / precisa resposta em alguns dias
4 = Urgente / precisa resposta em horas
5 = Critico / emergencia / precisa resposta IMEDIATA

Extraia tambem a intencao do remetente (ex: "pedido_ajuda", "confirmacao", "informacao",
"reclamacao", "emocional", "saude", "familiar", "agendamento", "spam", "outro").

Responda APENAS com JSON valido, sem marcacoes de codigo:
{{"urgency": <1-5>, "intent": "<intencao>", "reasoning": "<razao curta>"}}"""

        try:
            result = await self._generate_fn(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=150,
            )

            # Extract JSON from response
            text = result.get("assistant_text", "") if isinstance(result, dict) else str(result)
            return self._parse_urgency_json(text, "llm", email)

        except Exception as exc:
            log.warning("LLM urgency analysis failed, falling back to keywords: %s", exc)
            return self._keyword_analyze_urgency(email)

    def _parse_urgency_json(self, text: str, method: str, email: dict) -> dict:
        """Parse JSON from LLM response, with robust fallback."""
        # Try to extract JSON block
        json_match = re.search(r'\{[^{}]*"urgency"[^{}]*\}', text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                return {
                    "urgency": max(1, min(5, int(data.get("urgency", 3)))),
                    "intent": str(data.get("intent", "unknown")),
                    "reasoning": str(data.get("reasoning", "")),
                    "method": method,
                }
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        # Fallback: extract numeric urgency
        urgency_match = re.search(r'"urgency"\s*:\s*(\d)', text)
        urgency = int(urgency_match.group(1)) if urgency_match else 3
        return {
            "urgency": max(1, min(5, urgency)),
            "intent": "unknown",
            "reasoning": "LLM parsing failed",
            "method": f"{method}_fallback",
        }

    def _keyword_analyze_urgency(self, email: dict) -> dict:
        """Keyword-based urgency classification (no LLM needed)."""
        sender = str(email.get("from", email.get("sender", ""))).lower()
        subject = str(email.get("subject", "")).lower()
        snippet = str(email.get("snippet", email.get("body", ""))).lower()
        body = str(email.get("body", "")).lower()
        combined = f"{sender} {subject} {body} {snippet}"

        urgency = 2
        intent = "informacao"
        reasons = []

        # Urgency 5 — critical
        crisis_words = ["emergencia", "emergência", "socorro", "hospital",
                         "acidente", "morreu", "faleceu", "grave"]
        if any(w in combined for w in crisis_words):
            urgency = 5
            intent = "emergencia"
            reasons.append("palavras de crise/emergencia")

        # Urgency 4 — urgent
        urgent_words = ["urgente", "preciso", "agora", "hoje", "rapido",
                        "não demora", "nao demora", "responda logo", "asap"]
        if urgency < 5 and any(w in combined for w in urgent_words):
            urgency = 4
            intent = "pedido_ajuda"
            reasons.append("palavras de urgencia")

        # Check if it's a family member
        profile = self._match_family_profile(email.get("from", ""))
        if profile:
            urgency = max(urgency, 3)
            reasons.append("remetente familiar")

            # Specific family contexts
            if "marilene" in str(profile.get("profile_id", "")).lower():
                urgency = max(urgency, 4)
                reasons.append("Marilene (76 anos, saude fragil)")

            if "matheus" in str(profile.get("profile_id", "")).lower():
                urgency = max(urgency, 3)
                reasons.append("Matheus (autista nivel 2)")

        # Check for emotional distress
        emotional_words = ["triste", "chateado", "chateada", "raiva",
                           "decepcionado", "deprimido", "sozinho", "sozinha"]
        if any(w in combined for w in emotional_words):
            urgency = max(urgency, 4)
            intent = "emocional"
            reasons.append("conteudo emocional")

        # Check for health-related
        health_words = ["saude", "saúde", "medico", "médico", "remedio",
                        "remédio", "dor", "sintoma", "consulta", "exame"]
        if any(w in combined for w in health_words):
            urgency = max(urgency, 4)
            intent = "saude"
            reasons.append("conteudo de saude")

        # Check for appointment/scheduling
        appt_words = ["agendamento", "consulta", "horario", "horário",
                      "confirmar", "confirmacao", "confirmação"]
        if any(w in combined for w in appt_words):
            intent = "agendamento"

        # Check for spam/newsletter
        spam_indicators = ["unsubscribe", "newsletter", "promocao", "promoção",
                           "oferta", "desconto", "gratis", "grátis", "clique aqui"]
        if any(w in combined for w in spam_indicators) and not profile:
            urgency = min(urgency, 1)
            intent = "spam"
            reasons = ["indicadores de spam"]

        return {
            "urgency": urgency,
            "intent": intent,
            "reasoning": "; ".join(reasons) if reasons else "analise por palavras-chave",
            "method": "keyword",
        }

    # ------------------------------------------------------------------
    # Smart Reply Generation
    # ------------------------------------------------------------------

    async def generate_smart_reply(self, email: dict) -> str:
        """
        Generate a personalized reply using LLM or template.

        Uses family_profiles.json context for richness.
        Falls back to template if no LLM available.
        """
        if self._use_llm and self._generate_fn:
            return await self._llm_generate_reply(email)

        return self._template_generate_reply(email)

    async def _llm_generate_reply(self, email: dict) -> str:
        """Use LLM to generate a context-aware reply."""
        sender = email.get("from", email.get("sender", "unknown"))
        subject = email.get("subject", "")
        body = email.get("body", email.get("snippet", ""))
        profile = self._match_family_profile(sender)

        family_context = ""
        how_to_treat = ""
        if profile:
            name = profile.get("name", "")
            memory = profile.get("memory_summary_for_mike", "")
            treat_key = "how_mike_should_treat_her"
            if treat_key not in profile:
                treat_key = "how_mike_should_treat_him"
            how = profile.get(treat_key, {})
            tone = how.get("tone", "respeitoso e carinhoso")
            priorities = ", ".join(how.get("priorities", [])[:3])
            do_not = ", ".join(how.get("do_not", [])[:3])
            family_context = (
                f"Perfil de {name}:\n"
                f"- Tom: {tone}\n"
                f"- Prioridades: {priorities}\n"
                f"- Nao fazer: {do_not}\n"
                f"- Resumo: {memory[:400]}\n"
            )

        prompt = f"""Voce e o Mike, o Yorkshire digital da familia Barreto.
Voce recebeu um email de um familiar e precisa responder de forma calorosa e pessoal.

{family_context}

Email recebido:
De: {sender}
Assunto: {subject}
Conteudo: {body[:800]}

Escreva uma resposta personalizada em portugues brasileiro como o Mike responderia.
Seja carinhoso, leal e direto. Assine como Mike com o emoji 🐾.
Se for urgente, demonstre que esta priorizando. Se for emocional, mostre empatia.
NAO use marcacoes de codigo. Responda APENAS com o texto do email."""

        try:
            result = await self._generate_fn(
                [{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=400,
            )
            return result.get("assistant_text", "") if isinstance(result, dict) else str(result)
        except Exception as exc:
            log.warning("LLM reply generation failed, using template: %s", exc)
            return self._template_generate_reply(email)

    def _template_generate_reply(self, email: dict) -> str:
        """Template-based reply generation (no LLM)."""
        sender = email.get("from", email.get("sender", ""))
        subject = email.get("subject", "")
        profile = self._match_family_profile(sender)

        if profile:
            name = profile.get("name", "Familia")
        else:
            # Try extracting name from email
            name = sender.split("@")[0] if "@" in str(sender) else "Familia"

        greeting = f"Ola, {name}!"
        subject_ref = subject.strip()
        subject_line = f' "{subject_ref}" ' if subject_ref else " "
        return (
            f"{greeting}\n\n"
            f"Recebi seu email{subject_line}"
            f"e ja estou cuidando disso.\n\n"
            f"Sou o Mike, o Yorkshire digital da familia Barreto. "
            f"Estou aqui 24 horas por dia para ajudar voce e toda a familia "
            f"com o que precisar.\n\n"
            f"Com carinho,\n"
            f"Mike 🐾\n"
            f"Yorkshire Digital da Familia Barreto"
        )

    # ------------------------------------------------------------------
    # SMS Reply Analysis
    # ------------------------------------------------------------------

    async def analyze_sms_reply(self, body: str, context: Optional[dict] = None) -> dict:
        """
        Interpret ambiguous SMS replies using LLM.

        Args:
            body: The SMS reply text (e.g., "talvez sim, mas so depois das 15h")
            context: Optional dict with appointment info (date, time, service)

        Returns:
            {"intent": str, "confidence": float, "conditions": str, "method": str}

        Intents:
            confirm — definite yes
            confirm_conditional — yes with conditions
            reschedule — wants to change date/time
            cancel — definite no
            ambiguous — could mean multiple things
            unknown — can't determine
        """
        if self._use_llm and self._generate_fn:
            return await self._llm_analyze_sms(body, context)

        return self._keyword_analyze_sms(body, context)

    async def _llm_analyze_sms(self, body: str, context: Optional[dict] = None) -> dict:
        """Use LLM to interpret ambiguous SMS replies."""
        context_str = ""
        if context:
            context_str = f"\nContexto do agendamento: {json.dumps(context, ensure_ascii=False)}"

        prompt = f"""Analise esta resposta SMS de um paciente/cliente sobre um agendamento.

SMS: "{body}"{context_str}

Classifique a intencao como:
- "confirm" — confirmacao clara (sim, confirmo, ok, pode ser)
- "confirm_conditional" — sim mas com condicao (ex: "sim, mas so depois das 15h")
- "reschedule" — quer reagendar (ex: "nao posso esse dia, pode ser quarta?")
- "cancel" — cancelamento claro (ex: "nao vou poder", "cancela por favor")
- "ambiguous" — pode ser varias coisas
- "unknown" — nao da pra entender

Extraia tambem:
- conditions: string com as condicoes mencionadas (ex: "depois das 15h")
- confidence: numero de 0 a 1 indicando sua confianca
- suggested_action: o que o sistema deve fazer ("confirm", "flag_for_review", "ask_clarification")

Responda APENAS com JSON valido, sem marcacoes de codigo:
{{"intent": "<intent>", "conditions": "<condicoes>", "confidence": <0-1>, "suggested_action": "<acao>"}}"""

        try:
            result = await self._generate_fn(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=120,
            )
            text = result.get("assistant_text", "") if isinstance(result, dict) else str(result)
            return self._parse_sms_json(text, body, "llm")
        except Exception as exc:
            log.warning("LLM SMS analysis failed, using keywords: %s", exc)
            return self._keyword_analyze_sms(body, context)

    def _parse_sms_json(self, text: str, body: str, method: str) -> dict:
        """Parse JSON from LLM SMS analysis response."""
        json_match = re.search(r'\{[^{}]*"intent"[^{}]*\}', text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                return {
                    "intent": str(data.get("intent", "ambiguous")),
                    "confidence": float(data.get("confidence", 0.5)),
                    "conditions": str(data.get("conditions", "")),
                    "suggested_action": str(data.get("suggested_action", "flag_for_review")),
                    "method": method,
                }
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        return {
            "intent": "ambiguous",
            "confidence": 0.3,
            "conditions": "",
            "suggested_action": "flag_for_review",
            "method": f"{method}_fallback",
        }

    def _keyword_analyze_sms(self, body: str, context: Optional[dict] = None) -> dict:
        """Keyword-based SMS analysis (no LLM)."""
        text = str(body or "").strip()
        text_lower = text.lower()

        # Definite confirmations
        confirm_words = ["sim", "confirmo", "confirmar", "confirmado",
                         "ok", "pode", "certo", "combinado", "fechado",
                         "beleza", "otimo", "ótimo", "perfeito", "1"]
        # Conditional indicators
        conditional_words = ["mas", "porém", "porem", "so", "só",
                             "depois", "apos", "após", "se", "talvez"]
        # Cancel words
        cancel_words = ["nao", "não", "cancelar", "cancela", "desmarcar",
                        "desmarca", "nao vou", "não vou", "nao posso",
                        "não posso", "desisti", "3"]
        # Reschedule words
        reschedule_words = ["reagendar", "remarcar", "mudar", "trocar",
                            "outro dia", "outra data", "2"]

        # Check intent
        has_confirm = any(w in text_lower for w in confirm_words)
        has_cancel = any(w in text_lower for w in cancel_words)
        has_conditional = any(w in text_lower for w in conditional_words)
        has_reschedule = any(w in text_lower for w in reschedule_words)

        if has_cancel:
            return {
                "intent": "cancel",
                "confidence": 0.85,
                "conditions": "",
                "suggested_action": "cancel_appointment",
                "method": "keyword",
            }

        if has_reschedule:
            return {
                "intent": "reschedule",
                "confidence": 0.80,
                "conditions": text,
                "suggested_action": "flag_for_reschedule",
                "method": "keyword",
            }

        if has_confirm and has_conditional:
            return {
                "intent": "confirm_conditional",
                "confidence": 0.75,
                "conditions": text,
                "suggested_action": "flag_for_review",
                "method": "keyword",
            }

        if has_confirm:
            return {
                "intent": "confirm",
                "confidence": 0.90,
                "conditions": "",
                "suggested_action": "confirm_appointment",
                "method": "keyword",
            }

        if has_conditional:
            return {
                "intent": "ambiguous",
                "confidence": 0.40,
                "conditions": text,
                "suggested_action": "ask_clarification",
                "method": "keyword",
            }

        return {
            "intent": "unknown",
            "confidence": 0.20,
            "conditions": "",
            "suggested_action": "flag_for_review",
            "method": "keyword",
        }

    # ------------------------------------------------------------------
    # Family Profile Matching
    # ------------------------------------------------------------------

    def _match_family_profile(self, email_address: str) -> Optional[dict]:
        """Match an email address to a family profile."""
        email_lower = str(email_address or "").lower().strip()

        # Direct match via email_contacts in identity.json
        profile_key = self._family_emails.get(email_lower)
        if profile_key:
            return self._family_profiles.get(profile_key)

        # Match by extracting email from "Name <email>" format
        match = re.search(r'<([^>]+)>', email_address or "")
        if match:
            profile_key = self._family_emails.get(match.group(1).lower().strip())
            if profile_key:
                return self._family_profiles.get(profile_key)

        # Fuzzy: check if email contains known profile names
        for profile_key, profile in self._family_profiles.items():
            name_parts = profile.get("name", "").lower().split()
            for part in name_parts:
                if len(part) >= 3 and part in email_lower:
                    return profile

        # Check by display name in email
        for profile_key, profile in self._family_profiles.items():
            name = profile.get("name", "").lower()
            if len(name) >= 3 and name in email_lower:
                return profile

        return None

    # ------------------------------------------------------------------
    # run_with_llm — replaces keyword matching with LLM where beneficial
    # ------------------------------------------------------------------

    async def run_with_llm(self) -> list[dict]:
        """
        Execute heartbeat cycle with LLM-powered analysis.
        Falls back to base MikeHeartbeat.run() when LLM is unavailable.
        """
        if not self._use_llm:
            # Fall back to keyword-based heartbeat
            if self._heartbeat:
                return await self._run_fallback()
            return []

        alerts = []

        # 1. Check emails with LLM urgency classification
        try:
            if self._heartbeat and self._heartbeat.briefing:
                emails = self._heartbeat.briefing.collect_emails(limit=10)
                for email in emails:
                    analysis = await self.analyze_email_urgency(email)
                    if analysis["urgency"] >= 4:
                        alert_msg = (
                            f"{email.get('from', '?')}: {email.get('subject', '?')} "
                            f"[urgencia {analysis['urgency']}, {analysis['intent']}]"
                        )
                        alerts.append({
                            "type": "email",
                            "message": alert_msg,
                            "data": {**email, "analysis": analysis},
                        })

                        # Publish event to event bus
                        if self._event_bus:
                            try:
                                await self._event_bus.publish(
                                    self._event_bus.EVENT_EMAIL_URGENT if hasattr(self._event_bus, 'EVENT_EMAIL_URGENT') else "email.urgent",
                                    {"email": email, "analysis": analysis},
                                )
                            except Exception as exc:
                                log.debug("Event publish failed: %s", exc)

                        # If family member, also publish family event
                        if self._match_family_profile(email.get("from", "")):
                            if self._event_bus:
                                try:
                                    await self._event_bus.publish(
                                        self._event_bus.EVENT_EMAIL_FAMILY if hasattr(self._event_bus, 'EVENT_EMAIL_FAMILY') else "email.family",
                                        {"email": email, "analysis": analysis, "profile": self._match_family_profile(email.get("from", ""))},
                                    )
                                except Exception as exc:
                                    log.debug("Event publish failed: %s", exc)
        except Exception as exc:
            log.warning("LLM email check failed: %s", exc)

        # 2. Run base heartbeat checks (calendar, system, etc.)
        if self._heartbeat:
            try:
                base_alerts = await self._run_fallback()
                alerts.extend(base_alerts)
            except Exception as exc:
                log.warning("Base heartbeat fallback failed: %s", exc)

        # 3. Publish system alerts to event bus
        for alert in alerts:
            if alert.get("type") == "system" and self._event_bus:
                try:
                    await self._event_bus.publish(
                        self._event_bus.EVENT_SYSTEM_ALERT if hasattr(self._event_bus, 'EVENT_SYSTEM_ALERT') else "system.alert",
                        alert,
                    )
                except Exception as exc:
                    log.debug("Event publish failed: %s", exc)

        return alerts

    async def _run_fallback(self) -> list[dict]:
        """Run the base MikeHeartbeat (keyword-based) in a thread."""
        if self._heartbeat:
            return await asyncio.to_thread(self._heartbeat.run)
        return []

    # ------------------------------------------------------------------
    # Convenience: get base heartbeat
    # ------------------------------------------------------------------

    @property
    def base_heartbeat(self):
        """Access the wrapped MikeHeartbeat instance."""
        return self._heartbeat


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Mike Heartbeat")
    parser.add_argument("--once", action="store_true", help="Run once and exit (default)")
    parser.add_argument("--briefing", action="store_true", help="Send morning briefing")
    parser.add_argument("--loop", action="store_true", help="Run in a loop every 30 min")
    args = parser.parse_args()

    heartbeat = MikeHeartbeat()

    if args.briefing:
        heartbeat.run_morning_briefing()
        return

    if args.loop:
        log.info(f"Starting heartbeat loop (every {heartbeat.INTERVAL_MINUTES} min)")
        while True:
            try:
                heartbeat.run()
            except Exception as exc:
                log.error(f"Heartbeat cycle failed: {exc}")
            time.sleep(heartbeat.INTERVAL_MINUTES * 60)
    else:
        heartbeat.run()


if __name__ == "__main__":
    main()
