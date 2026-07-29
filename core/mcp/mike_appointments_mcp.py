"""MCP facade for Mike's persistent appointment state machine."""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from mike_appointments import AppointmentEngine


mcp = FastMCP("Mike Appointments MCP", json_response=True)
_ENGINE: Optional[AppointmentEngine] = None


def _engine() -> AppointmentEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = AppointmentEngine()
    return _ENGINE


def _record(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [_record(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else value


@mcp.tool(description="Create a persistent family appointment.")
def create_appointment(
    contact_name: str,
    contact_phone: str,
    appointment_at: str,
    service_type: str = "",
    contact_email: str = "",
    duration_min: int = 60,
) -> dict:
    if not contact_name.strip() or not contact_phone.strip() or not appointment_at.strip():
        raise ValueError("contact_name, contact_phone and appointment_at are required")
    return _record(_engine().schedule(
        contact_name=contact_name.strip(),
        contact_phone=contact_phone.strip(),
        appointment_at=appointment_at.strip(),
        service_type=service_type.strip(),
        contact_email=contact_email.strip(),
        duration_min=max(1, int(duration_min)),
    ))


@mcp.tool(description="List today's persistent appointments.")
def list_today() -> list[dict]:
    return _record(_engine().list_today())


@mcp.tool(description="List persistent appointments in the next number of days.")
def list_upcoming(days: int = 7) -> list[dict]:
    return _record(_engine().list_upcoming(days=max(1, min(int(days), 365))))


@mcp.tool(description="Get one appointment by numeric ID.")
def get_appointment(appointment_id: int) -> Optional[dict]:
    return _record(_engine().db.get(int(appointment_id)))


@mcp.tool(description="Cancel an appointment.")
def cancel_appointment(appointment_id: int, reason: str = "") -> dict:
    return _record(_engine().cancel(int(appointment_id), reason=reason))


@mcp.tool(description="Reschedule an appointment to a new ISO-8601 date/time.")
def reschedule_appointment(appointment_id: int, new_datetime: str) -> dict:
    if not new_datetime.strip():
        raise ValueError("new_datetime is required")
    return _record(_engine().reschedule(int(appointment_id), new_datetime.strip()))


@mcp.tool(description="Confirm an appointment.")
def confirm_appointment(appointment_id: int, note: str = "") -> dict:
    return _record(_engine().confirm(int(appointment_id), note=note))


@mcp.tool(description="Mark a reminder-sent appointment as attended.")
def mark_attended(appointment_id: int) -> dict:
    return _record(_engine().mark_attended(int(appointment_id)))


@mcp.tool(description="Mark a reminder-sent appointment as a no-show.")
def mark_no_show(appointment_id: int) -> dict:
    return _record(_engine().mark_no_show(int(appointment_id)))


@mcp.tool(description="Summarize appointment counts and today's schedule.")
def appointment_status() -> dict:
    return _engine().status_summary()


if __name__ == "__main__":
    mcp.run(transport="stdio")
