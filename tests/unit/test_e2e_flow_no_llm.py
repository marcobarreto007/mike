# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
End-to-end flow test for the MIKE chat completion pipeline WITHOUT real LLM.

Simulates the full request -> response pipeline using mock components.
Every external dependency (LLM, tools, memory, web search) is replaced with
mocks so the test passes without any backend configured.

Run standalone with:

    python tests/unit/test_e2e_flow_no_llm.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

# Ensure the project root and sub-module paths are on sys.path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

_core_server = _project_root / "core" / "server"
if str(_core_server) not in sys.path:
    sys.path.insert(0, str(_core_server))

_core_chat = _project_root / "core" / "chat"
if str(_core_chat) not in sys.path:
    sys.path.insert(0, str(_core_chat))

_core_memory = _project_root / "core" / "memory"
if str(_core_memory) not in sys.path:
    sys.path.insert(0, str(_core_memory))

_core_integrations = _project_root / "core" / "integrations"
if str(_core_integrations) not in sys.path:
    sys.path.insert(0, str(_core_integrations))

_core_orchestration = _project_root / "core" / "orchestration"
if str(_core_orchestration) not in sys.path:
    sys.path.insert(0, str(_core_orchestration))

_core_comms = _project_root / "core" / "comms"
if str(_core_comms) not in sys.path:
    sys.path.insert(0, str(_core_comms))

os.environ.setdefault("MIKE_MEM0_MODE", "off")
os.environ.setdefault("MIKE_MCP_MAX_STEPS", "3")

# We use the standalone tool loop from the companion test file instead of
# importing the full mike_server (which has heavy transitive deps).
from test_tool_loop_flow import (
    _run_tool_loop,
    MockGenerateFn,
    MockExecuteToolFn,
    _tc,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndToEndFlowNoLLM(unittest.TestCase):
    """Simulates the full HTTP request -> response pipeline without real LLM.

    Uses the standalone tool loop from test_tool_loop_flow to avoid importing
    the heavy mike_server module with all its transitive dependencies.
    """

    def test_basic_response_structure(self):
        """Simulate a non-streaming response with one tool call."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("web.search", {"query": "Python 3.13"}), "completion_tokens": 20},
            {"assistant_text": "Python 3.13 was released in October 2024.", "completion_tokens": 15},
        ])
        exec_fn = MockExecuteToolFn({
            "web.search": {"ok": True, "text": "Python 3.13 release notes...", "content_types": ["TextContent"]},
        })

        # This simulates what chat_completions() does internally
        result = asyncio.run(_run_tool_loop(
            [{"role": "user", "content": "Tell me about Python 3.13"}],
            gen,
            exec_fn,
            max_steps=3,
            last_user_msg="Tell me about Python 3.13",
        ))

        # Verify response structure
        self.assertIn("assistant_text", result)
        self.assertIn("completion_tokens", result)
        self.assertIn("tool_calls", result)
        self.assertIn("elapsed", result)

        self.assertIsInstance(result["assistant_text"], str)
        self.assertIsInstance(result["completion_tokens"], int)
        self.assertIsInstance(result["tool_calls"], list)

        print("✅ test_basic_response_structure PASSED")

    def test_no_tool_response_structure(self):
        """Simulate a response without any tool calls."""
        gen = MockGenerateFn([
            {"assistant_text": "Hello! I'm Mike, your loyal companion.", "completion_tokens": 10},
        ])
        exec_fn = MockExecuteToolFn({})

        result = asyncio.run(_run_tool_loop(
            [{"role": "user", "content": "Hello Mike!"}],
            gen,
            exec_fn,
        ))

        self.assertEqual(len(result["tool_calls"]), 0)
        self.assertIn("Mike", result["assistant_text"])
        self.assertGreater(result["completion_tokens"], 0)
        print("✅ test_no_tool_response_structure PASSED")

    def test_multi_tool_full_flow(self):
        """Simulate multiple tool calls in a single conversation turn."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("list_directory", {"path": "/tmp"}), "completion_tokens": 10},
            {"assistant_text": _tc("read_text_file", {"path": "/tmp/x.txt"}), "completion_tokens": 10},
            {"assistant_text": "The file contains important data.", "completion_tokens": 8},
        ])
        exec_fn = MockExecuteToolFn({
            "list_directory": {"ok": True, "text": "[\"x.txt\"]", "content_types": ["TextContent"]},
            "read_text_file": {"ok": True, "text": "file contents", "content_types": ["TextContent"]},
        })

        result = asyncio.run(_run_tool_loop(
            [{"role": "user", "content": "What's in /tmp/x.txt?"}],
            gen,
            exec_fn,
            max_steps=5,
        ))

        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertEqual(exec_fn.calls[0]["name"], "list_directory")
        self.assertEqual(exec_fn.calls[1]["name"], "read_text_file")
        self.assertIn("data", result["assistant_text"].lower())
        print("✅ test_multi_tool_full_flow PASSED")


