"""Google Calendar MCP server for Mike."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP


PROJECT_ROOT = Path(
    os.getenv("MIKE_HOME") or Path(__file__).resolve().parents[2]
).resolve()
for module_dir in (PROJECT_ROOT / "core" / "integrations",):
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

from mike_google_auth import calendar_service  # noqa: E402


mcp = FastMCP("Mike Google Calendar MCP", json_response=True)
DEFAULT_TIMEZONE = os.getenv("MIKE_CALENDAR_TIMEZONE", "America/Toronto")


def _service():
    service, _token_path = calendar_service()
    return service


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start") or {}
    end = event.get("end") or {}
    return {
        "id": event.get("id"),
        "summary": event.get("summary") or "(sem titulo)",
        "description": event.get("description") or "",
        "location": event.get("location") or "",
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "timezone": start.get("timeZone") or DEFAULT_TIMEZONE,
        "status": event.get("status"),
        "html_link": event.get("htmlLink"),
        "attendees": [
            {
                "email": item.get("email"),
                "response_status": item.get("responseStatus"),
            }
            for item in (event.get("attendees") or [])
        ],
    }


@mcp.tool(description="Lista os calendarios Google disponiveis para a familia.")
def list_calendars() -> list[dict[str, Any]]:
    result = _service().calendarList().list().execute()
    return [
        {
            "id": item.get("id"),
            "summary": item.get("summary"),
            "primary": bool(item.get("primary")),
            "access_role": item.get("accessRole"),
            "timezone": item.get("timeZone"),
        }
        for item in (result.get("items") or [])
    ]


@mcp.tool(description=(
    "Lista eventos reais do Google Calendar em um intervalo ISO 8601. "
    "Se o intervalo nao for informado, usa agora ate os proximos 7 dias."
))
def list_events(
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 20,
    query: str = "",
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    lower = time_min or now.isoformat()
    upper = time_max or (now + timedelta(days=7)).isoformat()
    request = _service().events().list(
        calendarId=calendar_id,
        timeMin=lower,
        timeMax=upper,
        maxResults=max(1, min(int(max_results), 100)),
        singleEvents=True,
        orderBy="startTime",
        q=query or None,
    )
    return [_event_summary(item) for item in (request.execute().get("items") or [])]


@mcp.tool(description=(
    "Cria um evento real no Google Calendar. Requer inicio e fim em ISO 8601."
))
def create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    timezone_name: str = DEFAULT_TIMEZONE,
    attendee_emails: Optional[list[str]] = None,
) -> dict[str, Any]:
    if not summary.strip() or not start.strip() or not end.strip():
        raise ValueError("summary, start e end sao obrigatorios")
    body: dict[str, Any] = {
        "summary": summary.strip(),
        "description": description,
        "location": location,
        "start": {"dateTime": start, "timeZone": timezone_name},
        "end": {"dateTime": end, "timeZone": timezone_name},
    }
    attendees = [
        {"email": email.strip()}
        for email in (attendee_emails or [])
        if email and email.strip()
    ]
    if attendees:
        body["attendees"] = attendees
    event = _service().events().insert(
        calendarId=calendar_id,
        body=body,
        sendUpdates="all" if attendees else "none",
    ).execute()
    return _event_summary(event)


@mcp.tool(description="Atualiza campos de um evento existente no Google Calendar.")
def update_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    location: str = "",
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    if not event_id.strip():
        raise ValueError("event_id e obrigatorio")
    patch: dict[str, Any] = {}
    if summary:
        patch["summary"] = summary
    if description:
        patch["description"] = description
    if location:
        patch["location"] = location
    if start:
        patch["start"] = {"dateTime": start, "timeZone": timezone_name}
    if end:
        patch["end"] = {"dateTime": end, "timeZone": timezone_name}
    if not patch:
        raise ValueError("Informe pelo menos um campo para atualizar")
    event = _service().events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body=patch,
        sendUpdates="all",
    ).execute()
    return _event_summary(event)


@mcp.tool(description="Remove um evento existente do Google Calendar.")
def delete_event(event_id: str, calendar_id: str = "primary") -> dict[str, Any]:
    if not event_id.strip():
        raise ValueError("event_id e obrigatorio")
    _service().events().delete(
        calendarId=calendar_id,
        eventId=event_id,
        sendUpdates="all",
    ).execute()
    return {"deleted": True, "event_id": event_id, "calendar_id": calendar_id}


@mcp.tool(description="Consulta disponibilidade livre/ocupada no Google Calendar.")
def free_busy(
    time_min: str,
    time_max: str,
    calendar_ids: Optional[list[str]] = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    ids = calendar_ids or ["primary"]
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "timeZone": timezone_name,
        "items": [{"id": calendar_id} for calendar_id in ids],
    }
    return _service().freebusy().query(body=body).execute()


if __name__ == "__main__":
    mcp.run(transport="stdio")
