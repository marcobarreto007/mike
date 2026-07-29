"""
Mike E2E Full — testa cada tool, memória e stress no pipeline.
Uso: python tests/e2e/test_e2e_full.py [--port 8083] [--url http://127.0.0.1:8083]
"""
from __future__ import annotations

if __name__ != "__main__":
    import pytest

    pytest.skip("live-server E2E script; run directly when needed", allow_module_level=True)

import io
import sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8083)
parser.add_argument("--url",  type=str, default="")
parser.add_argument("--timeout", type=int, default=90)
parser.add_argument("--tool-timeout", type=int, default=180)
parser.add_argument("--stress-workers", type=int, default=5)
parser.add_argument("--marco-password", type=str, default="", help="Senha do perfil Marco para testar write_file")
args, _ = parser.parse_known_args()

BASE    = args.url.rstrip("/") if args.url else f"http://127.0.0.1:{args.port}"
TIMEOUT = args.timeout
TOOL_TIMEOUT = args.tool_timeout

PASS = FAIL = SKIP = 0
RESULTS: list[dict] = []
_lock = threading.Lock()

CYAN  = "\033[96m"
GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW= "\033[93m"
RESET = "\033[0m"
BOLD  = "\033[1m"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(method: str, path: str, body=None, timeout=None, stream=False) -> tuple[int, object]:
    url  = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    if stream:
        hdrs["Accept"] = "text/event-stream"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        r   = urllib.request.urlopen(req, timeout=timeout or TIMEOUT)
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
    except Exception as exc:
        return 0, str(exc)


_MARCO_SESSION_COOKIE: str = ""

def _login_marco(password: str) -> bool:
    """Faz login como Marco e salva o session cookie para writes."""
    global _MARCO_SESSION_COOKIE
    body = json.dumps({"profile": "marco", "password": password}).encode()
    req  = urllib.request.Request(
        f"{BASE}/v1/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            for hdr in r.headers.get_all("Set-Cookie") or []:
                if "mike_session=" in hdr:
                    _MARCO_SESSION_COOKIE = hdr.split(";")[0].strip()
                    return True
            d = json.loads(r.read().decode())
            token = d.get("token") or d.get("session_token") or d.get("session_id") or ""
            if token:
                _MARCO_SESSION_COOKIE = f"mike_session={token}"
                return True
    except Exception:
        pass
    return False


def _stream_chat(messages: list[dict], timeout=None, with_marco_session: bool = False) -> dict:
    """Faz uma requisição streaming e retorna {text, tool_calls, keepalives, error}."""
    body = {
        "model": "mike",
        "stream": True,
        "messages": messages,
        "max_tokens": 512,
    }
    url  = f"{BASE}/v1/chat/completions"
    data = json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if with_marco_session and _MARCO_SESSION_COOKIE:
        hdrs["Cookie"] = _MARCO_SESSION_COOKIE
    req  = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    result = {"text": "", "tool_calls": [], "keepalives": 0, "error": None, "elapsed": 0.0}
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or TOOL_TIMEOUT) as r:
            buf = ""
            while True:
                chunk = r.read(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    part, buf = buf.split("\n\n", 1)
                    for line in part.split("\n"):
                        line = line.strip()
                        if line == ": keep-alive" or line.startswith(":"):
                            result["keepalives"] += 1
                            continue
                        if line == "data: [DONE]":
                            break
                        if not line.startswith("data: "):
                            continue
                        try:
                            d = json.loads(line[6:])
                            if d.get("error"):
                                result["error"] = str(d["error"])
                            delta = (d.get("choices") or [{}])[0].get("delta", {})
                            if delta.get("content"):
                                result["text"] += delta["content"]
                            tc = d.get("tool_calls")
                            if tc:
                                result["tool_calls"] = tc
                        except Exception:
                            pass
    except Exception as exc:
        result["error"] = str(exc)
    result["elapsed"] = round(time.time() - t0, 2)
    return result


def check(name: str, ok: bool, detail: str = "", section: str = "") -> bool:
    global PASS, FAIL
    with _lock:
        if ok:
            PASS += 1
            tag = f"{GREEN}PASS{RESET}"
        else:
            FAIL += 1
            tag = f"{RED}FAIL{RESET}"
        detail_str = f" — {detail}" if detail else ""
        print(f"  [{tag}] {name}{detail_str}")
        RESULTS.append({"section": section, "name": name, "ok": ok, "detail": detail})
    return ok


def skip(name: str, reason: str = "", section: str = ""):
    global SKIP
    with _lock:
        SKIP += 1
        print(f"  [{YELLOW}SKIP{RESET}] {name}{' — ' + reason if reason else ''}")
        RESULTS.append({"section": section, "name": name, "ok": None, "detail": reason})


def section(title: str):
    print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'='*60}{RESET}")


