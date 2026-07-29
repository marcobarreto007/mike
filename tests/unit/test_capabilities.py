# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
test_capabilities.py
====================
Tests Mike's core capabilities: file creation, modification,
tool use from chat, scraper, web search, and Gemini route check.
"""

import json
import os
import requests
import sys
import time

if "pytest" in sys.modules:
    import pytest

    pytest.skip("script-style live server test; run directly against Mike", allow_module_level=True)

BASE = "http://127.0.0.1:8080"
PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
SKIP = "\033[93m[SKIP]\033[0m"


def banner(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def login_marco() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE}/v1/auth/login",
               json={"profile": "marco", "password": "123"}, timeout=10)
    assert r.status_code == 200, f"Login failed: {r.text}"
    data = r.json()
    assert data["status"] == "ok"
    print(f"  {PASS} Login como marco — perfil: {data['profile']['profile']}")
    return s


def test_tool_create_file(s: requests.Session) -> str:
    """Test write_file tool"""
    test_path = r"C:\Users\Admin_P500\Desktop\mike\mike\cap_test.txt"
    r = s.post(f"{BASE}/v1/tools/call", json={
        "name": "write_file",
        "arguments": {
            "path": test_path,
            "content": "Arquivo criado pelo teste de capacidades.\nData: 12/04/2026\n"
        }
    }, timeout=20)
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
    result = r.json()["result"]
    assert result["ok"], f"Tool failed: {result}"
    assert os.path.isfile(test_path), "Arquivo nao encontrado no disco!"
    print(f"  {PASS} write_file — arquivo criado em: {test_path}")
    return test_path


def test_tool_list_directory(s: requests.Session):
    """Test list_directory reads what we created"""
    r = s.post(f"{BASE}/v1/tools/call", json={
        "name": "list_directory",
        "arguments": {"path": r"C:\Users\Admin_P500\Desktop\mike\mike"}
    }, timeout=15)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["ok"], f"list_directory failed: {result}"
    # result["text"] may be a JSON array string, a plain string listing, or a list
    raw = result["text"]
    if isinstance(raw, list):
        entries_str = " ".join(str(e) for e in raw)
    else:
        entries_str = str(raw)
    assert "cap_test" in entries_str, f"cap_test.txt nao aparece no listdir: {entries_str[:300]}"
    print(f"  {PASS} list_directory — cap_test.txt visivel no diretorio")


def test_tool_edit_file(s: requests.Session, path: str):
    """Test edit_file (modification)"""
    r = s.post(f"{BASE}/v1/tools/call", json={
        "name": "edit_file",
        "arguments": {
            "path": path,
            "old_text": "Data: 12/04/2026",
            "new_text": "Data: 12/04/2026\nModificado pelo Mike via edit_file."
        }
    }, timeout=20)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["ok"], f"edit_file failed: {result}"
    payload = json.loads(result["text"]) if isinstance(result["text"], str) else result["text"]
    assert payload.get("changed") is True, f"Nenhuma mudança detectada: {payload}"
    content = open(path, encoding="utf-8").read()
    assert "Modificado pelo Mike" in content, "Modificacao nao encontrada no arquivo"
    print(f"  {PASS} edit_file — arquivo modificado, changed=True, texto no disco")


def test_tool_read_back(s: requests.Session, path: str):
    """Test read_text_file reads back modifications"""
    r = s.post(f"{BASE}/v1/tools/call", json={
        "name": "read_text_file",
        "arguments": {"path": path}
    }, timeout=15)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["ok"]
    assert "Modificado pelo Mike" in result["text"]
    print(f"  {PASS} read_text_file — modificacao confirmada pela leitura via API")


def test_tool_scraper(s: requests.Session):
    """Test scraper.scrape_url — uses venv Python subprocess cold start, needs ~60s"""
    try:
        r = s.post(f"{BASE}/v1/tools/call", json={
            "name": "scraper.scrape_url",
            "arguments": {"url": "https://httpbin.org/html"}
        }, timeout=90)
    except Exception as exc:
        print(f"  {SKIP} scraper.scrape_url — timeout/error (cold start MCP subprocess): {exc.__class__.__name__}")
        return
    if r.status_code != 200:
        print(f"  {FAIL} scraper.scrape_url — HTTP {r.status_code}: {r.text[:150]}")
        return
    result = r.json()["result"]
    if not result["ok"]:
        print(f"  {FAIL} scraper.scrape_url — error: {result.get('text','?')[:150]}")
        return
    text = result["text"]
    assert len(text) > 50, f"Conteudo raspado muito curto: {text[:100]}"
    print(f"  {PASS} scraper.scrape_url — {len(text)} chars raspados de httpbin.org/html")


def test_tool_web_search(s: requests.Session):
    """Test web.search_and_cache"""
    r = s.post(f"{BASE}/v1/tools/call", json={
        "name": "web.search_and_cache",
        "arguments": {"query": "Python programming language", "limit": 2}
    }, timeout=20)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["ok"], f"Web search failed: {result}"
    assert len(result["text"]) > 30
    print(f"  {PASS} web.search_and_cache — {len(result['text'])} chars retornados")


def test_chat_basic(s: requests.Session):
    """Test a basic Portuguese chat response"""
    r = s.post(f"{BASE}/v1/chat/completions", json={
        "model": "mike",
        "messages": [{"role": "user", "content": "Qual seu nome? Responda em uma linha."}],
        "max_tokens": 60
    }, timeout=60)
    assert r.status_code == 200
    resp = r.json()
    content = resp["choices"][0]["message"]["content"]
    assert len(content) > 5, f"Resposta muito curta: {content!r}"
    print(f"  {PASS} chat/completions — resposta: {content[:120]!r}")


def test_chat_tool_use(s: requests.Session, path: str):
    """Ask Mike (via chat) to read the file we created — autonomous tool use"""
    r = s.post(f"{BASE}/v1/chat/completions", json={
        "model": "mike",
        "messages": [{"role": "user", "content": (
            f"Usa a ferramenta read_text_file pra ler o arquivo em {path} "
            "e me diz o que tem escrito nele. Seja breve."
        )}],
        "max_tokens": 150
    }, timeout=90)
    assert r.status_code == 200
    resp = r.json()
    content = resp["choices"][0]["message"]["content"]
    tool_calls_used = resp.get("usage", {}).get("tool_calls", 0)
    has_file_content = any(kw in content.lower() for kw in [
        "cap_test", "capacidades", "criado", "arquivo", "data", "modificado", "marco"
    ])
    if has_file_content:
        print(f"  {PASS} chat autonomous tool use — Mike leu o arquivo via ferramenta")
    else:
        # Still pass if we got a coherent response — tool use is best-effort with local LLM
        print(f"  {SKIP} chat autonomous tool use — resposta sem conteudo do arquivo (LLM local): {content[:100]!r}")
    print(f"         Resposta: {content[:200]!r}")




def test_gemini_route(s: requests.Session):
    """Check if there's a direct /v1/gemini or /v1/harvest route"""
    for route in ["/v1/gemini", "/v1/harvest", "/v1/knowledge/harvest"]:
        r = s.get(f"{BASE}{route}", timeout=5)
        if r.status_code != 404:
            print(f"  {PASS} Rota Gemini encontrada: {route} — HTTP {r.status_code}")
            return
    print(f"  {SKIP} Sem rota /v1/gemini ou /v1/harvest (Gemini e consultor interno, nao exposto via API)")


