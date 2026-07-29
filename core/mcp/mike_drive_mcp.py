"""Read-only Google Drive MCP server for Mike."""

from __future__ import annotations

import io
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


PROJECT_ROOT = Path(
    os.getenv("MIKE_HOME") or Path(__file__).resolve().parents[2]
).resolve()
integration_dir = PROJECT_ROOT / "core" / "integrations"
if str(integration_dir) not in sys.path:
    sys.path.insert(0, str(integration_dir))

from mike_google_auth import drive_service  # noqa: E402


mcp = FastMCP("Mike Google Drive MCP", json_response=True)
_MAX_READ_BYTES = 8 * 1024 * 1024
_MAX_TEXT_CHARS = 50000


def _service():
    service, _token_path = drive_service()
    return service


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "mime_type": item.get("mimeType"),
        "modified_time": item.get("modifiedTime"),
        "size": item.get("size"),
        "web_view_link": item.get("webViewLink"),
        "parents": item.get("parents") or [],
    }


def _escape_drive_literal(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _list(q: str, limit: int) -> list[dict[str, Any]]:
    page_size = max(1, min(int(limit), 100))
    response = (
        _service()
        .files()
        .list(
            q=q,
            pageSize=page_size,
            orderBy="modifiedTime desc",
            fields=(
                "files(id,name,mimeType,modifiedTime,size,webViewLink,parents)"
            ),
        )
        .execute()
    )
    return [_summary(item) for item in response.get("files", [])]


@mcp.tool(description="List Google Drive files, optionally using a Drive query.")
def list_drive_files(limit: int = 25, query: str = "", folder_id: str = "") -> list[dict]:
    clauses = ["trashed=false"]
    if query.strip():
        clauses.append(f"({query.strip()})")
    if folder_id.strip():
        clauses.append(f"'{_escape_drive_literal(folder_id.strip())}' in parents")
    return _list(" and ".join(clauses), limit)


@mcp.tool(description="Search Google Drive file names and full text.")
def search_drive(search_term: str, limit: int = 25) -> list[dict]:
    term = _escape_drive_literal(search_term.strip())
    if not term:
        raise ValueError("search_term is required")
    return _list(
        f"trashed=false and (name contains '{term}' or fullText contains '{term}')",
        limit,
    )


def _download_bytes(service, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buffer,
        service.files().get_media(fileId=file_id),
        chunksize=1024 * 1024,
    )
    done = False
    while not done:
        _status, done = downloader.next_chunk()
        if buffer.tell() > _MAX_READ_BYTES:
            raise ValueError("Drive file exceeds the 8 MiB read limit")
    return buffer.getvalue()


def _extract_pdf(data: bytes) -> str:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as document:
        return "\n".join(page.get_text("text") for page in document)


def _extract_docx(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


@mcp.tool(description="Read and extract text from a Google Drive file.")
def read_drive_file(file_id: str) -> dict:
    fid = file_id.strip()
    if not fid:
        raise ValueError("file_id is required")
    service = _service()
    meta = (
        service.files()
        .get(
            fileId=fid,
            fields="id,name,mimeType,modifiedTime,size,webViewLink,parents",
        )
        .execute()
    )
    mime = str(meta.get("mimeType") or "")
    if mime == "application/vnd.google-apps.document":
        data = service.files().export(fileId=fid, mimeType="text/plain").execute()
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    elif mime == "application/vnd.google-apps.spreadsheet":
        data = service.files().export(fileId=fid, mimeType="text/csv").execute()
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    elif mime == "application/vnd.google-apps.presentation":
        data = service.files().export(fileId=fid, mimeType="application/pdf").execute()
        text = _extract_pdf(data if isinstance(data, bytes) else bytes(data))
    else:
        data = _download_bytes(service, fid)
        if mime == "application/pdf":
            text = _extract_pdf(data)
        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            text = _extract_docx(data)
        elif (
            mime.startswith("text/")
            or mime in {"application/json", "application/xml", "text/csv"}
        ):
            text = data.decode("utf-8", errors="replace")
        else:
            raise ValueError(f"Unsupported Drive file type for text extraction: {mime}")
    truncated = len(text) > _MAX_TEXT_CHARS
    return {
        "file": _summary(meta),
        "text": text[:_MAX_TEXT_CHARS],
        "truncated": truncated,
    }


@mcp.tool(description="List Google Drive files modified during the last number of hours.")
def recent_drive_changes(hours: int = 24, limit: int = 50) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(
        hours=max(1, min(int(hours), 24 * 365))
    )
    timestamp = since.isoformat().replace("+00:00", "Z")
    return _list(f"trashed=false and modifiedTime > '{timestamp}'", limit)


if __name__ == "__main__":
    mcp.run(transport="stdio")
