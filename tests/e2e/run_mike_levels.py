# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

import json
import sys
import time
from pathlib import Path

import requests
import urllib.parse


BASE_URL = "http://127.0.0.1:8080"
ROOT = Path(__file__).resolve().parents[1]
WEB_CACHE_DIR = ROOT / "mike" / "knowledge" / "web_cache"


def fetch_json(path: str, method: str = "GET", body: dict | None = None, timeout: int = 120):
    response = requests.request(
        method=method,
        url=BASE_URL + path,
        json=body,
        headers={"Accept": "application/json"},
        timeout=(15, timeout),
    )
    response.raise_for_status()
    return response.json()


def fetch_stream_chat(body: dict, timeout: int = 180):
    chunks = []
    full_text = ""
    response = requests.post(
        BASE_URL + "/v1/chat/completions",
        json=body,
        headers={"Accept": "text/event-stream"},
        timeout=(15, timeout),
        stream=True,
    )
    response.raise_for_status()
    buffer = ""
    try:
        for raw in response.iter_content(chunk_size=None, decode_unicode=True):
            buffer += raw or ""
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                for line in event.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    chunks.append(line)
                    if line == "data: [DONE]":
                        return {"chunks": chunks, "text": full_text}
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[6:])
                    content = payload.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    full_text += content
    finally:
        response.close()
    return {"chunks": chunks, "text": full_text}


