#!/usr/bin/env python3
"""Run 20 individual integration checks focused on memory and tools endpoints."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Tuple


BASE_URL = os.getenv("MIKE_BASE_URL", "http://127.0.0.1:8083")


def _http_json(method: str, path: str, payload: Dict[str, Any] | None = None) -> Tuple[int, Dict[str, Any]]:
    url = BASE_URL + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        parsed: Dict[str, Any]
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return exc.code, parsed


def _tool_call(name: str, arguments: Dict[str, Any] | None = None) -> Tuple[int, Dict[str, Any]]:
    payload = {"name": name, "arguments": arguments or {}}
    return _http_json("POST", "/v1/tools/call", payload)


def _extract_tools(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools = payload.get("tools")
    if isinstance(tools, list):
        return tools
    if isinstance(payload, list):
        return payload
    return []


def _tool_text(payload: Dict[str, Any]) -> str:
    return str(payload.get("result", {}).get("text", ""))


def _tool_ok(payload: Dict[str, Any]) -> bool:
    return bool(payload.get("result", {}).get("ok", False))


def run() -> int:
    run_id = str(int(time.time()))
    workspace_dir = f"F:\\mike\\runtime\\cache\\debug_tools_20_{run_id}"
    test_file = workspace_dir + "\\case.txt"
    memory_marker = f"debug-memory-tools-{run_id}"
    session_id = "debug-20-tests"
    checkpoint_label = f"debug-20-checkpoint-{run_id}"
    hot_cache_key = f"debug20-cache-{run_id}"

    state: Dict[str, Any] = {
        "tools_payload": {},
        "checkpoint_id": None,
    }

    tests: List[Tuple[str, Any]] = []

    def t(name: str):
        def _decorator(fn):
            tests.append((name, fn))
            return fn

        return _decorator

    @t("health endpoint returns 200")
    def _():
        status, payload = _http_json("GET", "/health")
        assert status == 200, f"status={status} payload={payload}"

    @t("tools catalog endpoint returns 200")
    def _():
        status, payload = _http_json("GET", "/v1/tools")
        assert status == 200, f"status={status}"
        state["tools_payload"] = payload

    @t("tools catalog contains memory.session_summary")
    def _():
        tools = _extract_tools(state["tools_payload"])
        names = {str(x.get("name", "")) for x in tools}
        assert "memory.session_summary" in names, "memory.session_summary missing"

    @t("tools catalog contains memory.checkpoint_save")
    def _():
        tools = _extract_tools(state["tools_payload"])
        names = {str(x.get("name", "")) for x in tools}
        assert "memory.checkpoint_save" in names, "memory.checkpoint_save missing"

    @t("list_allowed_directories tool returns workspace path")
    def _():
        status, payload = _tool_call("list_allowed_directories", {})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), f"tool not ok: {_tool_text(payload)}"
        text = _tool_text(payload)
        assert "F:\\mike" in text, f"unexpected allowed path text={text}"

    @t("create_directory tool creates debug folder")
    def _():
        status, payload = _tool_call("create_directory", {"path": workspace_dir})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)

    @t("write_file tool writes test file")
    def _():
        status, payload = _tool_call("write_file", {"path": test_file, "content": f"line-a-{run_id}"})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)

    @t("read_text_file tool reads written content")
    def _():
        status, payload = _tool_call("read_text_file", {"path": test_file})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)
        assert f"line-a-{run_id}" in _tool_text(payload), _tool_text(payload)

    @t("edit_file tool updates existing content")
    def _():
        status, payload = _tool_call(
            "edit_file",
            {"path": test_file, "old_text": f"line-a-{run_id}", "new_text": f"line-b-{run_id}"},
        )
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)

    @t("read_text_file confirms edited content")
    def _():
        status, payload = _tool_call("read_text_file", {"path": test_file})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)
        text = _tool_text(payload)
        assert f"line-b-{run_id}" in text and f"line-a-{run_id}" not in text, text

    @t("delete_file removes test file")
    def _():
        status, payload = _tool_call("delete_file", {"path": test_file})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)

    @t("read_text_file on deleted file returns expected error")
    def _():
        status, payload = _tool_call("read_text_file", {"path": test_file})
        assert status == 200, f"status={status}"
        assert not _tool_ok(payload), "expected tool failure after delete"
        assert "Path does not exist" in _tool_text(payload), _tool_text(payload)

    @t("memory add endpoint stores marker content")
    def _():
        status, payload = _http_json(
            "POST",
            "/v1/memory/add",
            {"content": f"conteudo {memory_marker}", "session_id": session_id},
        )
        assert status == 200, f"status={status} payload={payload}"
        assert str(payload.get("status")) == "ok", f"unexpected payload={payload}"

    @t("memory search endpoint retrieves marker content")
    def _():
        q = urllib.parse.quote(memory_marker)
        status, payload = _http_json("GET", f"/v1/memory/search?q={q}&k=5")
        assert status == 200, f"status={status} payload={payload}"
        results = payload.get("results", [])
        assert isinstance(results, list) and results, f"empty results payload={payload}"
        joined = "\n".join(str(r.get("content", "")) for r in results if isinstance(r, dict))
        assert memory_marker in joined, f"marker not found in search results: {joined}"

    @t("memory.session_summary tool accepts summary")
    def _():
        status, payload = _tool_call(
            "memory.session_summary",
            {"summary": f"Resumo de teste {run_id}", "topics": ["memoria", "tools", "debug"]},
        )
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)

    @t("memory.checkpoint_save creates checkpoint")
    def _():
        status, payload = _tool_call("memory.checkpoint_save", {"label": checkpoint_label})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)
        text = _tool_text(payload)
        match = re.search(r"ID:\s*(ckpt-[a-z0-9]+)", text)
        assert match, f"checkpoint id not found in text={text}"
        state["checkpoint_id"] = match.group(1)

    @t("memory.checkpoint_list includes saved checkpoint")
    def _():
        status, payload = _tool_call("memory.checkpoint_list", {"limit": 10})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)
        text = _tool_text(payload)
        assert checkpoint_label in text or str(state["checkpoint_id"]) in text, text

    @t("memory.checkpoint_restore handles invalid id")
    def _():
        status, payload = _tool_call("memory.checkpoint_restore", {"checkpoint_id": "ckpt-inexistente-20"})
        assert status == 200, f"status={status}"
        assert not _tool_ok(payload), "expected restore to fail with invalid id"
        assert "nao encontrado" in _tool_text(payload).lower(), _tool_text(payload)

    @t("mike.hot_cache_add stores key/content")
    def _():
        status, payload = _tool_call(
            "mike.hot_cache_add",
            {"key": hot_cache_key, "content": f"conteudo-cache-{run_id}", "tags": ["debug", "tools"]},
        )
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)

    @t("mike.hot_cache_list shows recently added key")
    def _():
        status, payload = _tool_call("mike.hot_cache_list", {})
        assert status == 200, f"status={status}"
        assert _tool_ok(payload), _tool_text(payload)
        assert hot_cache_key in _tool_text(payload), _tool_text(payload)

    results: List[Tuple[int, str, bool, str]] = []
    for idx, (name, fn) in enumerate(tests, start=1):
        try:
            fn()
            results.append((idx, name, True, "ok"))
        except AssertionError as exc:
            results.append((idx, name, False, str(exc)))
        except Exception as exc:  # pragma: no cover - integration safety
            results.append((idx, name, False, f"unexpected error: {exc}"))

    passed = sum(1 for _, _, ok, _ in results if ok)
    total = len(results)

    print("=" * 72)
    print(f"MEMORY + TOOLS TEST RUN (20 individual checks) | base={BASE_URL}")
    print("=" * 72)
    for idx, name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"[{idx:02d}] {status} - {name}")
        if not ok:
            print(f"     detail: {detail}")

    print("-" * 72)
    print(f"RESULT: {passed}/{total} passed")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