# ---------------------------------------------------------------------------
# 0. Wait for server
# ---------------------------------------------------------------------------
section("0. AGUARDANDO SERVIDOR")
print("  Tentando conectar...", end="", flush=True)
for attempt in range(30):
    s, b = _req("GET", "/health", timeout=5)
    if s == 200:
        print(f" OK ({attempt+1} tentativas)")
        break
    print(".", end="", flush=True)
    time.sleep(5)
else:
    print(f"\n{RED}Servidor não respondeu após 150s. Abortando.{RESET}")
    sys.exit(2)

# ---------------------------------------------------------------------------
# 1. Health & Info
# ---------------------------------------------------------------------------
section("1. HEALTH & INFO")
s, b = _req("GET", "/health")
check("GET /health -> 200",            s == 200,                        f"status={s}",  "health")
check("status == ok",                 isinstance(b, dict) and b.get("status") == "ok", str(b)[:60], "health")
check("model field presente",         isinstance(b, dict) and bool(b.get("model")),    str(b)[:60], "health")
check("mcp_tools_enabled == True",    isinstance(b, dict) and b.get("mcp_tools_enabled") is True, str(b)[:60], "health")
check("web_search_ready == True",     isinstance(b, dict) and b.get("web_search_ready") is True, str(b)[:60], "health")

s, b = _req("GET", "/v1/tools")
check("GET /v1/tools -> 200", s == 200, f"status={s}", "health")
if isinstance(b, dict) and "tools" in b:
    tool_names = {t.get("name") for t in b["tools"]}
elif isinstance(b, list):
    tool_names = {t.get("name") for t in b}
else:
    tool_names = set()
check(f"Manifest tem ≥10 tools ({len(tool_names)} encontradas)", len(tool_names) >= 10, str(sorted(tool_names))[:80], "health")

CORE_TOOLS = ["write_file", "edit_file", "read_text_file", "list_directory",
              "scraper.browse_search", "scraper.browse_open", "scraper.scrape_url",
              "email.list_inbox", "calendar.list_events"]
for t in CORE_TOOLS:
    check(f"  tool '{t}' no manifest", t in tool_names, section="health")

# ---------------------------------------------------------------------------
# 2. Chat direto (sem tools)
# ---------------------------------------------------------------------------
section("2. CHAT DIRETO (sem tools)")
s, b = _req("POST", "/v1/chat/completions", body={
    "model": "mike", "stream": False,
    "messages": [{"role": "user", "content": "responda apenas a palavra: PRONTO"}],
    "max_tokens": 32,
}, timeout=TIMEOUT)
check("POST /v1/chat/completions -> 200", s == 200, f"status={s}", "chat_direct")
if isinstance(b, dict) and b.get("choices"):
    txt = b["choices"][0].get("message", {}).get("content", "")
    check("resposta tem texto", bool(txt), repr(txt[:60]), "chat_direct")
    check("resposta não é vazia/erro", "error" not in txt.lower()[:20], txt[:60], "chat_direct")
else:
    check("resposta tem choices", False, str(b)[:100], "chat_direct")

# ---------------------------------------------------------------------------
# 3. Streaming (SSE)
# ---------------------------------------------------------------------------
section("3. STREAMING SSE")
r = _stream_chat([{"role": "user", "content": "responda apenas: STREAM OK"}])
check("streaming sem erro",       r["error"] is None,  r["error"] or "", "streaming")
check("streaming tem texto",      bool(r["text"]),      repr(r["text"][:60]), "streaming")
check("elapsed < 60s",            r["elapsed"] < 60,   f"{r['elapsed']}s", "streaming")
print(f"    texto recebido: {repr(r['text'][:80])!s}  |  elapsed: {r['elapsed']}s")

# ---------------------------------------------------------------------------
# 4. Tool — Workspace (arquivos locais)
# ---------------------------------------------------------------------------
section("4. TOOL — WORKSPACE (arquivos)")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
E2E_FILE_ABS = str(PROJECT_ROOT / "runtime" / "cache" / "e2e_test_marker.txt").replace("\\", "/")
MARKER       = f"e2e_marker_{int(time.time())}"

