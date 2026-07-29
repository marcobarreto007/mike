# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Comprehensive tests for the MIKE tool-call loop WITHOUT real LLM calls.

Exercises:
- Single / multi-step tool calls
- No-tool fast path
- Max-steps exceeded
- Tool execution errors
- Malformed tool calls
- Streaming split detection
- Price hallucination guard
- Internet denial guard

All tests use MockGenerateFn and MockExecuteToolFn to simulate the LLM
and tool-execution layer.  Must be runnable standalone with:

    python tests/unit/test_tool_loop_flow.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the project root is on sys.path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

os.environ.setdefault("MIKE_MEM0_MODE", "off")
os.environ.setdefault("MIKE_MCP_MAX_STEPS", "3")

# ---------------------------------------------------------------------------
# Path hack – sys.path already set to project root above, but
# core/server and core/chat are not packages; we import their modules directly.
# ---------------------------------------------------------------------------
_core_server = _project_root / "core" / "server"
if str(_core_server) not in sys.path:
    sys.path.insert(0, str(_core_server))

_core_chat = _project_root / "core" / "chat"
if str(_core_chat) not in sys.path:
    sys.path.insert(0, str(_core_chat))


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class MockResponse:
    """Simulates an LLM response dict so _response_text() can consume it."""

    def __init__(self, text: str, completion_tokens: int = 100):
        self._text = text
        self._tokens = completion_tokens

    def to_dict(self) -> dict:
        return {
            "choices": [
                {"message": {"content": self._text}}
            ],
            "usage": {"completion_tokens": self._tokens},
        }


class MockGenerateFn:
    """Simulates the ``generate_fn`` used by the tool loop.

    Each call returns the next pre-programmed response.  When the list is
    exhausted it returns a simple "Done." message.
    """

    def __init__(self, responses: list[dict]):
        self.responses = responses  # list of {"assistant_text": str, "completion_tokens": int}
        self.call_count = 0

    async def __call__(self, messages, req=None, **kwargs):
        if self.call_count >= len(self.responses):
            return {"assistant_text": "Done.", "completion_tokens": 5}
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp


class MockExecuteToolFn:
    """Simulates tool execution.

    Maps tool names to pre-programmed results.  Records every call in
    ``self.calls``.
    """

    def __init__(self, tool_results: dict[str, dict]):
        self.tool_results = tool_results
        self.calls: list[dict] = []

    async def __call__(self, tool_name: str, arguments: dict):
        self.calls.append({"name": tool_name, "arguments": arguments})
        default = {"ok": True, "text": "mock result", "content_types": ["TextContent"]}
        return self.tool_results.get(tool_name, default)


# ---------------------------------------------------------------------------
# Minimal standalone tool loop (mirrors _generate_response_with_tools logic)
# ---------------------------------------------------------------------------


