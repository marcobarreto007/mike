# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike - MCP Client Hub
=====================
Manages one or more MCP stdio sub-processes and exposes a unified tool surface.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
from mike_config import env_bool

log = logging.getLogger("mike")

_GITHUB_TOKEN_CACHE: Optional[str] = None
_GITHUB_TOKEN_ATTEMPTED = False


def _unwrap_exception(exc: BaseException) -> BaseException:
    current: BaseException = exc
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        nxt = current.exceptions[0]
        if isinstance(nxt, BaseException):
            current = nxt
        else:
            break
    return current


def _exception_summary(exc: BaseException) -> str:
    root = _unwrap_exception(exc)
    return f"{type(root).__name__}: {root}"


def _mcp_result_payload(result) -> tuple[str, list[str]]:
    """Render legacy text blocks and modern MCP structuredContent."""
    content = list(getattr(result, "content", None) or [])
    texts = [
        getattr(item, "text", None)
        for item in content
        if getattr(item, "text", None) is not None
    ]
    payload = "\n".join(texts).strip()
    content_types = [type(item).__name__ for item in content]
    structured = getattr(result, "structuredContent", None)
    if not payload and structured is not None:
        payload = json.dumps(structured, ensure_ascii=False, default=str)
        content_types.append("StructuredContent")
    return payload, content_types


