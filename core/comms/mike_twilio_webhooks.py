# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Twilio Webhooks
=====================

FastAPI router with Twilio webhook endpoints for:
- Voice call TwiML (appointment confirmation prompt)
- DTMF gather (handle digit responses)
- Call status callbacks
- SMS status callbacks
- Incoming SMS (reply handling)

Security: Twilio webhooks fail closed unless MIKE_TWILIO_ENABLED=true
and MIKE_TWILIO_AUTH_TOKEN is configured. Signatures are validated via
X-Twilio-Signature. Validation can only be skipped explicitly for local
development after both prerequisites are present.
"""

from __future__ import annotations

import asyncio as _asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import urllib.parse
from typing import Any, Optional

from mike_config import env_bool
from core.shared.task_utils import _handle_task_exception

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, Response

log = logging.getLogger("mike.twilio.webhooks")


router = APIRouter(prefix="/twilio", tags=["twilio-webhooks"])


# Event bus reference — set by server startup, or None if not configured
_event_bus: Any = None


def set_event_bus(bus: Any) -> None:
    """Set the event bus instance for publishing events from webhook handlers."""
    global _event_bus
    _event_bus = bus


# ------------------------------------------------------------------
# Signature validation
# ------------------------------------------------------------------

def _can_skip_signature_validation(request: Request) -> bool:
    """Allow the explicit development bypass only on a loopback URL/client."""
    if not env_bool("MIKE_TWILIO_SKIP_VALIDATION", False):
        return False
    client_host = request.client.host if request.client else ""
    try:
        client_is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        client_is_loopback = client_host.lower() == "localhost"
    request_host = (request.url.hostname or "").lower()
    try:
        host_is_loopback = ipaddress.ip_address(request_host).is_loopback
    except ValueError:
        host_is_loopback = request_host == "localhost"
    return client_is_loopback and host_is_loopback


def _validate_twilio_signature(request: Request, body_params: dict) -> bool:
    """Validate Twilio webhook signature (X-Twilio-Signature)."""
    # Routes are always registered, so configuration is checked per request.
    # Missing configuration is never silently interpreted as development mode.
    if not env_bool("MIKE_TWILIO_ENABLED", False):
        return False

    auth_token = os.getenv("MIKE_TWILIO_AUTH_TOKEN", "").strip()
    if not auth_token:
        return False

    if _can_skip_signature_validation(request):
        return True

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    # Reconstruct the URL Twilio called
    url = str(request.url)
    # Sort POST params and append to URL
    sorted_params = sorted(body_params.items())
    data_string = url + "".join(f"{k}{v}" for k, v in sorted_params)

    expected = hmac.new(
        auth_token.encode("utf-8"),
        data_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()

    import base64
    expected_b64 = base64.b64encode(expected).decode()
    return hmac.compare_digest(signature, expected_b64)


def _twiml_response(xml: str) -> Response:
    """Return a TwiML XML response."""
    return Response(content=xml, media_type="application/xml")


# ------------------------------------------------------------------
# Voice: Appointment confirmation prompt
# ------------------------------------------------------------------

@router.post("/voice/appointment")
async def voice_appointment(request: Request):
    """
    Twilio calls this URL when the call connects.
    Returns TwiML with appointment details and DTMF gather.
    """
    form = await request.form()
    params = dict(form)

    if not _validate_twilio_signature(request, params):
        return PlainTextResponse("Forbidden", status_code=403)

    # Get appointment context from query params
    query = dict(request.query_params)
    appt_id = query.get("appt_id", "0")
    name = query.get("name", "")
    at = query.get("at", "")
    service = query.get("service", "")

    # Check for answering machine
    answered_by = params.get("AnsweredBy", "human")
    if answered_by in ("machine_start", "machine_end_beep", "machine_end_silence"):
        # Leave a voicemail
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            f'<Say language="pt-BR" voice="Polly.Camila">'
            f'Olá{" " + name if name else ""}, aqui é do consultório. '
            f'Tentamos ligar para confirmar sua consulta. '
            f'Por favor retorne a ligação. Obrigado!'
            f'</Say>'
            '</Response>'
        )
        return _twiml_response(twiml)

    from mike_twilio import MikeTwilio
    gather_url = f"/twilio/voice/gather?appt_id={appt_id}"
    twiml = MikeTwilio.twiml_appointment_prompt(name, at, service, gather_url)
    return _twiml_response(twiml)


# ------------------------------------------------------------------
# Voice: DTMF gather callback
# ------------------------------------------------------------------

@router.post("/voice/gather")
async def voice_gather(request: Request):
    """
    Twilio sends the digit(s) the caller pressed.
    1=Confirm, 2=Reschedule, 3=Cancel.
    """
    form = await request.form()
    params = dict(form)

    if not _validate_twilio_signature(request, params):
        return PlainTextResponse("Forbidden", status_code=403)

    digit = str(params.get("Digits", ""))
    appt_id_str = request.query_params.get("appt_id", "0")

    try:
        appt_id = int(appt_id_str)
    except (ValueError, TypeError):
        appt_id = 0

    # Process the digit → appointment state change
    if appt_id > 0:
        _process_dtmf(appt_id, digit)

    from mike_twilio import MikeTwilio
    twiml = MikeTwilio.twiml_gather_response(digit, appt_id)
    return _twiml_response(twiml)


def _process_dtmf(appt_id: int, digit: str) -> None:
    """Apply DTMF digit to appointment state machine."""
    result_status = None
    try:
        from mike_appointments import AppointmentDB, AppointmentEngine, ApptStatus
        db = AppointmentDB()
        engine = AppointmentEngine(db=db)

        if digit == "1":
            engine.confirm(appt_id, "Confirmado via telefone (DTMF)")
            result_status = "confirmed"
            log.info(f"Appointment #{appt_id} confirmed via DTMF")
        elif digit == "2":
            # Mark as declined (will be rescheduled manually)
            engine.transition(appt_id, ApptStatus.DECLINED.value, "Pediu reagendamento via telefone")
            result_status = "declined"
            log.info(f"Appointment #{appt_id} marked for reschedule via DTMF")
        elif digit == "3":
            engine.cancel(appt_id, "Cancelado via telefone (DTMF)")
            result_status = "cancelled"
            log.info(f"Appointment #{appt_id} cancelled via DTMF")
        else:
            log.warning(f"Appointment #{appt_id}: invalid DTMF digit '{digit}'")

        # Publish appointment updated event
        if result_status and _event_bus:
            try:
                task = _asyncio.create_task(
                    _event_bus.publish(
                        _event_bus.EVENT_APPOINTMENT_UPDATED
                        if hasattr(_event_bus, 'EVENT_APPOINTMENT_UPDATED')
                        else "appointment.updated",
                        {
                            "appointment_id": appt_id,
                            "status": result_status,
                            "source": "dtmf",
                            "digit": digit,
                        },
                    )
                )
                task.add_done_callback(_handle_task_exception)
            except Exception:
                pass

    except Exception as exc:
        log.error(f"DTMF processing failed for #{appt_id}: {exc}")


# ------------------------------------------------------------------
# Voice: Call status callback
# ------------------------------------------------------------------

@router.post("/voice/status")
async def voice_status(request: Request):
    """
    Twilio reports call status changes (ringing, answered, completed, no-answer, busy, failed).
    """
    form = await request.form()
    params = dict(form)

    if not _validate_twilio_signature(request, params):
        return PlainTextResponse("Forbidden", status_code=403)

    call_sid = params.get("CallSid", "")
    call_status = params.get("CallStatus", "")
    appt_id_str = request.query_params.get("appt_id", "0")

    try:
        appt_id = int(appt_id_str)
    except (ValueError, TypeError):
        appt_id = 0

    log.info(f"Call status: SID={call_sid} status={call_status} appt_id={appt_id}")

    # Handle terminal call states
    if appt_id > 0 and call_status in ("no-answer", "busy", "failed"):
        try:
            from mike_appointments import AppointmentDB, AppointmentEngine, ApptStatus
            db = AppointmentDB()
            engine = AppointmentEngine(db=db)
            appt = db.get(appt_id)
            if appt and appt.status == ApptStatus.CALL_QUEUED.value:
                engine.transition(
                    appt_id,
                    ApptStatus.NO_ANSWER.value,
                    f"Call {call_status}: SID={call_sid}",
                )
                log.info(f"Appointment #{appt_id} → NO_ANSWER (call {call_status})")
                # Publish appointment updated event
                if _event_bus:
                    try:
                        task = _asyncio.create_task(
                            _event_bus.publish(
                                _event_bus.EVENT_APPOINTMENT_UPDATED
                                if hasattr(_event_bus, 'EVENT_APPOINTMENT_UPDATED')
                                else "appointment.updated",
                                {
                                    "appointment_id": appt_id,
                                    "status": "no_answer",
                                    "source": "voice_call",
                                    "call_status": call_status,
                                    "call_sid": call_sid,
                                },
                            )
                        )
                        task.add_done_callback(_handle_task_exception)
                    except Exception:
                        pass
        except Exception as exc:
            log.error(f"Call status processing failed for #{appt_id}: {exc}")

    return PlainTextResponse("OK")


# ------------------------------------------------------------------
# SMS: Status callback
# ------------------------------------------------------------------

@router.post("/sms/status")
async def sms_status(request: Request):
    """Twilio reports SMS delivery status."""
    form = await request.form()
    params = dict(form)

    if not _validate_twilio_signature(request, params):
        return PlainTextResponse("Forbidden", status_code=403)

    sms_sid = params.get("MessageSid", "")
    sms_status = params.get("MessageStatus", "")
    log.info(f"SMS status: SID={sms_sid} status={sms_status}")
    return PlainTextResponse("OK")


# ------------------------------------------------------------------
# SMS: Incoming message (reply handling)
# ------------------------------------------------------------------

@router.post("/sms/incoming")
async def sms_incoming(request: Request):
    """
    Handle incoming SMS replies.
    - "SIM" / "CONFIRMO" → confirm appointment
    - "REAGENDAR" → mark for reschedule
    - "CANCELAR" → cancel appointment
    """
    form = await request.form()
    params = dict(form)

    if not _validate_twilio_signature(request, params):
        return PlainTextResponse("Forbidden", status_code=403)

    from_phone = params.get("From", "")
    original_body = (params.get("Body", "") or "").strip()
    body_upper = original_body.upper()
    log.info(f"Incoming SMS from {from_phone}: {body_upper}")

    response_text = _handle_sms_reply(from_phone, body_upper)

    # Publish SMS reply event
    if _event_bus:
        try:
            task = _asyncio.create_task(
                _event_bus.publish(
                    _event_bus.EVENT_SMS_REPLY
                    if hasattr(_event_bus, 'EVENT_SMS_REPLY')
                    else "sms.reply",
                    {
                        "from_phone": from_phone,
                        "body": original_body,
                        "response": response_text,
                    },
                )
            )
            task.add_done_callback(_handle_task_exception)
        except Exception:
            pass

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Message>{response_text}</Message>'
        '</Response>'
    )
    return _twiml_response(twiml)


def _handle_sms_reply(phone: str, body_upper: str) -> str:
    """Process SMS reply and update appointment state. Returns reply text."""
    try:
        from mike_appointments import AppointmentDB, AppointmentEngine, ApptStatus
        db = AppointmentDB()
        engine = AppointmentEngine(db=db)

        # Find the most recent non-terminal appointment for this phone
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7)
        end = now + timedelta(days=30)
        appointments = db.list_between(start, end)
        target = None
        for appt in appointments:
            if appt.contact_phone == phone and appt.status not in (
                ApptStatus.ATTENDED.value, ApptStatus.CANCELLED.value,
            ):
                target = appt
                break

        if not target:
            return "Nao encontramos um agendamento ativo para este numero. Entre em contato pelo telefone do consultorio."

        if body_upper in ("SIM", "CONFIRMO", "CONFIRMAR", "OK", "1"):
            if target.status in (ApptStatus.SCHEDULED.value, ApptStatus.CALL_QUEUED.value,
                                  ApptStatus.REMINDER_SENT.value):
                engine.confirm(target.id, "Confirmado via SMS")
                return f"Consulta confirmada para {target.appointment_at}! Ate la!"
            elif target.status == ApptStatus.CONFIRMED.value:
                return "Sua consulta ja esta confirmada! Ate la!"
            else:
                return f"Status atual: {target.status}. Nao foi possivel confirmar por SMS."

        elif body_upper in ("REAGENDAR", "REMARCAR", "2"):
            engine.transition(
                target.id, ApptStatus.DECLINED.value, "Pediu reagendamento via SMS",
            )
            return "Certo! Entraremos em contato para reagendar. Obrigado!"

        elif body_upper in ("CANCELAR", "CANCELA", "NAO", "NÃO", "3"):
            engine.cancel(target.id, "Cancelado via SMS")
            return "Consulta cancelada. Se precisar reagendar, entre em contato."

        else:
            return (
                f"Nao entendi. Responda SIM para confirmar, REAGENDAR para mudar a data, "
                f"ou CANCELAR para cancelar. Sua consulta: {target.appointment_at}"
            )

    except ImportError:
        return "Sistema de agendamentos indisponivel no momento."
    except Exception as exc:
        log.error(f"SMS reply processing failed: {exc}")
        return "Ocorreu um erro. Por favor entre em contato por telefone."


# ------------------------------------------------------------------
# TTS Audio serving (for ElevenLabs cached files)
# ------------------------------------------------------------------

@router.get("/audio/{filename}")
async def serve_tts_audio(filename: str):
    """Serve cached TTS audio files for Twilio <Play> URLs."""
    from pathlib import Path as _Path

    # Validate filename: only allow safe characters
    if not filename.replace("_", "").replace("-", "").replace(".", "").isalnum():
        return PlainTextResponse("Invalid filename", status_code=400)
    if not filename.endswith(".mp3"):
        return PlainTextResponse("Only .mp3 files", status_code=400)

    project_root = _Path(__file__).resolve().parent.parent
    cache_dir = project_root / "runtime" / "cache" / "tts"
    audio_path = cache_dir / filename

    if not audio_path.exists():
        return PlainTextResponse("Not found", status_code=404)

    # Resolve to ensure no path traversal
    resolved = audio_path.resolve()
    if not str(resolved).startswith(str(cache_dir.resolve())):
        return PlainTextResponse("Forbidden", status_code=403)

    return Response(
        content=resolved.read_bytes(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
