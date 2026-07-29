# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Twilio Adapter
====================

SMS sending, voice call initiation, and webhook helpers for Twilio.
Uses only stdlib (urllib) — no twilio SDK needed.

Configuration (.env):
    MIKE_TWILIO_ACCOUNT_SID=ACxxx
    MIKE_TWILIO_AUTH_TOKEN=xxx
    MIKE_TWILIO_PHONE_FROM=+55119999xxxx
    MIKE_TWILIO_WEBHOOK_BASE=https://mike.supereziorealtime.com
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("mike.twilio")

_API_BASE = "https://api.twilio.com/2010-04-01"


class MikeTwilio:
    """Twilio REST API adapter (SMS + Voice) using only urllib."""

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        phone_from: Optional[str] = None,
        webhook_base: Optional[str] = None,
    ) -> None:
        self.account_sid = (account_sid or os.getenv("MIKE_TWILIO_ACCOUNT_SID", "")).strip()
        self.auth_token = (auth_token or os.getenv("MIKE_TWILIO_AUTH_TOKEN", "")).strip()
        self.phone_from = (phone_from or os.getenv("MIKE_TWILIO_PHONE_FROM", "")).strip()
        self.webhook_base = (webhook_base or os.getenv("MIKE_TWILIO_WEBHOOK_BASE", "")).strip().rstrip("/")
        self.enabled = bool(self.account_sid and self.auth_token and self.phone_from)

    # ------------------------------------------------------------------
    # Internal HTTP
    # ------------------------------------------------------------------

    def _auth_header(self) -> str:
        creds = f"{self.account_sid}:{self.auth_token}"
        b64 = base64.b64encode(creds.encode()).decode()
        return f"Basic {b64}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        form_data: Optional[dict] = None,
        timeout: float = 15.0,
    ) -> dict:
        """Make an authenticated request to Twilio REST API."""
        url = f"{_API_BASE}/Accounts/{self.account_sid}/{path}"
        body = None
        if form_data:
            body = urllib.parse.urlencode(form_data).encode("utf-8")

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self._auth_header())
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            log.error(f"Twilio {method} {path} → {exc.code}: {error_body[:500]}")
            raise
        except Exception as exc:
            log.error(f"Twilio {method} {path} → {exc}")
            raise

    # ------------------------------------------------------------------
    # SMS
    # ------------------------------------------------------------------

    def send_sms(
        self,
        to: str,
        body: str,
        *,
        status_callback: Optional[str] = None,
    ) -> dict:
        """
        Send an SMS message.

        Returns Twilio message resource dict with 'sid', 'status', etc.
        """
        if not self.enabled:
            log.warning("Twilio not configured — SMS not sent")
            return {"sid": "", "status": "disabled"}

        form = {
            "To": to,
            "From": self.phone_from,
            "Body": body[:1600],  # Twilio limit
        }
        if status_callback:
            form["StatusCallback"] = status_callback

        result = self._request("POST", "Messages.json", form_data=form)
        log.info(f"SMS sent to {to}: SID={result.get('sid', '?')} status={result.get('status', '?')}")
        return result

    def send_reminder_sms(
        self,
        to: str,
        contact_name: str,
        appointment_at: str,
        service_type: str = "",
    ) -> dict:
        """Send a friendly appointment reminder SMS."""
        service_text = f" ({service_type})" if service_type else ""
        # Parse time for display
        time_display = appointment_at
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(appointment_at.replace("Z", "+00:00"))
            time_display = dt.strftime("%d/%m às %H:%M")
        except (ValueError, TypeError):
            pass

        body = (
            f"Oi {contact_name}! Lembrete: sua consulta{service_text} é {time_display}. "
            f"Responda SIM para confirmar ou REAGENDAR para mudar. "
            f"Qualquer dúvida, estamos à disposição!"
        )
        callback = f"{self.webhook_base}/twilio/sms/status" if self.webhook_base else None
        return self.send_sms(to, body, status_callback=callback)

    def send_noshow_sms(
        self,
        to: str,
        contact_name: str,
    ) -> dict:
        """Send SMS after a no-show."""
        body = (
            f"Oi {contact_name}, sentimos sua falta hoje! "
            f"Se quiser reagendar, responda REAGENDAR ou entre em contato. "
            f"Esperamos ver você em breve!"
        )
        return self.send_sms(to, body)

    # ------------------------------------------------------------------
    # Voice calls
    # ------------------------------------------------------------------

    def initiate_call(
        self,
        to: str,
        *,
        twiml_url: Optional[str] = None,
        twiml_xml: Optional[str] = None,
        status_callback: Optional[str] = None,
        timeout: int = 30,
        machine_detection: str = "Enable",
    ) -> dict:
        """
        Initiate an outbound voice call.

        Provide either twiml_url (a URL returning TwiML) or twiml_xml (inline TwiML).
        Returns Twilio call resource dict with 'sid', 'status', etc.
        """
        if not self.enabled:
            log.warning("Twilio not configured — call not initiated")
            return {"sid": "", "status": "disabled"}

        form: dict = {
            "To": to,
            "From": self.phone_from,
            "Timeout": str(timeout),
            "MachineDetection": machine_detection,
        }

        if twiml_xml:
            form["Twiml"] = twiml_xml
        elif twiml_url:
            form["Url"] = twiml_url
        elif self.webhook_base:
            form["Url"] = f"{self.webhook_base}/twilio/voice/appointment"
        else:
            raise ValueError("No TwiML URL or XML provided for call")

        if status_callback:
            form["StatusCallback"] = status_callback
        elif self.webhook_base:
            form["StatusCallback"] = f"{self.webhook_base}/twilio/voice/status"

        result = self._request("POST", "Calls.json", form_data=form)
        log.info(f"Call initiated to {to}: SID={result.get('sid', '?')} status={result.get('status', '?')}")
        return result

    def initiate_confirmation_call(
        self,
        to: str,
        contact_name: str,
        appointment_at: str,
        service_type: str = "",
        appt_id: int = 0,
    ) -> dict:
        """
        Initiate a confirmation call with DTMF prompt.

        The call uses a TwiML webhook that plays a message and collects
        digit input (1=confirm, 2=reschedule, 3=cancel).
        """
        # Build the webhook URL with appointment context
        params = urllib.parse.urlencode({
            "appt_id": appt_id,
            "name": contact_name,
            "at": appointment_at,
            "service": service_type,
        })
        twiml_url = f"{self.webhook_base}/twilio/voice/appointment?{params}" if self.webhook_base else None

        if not twiml_url:
            # Fallback: inline TwiML
            service_text = f" de {service_type}" if service_type else ""
            twiml_xml = (
                f'<Response>'
                f'<Say language="pt-BR" voice="Polly.Camila">'
                f'Olá {contact_name}, aqui é do consultório. '
                f'Você tem uma consulta{service_text} marcada para {appointment_at}. '
                f'Pressione 1 para confirmar, 2 para reagendar, ou 3 para cancelar.'
                f'</Say>'
                f'<Gather numDigits="1" timeout="10" action="{self.webhook_base}/twilio/voice/gather">'
                f'<Say language="pt-BR" voice="Polly.Camila">Pressione 1, 2 ou 3.</Say>'
                f'</Gather>'
                f'<Say language="pt-BR" voice="Polly.Camila">Não entendi. Ligamos novamente depois.</Say>'
                f'</Response>'
            )
            return self.initiate_call(to, twiml_xml=twiml_xml)

        return self.initiate_call(
            to,
            twiml_url=twiml_url,
            status_callback=f"{self.webhook_base}/twilio/voice/status?appt_id={appt_id}" if self.webhook_base else None,
        )

    # ------------------------------------------------------------------
    # Status lookups
    # ------------------------------------------------------------------

    def get_message(self, sid: str) -> dict:
        """Get a message resource by SID."""
        return self._request("GET", f"Messages/{sid}.json")

    def get_call(self, sid: str) -> dict:
        """Get a call resource by SID."""
        return self._request("GET", f"Calls/{sid}.json")

    # ------------------------------------------------------------------
    # TwiML Helpers (for generating webhook responses)
    # ------------------------------------------------------------------

    @staticmethod
    def twiml_appointment_prompt(
        contact_name: str,
        appointment_at: str,
        service_type: str = "",
        gather_action: str = "/twilio/voice/gather",
    ) -> str:
        """Generate TwiML XML for the appointment confirmation prompt."""
        service_text = f" de {service_type}" if service_type else ""
        # Parse for friendly display
        time_display = appointment_at
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(appointment_at.replace("Z", "+00:00"))
            time_display = dt.strftime("%d de %B às %H:%M")
        except (ValueError, TypeError):
            pass

        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            f'<Say language="pt-BR" voice="Polly.Camila">'
            f'Olá {_xml_escape(contact_name)}, aqui é do consultório. '
            f'Você tem uma consulta{_xml_escape(service_text)} marcada para {_xml_escape(time_display)}. '
            f'Pressione 1 para confirmar, 2 para reagendar, ou 3 para cancelar.'
            f'</Say>'
            f'<Gather numDigits="1" timeout="10" action="{_xml_escape(gather_action)}">'
            f'<Say language="pt-BR" voice="Polly.Camila">Pressione 1, 2 ou 3.</Say>'
            f'</Gather>'
            f'<Say language="pt-BR" voice="Polly.Camila">Não entendi. Ligamos novamente depois.</Say>'
            '</Response>'
        )

    @staticmethod
    def twiml_gather_response(digit: str, appt_id: int = 0) -> str:
        """Generate TwiML response based on the digit pressed."""
        if digit == "1":
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response>'
                '<Say language="pt-BR" voice="Polly.Camila">'
                'Perfeito! Sua consulta está confirmada. Até lá!'
                '</Say>'
                '</Response>'
            )
        elif digit == "2":
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response>'
                '<Say language="pt-BR" voice="Polly.Camila">'
                'Certo, vamos reagendar. Entraremos em contato para combinar um novo horário. Obrigado!'
                '</Say>'
                '</Response>'
            )
        elif digit == "3":
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response>'
                '<Say language="pt-BR" voice="Polly.Camila">'
                'Consulta cancelada. Se precisar reagendar, entre em contato. Obrigado!'
                '</Say>'
                '</Response>'
            )
        else:
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response>'
                '<Say language="pt-BR" voice="Polly.Camila">'
                'Opção inválida. Ligamos novamente depois. Obrigado!'
                '</Say>'
                '</Response>'
            )


def _xml_escape(text: str) -> str:
    """Minimal XML escaping for TwiML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