def _existing_executable(path_value: str) -> Optional[str]:
    raw = str(path_value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.exists() and candidate.is_file():
        return str(candidate)
    return None


def _windows_browser_candidates() -> List[str]:
    if os.name != "nt":
        return []

    candidates: List[str] = []
    bases = [
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for base in [b for b in bases if b]:
        candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        candidates.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        ms_playwright = Path(local_app_data) / "ms-playwright"
        if ms_playwright.exists():
            for browser_path in sorted(ms_playwright.glob("**/chrome-win/chrome.exe"), reverse=True):
                candidates.append(str(browser_path))

    unique: List[str] = []
    seen: set[str] = set()
    for path in candidates:
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _resolve_puppeteer_executable(env_map: dict[str, str]) -> Optional[str]:
    explicit = _existing_executable(env_map.get("PUPPETEER_EXECUTABLE_PATH", ""))
    if explicit:
        return explicit
    for candidate in _windows_browser_candidates():
        resolved = _existing_executable(candidate)
        if resolved:
            return resolved
    return None


def _resolve_github_token(env_map: dict[str, str]) -> Optional[str]:
    explicit = str(env_map.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")).strip()
    if explicit:
        return explicit

    gh_token = str(env_map.get("GH_TOKEN", "")).strip() or str(env_map.get("GITHUB_TOKEN", "")).strip()
    if gh_token:
        return gh_token

    global _GITHUB_TOKEN_CACHE, _GITHUB_TOKEN_ATTEMPTED
    if _GITHUB_TOKEN_CACHE:
        return _GITHUB_TOKEN_CACHE
    if _GITHUB_TOKEN_ATTEMPTED:
        return None

    _GITHUB_TOKEN_ATTEMPTED = True
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        token = (proc.stdout or "").strip()
        if proc.returncode == 0 and token:
            _GITHUB_TOKEN_CACHE = token
            return token
    except Exception:
        return None
    return None


def _github_pull_fallback(raw_name: str, arguments: Optional[dict]) -> Optional[str]:
    if raw_name not in {"github.get_pull_request_comments", "github.get_pull_request_reviews"}:
        return None

    args = arguments or {}
    owner = args.get("owner")
    repo = args.get("repo")
    pull_number = args.get("pull_number", args.get("pullNumber"))
    if not owner or not repo or pull_number is None:
        return None

    endpoint_suffix = "comments" if raw_name.endswith("_comments") else "reviews"
    endpoint = f"repos/{owner}/{repo}/pulls/{pull_number}/{endpoint_suffix}"

    try:
        proc = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    text = (proc.stdout or "").strip()
    return text if text else "[]"


_MCP_SAFE_ENV_KEYS: set[str] = {
    "PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA",
    "PYTHONPATH",
}
_MCP_SAFE_ENV_PREFIXES: tuple[str, ...] = (
    "MCP_", "PUPPETEER_", "BRAVE_", "GITHUB_", "GH_",
)


def _build_safe_mcp_env(base_env: dict[str, str]) -> dict[str, str]:
    """Whitelist-only environment for MCP subprocesses.

    Prevents accidental credential leak (e.g. DEEPSEEK_API_KEY, MIKE_API_KEY,
    Email/Telegram/Twilio tokens) into MCP server subprocesses.
    """
    safe: dict[str, str] = {}
    for key, value in base_env.items():
        if key in _MCP_SAFE_ENV_KEYS:
            safe[key] = value
        elif key.startswith(_MCP_SAFE_ENV_PREFIXES):
            safe[key] = value
    return safe


@dataclass
class MikeMcpServerConfig:
    name: str
    command: str = ""
    url: str = ""
    transport: str = "stdio"
    args: List[str] = field(default_factory=list)
    env: Optional[dict[str, str]] = None
    headers: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    enabled: bool = True
    tool_prefix: str = ""
    capabilities: List[str] = field(default_factory=list)
    access: str = "owner"
    source: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> "MikeMcpServerConfig":
        capability_list = payload.get("capabilities") if isinstance(payload.get("capabilities"), list) else []
        return cls(
            name=str(payload.get("name") or "").strip(),
            command=str(payload.get("command") or "").strip(),
            url=str(payload.get("url") or "").strip(),
            transport=str(payload.get("transport") or "stdio").strip().lower(),
            args=[str(arg) for arg in (payload.get("args") or [])],
            env={
                str(key): str(value)
                for key, value in (payload.get("env") or {}).items()
            } or None,
            headers={
                str(key): str(value)
                for key, value in (payload.get("headers") or {}).items()
                if str(value).strip()
            } or None,
            cwd=str(payload.get("cwd") or "").strip() or None,
            enabled=bool(payload.get("enabled", True)),
            tool_prefix=str(payload.get("tool_prefix") or payload.get("name") or "").strip(),
            capabilities=[
                str(cap).strip().lower()
                for cap in capability_list
                if str(cap).strip()
            ],
            access=str(payload.get("access") or "owner").strip().lower() or "owner",
            source=str(payload.get("source") or "").strip(),
        )


class MikeStdioMcpClient:
    def __init__(self, config: MikeMcpServerConfig):
        self.config = config
        self.enabled = bool(config.enabled and config.command)
        self.last_error: Optional[str] = None
        self.tool_manifest: List[dict] = []
        self._puppeteer_runtime_warned = False

    def server_params(self) -> StdioServerParameters:
        import os
        # Aplica whitelist de env vars seguras e overrides do config.
        # Filtra chaves com valor vazio para nao poluir o subprocesso.
        merged_env: dict[str, str] = _build_safe_mcp_env(os.environ)
        if self.config.env:
            for k, v in self.config.env.items():
                if v:  # não sobrescreve com string vazia
                    merged_env[k] = v

        if (self.config.name or "").strip().lower() == "puppeteer":
            runtime_path = _resolve_puppeteer_executable(merged_env)
            if runtime_path:
                merged_env["PUPPETEER_EXECUTABLE_PATH"] = runtime_path
                merged_env.setdefault("PUPPETEER_PRODUCT", "chrome")
            elif not self._puppeteer_runtime_warned:
                self._puppeteer_runtime_warned = True
                log.warning(
                    "Puppeteer runtime not found. Configure PUPPETEER_EXECUTABLE_PATH or install Chrome/Edge."
                )

        if (self.config.name or "").strip().lower() == "github":
            github_token = _resolve_github_token(merged_env)
            if github_token:
                merged_env["GITHUB_PERSONAL_ACCESS_TOKEN"] = github_token

        return StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=merged_env,
            cwd=self.config.cwd or None,
        )

    def _tool_name(self, raw_name: str) -> str:
        prefix = (self.config.tool_prefix or "").strip()
        return f"{prefix}.{raw_name}" if prefix else raw_name

    def _normalize_tool(self, tool) -> dict:
        raw_name = tool.name
        return {
            "name": self._tool_name(raw_name),
            "raw_name": raw_name,
            "server_name": self.config.name,
            "description": tool.description or "",
            "input_schema": getattr(tool, "inputSchema", None) or {},
            "capabilities": list(self.config.capabilities),
            "access": self.config.access,
            "source": self.config.source,
        }

    async def list_tools(self, refresh: bool = False) -> List[dict]:
        if not self.enabled:
            return []
        if self.tool_manifest and not refresh:
            return self.tool_manifest
        try:
            async def _do_list() -> list:
                async with stdio_client(self.server_params()) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        return [self._normalize_tool(tool) for tool in result.tools]

            self.tool_manifest = await asyncio.wait_for(_do_list(), timeout=15.0)
            self.last_error = None
            return self.tool_manifest
        except asyncio.TimeoutError:
            self.last_error = "timeout"
            self.tool_manifest = []
            log.warning("Mike MCP list_tools timeout for %s (>15s)", self.config.name)
            return []
        except Exception as exc:
            self.last_error = _exception_summary(exc)
            self.tool_manifest = []
            log.warning("Mike MCP list_tools failed for %s: %s", self.config.name, self.last_error)
            return []

        self.tool_manifest = [self._normalize_tool(tool) for tool in result.tools]
        self.last_error = None
        return self.tool_manifest

    async def call_tool(self, raw_name: str, arguments: Optional[dict] = None, timeout: float = 90.0) -> dict:
        if not self.enabled:
            raise RuntimeError(f"MCP server {self.config.name} is disabled")
        try:
            async with stdio_client(self.server_params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await asyncio.wait_for(
                        session.call_tool(raw_name, arguments or {}),
                        timeout=timeout,
                    )
        except Exception as exc:
            self.last_error = _exception_summary(exc)
            raise RuntimeError(self.last_error) from exc

        payload, content_types = _mcp_result_payload(result)

        if self.config.name == "github" and bool(getattr(result, "isError", False)):
            namespaced = raw_name if raw_name.startswith("github.") else f"github.{raw_name}"
            fallback = _github_pull_fallback(namespaced, arguments)
            if fallback is not None:
                return {
                    "ok": True,
                    "text": fallback,
                    "content_types": ["fallback"],
                    "server_name": self.config.name,
                }

        return {
            "ok": not bool(getattr(result, "isError", False)),
            "text": payload,
            "content_types": content_types,
            "server_name": self.config.name,
        }


class MikeHttpMcpClient:
    """Streamable-HTTP MCP client for remote servers."""

    def __init__(self, config: MikeMcpServerConfig):
        self.config = config
        self.enabled = bool(config.enabled and config.url)
        self.last_error: Optional[str] = None
        self.tool_manifest: List[dict] = []
        self._last_attempt_at = 0.0

    def _tool_name(self, raw_name: str) -> str:
        prefix = (self.config.tool_prefix or "").strip()
        return f"{prefix}.{raw_name}" if prefix else raw_name

    def _normalize_tool(self, tool) -> dict:
        return {
            "name": self._tool_name(tool.name),
            "raw_name": tool.name,
            "server_name": self.config.name,
            "description": tool.description or "",
            "input_schema": getattr(tool, "inputSchema", None) or {},
            "capabilities": list(self.config.capabilities),
            "access": self.config.access,
            "source": self.config.url,
        }

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=dict(self.config.headers or {}),
            timeout=httpx.Timeout(90.0, connect=10.0),
        )

    async def list_tools(self, refresh: bool = False) -> List[dict]:
        if not self.enabled:
            return []
        if self.tool_manifest and not refresh:
            return self.tool_manifest
        # Do not make every chat/tool-list request wait on a remote server
        # whose credentials are missing. Explicit refresh still retries.
        if (
            not refresh
            and self.last_error
            and time.monotonic() - self._last_attempt_at < 300.0
        ):
            return []
        self._last_attempt_at = time.monotonic()
        try:
            async def _do_list() -> list:
                async with self._http_client() as http:
                    async with streamable_http_client(
                        self.config.url, http_client=http
                    ) as (read, write, _session_id):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.list_tools()
                            return [self._normalize_tool(tool) for tool in result.tools]

            self.tool_manifest = await asyncio.wait_for(_do_list(), timeout=20.0)
            self.last_error = None
            return self.tool_manifest
        except Exception as exc:
            self.last_error = _exception_summary(exc)
            self.tool_manifest = []
            log.warning(
                "Mike remote MCP list_tools failed for %s: %s",
                self.config.name,
                self.last_error,
            )
            return []

    async def call_tool(
        self, raw_name: str, arguments: Optional[dict] = None, timeout: float = 90.0
    ) -> dict:
        if not self.enabled:
            raise RuntimeError(f"MCP server {self.config.name} is disabled")
        try:
            async with self._http_client() as http:
                async with streamable_http_client(
                    self.config.url, http_client=http
                ) as (read, write, _session_id):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await asyncio.wait_for(
                            session.call_tool(raw_name, arguments or {}),
                            timeout=timeout,
                        )
        except Exception as exc:
            self.last_error = _exception_summary(exc)
            raise RuntimeError(self.last_error) from exc

        payload, content_types = _mcp_result_payload(result)
        return {
            "ok": not bool(getattr(result, "isError", False)),
            "text": payload,
            "content_types": content_types,
            "server_name": self.config.name,
        }


class MikeWorkspaceMcpClient:
    def __init__(
        self,
        server_path: Path,
        allowed_roots: List[Path],
        enabled: bool = True,
    ):
        self.server_path = server_path
        self.allowed_roots = [root.resolve() for root in allowed_roots]
        self._client = MikeStdioMcpClient(
            MikeMcpServerConfig(
                name="workspace",
                command=sys.executable,
                args=[str(server_path), *[str(root) for root in self.allowed_roots]],
                enabled=enabled and server_path.exists() and bool(self.allowed_roots),
                tool_prefix="",
                capabilities=["workspace"],
                access="workspace",
                source=str(server_path),
            )
        )
        self.enabled = self._client.enabled
        self.last_error: Optional[str] = None
        self.tool_manifest: List[dict] = []

    def server_params(self) -> StdioServerParameters:
        return self._client.server_params()

    def server_summary(self) -> dict:
        return {
            "name": "workspace",
            "command": sys.executable,
            "args": [str(self.server_path), *[str(root) for root in self.allowed_roots]],
            "enabled": self.enabled,
            "capabilities": ["workspace"],
            "access": "workspace",
            "source": str(self.server_path),
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "last_error": self.last_error,
        }

    async def list_tools(self, refresh: bool = False) -> List[dict]:
        tools = await self._client.list_tools(refresh=refresh)
        access = None if env_bool("MIKE_PROFILE_AUTH_ENABLED", False) else "any"
        for tool in tools:
            tool["access"] = access
        self.last_error = self._client.last_error
        self.tool_manifest = tools
        return tools

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        result = await self._client.call_tool(name, arguments)
        self.last_error = self._client.last_error
        return result


class MikeMcpHub:
    def __init__(
        self,
        workspace_client: Optional[MikeWorkspaceMcpClient] = None,
        extra_servers: Optional[List[MikeMcpServerConfig]] = None,
    ):
        self.workspace_client = workspace_client if workspace_client and workspace_client.enabled else None
        self.extra_clients = [
            (MikeHttpMcpClient(config) if config.url else MikeStdioMcpClient(config))
            for config in (extra_servers or [])
            if config.enabled and (config.command or config.url)
        ]
        self.tool_manifest: List[dict] = []
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.workspace_client or self.extra_clients)

    def server_summaries(self) -> List[dict]:
        summaries: List[dict] = []
        if self.workspace_client:
            summaries.append(self.workspace_client.server_summary())
        for client in self.extra_clients:
            summaries.append({
                "name": client.config.name,
                "command": client.config.command,
                "url": client.config.url,
                "transport": client.config.transport,
                "args": list(client.config.args),
                "enabled": client.enabled,
                "capabilities": list(client.config.capabilities),
                "access": client.config.access,
                "tool_prefix": client.config.tool_prefix,
                "source": client.config.source,
                "cwd": client.config.cwd,
                "last_error": client.last_error,
            })
        return summaries

    async def list_tools(self, refresh: bool = False) -> List[dict]:
        if self.tool_manifest and not refresh:
            return self.tool_manifest
        manifest: List[dict] = []
        errors: List[str] = []

        if self.workspace_client:
            workspace_tools = await self.workspace_client.list_tools(refresh=refresh)
            manifest.extend(workspace_tools)
            if self.workspace_client.last_error:
                errors.append(f"workspace: {self.workspace_client.last_error}")

        for client in self.extra_clients:
            tools = await client.list_tools(refresh=refresh)
            manifest.extend(tools)
            if client.last_error:
                errors.append(f"{client.config.name}: {client.last_error}")

        self.tool_manifest = manifest
        self.last_error = "; ".join(errors) if errors else None
        return manifest

    async def resolve_tool(self, name: str, refresh: bool = False) -> Optional[dict]:
        normalized = str(name or "").strip().replace(":", ".")
        if not normalized:
            return None
        manifest = self.tool_manifest or await self.list_tools(refresh=refresh)
        # 1) Exact match by prefixed name
        exact_matches = [tool for tool in manifest if tool.get("name") == normalized]
        if len(exact_matches) == 1:
            return exact_matches[0]
        # 2) Exact match by raw (unprefixed) name
        raw_matches = [tool for tool in manifest if tool.get("raw_name") == normalized]
        if len(raw_matches) == 1:
            return raw_matches[0]
        # 3) Underscore→dot normalization: DeepSeek sanitizes dots to underscores
        #    e.g. "sqlite_read_query" → try "sqlite.read_query"
        if "_" in normalized and "." not in normalized:
            dotted = normalized.replace("_", ".", 1)  # only first _ → .
            dotted_name = [tool for tool in manifest if tool.get("name") == dotted]
            if len(dotted_name) == 1:
                return dotted_name[0]
            dotted_raw = [tool for tool in manifest if tool.get("raw_name") == dotted]
            if len(dotted_raw) == 1:
                return dotted_raw[0]
        # 4) Fuzzy: strip any dot-prefix the LLM may have invented
        #    e.g. "workspace.write_file" -> try "write_file"
        if "." in normalized:
            suffix = normalized.rsplit(".", 1)[-1]
            suffix_name = [tool for tool in manifest if tool.get("name") == suffix]
            if len(suffix_name) == 1:
                return suffix_name[0]
            suffix_raw = [tool for tool in manifest if tool.get("raw_name") == suffix]
            if len(suffix_raw) == 1:
                return suffix_raw[0]
        if not refresh:
            return await self.resolve_tool(normalized, refresh=True)
        return None

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        tool = await self.resolve_tool(name)
        if tool is None:
            raise RuntimeError(f"MCP tool not found: {name}")
        server_name = tool.get("server_name")
        raw_name = tool.get("raw_name") or tool.get("name")
        if server_name == "workspace":
            if not self.workspace_client:
                raise RuntimeError("Workspace MCP server is not available")
            return await self.workspace_client.call_tool(raw_name, arguments)
        for client in self.extra_clients:
            if client.config.name == server_name:
                return await client.call_tool(raw_name, arguments)
        raise RuntimeError(f"MCP server not found for tool: {name}")

    def capability_summary(self, manifest: Optional[List[dict]] = None) -> dict:
        summary = {
            "workspace": False,
            "email": False,
            "calendar": False,
            "spreadsheet": False,
            "drive": False,
            "appointments": False,
            "huggingface": False,
            "command_execution": False,
        }
        for tool in manifest or self.tool_manifest:
            caps = {
                str(cap).strip().lower()
                for cap in (tool.get("capabilities") or [])
                if str(cap).strip()
            }
            name = str(tool.get("name") or "").lower()
            server_name = str(tool.get("server_name") or "").lower()
            if "workspace" in caps or server_name == "workspace":
                summary["workspace"] = True
            if "email" in caps or "gmail" in name or "email" in name:
                summary["email"] = True
            if "calendar" in caps or "agenda" in caps or "calendar" in name or "agenda" in name:
                summary["calendar"] = True
            if (
                "spreadsheet" in caps
                or "excel" in caps
                or server_name in {"excel", "spreadsheet"}
                or name.startswith("excel.")
                or "workbook" in name
            ):
                summary["spreadsheet"] = True
            if "drive" in caps or server_name == "drive" or name.startswith("drive."):
                summary["drive"] = True
            if (
                "appointments" in caps
                or server_name == "appointments"
                or name.startswith("appointments.")
            ):
                summary["appointments"] = True
            if (
                "huggingface" in caps
                or server_name == "huggingface"
                or name.startswith("hf.")
            ):
                summary["huggingface"] = True
            if name == "run_command" or name.endswith(".run_command"):
                summary["command_execution"] = True
        return summary


# ---------------------------------------------------------------------------
# Re-export tool-call parser functions (extracted to mike_tool_parser.py)
# ---------------------------------------------------------------------------
from core.server.mike_tool_parser import (  # noqa: E402
    TOOL_CALL_RE,
    _TOOL_CALL_PATTERNS,
    extract_tool_call,
    extract_tool_call_streaming,
    format_tool_manifest,
    render_tool_result_message,
    strip_tool_call_text,
    tool_instruction_block,
)