# Login como Marco (necessário para write_file)
_marco_logged_in = False
if args.marco_password:
    _marco_logged_in = _login_marco(args.marco_password)
    tag = "OK" if _marco_logged_in else "FALHOU"
    print(f"  [{'LOGIN ' + tag}] Marco login com --marco-password")

# 4a. Escrever arquivo via tool (exige sessão Marco)
if not _marco_logged_in:
    skip("write_file: arquivo criado em disco", "passe --marco-password=SENHA para testar writes", "workspace")
    skip("read_file: marker encontrado na resposta", "depende do write_file", "workspace")
else:
    r = _stream_chat([{
        "role": "user",
        "content": (
            f"USE A TOOL write_file AGORA. "
            f"Escreva o arquivo '{E2E_FILE_ABS}' com o conteúdo exato: '{MARKER}'. "
            f"Não explique, só execute a tool."
        ),
    }], with_marco_session=True)
    check("write_file: sem erro de stream",  r["error"] is None,   r["error"] or "",   "workspace")
    wrote = any(t.get("name") == "write_file" for t in r.get("tool_calls", []))
    check("write_file: tool_call presente",  wrote,                 str(r["tool_calls"])[:120], "workspace")
    file_ok = Path(E2E_FILE_ABS).exists()
    check("write_file: arquivo criado em disco", file_ok,          E2E_FILE_ABS[-50:], "workspace")

    # 4b. Ler arquivo via tool
    r = _stream_chat([{
        "role": "user",
        "content": f"USE A TOOL read_text_file para ler o arquivo '{E2E_FILE_ABS}'. Só execute, não explique.",
    }], with_marco_session=True)
    check("read_file: sem erro de stream",   r["error"] is None,   r["error"] or "",   "workspace")
    read_ok = any(t.get("name") == "read_text_file" for t in r.get("tool_calls", []))
    check("read_file: tool_call presente",   read_ok,              str(r["tool_calls"])[:120], "workspace")
    marker_in_resp = MARKER in r["text"] or any(MARKER in str(t) for t in r.get("tool_calls", []))
    check("read_file: marker encontrado na resposta", marker_in_resp, repr(r["text"][:100]), "workspace")

    try:
        Path(E2E_FILE_ABS).unlink(missing_ok=True)
    except Exception:
        pass

# 4c. Listar diretório (read-only, não precisa de Marco)
list_dir = str(PROJECT_ROOT / "runtime" / "cache").replace("\\", "/")
r = _stream_chat([{
    "role": "user",
    "content": f"USE A TOOL list_directory para listar '{list_dir}'. Execute agora.",
}])
check("list_dir: tool_call presente",
      any(t.get("name") == "list_directory" for t in r.get("tool_calls", [])),
      str(r["tool_calls"])[:100], "workspace")

# ---------------------------------------------------------------------------
# 5. Tool — Web Search
# ---------------------------------------------------------------------------
section("5. TOOL — WEB SEARCH")
r = _stream_chat([{
    "role": "user",
    "content": "USE A TOOL web_search ou browse_search AGORA para buscar: 'temperatura Montreal hoje celsius'. Execute a tool imediatamente.",
}], timeout=TOOL_TIMEOUT)
check("web_search: sem erro de stream", r["error"] is None, r["error"] or "", "web")
web_called = any(t.get("name") in ("web_search", "browse_search", "search_web",
                                      "scraper.browse_search", "web.search_and_cache") for t in r.get("tool_calls", []))
check("web_search: tool chamada",       web_called,         str(r["tool_calls"])[:150], "web")
check("web_search: resposta tem texto", bool(r["text"]),    repr(r["text"][:80]), "web")
check("web_search: elapsed < 120s",    r["elapsed"] < 120, f"{r['elapsed']}s", "web")
print(f"    resposta: {repr(r['text'][:100])}  |  elapsed: {r['elapsed']}s")

# ---------------------------------------------------------------------------
# 6. Tool — Scraper / Browse
# ---------------------------------------------------------------------------
section("6. TOOL — SCRAPER (browse_open / scrape_url)")
r = _stream_chat([{
    "role": "user",
    "content": "USE A TOOL browse_open ou scrape_url AGORA para acessar: 'https://wttr.in/Montreal?format=3'. Só execute a tool.",
}], timeout=TOOL_TIMEOUT)
check("scraper: sem erro de stream", r["error"] is None, r["error"] or "", "scraper")
scraper_called = any(t.get("name") in ("browse_open", "scrape_url", "browse_url",
                                          "scraper.browse_open", "scraper.scrape_url") for t in r.get("tool_calls", []))
