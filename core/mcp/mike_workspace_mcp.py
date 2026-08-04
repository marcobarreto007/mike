# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Workspace MCP Server
=========================

Local filesystem MCP server restricted to explicitly allowed roots.
Exposes safe workspace-oriented tools for reading, writing, editing,
listing and deleting files/directories inside approved roots only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from mike_mcp_common import (
    collect_allowed_roots,
    normalize_path,
    resolve_safe_path,
)


mcp = FastMCP("Mike Workspace MCP", json_response=True)

ALLOWED_ROOTS = collect_allowed_roots(sys.argv[1:])


def _ensure_not_root(path: Path) -> None:
    for root in ALLOWED_ROOTS:
        if normalize_path(path) == normalize_path(root):
            raise ValueError(f"Refusing to delete allowed root: {path}")


@mcp.tool(description="List the directories this MCP server can access.")
def list_allowed_directories() -> list[str]:
    return [str(root) for root in ALLOWED_ROOTS]


@mcp.tool(description="Create a directory inside an allowed root.")
def create_directory(path: str) -> str:
    target = resolve_safe_path(path, ALLOWED_ROOTS)
    target.mkdir(parents=True, exist_ok=True)
    return f"Directory ready: {target}"


@mcp.tool(description="List files and directories at a path inside an allowed root.")
def list_directory(path: str = ".") -> list[str]:
    target = resolve_safe_path(path, ALLOWED_ROOTS, must_exist=True)
    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {target}")
    entries = []
    for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
        prefix = "[DIR]" if child.is_dir() else "[FILE]"
        entries.append(f"{prefix} {child.name}")
    return entries


@mcp.tool(description="Read a UTF-8 text file inside an allowed root.")
def read_text_file(path: str, head: Optional[int] = None, tail: Optional[int] = None) -> str:
    if head is not None and tail is not None:
        raise ValueError("Cannot use head and tail together.")
    target = resolve_safe_path(path, ALLOWED_ROOTS, must_exist=True)
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {target}")
    lines = target.read_text(encoding="utf-8-sig").splitlines()
    if head is not None:
        lines = lines[: max(0, head)]
    if tail is not None:
        lines = lines[-max(0, tail) :]
    return "\n".join(lines)


@mcp.tool(description="Create or overwrite a UTF-8 text file inside an allowed root.")
def write_file(path: str, content: str) -> str:
    target = resolve_safe_path(path, ALLOWED_ROOTS)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {target}"


@mcp.tool(description="Apply a text replacement to a file inside an allowed root.")
def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False, dry_run: bool = False) -> dict:
    target = resolve_safe_path(path, ALLOWED_ROOTS, must_exist=True)
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {target}")
    original = target.read_text(encoding="utf-8-sig")
    if old_text not in original:
        raise ValueError("old_text not found in target file.")
    if replace_all:
        updated = original.replace(old_text, new_text)
        replacements = original.count(old_text)
    else:
        updated = original.replace(old_text, new_text, 1)
        replacements = 1
    if not dry_run:
        target.write_text(updated, encoding="utf-8")
    return {
        "path": str(target),
        "dry_run": dry_run,
        "replacements": replacements,
        "changed": original != updated,
    }


@mcp.tool(description="Delete a file inside an allowed root.")
def delete_file(path: str, missing_ok: bool = False) -> str:
    target = resolve_safe_path(path, ALLOWED_ROOTS)
    if not target.exists():
        if missing_ok:
            return f"File already absent: {target}"
        raise FileNotFoundError(f"File not found: {target}")
    if not target.is_file():
        raise ValueError(f"Not a file: {target}")
    target.unlink()
    return f"Deleted file: {target}"


@mcp.tool(description="Delete a directory inside an allowed root. Recursive mode is required for non-empty directories.")
def delete_directory(path: str, recursive: bool = False, missing_ok: bool = False) -> str:
    target = resolve_safe_path(path, ALLOWED_ROOTS)
    _ensure_not_root(target)
    if not target.exists():
        if missing_ok:
            return f"Directory already absent: {target}"
        raise FileNotFoundError(f"Directory not found: {target}")
    if not target.is_dir():
        raise ValueError(f"Not a directory: {target}")
    if recursive:
        shutil.rmtree(target)
    else:
        target.rmdir()
    return f"Deleted directory: {target}"


@mcp.tool(description="Move or rename a file or directory inside allowed roots.")
def move_path(source: str, destination: str) -> str:
    src = resolve_safe_path(source, ALLOWED_ROOTS, must_exist=True)
    dst = resolve_safe_path(destination, ALLOWED_ROOTS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return f"Moved {src} -> {dst}"


@mcp.tool(description="Get basic metadata for a path inside an allowed root.")
def get_path_info(path: str) -> dict:
    target = resolve_safe_path(path, ALLOWED_ROOTS, must_exist=True)
    stat = target.stat()
    return {
        "path": str(target),
        "type": "directory" if target.is_dir() else "file",
        "size_bytes": stat.st_size,
        "modified_time": stat.st_mtime,
    }


@mcp.tool(
    description=(
        "Run a non-interactive PowerShell command from an allowed workspace "
        "directory. Output is capped and credential-like environment variables "
        "are removed. This is an owner-only operational tool."
    )
)
def run_command(
    command: str,
    cwd: str = ".",
    timeout_seconds: int = 60,
    max_output_chars: int = 20000,
) -> dict:
    if not str(command or "").strip():
        raise ValueError("command is required")
    # Kill-switch global: se o panic-stop estiver engaged, NENHUM comando e executado.
    _ks_block = None
    try:
        from mike_kill_switch import assert_not_frozen, KillSwitchEngaged
        try:
            assert_not_frozen(f"run_command: {str(command)[:80]}")
        except KillSwitchEngaged as exc:
            _ks_block = str(exc)
    except Exception:
        pass  # kill-switch indisponivel: nao bloqueia a tool (fail-open)
    if _ks_block:
        return {
            "exit_code": -1,
            "cwd": cwd,
            "output": f"[BLOCKED BY KILL-SWITCH] {_ks_block}",
            "truncated": False,
        }
    workdir = resolve_safe_path(cwd, ALLOWED_ROOTS, must_exist=True)
    if not workdir.is_dir():
        raise NotADirectoryError(f"Not a directory: {workdir}")
    timeout = max(1, min(int(timeout_seconds), 300))
    output_limit = max(1000, min(int(max_output_chars), 100000))
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not any(
            marker in key.upper()
            for marker in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "CREDENTIAL")
        )
    }
    
    # Platform-specific command execution
    if sys.platform == "win32":
        cmd_args = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            str(command),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        # Linux/Unix: use shell command
        cmd_args = ["/bin/bash", "-c", str(command)]
        creationflags = 0
    
    completed = subprocess.run(
        cmd_args,
        cwd=str(workdir),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=creationflags,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
    truncated = len(combined) > output_limit
    if truncated:
        combined = combined[:output_limit] + "\n...[output truncated]"
    return {
        "exit_code": completed.returncode,
        "cwd": str(workdir),
        "output": combined,
        "truncated": truncated,
    }


@mcp.tool(
    description=(
        "PowerShell-specific alias for run_command. Executes a non-interactive "
        "script in an allowed workspace directory with the same safety limits."
    )
)
def execute_powershell(
    script: str,
    cwd: str = ".",
    timeout_seconds: int = 60,
    max_output_chars: int = 20000,
) -> dict:
    return run_command(
        command=script,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