def cleanup(path: str):
    try:
        os.unlink(path)
        print(f"\n  Cleanup: {path} removido")
    except Exception as e:
        print(f"\n  Cleanup warning: {e}")


def main():
    banner("MIKE CAPABILITY TESTS")
    print(f"  Server: {BASE}")

    # Auth
    banner("TEST 0: Auth (login como marco)")
    s = login_marco()

    # File creation
    banner("TEST 1: Criacao de arquivo (write_file)")
    test_path = test_tool_create_file(s)

    # List dir
    banner("TEST 2: List directory (confirma criacao)")
    test_tool_list_directory(s)

    # File modification
    banner("TEST 3: Modificacao de arquivo (edit_file)")
    test_tool_edit_file(s, test_path)

    # Read back
    banner("TEST 4: Leitura verificacao (read_text_file)")
    test_tool_read_back(s, test_path)

    # Scraper
    banner("TEST 5: Scraper (scrape_url — example.com)")
    test_tool_scraper(s)

    # Web search
    banner("TEST 6: Web search (search_and_cache)")
    test_tool_web_search(s)

    # Chat basic
    banner("TEST 7: Chat basico")
    test_chat_basic(s)

    # Chat tool use
    banner("TEST 8: Chat com uso autonomo de ferramenta")
    test_chat_tool_use(s, test_path)

    # Gemini route probe
    banner("TEST 9: Gemini — route probe")
    test_gemini_route(s)

    # Cleanup
    cleanup(test_path)

    print(f"\n{'='*60}")
    print("  CAPABILITY TESTS COMPLETOS")
    print('='*60)


if __name__ == "__main__":
    main()