check("scraper: tool chamada",       scraper_called,      str(r["tool_calls"])[:150], "scraper")
print(f"    elapsed: {r['elapsed']}s")

# ---------------------------------------------------------------------------
# 7. Tool — Email (list_inbox)
# ---------------------------------------------------------------------------
section("7. TOOL — EMAIL")
r = _stream_chat([{
    "role": "user",
    "content": "USE A TOOL email.list_inbox AGORA para listar meus emails mais recentes. Execute imediatamente.",
}], timeout=TOOL_TIMEOUT)
check("email: sem erro de stream",  r["error"] is None,  r["error"] or "",  "email")
email_called = any("email" in t.get("name", "").lower() or "inbox" in t.get("name", "").lower()
                   for t in r.get("tool_calls", []))
check("email: tool chamada",        email_called,         str(r["tool_calls"])[:150], "email")
print(f"    elapsed: {r['elapsed']}s")

# ---------------------------------------------------------------------------
# 8. Tool — Calendar
# ---------------------------------------------------------------------------
section("8. TOOL — CALENDAR")
r = _stream_chat([{
    "role": "user",
    "content": "USE A TOOL calendar.list_events AGORA para ver meus eventos desta semana. Execute imediatamente.",
}], timeout=TOOL_TIMEOUT)
check("calendar: sem erro de stream", r["error"] is None, r["error"] or "", "calendar")
cal_called = any("calendar" in t.get("name", "").lower() or "event" in t.get("name", "").lower()
                 for t in r.get("tool_calls", []))
check("calendar: tool chamada",       cal_called,          str(r["tool_calls"])[:150], "calendar")
print(f"    elapsed: {r['elapsed']}s")

# ---------------------------------------------------------------------------
# 9. Memória — save / search / RAG
# ---------------------------------------------------------------------------
section("9. MEMÓRIA")
mem_marker = f"mike_e2e_mem_{int(time.time())}"

# 9a. Salvar memória
s, b = _req("POST", "/v1/memory/add", body={"content": mem_marker, "session_id": "e2e"})
check("POST /v1/memory/add -> 2xx", s in (200, 201), f"status={s}: {str(b)[:60]}", "memory")

time.sleep(2)

# 9b. Buscar memória
s, b = _req("GET", f"/v1/memory/search?q={mem_marker}&k=3")
check("GET /v1/memory/search -> 200", s == 200, f"status={s}", "memory")
if isinstance(b, dict):
    results = b.get("results") or b.get("hits") or []
elif isinstance(b, list):
    results = b
else:
    results = []
found = any(mem_marker in str(r) for r in results)
check("marker recuperável na busca", found, f"results={str(results)[:100]}", "memory")

# 9c. Checkpoint save/list
s, b = _req("POST", "/v1/memory/checkpoint/save", body={"session_id": "e2e", "label": "e2e_test"})
check("checkpoint save -> 2xx", s in (200, 201), f"status={s}: {str(b)[:60]}", "memory")

s, b = _req("GET", "/v1/memory/checkpoint/list?session_id=e2e")
check("checkpoint list -> 200", s == 200, f"status={s}: {str(b)[:60]}", "memory")

# 9d. RAG via chat
r = _stream_chat([{
    "role": "user",
    "content": f"O que você sabe sobre '{mem_marker}'? Consulte sua memória.",
}])
check("RAG: sem erro de stream", r["error"] is None, r["error"] or "", "memory")
check("RAG: resposta tem texto",  bool(r["text"]),   repr(r["text"][:60]), "memory")

# ---------------------------------------------------------------------------
# 10. Stress — Concurrent requests
# ---------------------------------------------------------------------------
section("10. STRESS — REQUISIÇÕES CONCORRENTES")
N_WORKERS = args.stress_workers

def _concurrent_request(i: int) -> dict:
    t0 = time.time()
    s, b = _req("POST", "/v1/chat/completions", body={
        "model": "mike", "stream": False,
        "messages": [{"role": "user", "content": f"responda apenas: worker{i}"}],
        "max_tokens": 16,
    }, timeout=TOOL_TIMEOUT)
    elapsed = round(time.time() - t0, 2)
    ok = s == 200 and isinstance(b, dict) and bool(b.get("choices"))
    return {"i": i, "ok": ok, "status": s, "elapsed": elapsed}

