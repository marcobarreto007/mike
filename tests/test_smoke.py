"""
Smoke tests mínimos — devem passar antes de qualquer refactor.
Uso: python tests/test_smoke.py [--port 8083]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8083)
parser.add_argument("--timeout", type=int, default=60)
parser.add_argument("--skip-chat", action="store_true")
args, _ = parser.parse_known_args()

BASE = f"http://127.0.0.1:{args.port}"
TIMEOUT = args.timeout
SKIP_CHAT = args.skip_chat or os.getenv("MIKE_SMOKE_SKIP_CHAT", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
PASS = 0
FAIL = 0


def _req(method: str, path: str, body=None, timeout=None) -> tuple[int, dict | str]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout or TIMEOUT)
        raw = r.read().decode("utf-8", errors="replace")
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, raw
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{' — ' + detail if detail else ''}")
    return ok


# ---------------------------------------------------------------------------
# 1. Health
# ---------------------------------------------------------------------------
print("\n=== 1. Health ===")
status, body = _req("GET", "/health")
check("GET /health -> 200", status == 200, f"got {status}")
check("/health status == healthy/ok", isinstance(body, dict) and body.get("status") in ("ok", "healthy"), str(body)[:80])
check("/health has model field", isinstance(body, dict) and bool(body.get("model")), str(body)[:80])

# ---------------------------------------------------------------------------
# 2. Chat completion
# ---------------------------------------------------------------------------
print("\n=== 2. Chat completion ===")
if SKIP_CHAT:
    check("chat completion skipped by config", True)
else:
    status, body = _req("POST", "/v1/chat/completions", body={
        "model": "mike",
        "stream": False,
        "messages": [{"role": "user", "content": "responda apenas: ok"}],
    }, timeout=120)
    check("POST /v1/chat/completions -> 200", status == 200, f"got {status}")
    # Servidor retorna choices (OpenAI compat) OU assistant_text (formato nativo)
    has_choices = isinstance(body, dict) and (bool(body.get("choices")) or bool(body.get("assistant_text")))
    check("response has choices/assistant_text", has_choices, str(body)[:120])
    if has_choices and body.get("choices"):
        text = body["choices"][0].get("message", {}).get("content", "")
        check("response has text content", bool(text), repr(text[:80]))

# ---------------------------------------------------------------------------
# 3. Tool manifest (write_file must be present — requires local=marco)
# ---------------------------------------------------------------------------
print("\n=== 3. Tool manifest ===")
status, body = _req("GET", "/v1/tools")
check("GET /v1/tools -> 200", status == 200, f"got {status}")
if isinstance(body, dict) and "tools" in body:
    names = {t.get("name") for t in body["tools"]}
elif isinstance(body, list):
    names = {t.get("name") for t in body}
else:
    names = set()
check("write_file in manifest (local=marco)",  "write_file" in names, f"tools: {sorted(names)[:5]}...")
check("edit_file in manifest",                 "edit_file" in names)
check("list_directory in manifest",            "list_directory" in names)

# ---------------------------------------------------------------------------
# 4. Memory — save + search
# ---------------------------------------------------------------------------
print("\n=== 4. Memory ===")
marker = f"smoke_test_{int(time.time())}"
save_status, save_body = _req("POST", "/v1/memory/add", body={"content": marker, "session_id": "smoke"})
check("POST /v1/memory -> 200 or 201", save_status in (200, 201), f"got {save_status}: {str(save_body)[:80]}")

time.sleep(1)
search_status, search_body = _req("GET", f"/v1/memory/search?q={marker}&k=3")
check("GET /v1/memory/search -> 200", search_status == 200, f"got {search_status}")
if isinstance(search_body, dict):
    results = search_body.get("results") or search_body.get("hits") or []
elif isinstance(search_body, list):
    results = search_body
else:
    results = []
found = any(marker in str(r) for r in results)
check("saved memory retrievable", found, f"results: {str(results)[:120]}")

# ---------------------------------------------------------------------------
# 5. E2E extras — stats, autonomia, dashboard, tools locais
# ---------------------------------------------------------------------------
print("\n=== 5. E2E extras ===")

# Stats
stat_status, stat_body = _req("GET", "/v1/../stats")  # sem auth path redirect
# tenta direto
stat_status, stat_body = _req("GET", "/stats")
check("GET /stats -> 200", stat_status == 200, f"got {stat_status}")
if isinstance(stat_body, dict):
    check("stats has cuda_detected bool", isinstance(stat_body.get("cuda_detected"), bool),
        str(stat_body.get("cuda_detected")))
    check("stats has llm_backend", bool(stat_body.get("llm_backend")),
        str(stat_body.get("llm_backend")))

# Autonomia
auto_status, auto_body = _req("GET", "/v1/autonomy/status")
check("GET /v1/autonomy/status -> 200", auto_status == 200, f"got {auto_status}")
if isinstance(auto_body, dict):
    check("autonomia ativa", auto_body.get("running") == True, str(auto_body.get("running")))

# mike.introspect
ti_status, ti_body = _req("POST", "/v1/tools/call", body={"name": "mike.introspect", "arguments": {}})
check("mike.introspect -> 200", ti_status == 200, f"got {ti_status}")
if isinstance(ti_body, dict):
    result_text = (ti_body.get("result") or {}).get("text", "") if isinstance(ti_body.get("result"), dict) else ""
    check("introspect retornou mapa de código", "PROJECT_ROOT" in result_text or "mike_server" in result_text,
          result_text[:80] if result_text else "sem texto")

# mike.hot_cache_list
hc_status, hc_body = _req("POST", "/v1/tools/call", body={"name": "mike.hot_cache_list", "arguments": {}})
check("mike.hot_cache_list -> 200", hc_status == 200, f"got {hc_status}")

# Compat legado: parameters ainda aceito
legacy_status, legacy_body = _req(
    "POST",
    "/v1/tools/call",
    body={"name": "list_directory", "parameters": {"path": "."}},
)
check("legacy parameters -> 200", legacy_status == 200, f"got {legacy_status}")
if isinstance(legacy_body, dict):
    result_text = (legacy_body.get("result") or {}).get("text", "") if isinstance(legacy_body.get("result"), dict) else ""
    check("legacy parameters executou tool", bool(result_text), "sem retorno de tool")

# Payload ambiguo deve falhar
conflict_status, conflict_body = _req(
    "POST",
    "/v1/tools/call",
    body={
        "name": "list_directory",
        "arguments": {"path": "."},
        "parameters": {"path": "core"},
    },
)
check("arguments+parameters conflitantes -> 422", conflict_status == 422, f"got {conflict_status}")
if isinstance(conflict_body, dict):
    check("conflict retorna mensagem clara", "apenas 'arguments'" in str(conflict_body.get("error", "")), str(conflict_body)[:120])

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
total = PASS + FAIL
print(f"\n{'='*40}")
result_tag = "OK" if FAIL == 0 else "FAILED"
print(f"  {PASS}/{total} passed [{result_tag}]")
print(f"{'='*40}\n")
sys.exit(0 if FAIL == 0 else 1)