class TestResponseFormatting(unittest.TestCase):
    """Tests for response formatting and protocol conversion."""

    def test_sse_event_format(self):
        """Verify SSE event formatting output (self-contained, no server import)."""
        import time

        # Replicate mike_server._sse_comment
        def _sse_comment(comment: str = "keep-alive") -> str:
            return f": {comment}\n\n"

        # Replicate mike_server._sse_event
        def _sse_event(payload: dict) -> str:
            return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        # Replicate mike_server._sse_content_chunk
        def _sse_content_chunk(rid: str, content: str) -> str:
            return _sse_event({
                "id": rid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "mike",
                "choices": [
                    {"index": 0, "delta": {"content": content}, "finish_reason": None}
                ],
            })

        # SSE comment
        comment = _sse_comment("keep-alive")
        self.assertIn("keep-alive", comment)
        self.assertTrue(comment.startswith(":"))

        # SSE event with data
        event = _sse_event({"test": "value"})
        self.assertTrue(event.startswith("data: "))
        self.assertTrue(event.endswith("\n\n"))
        parsed = json.loads(event[6:].rstrip("\n"))
        self.assertEqual(parsed["test"], "value")

        # SSE content chunk
        chunk = _sse_content_chunk("rid-123", "Hello")
        self.assertIn("rid-123", chunk)
        self.assertIn("Hello", chunk)

        print("✅ test_sse_event_format PASSED")

    def test_response_text_cleanup(self):
        """Verify _response_text handles the mock response format."""
        import mike_completions as _mc

        raw = {
            "choices": [{"message": {"content": "  Here is your answer.  "}}],
            "usage": {"completion_tokens": 5},
        }
        text = _mc.response_text(raw)
        self.assertIsInstance(text, str)
        self.assertIn("Here", text)
        print("✅ test_response_text_cleanup PASSED")

    def test_stream_delta_parsing(self):
        """Verify _response_stream_delta handles various chunk formats."""
        import mike_completions as _mc

        # OpenAI-style chunk
        self.assertEqual(_mc.response_stream_delta({
            "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
        }), "Hello")

        # Full message-style chunk
        self.assertEqual(_mc.response_stream_delta({
            "choices": [{"message": {"content": "World"}}],
        }), "World")

        # Empty delta
        self.assertEqual(_mc.response_stream_delta({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }), "")

        # Empty chunk
        self.assertEqual(_mc.response_stream_delta({}), "")

        # Text field (alternative format)
        self.assertEqual(_mc.response_stream_delta({
            "choices": [{"text": "AltText"}],
        }), "AltText")

        print("✅ test_stream_delta_parsing PASSED")

    def test_tool_call_protocol_roundtrip(self):
        """Verify tool call extraction, rendering, and stripping end-to-end."""
        from mike_mcp_client import (
            extract_tool_call,
            strip_tool_call_text,
            render_tool_result_message,
        )

        # LLM emits tool call
        raw = '<tool_call>{"name":"web.search","arguments":{"query":"Python"}}</tool_call>'

        # Extract
        tc = extract_tool_call(raw)
        self.assertIsNotNone(tc)
        self.assertEqual(tc["name"], "web.search")

        # Strip (removes tool call from conversation history)
        clean = strip_tool_call_text(raw)
        self.assertEqual(clean.strip(), "")

        # Render result for next LLM turn
        msg = render_tool_result_message(
            "web.search",
            {"query": "Python"},
            {"ok": True, "text": "Found: python.org"},
        )
        self.assertIn("RESULTADO DA TOOL", msg)
        self.assertIn("web.search", msg)
        self.assertIn("python.org", msg)

        print("✅ test_tool_call_protocol_roundtrip PASSED")

    def test_price_pattern_detection(self):
        """Verify price hallucination patterns are detected."""
        import mike_completions as _mc

        # These should trigger
        self.assertTrue(_mc._PRICE_PATTERN_RE.search("The price is $799.99"))
        self.assertTrue(_mc._PRICE_PATTERN_RE.search("Custa R$ 1500,00"))
        self.assertTrue(_mc._PRICE_PATTERN_RE.search("It's CAD 45.00"))
        self.assertTrue(_mc._PRICE_PATTERN_RE.search("USD 1234.56 is the price"))
        self.assertTrue(_mc._PRICE_PATTERN_RE.search("That's 99.99 EUR"))

        # These should not trigger
        self.assertFalse(_mc._PRICE_PATTERN_RE.search("No prices here"))
        self.assertFalse(_mc._PRICE_PATTERN_RE.search("The year is 2024"))
        self.assertFalse(_mc._PRICE_PATTERN_RE.search("Version 3.14"))

        print("✅ test_price_pattern_detection PASSED")

    def test_internet_denial_detection(self):
        """Verify internet denial patterns are detected."""
        import mike_completions as _mc

        denials = [
            "Não tenho acesso a notícias em tempo real",
            "Não consigo acessar a internet",
            "Não tenho acesso à internet",
            "Meu conhecimento vai até 2024",
            "Meu conhecimento se limita até 2024",
            "Meu conhecimento termina até 2023",
            "Não posso navegar na web",
            "Sem acesso à internet",
            "Não tenho capacidade de buscar",
        ]

        for denial in denials:
            self.assertTrue(
                _mc._contains_internet_denial(denial),
                f"Should detect denial: {denial!r}",
            )

        # Non-denials
        self.assertFalse(_mc._contains_internet_denial("Here are the latest news"))
        self.assertFalse(_mc._contains_internet_denial("Let me search the web for you"))
        self.assertFalse(_mc._contains_internet_denial(""))

        print("✅ test_internet_denial_detection PASSED")

    def test_stream_prefix_state(self):
        """Verify stream prefix state machine classifies text correctly (self-contained)."""
        import re

        # Replicate mike_server._REASONING_LEAK_PREFIXES (subset used by _stream_prefix_state)
        _REASONING_LEAK_PREFIXES = (
            "here's a thinking process:",
            "here's a thinking process:",
            "analyze user input:",
            "<think>",
        )
        _REASONING_ANYWHERE_RE = re.compile(
            r"(here[''']?s a thinking process|analy[sz]e user input|"
            r"<think>|</think>)",
            re.IGNORECASE,
        )

        _UNICODE_SMART_QUOTES_RE = re.compile(r"[`'‘’‚‛′]")

        def _normalize_reasoning_text(text: str) -> str:
            stripped = re.sub(r"^[\s]+", "", text or "")
            stripped = _UNICODE_SMART_QUOTES_RE.sub("'", stripped)
            return stripped.lower()

        def _stream_prefix_state(raw_text: str) -> str:
            stripped = (raw_text or "").lstrip()
            if not stripped:
                return "pending"

            lowered = _normalize_reasoning_text(raw_text)
            if not lowered:
                return "pending"

            if lowered.startswith("<think>"):
                return "reasoning"
            if "<think>"[:len(lowered)] == lowered and len(lowered) < 7:
                return "pending"

            for marker in _REASONING_LEAK_PREFIXES:
                if marker.startswith(lowered) and len(lowered) <= len(marker):
                    return "pending"
                if lowered.startswith(marker):
                    return "reasoning"

            if len(lowered) >= 30 and _REASONING_ANYWHERE_RE.search(lowered[:600]):
                return "reasoning"

            # Tool call detection
            tool_prefix = "<tool_call>"
            if tool_prefix.startswith(stripped):
                return "pending"
            if stripped.startswith(tool_prefix):
                return "tool"
            if stripped[0] == "<" and len(stripped) < len(tool_prefix):
                return "pending"
            if stripped.startswith("```"):
                if len(stripped) < 20:
                    return "pending"
                if '"name"' in stripped[:200]:
                    return "tool"
            if stripped[0] == "{":
                if len(stripped) < 16:
                    return "pending"
                if '"name"' in stripped[:200]:
                    return "tool"
            return "text"

        # Pending (not enough data)
        self.assertEqual(_stream_prefix_state(""), "pending")
        self.assertEqual(_stream_prefix_state("<"), "pending")
        self.assertEqual(_stream_prefix_state("<tool_"), "pending")

        # Tool
        self.assertEqual(_stream_prefix_state(
            '<tool_call>{"name":"web.search"'
        ), "tool")

        # Text
        self.assertEqual(_stream_prefix_state("Hello, I'm Mike!"), "text")
        self.assertEqual(_stream_prefix_state("Ola! Como vai?"), "text")

        # Reasoning
        self.assertEqual(_stream_prefix_state("<think>"), "reasoning")

        print("✅ test_stream_prefix_state PASSED")


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------


