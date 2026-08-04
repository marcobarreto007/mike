# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike - Teste Completo Isolado do Modelo
========================================
Mata processos anteriores, sobe o servidor, e testa TODOS os endpoints
e tools disponíveis. Pergunta ao modelo e exercita cada capacidade.

Uso:
    python tests/test_mike_full_isolated.py
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
import textwrap

if "pytest" in sys.modules:
    import pytest

    pytest.skip("full isolated live-server test; run directly when needed", allow_module_level=True)

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = "http://127.0.0.1:8083"
STARTUP_TIMEOUT = 240  # segundos para o modelo carregar
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER_SCRIPT = os.path.join(PROJECT_ROOT, "core", "server", "mike_server.py")
PYTHON = sys.executable

PASS = 0
FAIL = 0
SKIP = 0
RESULTS = []
server_proc = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def banner(text):
    print(f"\n{'='*64}")
    print(f"  {text}")
    print(f"{'='*64}")


def section(text):
    print(f"\n--- {text} ---")


def http(method, path, body=None, timeout=180, expect_status=200):
    """Faz uma requisição HTTP e retorna (status, json_or_text)."""
    url = f"{BASE}{path}"
    try:
        if body is not None:
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
        else:
            req = urllib.request.Request(url)
        req.get_method = lambda: method
        r = urllib.request.urlopen(req, timeout=timeout)
        content = r.read().decode("utf-8", errors="replace")
        try:
            return r.status, json.loads(content)
        except Exception:
            return r.status, content
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            return e.code, json.loads(body_err)
        except Exception:
            return e.code, body_err
    except Exception as exc:
        return 0, str(exc)


def is_server_alive():
    """Verifica se o servidor está vivo."""
    try:
        status, data = http("GET", "/health", timeout=5)
        return status == 200
    except Exception:
        return False


def wait_for_server(timeout=STARTUP_TIMEOUT):
    """Espera o servidor responder."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        if server_proc and server_proc.poll() is not None:
            return False
        if is_server_alive():
            return True
    return False


def ensure_server():
    """Garante que o servidor está rodando, reinicia se necessário."""
    global server_proc
    if is_server_alive():
        return True
    print("  ⚠️  Servidor caiu! Tentando reiniciar...")
    start_server()
    if wait_for_server():
        print("  ✅ Servidor reiniciado com sucesso!")
        return True
    else:
        print("  ❌ Falha ao reiniciar servidor")
        return False


def test(name, method, path, body=None, expect_status=200, timeout=180):
    """Executa um teste e registra resultado."""
    global PASS, FAIL
    status, data = http(method, path, body=body, timeout=timeout)
    if status == expect_status:
        PASS += 1
        tag = "PASS"
    else:
        FAIL += 1
        tag = "FAIL"
    preview = ""
    if isinstance(data, dict):
        preview = json.dumps(data, ensure_ascii=False)[:250]
    elif isinstance(data, str):
        preview = data[:250]
    print(f"  [{'✅' if tag=='PASS' else '❌'}] {name} → {status} | {preview}")
    RESULTS.append((name, tag, status))
    return data


def test_chat(prompt, label=None, max_tokens=200, stream=False, raw_mode=False,
              private_mode=False, session_id="test-isolated"):
    """Envia um chat pro Mike e mostra a resposta."""
    global PASS, FAIL
    name = label or f"Chat: {prompt[:50]}..."

    # Verifica se o servidor está vivo antes de cada chat
    if not is_server_alive():
        if not ensure_server():
            FAIL += 1
            print(f"  [❌] {name} → SKIP (servidor caiu)")
            RESULTS.append((name, "FAIL", "server_down"))
            return None

    body = {
        "model": "mike",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": stream,
        "session_id": session_id,
    }
    if raw_mode:
        body["raw_mode"] = True
    if private_mode:
        body["private_mode"] = True

    t0 = time.time()
    status, data = http("POST", "/v1/chat/completions", body=body, timeout=180)
    elapsed = time.time() - t0

    if status == 200 and isinstance(data, dict) and data.get("choices"):
        PASS += 1
        msg = data["choices"][0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        tps = usage.get("completion_tokens", 0) / max(elapsed, 0.01)
        tool_calls = data.get("tool_calls", [])
        print(f"  [✅] {name} → {status} ({elapsed:.1f}s, {tps:.1f} tok/s)")
        for line in textwrap.wrap(msg[:500], width=80):
            print(f"       💬 {line}")
        if tool_calls:
            print(f"       🔧 Tools usadas: {len(tool_calls)}")
            for tc in tool_calls:
                print(f"          → {tc.get('name', '?')}")
        RESULTS.append((name, "PASS", status))
        return data
    else:
        FAIL += 1
        preview = json.dumps(data, ensure_ascii=False)[:200] if isinstance(data, dict) else str(data)[:200]
        print(f"  [❌] {name} → {status} | {preview}")
        RESULTS.append((name, "FAIL", status))
        return data


def kill_mike_processes():
    """Mata todos os processos Mike anteriores."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["powershell", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Where-Object { $_.Name -match '^python(\\.exe)?$' -and $_.CommandLine -match 'mike_server\\.py' } | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
                 "Write-Host \"Killed PID $($_.ProcessId)\" }"],
                capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip():
                print(f"  Processos mortos: {result.stdout.strip()}")
            else:
                print("  Nenhum processo Mike anterior encontrado.")
        except Exception as exc:
            print(f"  Aviso: erro ao matar processos: {exc}")
    else:
        os.system("pkill -f mike_server.py 2>/dev/null || true")
    time.sleep(2)


