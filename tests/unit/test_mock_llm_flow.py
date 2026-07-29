#!/usr/bin/env python
# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Test the Mock LLM backend and Model Router flow.

Validates:
  1. MikeMockLLM.chat_completion() — search, tool results, simple chat
  2. MikeMockLLM.chat_completion_stream() — chunk collection
  3. MikeModelRouter.select_backend() — simple/complex routing
  4. MikeModelRouter health checks and unhealthy marking
  5. MikeModelRouter.estimate_complexity()

Run:
    python tests/unit/test_mock_llm_flow.py
"""

import json
import os
import sys
from pathlib import Path

# Ensure project root modules are importable
_project_root = Path(__file__).resolve().parents[2]
_src = _project_root / "core" / "server"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASS = 0
_FAIL = 0


def _test(description: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"   PASS  {description}")
    else:
        _FAIL += 1
        print(f"   FAIL  {description}  -- {detail}")


def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# Part A — MikeMockLLM
# ---------------------------------------------------------------------------

def test_mock_llm_chat_completion():
    _section("Part A: MikeMockLLM — chat_completion()")

    from mike_mock_llm import MikeMockLLM

    mock = MikeMockLLM(name="mock")

    # ── Simple chat (greeting) ──
    msgs_greet = [
        {"role": "user", "content": "Ola, tudo bem?"},
    ]
    resp_greet = mock.chat_completion(msgs_greet)
    _test("Returns a dict", isinstance(resp_greet, dict))
    _test("Has 'choices' key", "choices" in resp_greet)
    _test("Has 'usage' key", "usage" in resp_greet)
    _test("Has one choice", len(resp_greet.get("choices", [])) == 1)
    msg = resp_greet["choices"][0]["message"]
    _test("Message has 'role': assistant", msg.get("role") == "assistant")
    _test("Message has 'content'", bool(msg.get("content")))
    _test("Usage tokens > 0", resp_greet["usage"]["completion_tokens"] > 0)
    _test("Usage tokens > 0", resp_greet["usage"]["prompt_tokens"] > 0)

    # ── Search request → should emit <tool_call> ──
    msgs_search = [
        {"role": "user", "content": "Pesquise sobre inteligencia artificial no Brasil"},
    ]
    resp_search = mock.chat_completion(msgs_search)
    content_s = resp_search["choices"][0]["message"]["content"]
    _test("Search triggers <tool_call>", "<tool_call>" in content_s,
          f"content={content_s[:80]}")
    _test("Tool call contains browse_search", "browse_search" in content_s,
          f"content={content_s[:80]}")
    tool_json_s = content_s.replace("<tool_call>", "").replace("</tool_call>", "").strip()
    tool_call_s = json.loads(tool_json_s)
    _test("Arguments has 'query'", "query" in tool_call_s.get("arguments", {}))

    # ── File read → emit <tool_call> with read_text_file ──
    msgs_read = [
        {"role": "user", "content": "Leia o arquivo de configuracao"},
    ]
    resp_read = mock.chat_completion(msgs_read)
    content_r = resp_read["choices"][0]["message"]["content"]
    _test("File read triggers <tool_call>", "<tool_call>" in content_r,
          f"content={content_r[:80]}")
    _test("Tool call is read_text_file", "read_text_file" in content_r)

    # ── File write → emit <tool_call> with write_file ──
    msgs_write = [
        {"role": "user", "content": "Crie um arquivo chamado teste.txt com ola mundo"},
    ]
    resp_write = mock.chat_completion(msgs_write)
    content_w = resp_write["choices"][0]["message"]["content"]
    _test("File write triggers <tool_call>", "<tool_call>" in content_w,
          f"content={content_w[:80]}")
    _test("Tool call is write_file", "write_file" in content_w)

    # ── Tool results → summarize ──
    msgs_tool = [
        {"role": "user", "content": "Pesquise sobre gatos"},
        {"role": "assistant", "content": '<tool_call>{"name":"browse_search","arguments":{"query":"gatos"}}</tool_call>'},
        {"role": "tool", "content": "Gatos sao felinos domesticos muito populares."},
    ]
    resp_tool = mock.chat_completion(msgs_tool)
    content_t = resp_tool["choices"][0]["message"]["content"]
    _test("Tool results summary response", len(content_t) > 20,
          f"content={content_t[:80]}")
    _test("No nested <tool_call> in summary", "<tool_call>" not in content_t)

    # ── Different invocations produce varied responses ──
    responses = []
    for _ in range(5):
        r = mock.chat_completion([{"role": "user", "content": "Ola"}])
        responses.append(r["choices"][0]["message"]["content"])
    unique = len(set(responses))
    _test("Varied responses across calls", unique >= 2,
          f"got {unique} unique responses from 5 calls")


def test_mock_llm_stream():
    _section("Part A: MikeMockLLM — chat_completion_stream()")

    from mike_mock_llm import MikeMockLLM

    mock = MikeMockLLM(name="mock")

    msgs = [{"role": "user", "content": "Ola Mike, como voce esta?"}]
    chunks = list(mock.chat_completion_stream(msgs))
    _test("Stream produces chunks", len(chunks) >= 2)

    # Check chunk structure
    first_chunk = chunks[0]
    _test("Chunk has 'id'", "id" in first_chunk)
    _test("Chunk has 'choices'", "choices" in first_chunk)
    _test("Chunk 'object' is chat.completion.chunk",
          first_chunk.get("object") == "chat.completion.chunk")

    # Last chunk should have finish_reason = "stop"
    last_chunk = chunks[-1]
    finish = last_chunk["choices"][0].get("finish_reason")
    _test("Last chunk finish_reason is 'stop'", finish == "stop",
          f"got {finish}")

    # Delta content from non-final chunks
    contents = []
    for c in chunks[:-1]:
        delta = c["choices"][0].get("delta", {})
        if "content" in delta and delta["content"]:
            contents.append(delta["content"])
    _test("Non-final chunks have content deltas", len(contents) > 0,
          f"got {len(contents)} content chunks")

    # Reconstruct text
    full_text = "".join(contents).strip()
    _test("Reconstructed text not empty", len(full_text) > 0,
          f"text='{full_text}'")

    # ── Stream with search request ──
    msgs_search = [{"role": "user", "content": "Busque por gatos na internet"}]
    chunks_s = list(mock.chat_completion_stream(msgs_search))
    delta_text = ""
    for c in chunks_s[:-1]:
        d = c["choices"][0].get("delta", {})
        delta_text += d.get("content", "")
    _test("Stream search emits <tool_call>", "<tool_call>" in delta_text)


# ---------------------------------------------------------------------------
# Part B — MikeModelRouter
# ---------------------------------------------------------------------------

def test_model_router():
    _section("Part B: MikeModelRouter — routing & health")

    from mike_mock_llm import MikeMockLLM
    from mike_model_router import MikeModelRouter

    mock_a = MikeMockLLM(name="mock_a")
    mock_b = MikeMockLLM(name="mock_b")

    backends = {"mock_a": mock_a, "mock_b": mock_b}
    priority = ["mock_a", "mock_b"]
    router = MikeModelRouter(backends=backends, priority_order=priority)

    # ── Basic properties ──
    _test("backends dict has both", set(router.backends.keys()) == {"mock_a", "mock_b"})
    _test("priority list correct", router.priority == ["mock_a", "mock_b"])
    _test("healthy_backends returns both",
          router.healthy_backends == ["mock_a", "mock_b"])

    # ── select_backend ──
    b_simple = router.select_backend("simple")
    _test("simple → cheapest (last)", b_simple == "mock_b",
          f"got {b_simple}")

    b_normal = router.select_backend("normal")
    _test("normal → first available", b_normal == "mock_a",
          f"got {b_normal}")

    b_complex = router.select_backend("complex")
    _test("complex → highest priority", b_complex == "mock_a",
          f"got {b_complex}")

    # ── Mark mock_a unhealthy; simple should still get mock_b,
    #     complex should now get mock_b (best remaining) ──
    router.set_unhealthy("mock_a")
    _test("is_healthy('mock_a') is False", not router.is_healthy("mock_a"))
    _test("is_healthy('mock_b') is True", router.is_healthy("mock_b"))

    b_simple2 = router.select_backend("simple")
    _test("simple after mock_a unhealthy → mock_b", b_simple2 == "mock_b",
          f"got {b_simple2}")

    b_complex2 = router.select_backend("complex")
    _test("complex after mock_a unhealthy → mock_b", b_complex2 == "mock_b",
          f"got {b_complex2}")

    # ── Restore health ──
    router.set_healthy("mock_a")
    _test("is_healthy('mock_a') restored", router.is_healthy("mock_a"))

    # ── health_status dict ──
    status = router.health_status()
    _test("health_status has both keys", set(status.keys()) == {"mock_a", "mock_b"})
    _test("health_status values are boolean", all(isinstance(v, bool) for v in status.values()))

    # ── get_backend ──
    _test("get_backend('mock_a') returns MikeMockLLM",
          isinstance(router.get_backend("mock_a"), MikeMockLLM))
    _test("get_backend('unknown') returns None",
          router.get_backend("unknown") is None)

    # ── resolve_backend ──
    name, client = router.resolve_backend(
        [{"role": "user", "content": "Ola"}], model_override="mock_b"
    )
    _test("resolve_backend with override → mock_b", name == "mock_b")
    _test("resolve_backend client isinstance MikeMockLLM",
          isinstance(client, MikeMockLLM))

    # model_override with unknown model falls back to routing
    name2, client2 = router.resolve_backend(
        [{"role": "user", "content": "Ola"}], model_override="nonexistent"
    )
    _test("resolve_backend unknown override → falls back to healthy",
          name2 in ("mock_a", "mock_b"))
    _test("resolve_backend unknown override → client not None",
          client2 is not None)

    # Without override, should route based on complexity
    name3, client3 = router.resolve_backend(
        [{"role": "user", "content": "Ola"}]
    )
    _test("resolve_backend no override → healthy backend",
          name3 in ("mock_a", "mock_b"))

    # ── All backends unhealthy → fallback ──
    router.set_unhealthy("mock_a")
    router.set_unhealthy("mock_b")
    name4 = router.select_backend("normal")
    _test("All unhealthy → falls back to priority[0]", name4 == "mock_a",
          f"got {name4}")

    router.set_healthy("mock_a")
    router.set_healthy("mock_b")


def test_estimate_complexity():
    _section("Part B: MikeModelRouter — estimate_complexity()")

    from mike_model_router import MikeModelRouter

    est = MikeModelRouter.estimate_complexity

    # Simple: short greeting
    msgs_simple = [{"role": "user", "content": "Ola"}]
    _test("Simple greeting → 'simple'", est(msgs_simple) == "simple",
          f"got {est(msgs_simple)}")

    # Normal: medium length with one complexity keyword
    msgs_normal = [{"role": "user", "content": "Explique como funciona um banco de dados SQL"}]
    result_n = est(msgs_normal)
    _test("Explain message not complex (score < 3)", result_n in ("simple", "normal"),
          f"got {result_n}")  # "explique" is 1 keyword → normal

    # Complex: multiple complexity keywords
    msgs_complex = [
        {"role": "user", "content": (
            "Analise a arquitetura do sistema e implemente uma estrategia "
            "detalhada para otimizar o banco de dados. Documente todo o processo."
        )},
    ]
    result_c = est(msgs_complex)
    _test("Complex message → 'complex'", result_c == "complex",
          f"got {result_c} (text has analise, arquitetura, implemente, estrategia, detalhada, otimizar, documente)")

    # High token count → complex  (need > 8000*4 = 32000 chars)
    big_text = "palavra " * 5000  # ~40 000 chars → ~10 000 tokens
    msgs_big = [{"role": "user", "content": big_text}]
    _test("Long message (>8000 est tokens) → 'complex'",
          est(msgs_big) == "complex",
          f"got {est(msgs_big)}, est_tokens={len(big_text)//4}")

    # High message count → complex
    msgs_many = [{"role": "user", "content": f"msg {i}"} for i in range(25)]
    _test("Many messages (>20) → 'complex'", est(msgs_many) == "complex",
          f"got {est(msgs_many)}")

    # Empty messages
    _test("Empty list → 'simple'", est([]) == "simple")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _PASS, _FAIL
    _PASS = 0
    _FAIL = 0

    test_mock_llm_chat_completion()
    test_mock_llm_stream()
    test_model_router()
    test_estimate_complexity()

    _section("Summary")
    total = _PASS + _FAIL
    print(f"  {_PASS} / {total} passed")
    if _FAIL:
        print(f"  {_FAIL} FAILURES")
        sys.exit(1)
    else:
        print("   All tests passed!")
    print()


if __name__ == "__main__":
    main()