def assert_true(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


def print_level(level: int, title: str):
    print(f"\nLEVEL {level}: {title}")
    print("-" * (9 + len(title)))


def level_1_runtime_health():
    print_level(1, "Runtime Health")
    health = fetch_json("/health", timeout=30)
    stats = fetch_json("/stats", timeout=30)
    assert_true(health["status"] == "ok", "health status is not ok")
    assert_true(stats["cuda_detected"] is True, "cuda_detected should be true")
    assert_true(stats["gpu_name"], "gpu_name should be populated")
    assert_true(stats["web_search_provider"] == "ddgs", "DDGS should be active")
    assert_true(stats["web_search_ready"] is True, "web search should be ready")
    assert_true(stats["rag_enabled"] is True, "RAG should be enabled")
    print(json.dumps({"health": health, "stats_subset": {
        "gpu_name": stats["gpu_name"],
        "web_search_provider": stats["web_search_provider"],
        "knowledge_documents": stats["knowledge_documents"],
        "conversation_records": stats["conversation_records"],
    }}, ensure_ascii=False, indent=2))


def level_2_retrieval_and_reindex():
    print_level(2, "Retrieval and Reindex")
    reindex = fetch_json("/v1/knowledge/reindex", method="POST", body={}, timeout=180)
    knowledge = fetch_json("/v1/knowledge/search?" + urllib.parse.urlencode({"q": "roadmap ECC", "limit": 3}), timeout=60)
    memory = fetch_json("/v1/memory/search?" + urllib.parse.urlencode({"q": "ddgs search quickstart", "limit": 3}), timeout=60)
    assert_true(reindex["status"] == "ok", "reindex failed")
    assert_true(reindex["knowledge_documents"] >= 20, "knowledge_documents unexpectedly low")
    assert_true(len(knowledge["results"]) >= 1, "knowledge search returned no results")
    assert_true("ECC" in knowledge["results"][0]["content"] or "roadmap" in knowledge["results"][0]["content"].lower(), "knowledge result did not match query")
    assert_true(isinstance(memory["results"], list), "memory results should be a list")
    print(json.dumps({
        "reindex": {"knowledge_documents": reindex["knowledge_documents"], "knowledge_chunks": reindex["knowledge_chunks"]},
        "knowledge_hit": knowledge["results"][0],
        "memory_count": len(memory["results"]),
    }, ensure_ascii=False, indent=2))


def level_3_web_search_and_cache():
    print_level(3, "Web Search and Cache")
    before = {p.name for p in WEB_CACHE_DIR.glob("*.md")} if WEB_CACHE_DIR.exists() else set()
    result = fetch_json("/v1/web/search?" + urllib.parse.urlencode({"q": "ddgs python package quickstart", "limit": 2}), timeout=60)
    after = {p.name for p in WEB_CACHE_DIR.glob("*.md")} if WEB_CACHE_DIR.exists() else set()
    new_files = sorted(after - before)
    assert_true(result["provider"] in {"ddgs", "duckduckgo"}, "web provider should be ddgs or fallback duckduckgo")
    assert_true(result["web_search_ready"] is True, "web search should be ready")
    assert_true(len(result["results"]) >= 1, "web search returned no results")
    assert_true(result["cached_file"], "web search did not report cache file")
    assert_true(result["results"][0]["url"].startswith("https://"), "web result should include https url")
    assert_true(len(new_files) >= 1 or Path(result["cached_file"]).exists(), "web cache file was not created")
    print(json.dumps({
        "provider": result["provider"],
        "cached_file": result["cached_file"],
        "first_result": result["results"][0],
        "new_cache_files": new_files,
    }, ensure_ascii=False, indent=2))


def level_4_chat_non_stream():
    print_level(4, "Chat Non-Streaming")
    session_id = f"level-4-{int(time.time())}"
    body = {
        "model": "mike",
        "session_id": session_id,
        "messages": [
            {
                "role": "user",
                "content": "Sem inventar e em uma frase curta: o que e o pacote DDGS em Python com base no que voce recuperou?",
            }
        ],
        "max_tokens": 60,
        "temperature": 0.1,
        "stream": False,
    }
    response = fetch_json("/v1/chat/completions", method="POST", body=body, timeout=180)
    text = response["choices"][0]["message"]["content"]
    stats = fetch_json("/stats", timeout=30)
    history = fetch_json(
        "/v1/chat/history?" + urllib.parse.urlencode({"session_id": session_id}),
        timeout=60,
    )
    assert_true("DDGS" in text or "DuckDuckGo" in text or "Python" in text, "chat response did not mention DDGS/Python")
    assert_true(stats["total_requests"] >= 1, "total_requests did not increase")
    assert_true(stats["last_web_hits"] >= 1 or stats["last_knowledge_hits"] >= 1, "chat did not use retrieval/web context")
    assert_true(len(history["messages"]) == 2, "session history should contain user + assistant turn")
    print(json.dumps({
        "response": text,
        "stats_subset": {
            "last_web_hits": stats["last_web_hits"],
            "last_knowledge_hits": stats["last_knowledge_hits"],
            "total_requests": stats["total_requests"],
        },
    }, ensure_ascii=False, indent=2))


def level_5_chat_streaming_and_memory_loop():
    print_level(5, "Streaming and Memory Loop")
    before_stats = fetch_json("/stats", timeout=30)
    unique_phrase = f"teste-nivel-5-{int(time.time())}"
    session_id = f"level-5-{int(time.time())}"
    stream_result = fetch_stream_chat(
        {
            "model": "mike",
            "session_id": session_id,
            "messages": [
                {
                    "role": "user",
                    "content": f"Responde curto e repete exatamente esta ancora, sem adicionar nada: {unique_phrase}",
                }
            ],
            "max_tokens": 40,
            "temperature": 0,
            "stream": True,
        },
        timeout=120,
    )
    time.sleep(1)
    stats = fetch_json("/stats", timeout=30)
    history = fetch_json(
        "/v1/chat/history?" + urllib.parse.urlencode({"session_id": session_id}),
        timeout=60,
    )
    memory = fetch_json(
        "/v1/memory/search?" + urllib.parse.urlencode({"q": unique_phrase, "limit": 3, "session_id": session_id}),
        timeout=60,
    )
    assert_true(len(stream_result["chunks"]) >= 2, "streaming did not produce chunks")
    assert_true(unique_phrase in stream_result["text"], "streaming response lost the anchor phrase")
    assert_true(stats["total_requests"] == before_stats["total_requests"] + 1, "streaming request count did not increment")
    assert_true(stats["total_tokens_generated"] > before_stats["total_tokens_generated"], "streaming tokens were not counted")
    assert_true(len(history["messages"]) == 2, "streaming conversation was not persisted in session history")
    assert_true(any(unique_phrase in item["content"] for item in memory["results"]), "memory search did not retrieve the streamed conversation")
    print(json.dumps({
        "stream_text": stream_result["text"],
        "chunk_count": len(stream_result["chunks"]),
        "stats_subset": {
            "total_requests": stats["total_requests"],
            "total_tokens_generated": stats["total_tokens_generated"],
            "last_speed_tps": stats["last_speed_tps"],
            "last_web_hits": stats["last_web_hits"],
            "last_memory_hits": stats["last_memory_hits"],
        },
        "memory_results": memory["results"],
    }, ensure_ascii=False, indent=2))


def main():
    failures = []
    start = time.time()
    levels = [
        level_1_runtime_health,
        level_2_retrieval_and_reindex,
        level_3_web_search_and_cache,
        level_4_chat_non_stream,
        level_5_chat_streaming_and_memory_loop,
    ]

    for level_fn in levels:
        try:
            level_fn()
        except Exception as exc:  # noqa: BLE001
            failures.append({"level": level_fn.__name__, "error": str(exc)})
            print(f"FAILED: {level_fn.__name__}: {exc}", file=sys.stderr)

    print("\nSUMMARY")
    print("-------")
    print(json.dumps({
        "duration_seconds": round(time.time() - start, 2),
        "failures": failures,
        "status": "ok" if not failures else "failed",
    }, ensure_ascii=False, indent=2))

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