async def _run_tool_loop(
    messages: list[dict],
    generate_fn,
    execute_tool_fn,
    *,
    max_steps: int = 3,
    last_user_msg: str = "",
) -> dict:
    """A self-contained tool loop for testing, independent of the server.

    Parameters mirror the shape used by :func:`mike_server._generate_response_with_tools`
    but all dependencies are injected.
    """
    from mike_mcp_client import (
        extract_tool_call,
        strip_tool_call_text,
        render_tool_result_message,
    )
    from mike_completions import (
        _PRICE_PATTERN_RE,
        _contains_internet_denial,
        _looks_like_search_request,
    )
    from mike_token_budget import count_tokens as _count_tokens

    working_messages = list(messages)
    total_tokens = 0
    total_elapsed = 0.0
    tool_calls: list[dict] = []

    for step in range(max_steps + 1):
        response = await generate_fn(working_messages)
        assistant_text = response.get("assistant_text", "")
        total_tokens += response.get("completion_tokens", 0)

        tool_call = extract_tool_call(assistant_text) if step < max_steps else None

        if not tool_call:
            # Price hallucination guard
            if (
                step == 0
                and not tool_calls
                and _PRICE_PATTERN_RE.search(assistant_text)
                and _looks_like_search_request(last_user_msg)
            ):
                working_messages.append({"role": "assistant", "content": assistant_text})
                working_messages.append({
                    "role": "user",
                    "content": (
                        "ATENCAO: voce citou precos/valores sem ter chamado nenhuma tool. "
                        "Isso e alucinacao. Use browse_search ou scrape_url AGORA para buscar dados reais."
                    ),
                })
                continue

            # Internet denial guard
            if (
                step == 0
                and not tool_calls
                and _contains_internet_denial(assistant_text)
            ):
                working_messages.append({"role": "assistant", "content": assistant_text})
                working_messages.append({
                    "role": "user",
                    "content": (
                        "ERRO: voce disse que nao tem acesso a noticias/internet, mas isso e FALSO. "
                        "Voce TEM busca web ativa via tool. Gere AGORA um <tool_call> com browse_search "
                        "ou web.search para buscar a informacao que o Marco pediu."
                    ),
                })
                continue

            return {
                "assistant_text": assistant_text,
                "completion_tokens": total_tokens,
                "elapsed": total_elapsed,
                "tool_calls": tool_calls,
            }

        # Execute tool
        tool_result = await execute_tool_fn(tool_call["name"], tool_call["arguments"])
        tool_calls.append({
            "name": tool_call["name"],
            "arguments": tool_call["arguments"],
            "ok": tool_result.get("ok", False),
            "text": tool_result.get("text", ""),
        })

        # Feed result back into working messages
        clean_assistant = strip_tool_call_text(assistant_text)
        working_messages.append({"role": "assistant", "content": clean_assistant or "(chamei tool)"})
        working_messages.append({
            "role": "user",
            "content": render_tool_result_message(
                tool_call["name"], tool_call["arguments"], tool_result
            ),
        })

    final_text = "Limite de etapas de tool atingido."
    return {
        "assistant_text": final_text,
        "completion_tokens": total_tokens + _count_tokens(final_text),
        "elapsed": total_elapsed,
        "tool_calls": tool_calls,
    }


# ---------------------------------------------------------------------------
# Helpers to build mock tool-call text the LLM would emit
# ---------------------------------------------------------------------------

def _tc(name: str, arguments: dict) -> str:
    """Format a tagged tool call the way the LLM would."""
    payload = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)
    return f"<tool_call>{payload}</tool_call>"


