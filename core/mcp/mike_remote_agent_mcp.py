"""MCP client for Mike's optional remote Windows agent."""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("Mike Remote Agent MCP", json_response=True)
REMOTE_URL = os.getenv("MIKE_REMOTE_AGENT_URL", "http://192.168.40.60:3000").rstrip("/")
REMOTE_KEY = os.getenv("MIKE_REMOTE_AGENT_KEY", "").strip()
EXEC_PATH = os.getenv("MIKE_REMOTE_AGENT_EXEC_PATH", "/api/powershell").strip()
TIMEOUT_SECONDS = max(2.0, min(float(os.getenv("MIKE_REMOTE_AGENT_TIMEOUT", "20")), 120.0))


def _headers(*, require_key: bool) -> dict[str, str]:
    if require_key and not REMOTE_KEY:
        raise RuntimeError(
            "MIKE_REMOTE_AGENT_KEY nao configurada; execucao remota bloqueada."
        )
    if not REMOTE_KEY:
        return {}
    return {
        "Authorization": f"Bearer {REMOTE_KEY}",
        "X-Mike-Key": REMOTE_KEY,
    }


def _response_payload(response: httpx.Response) -> Any:
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        return {"text": response.text}


def _execute(command: str, timeout_seconds: int = 60) -> Any:
    timeout = max(1, min(int(timeout_seconds), 300))
    with httpx.Client(timeout=max(TIMEOUT_SECONDS, timeout + 5)) as client:
        response = client.post(
            f"{REMOTE_URL}/{EXEC_PATH.lstrip('/')}",
            headers=_headers(require_key=True),
            json={"command": command, "timeout_seconds": timeout},
        )
    return _response_payload(response)


def _ps_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@mcp.tool(description="Check connectivity and health of Mike's configured remote agent.")
def check_remote_agent_health() -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.get(
                f"{REMOTE_URL}/health",
                headers=_headers(require_key=False),
            )
        payload = _response_payload(response)
        return {
            "reachable": True,
            "url": REMOTE_URL,
            "payload": payload,
        }
    except Exception as exc:
        return {
            "reachable": False,
            "url": REMOTE_URL,
            "error": f"{type(exc).__name__}: {exc}",
        }


@mcp.tool(description="Execute non-interactive PowerShell through the configured remote agent.")
def execute_remote_powershell(command: str, timeout_seconds: int = 60) -> Any:
    if not command.strip():
        raise ValueError("command is required")
    return _execute(command, timeout_seconds=timeout_seconds)


@mcp.tool(description="Check a process by name on the configured remote Windows machine.")
def check_remote_process(name: str) -> Any:
    process_name = name.strip()
    if not process_name:
        raise ValueError("name is required")
    command = (
        f"Get-Process -Name {_ps_literal(process_name)} -ErrorAction SilentlyContinue | "
        "Select-Object Id,ProcessName,StartTime,CPU,WorkingSet64 | ConvertTo-Json -Depth 3"
    )
    return _execute(command)


@mcp.tool(description="List a directory on the configured remote Windows machine.")
def list_remote_directory(path: str) -> Any:
    remote_path = path.strip()
    if not remote_path:
        raise ValueError("path is required")
    command = (
        f"Get-ChildItem -LiteralPath {_ps_literal(remote_path)} -Force | "
        "Select-Object Name,FullName,Length,LastWriteTime,"
        "@{Name='Type';Expression={if($_.PSIsContainer){'directory'}else{'file'}}} | "
        "ConvertTo-Json -Depth 3"
    )
    return _execute(command)


if __name__ == "__main__":
    mcp.run(transport="stdio")