print(f"  Disparando {N_WORKERS} requisições simultâneas...")
t_stress = time.time()
with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
    futures = [pool.submit(_concurrent_request, i) for i in range(N_WORKERS)]
    results_stress = [f.result() for f in as_completed(futures)]
elapsed_stress = round(time.time() - t_stress, 2)

ok_count  = sum(1 for r in results_stress if r["ok"])
fail_count = N_WORKERS - ok_count
max_elapsed = max(r["elapsed"] for r in results_stress)
avg_elapsed = round(sum(r["elapsed"] for r in results_stress) / N_WORKERS, 2)

for r in sorted(results_stress, key=lambda x: x["i"]):
    tag = f"{GREEN}OK{RESET}" if r["ok"] else f"{RED}FAIL{RESET}"
    print(f"    worker{r['i']}: [{tag}] status={r['status']} elapsed={r['elapsed']}s")

check(f"Concurrent: {ok_count}/{N_WORKERS} OK",        ok_count == N_WORKERS,  f"{fail_count} falhas", "stress")
check(f"Concurrent: max latência < 180s",              max_elapsed < 180,      f"max={max_elapsed}s", "stress")
print(f"  Total wall time: {elapsed_stress}s | avg: {avg_elapsed}s | max: {max_elapsed}s")

# ---------------------------------------------------------------------------
# 11. Stress — Streaming com keepalive durante tool
# ---------------------------------------------------------------------------
section("11. STRESS — STREAMING COM TOOL (keepalive check)")
r = _stream_chat([{
    "role": "user",
    "content": (
        "Primeiro USE A TOOL web_search para buscar 'hora atual Montreal'. "
        "Depois responda com o resultado."
    ),
}], timeout=TOOL_TIMEOUT)
check("stream+tool: sem erro",         r["error"] is None, r["error"] or "",       "stress_stream")
check("stream+tool: tool chamada ou resposta direta",
      bool(r["tool_calls"]) or bool(r["text"]), str(r["tool_calls"])[:100], "stress_stream")
check("stream+tool: resposta tem texto", bool(r["text"]),  repr(r["text"][:60]),    "stress_stream")
check("stream+tool: elapsed < 180s",   r["elapsed"] < 180, f"{r['elapsed']}s",     "stress_stream")
print(f"  keepalives recebidos: {r['keepalives']}  |  elapsed: {r['elapsed']}s")

# ---------------------------------------------------------------------------
# 12. Stress — Contexto longo
# ---------------------------------------------------------------------------
section("12. STRESS — CONTEXTO LONGO")
long_msg = "Analise este texto sem usar tools: " + ("Lorem ipsum " * 300) + ". Responda apenas: LONGO OK"
s, b = _req("POST", "/v1/chat/completions", body={
    "model": "mike", "stream": False,
    "messages": [{"role": "user", "content": long_msg}],
    "max_tokens": 32,
}, timeout=TOOL_TIMEOUT)
check("long context -> 200",      s == 200,    f"status={s}", "stress_long")
if isinstance(b, dict) and b.get("choices"):
    txt = b["choices"][0].get("message", {}).get("content", "")
    check("long context: tem resposta", bool(txt), repr(txt[:60]), "stress_long")

# ---------------------------------------------------------------------------
# Resumo Final
# ---------------------------------------------------------------------------
total = PASS + FAIL + SKIP
print(f"\n{BOLD}{'='*60}{RESET}")
result_tag = f"{GREEN}ALL PASSED{RESET}" if FAIL == 0 else f"{RED}{FAIL} FAILED{RESET}"
print(f"{BOLD}  RESULTADO: {PASS} passou | {FAIL} falhou | {SKIP} pulado  [{result_tag}]{RESET}")
print(f"{BOLD}{'='*60}{RESET}\n")

# Salva relatório JSON
report = {
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "base_url":  BASE,
    "total": total, "pass": PASS, "fail": FAIL, "skip": SKIP,
    "success": FAIL == 0,
    "results": RESULTS,
}
report_path = Path(__file__).parent / "e2e_report.json"
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  Relatório salvo em: {report_path}\n")

sys.exit(0 if FAIL == 0 else 1)