def _tc_without_close(name: str, arguments: dict) -> str:
    """Format a tool call missing the closing tag."""
    payload = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)
    return f"<tool_call>{payload}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestToolLoopFlow(unittest.IsolatedAsyncioTestCase):
    """Exercises the tool loop with mock LLM and tool execution."""

    async def test_single_tool_call(self):
        """Mock LLM emits one <tool_call>, tool executed, result fed back, final answer."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("web.search", {"query": "Python 3.13"}), "completion_tokens": 20},
            {"assistant_text": "Python 3.13 was released in October 2024.", "completion_tokens": 15},
        ])
        exec_fn = MockExecuteToolFn({
            "web.search": {"ok": True, "text": "Python 3.13 release notes...", "content_types": ["TextContent"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "Tell me about Python 3.13"}],
            gen,
            exec_fn,
            last_user_msg="Tell me about Python 3.13",
        )

        self.assertEqual(gen.call_count, 2, "Should have called LLM twice (tool + final)")
        self.assertEqual(len(exec_fn.calls), 1, "Should have executed 1 tool")
        self.assertEqual(exec_fn.calls[0]["name"], "web.search")
        self.assertIn("Python 3.13", result["assistant_text"])
        self.assertEqual(len(result["tool_calls"]), 1)
        print("✅ test_single_tool_call PASSED")

    async def test_multi_step_tool_calls(self):
        """LLM calls tool A, gets result, calls tool B, then gives final answer."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("list_directory", {"path": "/tmp"}), "completion_tokens": 15},
            {"assistant_text": _tc("read_text_file", {"path": "/tmp/a.txt"}), "completion_tokens": 15},
            {"assistant_text": "The file contains the answer you're looking for.", "completion_tokens": 12},
        ])
        exec_fn = MockExecuteToolFn({
            "list_directory": {"ok": True, "text": "a.txt, b.txt", "content_types": ["TextContent"]},
            "read_text_file": {"ok": True, "text": "file contents here", "content_types": ["TextContent"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "What's in /tmp/a.txt?"}],
            gen,
            exec_fn,
            last_user_msg="What's in /tmp/a.txt?",
        )

        self.assertEqual(gen.call_count, 3, "Should have called LLM 3 times")
        self.assertEqual(len(exec_fn.calls), 2, "Should have executed 2 tools")
        self.assertEqual(exec_fn.calls[0]["name"], "list_directory")
        self.assertEqual(exec_fn.calls[1]["name"], "read_text_file")
        print("✅ test_multi_step_tool_calls PASSED")

    async def test_no_tool_call(self):
        """LLM responds directly without tools -- loop exits immediately."""
        gen = MockGenerateFn([
            {"assistant_text": "Hello! I'm Mike, how can I help?", "completion_tokens": 10},
        ])
        exec_fn = MockExecuteToolFn({})

        result = await _run_tool_loop(
            [{"role": "user", "content": "Hello!"}],
            gen,
            exec_fn,
        )

        self.assertEqual(gen.call_count, 1, "Should have called LLM exactly once")
        self.assertEqual(len(exec_fn.calls), 0, "Should have executed 0 tools")
        self.assertIn("Hello", result["assistant_text"])
        print("✅ test_no_tool_call PASSED")

    async def test_max_steps_exceeded(self):
        """LLM keeps calling tools forever -- loop stops at max_steps."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("web.search", {"query": "step 1"}), "completion_tokens": 10},
            {"assistant_text": _tc("web.search", {"query": "step 2"}), "completion_tokens": 10},
            {"assistant_text": _tc("web.search", {"query": "step 3"}), "completion_tokens": 10},
            # At step == max_steps, tool_call is None so this is treated as plain text
            {"assistant_text": _tc("web.search", {"query": "step 4"}), "completion_tokens": 10},
        ])
        exec_fn = MockExecuteToolFn({
            "web.search": {"ok": True, "text": "search result", "content_types": ["TextContent"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "Keep searching!"}],
            gen,
            exec_fn,
            max_steps=3,
        )

        # max_steps=3 means at most 3 tool-calling iterations.
        # Steps 0,1,2 produce tool calls; step 3 (== max_steps) treats tool_call as None.
        self.assertEqual(len(result["tool_calls"]), 3, "Should cap at max_steps tool calls")
        self.assertIn("step 4", result["assistant_text"], "At step==max_steps, raw text returned as-is")
        print("✅ test_max_steps_exceeded PASSED")

    async def test_tool_execution_error(self):
        """Tool returns error -- LLM sees error and adapts."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("send_email", {"to": "x@y.com", "subject": "Hello"}), "completion_tokens": 18},
            {"assistant_text": "I couldn't send the email because SMTP is not configured.", "completion_tokens": 14},
        ])
        exec_fn = MockExecuteToolFn({
            "send_email": {"ok": False, "text": "SMTP not configured", "content_types": ["error"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "Send an email to x@y.com"}],
            gen,
            exec_fn,
            last_user_msg="Send an email to x@y.com",
        )

        self.assertEqual(gen.call_count, 2)
        self.assertEqual(len(exec_fn.calls), 1)
        self.assertFalse(result["tool_calls"][0]["ok"], "Tool result should indicate failure")
        print("✅ test_tool_execution_error PASSED")

    async def test_malformed_tool_call(self):
        """LLM emits invalid XML -- extraction fails gracefully, no crash."""
        gen = MockGenerateFn([
            {"assistant_text": "<tool_call>this is not json</tool_call>", "completion_tokens": 10},
        ])
        exec_fn = MockExecuteToolFn({})

        result = await _run_tool_loop(
            [{"role": "user", "content": "Do something"}],
            gen,
            exec_fn,
        )

        # Malformed tool call -> extraction returns None -> treated as plain text
        self.assertEqual(gen.call_count, 1)
        # The raw malformed text is emitted as assistant_text (extraction failed gracefully)
        self.assertIn("not json", result["assistant_text"])
        print("✅ test_malformed_tool_call PASSED")

    async def test_streaming_tool_call_split(self):
        """Tool call XML split across chunks -- extract_tool_call_streaming detects partial."""
        from mike_mcp_client import extract_tool_call_streaming

        # Simulate: chunk1 has "<tool_call>", chunk2 has JSON, chunk3 has "</tool_call>"
        chunk1 = "Let me search.\n<tool_"
        chunk2 = 'call>{"name":"web.search","arguments":{"query":"test"}}'
        chunk3 = "</tool_call>\nDone."

        # After chunk1: no <tool_call> yet
        r1 = extract_tool_call_streaming(chunk1)
        self.assertFalse(r1["complete"], "Chunk 1 should be incomplete")
        self.assertEqual(r1.get("partial_text", ""), "", "No open tag yet")

        # After chunk1+2: complete JSON between <tool_call> and end-of-string.
        # Even without </tool_call>, the balanced JSON object is parseable,
        # so the streaming detector considers it complete.
        r2 = extract_tool_call_streaming(chunk1 + chunk2)
        self.assertTrue(r2["complete"], "Chunk 1+2 has balanced JSON -- complete even without </tool_call>")
        self.assertEqual(r2["tool_call"]["name"], "web.search")

        # After chunk1+2+3: full tool call with closing tag
        r3 = extract_tool_call_streaming(chunk1 + chunk2 + chunk3)
        self.assertTrue(r3["complete"], "Full tool call with closing tag")
        self.assertEqual(r3["tool_call"]["name"], "web.search")
        self.assertEqual(r3["tool_call"]["arguments"], {"query": "test"})
        print("✅ test_streaming_tool_call_split PASSED")

    async def test_streaming_tool_call_bare_json(self):
        """Extract tool call streaming handles bare JSON without tags."""
        from mike_mcp_client import extract_tool_call_streaming

        # DeepSeek sometimes emits bare JSON tool calls without XML tags
        text = '{"name":"browse_search","arguments":{"query":"latest news"}}'
        r = extract_tool_call_streaming(text)
        self.assertTrue(r["complete"], "Bare JSON tool call should be detected as complete")
        self.assertEqual(r["tool_call"]["name"], "browse_search")
        print("✅ test_streaming_tool_call_bare_json PASSED")

    async def test_price_hallucination_guard(self):
        """LLM emits prices without tool -- guard triggers retry."""
        gen = MockGenerateFn([
            {"assistant_text": "The iPhone 15 costs $799. You should buy it!", "completion_tokens": 20},
            {"assistant_text": _tc("web.search", {"query": "iPhone 15 price 2025"}), "completion_tokens": 15},
            {"assistant_text": "Based on search results, the iPhone 15 starts at $699.", "completion_tokens": 14},
        ])
        exec_fn = MockExecuteToolFn({
            "web.search": {"ok": True, "text": "iPhone 15: $699 - $799", "content_types": ["TextContent"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "Quanto custa o iPhone 15?"}],
            gen,
            exec_fn,
            last_user_msg="Quanto custa o iPhone 15?",
        )

        # First response has prices → guard triggers retry → LLM calls tool
        self.assertGreaterEqual(gen.call_count, 3, "Should retry after price hallucination")
        self.assertGreaterEqual(len(exec_fn.calls), 1, "Should eventually call a tool")
        print("✅ test_price_hallucination_guard PASSED")

    async def test_internet_denial_guard(self):
        """LLM says 'no internet' -- guard triggers retry."""
        gen = MockGenerateFn([
            # Portuguese denial pattern that matches _NO_INTERNET_DENIAL_RE
            {"assistant_text": "Não tenho acesso a notícias em tempo real. Meu conhecimento vai até 2024...", "completion_tokens": 25},
            {"assistant_text": _tc("web.search", {"query": "latest news"}), "completion_tokens": 15},
            {"assistant_text": "Based on search results, here are the latest news...", "completion_tokens": 14},
        ])
        exec_fn = MockExecuteToolFn({
            "web.search": {"ok": True, "text": "Breaking news: ...", "content_types": ["TextContent"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "Tell me the latest news"}],
            gen,
            exec_fn,
            last_user_msg="Tell me the latest news",
        )

        # First response denies internet → guard triggers retry → LLM calls tool
        self.assertGreater(gen.call_count, 1, "Should retry after internet denial")
        self.assertGreaterEqual(len(exec_fn.calls), 1, "Should eventually call a tool")
        print("✅ test_internet_denial_guard PASSED")


# ---------------------------------------------------------------------------
# Pure unit-tests for extract_tool_call variants (no async)
# ---------------------------------------------------------------------------

class TestExtractToolCall(unittest.TestCase):
    """Tests for extract_tool_call and extract_tool_call_streaming in isolation."""

    @classmethod
    def setUpClass(cls):
        # Ensure mike_mcp_client is importable
        _server_path = str(_project_root / "core" / "server")
        if _server_path not in sys.path:
            sys.path.insert(0, _server_path)

    def test_tagged_json_basic(self):
        from mike_mcp_client import extract_tool_call

        text = '<tool_call>{"name":"web.search","arguments":{"query":"test"}}</tool_call>'
        result = extract_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "web.search")
        self.assertEqual(result["arguments"], {"query": "test"})

    def test_tagged_json_no_close(self):
        from mike_mcp_client import extract_tool_call

        text = '<tool_call>{"name":"web.search","arguments":{"query":"test"}}'
        result = extract_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "web.search")

    def test_bare_json(self):
        from mike_mcp_client import extract_tool_call

        text = '{"name":"browse_search","arguments":{"query":"news"}}'
        result = extract_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "browse_search")

    def test_nested_json_arguments(self):
        from mike_mcp_client import extract_tool_call

        text = '<tool_call>{"name":"process","arguments":{"items":[{"id":1,"tags":["a","b"]}],"config":{"nested":{"deep":true}}}}</tool_call>'
        result = extract_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "process")
        self.assertEqual(result["arguments"]["items"][0]["id"], 1)
        self.assertTrue(result["arguments"]["config"]["nested"]["deep"])

    def test_malformed_missing_name(self):
        from mike_mcp_client import extract_tool_call

        text = '<tool_call>{"arguments":{"q":"test"}}</tool_call>'
        result = extract_tool_call(text)
        self.assertIsNone(result, "Missing 'name' field should return None")

    def test_malformed_arguments_not_dict(self):
        from mike_mcp_client import extract_tool_call

        text = '<tool_call>{"name":"web.search","arguments":"not a dict"}</tool_call>'
        result = extract_tool_call(text)
        self.assertIsNone(result, "Non-dict arguments should return None")

    def test_malformed_empty_text(self):
        from mike_mcp_client import extract_tool_call

        self.assertIsNone(extract_tool_call(""))
        self.assertIsNone(extract_tool_call(None))

    def test_malformed_garbage_json(self):
        from mike_mcp_client import extract_tool_call

        text = '<tool_call>{not valid json}</tool_call>'
        result = extract_tool_call(text)
        self.assertIsNone(result, "Invalid JSON should not crash")

    def test_python_function_style(self):
        from mike_mcp_client import extract_tool_call

        # Test with shorthand name "DDGS" → maps to "web.search_and_cache"
        text = '<tool_call>DDGS(query="Python 3.13")</tool_call>'
        result = extract_tool_call(text)
        self.assertIsNotNone(result, "Python-style tool call should be detected")
        self.assertEqual(result["name"], "web.search_and_cache")
        self.assertEqual(result["arguments"]["query"], "Python 3.13")

        # Test with fully-qualified name "web.search" (not in _TOOL_NAME_MAP)
        text2 = '<tool_call>web.search(query="Python 3.14")</tool_call>'
        result2 = extract_tool_call(text2)
        self.assertIsNotNone(result2, "Python-style fully-qualified name should work")
        self.assertEqual(result2["name"], "web.search")

    def test_streaming_detection_partial(self):
        from mike_mcp_client import extract_tool_call_streaming

        # Only opening tag, no content
        r = extract_tool_call_streaming("<tool_call>")
        self.assertFalse(r["complete"], "Just an opening tag should be incomplete")
        self.assertNotEqual(r.get("partial_text", ""), "")

        # Opening tag + partial JSON
        r2 = extract_tool_call_streaming('<tool_call>{"name":"web.search","arg')
        self.assertFalse(r2["complete"], "Partial JSON should be incomplete")

    def test_streaming_detection_complete_no_close_tag(self):
        from mike_mcp_client import extract_tool_call_streaming

        # Complete JSON between <tool_call> and end, just missing </tool_call>
        text = '<tool_call>{"name":"web.search","arguments":{"query":"test"}}'
        r = extract_tool_call_streaming(text)
        # Balanced JSON without close tag is detected as complete
        self.assertTrue(r["complete"], "Balanced JSON even without </tool_call> should be complete")

    def test__TOOL_CALL_PATTERNS_defined(self):
        from mike_mcp_client import _TOOL_CALL_PATTERNS
        self.assertIsInstance(_TOOL_CALL_PATTERNS, dict)
        self.assertGreaterEqual(len(_TOOL_CALL_PATTERNS), 6)
        expected = {"tagged_json", "markdown_code_block", "bare_json", "function_call_invoke", "legacy_llama", "python_style"}
        self.assertTrue(expected.issubset(set(_TOOL_CALL_PATTERNS.keys())), f"Missing patterns: {expected - set(_TOOL_CALL_PATTERNS.keys())}")
        print("✅ test__TOOL_CALL_PATTERNS_defined PASSED")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("MIKE Tool Loop Flow Tests (no LLM required)")
    print("=" * 60)
    unittest.main(verbosity=2)
