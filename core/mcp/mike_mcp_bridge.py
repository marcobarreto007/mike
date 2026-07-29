# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike MCP Bridge Server
======================

Exposes MIKE's AI capabilities, memory, autonomy status, and task board
as native tools to Claude Code CLI and any MCP-compliant client.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Mike AI Bridge", json_response=True)

MIKE_BASE_URL = "http://127.0.0.1:8083"


def _http_get(endpoint: str) -> Dict[str, Any]:
    url = f"{MIKE_BASE_URL}{endpoint}"
    req = urllib.request.Request(url, headers={"User-Agent": "MikeMCPBridge/1.0"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_post(endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{MIKE_BASE_URL}{endpoint}"
    encoded = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", "User-Agent": "MikeMCPBridge/1.0"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


@mcp.tool()
def mike_get_status() -> str:
    """Retorna o status completo do MIKE, incluindo saúde do sistema e motor de autonomia."""
    try:
        health = _http_get("/health")
        autonomy = _http_get("/v1/autonomy/status")
        return json.dumps({"health": health, "autonomy": autonomy}, indent=2, ensure_ascii=False)
    except Exception as exc:
        return f"Erro ao conectar com o MIKE: {exc}"


@mcp.tool()
def mike_ask(prompt: str) -> str:
    """Envia uma pergunta ou instrução direta para o assistente MIKE e retorna sua resposta."""
    try:
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024
        }
        res = _http_post("/v1/chat/completions", payload)
        choices = res.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return json.dumps(res, ensure_ascii=False)
    except Exception as exc:
        return f"Erro ao comunicar com o MIKE: {exc}"


@mcp.tool()
def mike_list_tasks() -> str:
    """Lista todas as tarefas atuais na Lousa de Tarefas (Task Board) do MIKE."""
    try:
        autonomy = _http_get("/v1/autonomy/status")
        return json.dumps(autonomy.get("tasks", {}), indent=2, ensure_ascii=False)
    except Exception as exc:
        return f"Erro ao listar tarefas do MIKE: {exc}"


if __name__ == "__main__":
    mcp.run()