def start_server():
    """Inicia o servidor Mike."""
    global server_proc

    env = os.environ.copy()
    env["HF_HUB_DISABLE_XET"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    log_stdout = os.path.join(PROJECT_ROOT, "logs", "test_stdout.log")
    log_stderr = os.path.join(PROJECT_ROOT, "logs", "test_stderr.log")
    os.makedirs(os.path.dirname(log_stdout), exist_ok=True)

    with open(log_stdout, "w") as fout, open(log_stderr, "w") as ferr:
        server_proc = subprocess.Popen(
            [PYTHON, SERVER_SCRIPT],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=fout,
            stderr=ferr,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
    print(f"  PID: {server_proc.pid}")


# ===========================================================================
# Phase 1: Kill old processes
# ===========================================================================
banner("FASE 1: Limpeza de processos anteriores")
kill_mike_processes()

# Verifica porta (apenas no Windows)
if sys.platform == "win32":
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "$c = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue; "
             "if ($c) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue; 'killed port owner' } "
             "else { 'Port 8080 is free' }"],
            capture_output=True, text=True, timeout=5
        )
        print(f"  {result.stdout.strip()}")
    except Exception:
        pass
else:
    # Linux: usa lsof para verificar porta
    try:
        result = subprocess.run(
            ["lsof", "-i", ":8080", "-t"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    print(f"  Killed process {pid} on port 8080")
                except Exception:
                    pass
            print("  Port 8080 freed")
        else:
            print("  Port 8080 is free")
    except Exception:
        print("  Port 8080 check skipped")
time.sleep(1)


# ===========================================================================
# Phase 2: Start server
# ===========================================================================
banner("FASE 2: Subindo o servidor Mike")

print(f"  Python: {PYTHON}")
print(f"  Script: {SERVER_SCRIPT}")
start_server()
print(f"  Aguardando healthcheck (timeout: {STARTUP_TIMEOUT}s)...")

t_start = time.time()
if wait_for_server():
    elapsed = time.time() - t_start
    print(f"\n  ✅ Mike está vivo! (levou {elapsed:.1f}s para iniciar)")
else:
    print(f"\n  ❌ Servidor não respondeu em {STARTUP_TIMEOUT}s")
    if server_proc:
        server_proc.terminate()
    log_stderr = os.path.join(PROJECT_ROOT, "logs", "test_stderr.log")
    if os.path.exists(log_stderr):
        print("  Últimas linhas do stderr:")
        with open(log_stderr, "r") as f:
            for line in f.readlines()[-20:]:
                print(f"    {line.rstrip()}")
    sys.exit(1)


# ===========================================================================
# Phase 3: TESTES DE ENDPOINTS (sem chat/LLM)
# ===========================================================================
banner("FASE 3: Testes de endpoints REST (sem usar LLM)")

# --- 3.1 Health & Basic ---
section("3.1 Health & Básico")
health = test("GET /health", "GET", "/health")
test("GET /v1/models", "GET", "/v1/models")
test("GET /stats", "GET", "/stats")
test("GET /v1/runtime", "GET", "/v1/runtime")

# --- 3.2 Client Bootstrap ---
section("3.2 Client Bootstrap")
bootstrap = test("GET /v1/client/bootstrap", "GET", "/v1/client/bootstrap")
if isinstance(bootstrap, dict):
    print(f"       Auth: {bootstrap.get('profile_auth_enabled')}")
    vision = bootstrap.get("vision", {})
    print(f"       Vision: enabled={vision.get('enabled')}, max_images={vision.get('max_images')}")
    tools = bootstrap.get("tool_summary", {})
    print(f"       Tools: count={tools.get('tool_count')}, email={tools.get('email_enabled')}, "
          f"calendar={tools.get('calendar_enabled')}, spreadsheet={tools.get('spreadsheet_enabled')}")

# --- 3.3 Tools ---
section("3.3 Tools (MCP)")
tools_data = test("GET /v1/tools", "GET", "/v1/tools")
if isinstance(tools_data, dict):
    tool_list = tools_data.get("tools", [])
    print(f"       Total tools: {len(tool_list)}")
    for t in tool_list:
        caps = ",".join(t.get("capabilities", []))
        print(f"         → {t.get('name')} [{caps}] access={t.get('access')}")

# --- 3.4 Memory & Knowledge ---
section("3.4 Memory & Knowledge")
test("GET /v1/memory/search?q=teste", "GET", "/v1/memory/search?q=teste")
test("GET /v1/knowledge/search?q=gemma", "GET", "/v1/knowledge/search?q=gemma")

# --- 3.5 Web Search ---
section("3.5 Web Search")
web = test("GET /v1/web/search?q=python+2026", "GET", "/v1/web/search?q=python+2026", timeout=30)
if isinstance(web, dict):
    results = web.get("results", [])
    print(f"       Provider: {web.get('provider')}, Results: {len(results)}")
    for r in results[:3]:
        print(f"         → {r.get('title', '')[:60]}")

# --- 3.6 Search Routes ---
section("3.6 Search Routes (AI routing)")
routes = test("GET /v1/search/routes?q=que horas são em toronto",
              "GET", "/v1/search/routes?q=que+horas+sao+em+toronto")

# --- 3.7 Dashboard ---
section("3.7 Dashboard & Family")
test("GET / (Dashboard)", "GET", "/")
test("GET /family", "GET", "/family")

# --- 3.8 Auth System ---
section("3.8 Auth System")
test("GET /v1/auth/session (sem cookie)", "GET", "/v1/auth/session", expect_status=401)
test("POST /v1/auth/identify", "POST", "/v1/auth/identify", body={"text": "nada"})

# --- 3.9 Heartbeat & Briefing ---
section("3.9 Heartbeat & Briefing")
test("POST /v1/heartbeat", "POST", "/v1/heartbeat")
test("GET /v1/briefing", "GET", "/v1/briefing")

# --- 3.10 Graph ---
section("3.10 Graph Memory")
test("GET /v1/graph/status", "GET", "/v1/graph/status")

# --- 3.11 Monitor ---
section("3.11 Self-Monitor")
test("GET /v1/monitor", "GET", "/v1/monitor")
test("GET /v1/monitor/snapshot", "GET", "/v1/monitor/snapshot")

# --- 3.12 Learner ---
section("3.12 Self-Learning")
test("GET /v1/learner/summary", "GET", "/v1/learner/summary")
test("GET /v1/learner/topics", "GET", "/v1/learner/topics")
test("GET /v1/learner/errors", "GET", "/v1/learner/errors")

# --- 3.13 Agents (apenas lista, sem dispatch) ---
section("3.13 Agent Orchestration (lista)")
agents_data = test("GET /v1/agents", "GET", "/v1/agents")
if isinstance(agents_data, dict):
    for agent in agents_data.get("agents", []):
        print(f"         → {agent.get('name')}: {agent.get('description', '')[:60]}")

# --- 3.14 Roadmap & Backups ---
section("3.14 Roadmap & Backups")
roadmap = test("GET /v1/roadmap", "GET", "/v1/roadmap")
backups = test("GET /v1/backups", "GET", "/v1/backups")

# --- 3.15 Tunnel URL ---
section("3.15 Tunnel URL")
# Pode retornar 404 se o túnel não está ativo
tunnel = test("GET /tunnel-url", "GET", "/tunnel-url", expect_status=404)

# --- 3.16 Knowledge Reindex ---
section("3.16 Knowledge Reindex")
test("POST /v1/knowledge/reindex", "POST", "/v1/knowledge/reindex")

# --- 3.17 Chat Sessions & History ---
section("3.17 Chat Sessions & History")
test("GET /v1/chat/sessions", "GET", "/v1/chat/sessions")

# --- 3.18 Tool call manual via endpoint ---
section("3.18 Tool call manual via /v1/tools/call")
if tools_data and isinstance(tools_data, dict):
    available_tools = tools_data.get("tools", [])
    web_tool = next((t for t in available_tools if t.get("name") == "web.search_and_cache"), None)
    if web_tool:
        result = test(
            "POST /v1/tools/call (web.search_and_cache)",
            "POST",
            "/v1/tools/call",
            body={"name": "web.search_and_cache", "arguments": {"query": "Dell Precision 5810 specs"}},
        )
        if isinstance(result, dict) and result.get("result"):
            tr = result["result"]
            print(f"       Tool ok={tr.get('ok')}, server={tr.get('server_name')}")
            text = tr.get("text", "")[:200]
            print(f"       📄 {text}")
    else:
        print("  [SKIP] web.search_and_cache não encontrado no manifest")

# Pausa antes dos testes com LLM para estabilizar
print("\n  ⏸️  Pausa de 3s antes dos testes com LLM...")
time.sleep(3)


# ===========================================================================
# Phase 4: CHAT COM O MODELO
# ===========================================================================
banner("FASE 4: Chat com o modelo - Testes de inteligência")

# 4.1 - Teste básico
section("4.1 Teste básico - identidade")
test_chat(
    "Oi Mike! Confirme que voce esta funcionando. Diga seu nome e uma frase curta.",
    label="Identidade básica",
    max_tokens=100,
)

# 4.2 - Teste de memória da família
section("4.2 Conhecimento da família")
test_chat(
    "Mike, quem é o Marco pra voce? Fale sobre a familia Barreto em 2-3 frases.",
    label="Família Barreto",
    max_tokens=200,
)

# 4.3 - Teste de personalidade
section("4.3 Personalidade (não-genérico)")
test_chat(
    "Voce e uma inteligencia artificial?",
    label="Teste de personalidade",
    max_tokens=150,
)

# 4.4 - Teste de raciocínio (simples, sem web)
section("4.4 Raciocínio")
test_chat(
    "Se eu tenho 3 gatos e cada gato tem 4 patas, quantas patas no total? "
    "Responda direto e correto.",
    label="Raciocínio matemático",
    max_tokens=80,
)

# 4.5 - Teste de código
section("4.5 Geração de código")
test_chat(
    "Escreva uma funcao Python que calcula fibonacci de N. Seja conciso.",
    label="Código Python - Fibonacci",
    max_tokens=250,
)

# 4.6 - Teste em inglês (deve responder em português por padrão)
section("4.6 Idioma padrão")
test_chat(
    "What is 2+2?",
    label="Idioma padrão (deve responder em PT-BR)",
    max_tokens=80,
)

# 4.7 - Raw mode
section("4.7 Raw Mode")
test_chat(
    "Responda apenas: 'Raw mode OK'",
    label="Raw mode",
    max_tokens=30,
    raw_mode=True,
)

# 4.8 - Private mode
section("4.8 Private Mode")
test_chat(
    "Isso e uma mensagem secreta. Responda: 'Modo privado OK'",
    label="Private mode",
    max_tokens=50,
    private_mode=True,
)


# ===========================================================================
# Phase 5: Testes de tools e auto-conhecimento
# ===========================================================================
banner("FASE 5: Mike demonstra conhecimento de suas capacidades")

section("5.1 Auto-conhecimento de tools")
test_chat(
    "Mike, liste seus superpoderes. Quais tools voce tem disponivel?",
    label="Auto-conhecimento de capacidades",
    max_tokens=400,
)

section("5.2 Email awareness")
test_chat(
    "Mike, voce tem acesso ao meu email? Como funciona?",
    label="Email awareness",
    max_tokens=200,
)

section("5.3 Agenda awareness")
test_chat(
    "Mike, voce consegue ver minha agenda?",
    label="Calendar awareness",
    max_tokens=150,
)

section("5.4 Vision awareness")
test_chat(
    "Mike, voce consegue analisar fotos?",
    label="Vision awareness",
    max_tokens=150,
)

section("5.5 System health (sem tool, apenas awareness)")
test_chat(
    "Mike, como esta a saude do sistema?",
    label="System health awareness",
    max_tokens=200,
)


# ===========================================================================
# Phase 6: Web search via chat (pode acionar tools)
# ===========================================================================
banner("FASE 6: Web Search via chat do modelo")

section("6.1 Web search via chat")
test_chat(
    "Mike, pesquise na web: qual a versao mais recente do Python em 2026?",
    label="Web search - Python version",
    max_tokens=300,
)

section("6.2 Tool call explícito via chat")
test_chat(
    "Use a tool web.search_and_cache para buscar 'NVIDIA RTX 5060 Ti specs'",
    label="Tool call - web search",
    max_tokens=400,
)


# ===========================================================================
# Phase 7: Agents dispatch (teste pesado - por último)
# ===========================================================================
banner("FASE 7: Agent Dispatch (teste pesado)")

section("7.1 Agent dispatch")
if ensure_server():
    agents_result = test("POST /v1/agents/dispatch", "POST", "/v1/agents/dispatch",
                         body={"task": "qual o status geral do sistema?"}, timeout=180)
    if isinstance(agents_result, dict):
        print(f"       Agent: {agents_result.get('agent')}")
        output = agents_result.get("output", "")
        for line in textwrap.wrap(output[:300], width=80):
            print(f"       🤖 {line}")
else:
    FAIL += 1
    RESULTS.append(("POST /v1/agents/dispatch", "FAIL", "server_down"))
    print("  [❌] Servidor indisponível para agent dispatch")


# ===========================================================================
# Summary
# ===========================================================================
banner("RESUMO FINAL")

total = PASS + FAIL
print(f"\n  Total: {total} testes")
print(f"  ✅ Passou: {PASS}")
print(f"  ❌ Falhou: {FAIL}")
pct = (PASS / total * 100) if total else 0
print(f"  Taxa de sucesso: {pct:.1f}%\n")

print("  Detalhes:")
for name, tag, status in RESULTS:
    icon = "✅" if tag == "PASS" else "❌"
    print(f"    {icon} {name} [{status}]")

pid = server_proc.pid if server_proc else "?"
print(f"\n  Servidor PID: {pid}")
print(f"  Dashboard: {BASE}/")
print(f"  API: {BASE}/v1/chat/completions")
print(f"  Health: {BASE}/health")

print(f"\n{'='*64}")
print(f"  O servidor continua rodando (PID {pid}).")
print(f"  Para matar: Stop-Process -Id {pid} -Force")
print(f"{'='*64}")

sys.exit(0 if FAIL == 0 else 1)