class TestEndToEndFlowAsync(unittest.IsolatedAsyncioTestCase):
    """Async end-to-end tests for the complete tool loop pipeline."""

    async def test_single_tool_e2e(self):
        """Full pipeline: user request -> tool call -> result -> final answer."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("web.search", {"query": "latest news"}), "completion_tokens": 15},
            {"assistant_text": "Here are the latest news from around the world...", "completion_tokens": 20},
        ])
        exec_fn = MockExecuteToolFn({
            "web.search": {"ok": True, "text": "Breaking: ...", "content_types": ["TextContent"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "What's the latest news?"}],
            gen,
            exec_fn,
            last_user_msg="What's the latest news?",
        )

        self.assertEqual(gen.call_count, 2)
        self.assertEqual(len(exec_fn.calls), 1)
        self.assertTrue(result["tool_calls"][0]["ok"])
        print("✅ test_single_tool_e2e PASSED")

    async def test_error_recovery_e2e(self):
        """Tool fails, LLM adapts, gives an honest answer."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("send_email", {"to": "x@y.com", "subject": "Hello"}), "completion_tokens": 15},
            {"assistant_text": "I tried to send the email but the SMTP is not configured. Please check with Marco.", "completion_tokens": 18},
        ])
        exec_fn = MockExecuteToolFn({
            "send_email": {"ok": False, "text": "SMTP not configured", "content_types": ["error"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "Send email to x@y.com"}],
            gen,
            exec_fn,
            last_user_msg="Send email to x@y.com",
        )

        self.assertFalse(result["tool_calls"][0]["ok"])
        self.assertIn("SMTP", result["assistant_text"] or result["tool_calls"][0]["text"])
        print("✅ test_error_recovery_e2e PASSED")

    async def test_max_steps_e2e(self):
        """LLM loops on tools, capped at max_steps."""
        gen = MockGenerateFn([
            {"assistant_text": _tc("web.search", {"query": "a"}), "completion_tokens": 5},
            {"assistant_text": _tc("web.search", {"query": "b"}), "completion_tokens": 5},
            {"assistant_text": _tc("web.search", {"query": "c"}), "completion_tokens": 5},
            {"assistant_text": _tc("web.search", {"query": "d"}), "completion_tokens": 5},
        ])
        exec_fn = MockExecuteToolFn({
            "web.search": {"ok": True, "text": "result", "content_types": ["TextContent"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "Keep searching"}],
            gen,
            exec_fn,
            max_steps=3,
        )

        self.assertEqual(len(result["tool_calls"]), 3)
        print("✅ test_max_steps_e2e PASSED")

    async def test_price_guard_e2e(self):
        """Price hallucination guard triggers retry in full pipeline."""
        gen = MockGenerateFn([
            {"assistant_text": "The MacBook Pro costs $1,299. Should I order it?", "completion_tokens": 18},
            {"assistant_text": _tc("web.search", {"query": "MacBook Pro price 2025"}), "completion_tokens": 15},
            {"assistant_text": "Based on search, the MacBook Pro starts at $999 on Apple.com.", "completion_tokens": 16},
        ])
        exec_fn = MockExecuteToolFn({
            "web.search": {"ok": True, "text": "MacBook Pro: $999+", "content_types": ["TextContent"]},
        })

        result = await _run_tool_loop(
            [{"role": "user", "content": "Quanto custa o MacBook Pro?"}],
            gen,
            exec_fn,
            last_user_msg="Quanto custa o MacBook Pro?",
        )

        self.assertGreater(gen.call_count, 1)
        self.assertGreaterEqual(len(exec_fn.calls), 1)
        print("✅ test_price_guard_e2e PASSED")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("MIKE End-to-End Flow Tests (no LLM required)")
    print("=" * 60)
    unittest.main(verbosity=2)
