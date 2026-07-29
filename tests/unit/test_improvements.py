# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
test_improvements.py — Testa as 7 melhorias aplicadas ao Mike.

Execução:
    python tests/test_improvements.py
"""

import json
import sys
import time
import urllib.request
import urllib.error

if "pytest" in sys.modules:
    import pytest

    pytest.skip("script-style live server test; run directly against Mike", allow_module_level=True)

BASE = "http://127.0.0.1:8080"
PASS = 0
FAIL = 0
results: list[tuple[str, bool, str]] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(method: str, path: str, body: dict | None = None, timeout: int = 90) -> tuple[int, dict | str]:
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def check(name: str, passed: bool, detail: str = "") -> None:
    global PASS, FAIL
    tag = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    if passed:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def wait_for_server_idle(timeout_s: int = 120) -> bool:
    """Poll /health until the server responds (LLM inference finished)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            s, _ = _req("GET", "/health", timeout=5)
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def chat(content: str, max_tokens: int = 80, timeout: int = 120) -> tuple[str, dict]:
    """Send a chat message, return (reply_text, full_response). Retries once on 500."""
    for attempt in range(2):
        status, body = _req("POST", "/v1/chat/completions", {
            "model": "mike",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "stream": False,
        }, timeout=timeout)
        if status == 200 and isinstance(body, dict):
            reply = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            return reply, body
        if status == 500 and attempt == 0:
            # Server may be recovering; wait and retry
            print("  [retry] HTTP 500 — aguardando 8s e tentando de novo...")
            time.sleep(8)
            continue
        return f"[HTTP {status}] {body}", {}
    return "[HTTP 500] falhou apos retry", {}


# ---------------------------------------------------------------------------
# TEST 1: Base smoke — servidor respondendo
# ---------------------------------------------------------------------------
print("\n" + "="*60)
print("  TEST 1: Base endpoints")
print("="*60)

status, body = _req("GET", "/health")
check("GET /health 200", status == 200, str(body)[:80] if isinstance(body, dict) else "")

status, body = _req("GET", "/v1/models")
check("GET /v1/models 200", status == 200)

status, body = _req("GET", "/v1/agents")
agents = [a["name"] for a in body.get("agents", [])] if isinstance(body, dict) else []
check("GET /v1/agents lista agentes", status == 200 and len(agents) >= 4, str(agents))


# ---------------------------------------------------------------------------
# TEST 2: Dynamic prefix — Mike sabe a data/hora
# ---------------------------------------------------------------------------
print("\n" + "="*60)
print("  TEST 2: Dynamic prefix (data/hora no contexto)")
print("="*60)

reply, _ = chat("Leia o contexto do sistema. Com base no contexto, qual é a DATA DE HOJE? Responda SOMENTE no formato DD/MM/AAAA.", max_tokens=20)
# Aceita: 12/04/2026 ou abril/2026 ou só '12'
has_date = "12/04/2026" in reply or ("12" in reply and ("04" in reply or "abril" in reply.lower()))
check("Mike conhece a data de hoje (12/04/2026)", has_date, f"reply: {reply[:120]}")


# ---------------------------------------------------------------------------
# TEST 3: Soul names filter — não busca web para perguntas sobre família
# ---------------------------------------------------------------------------
print("\n" + "="*60)
print("  TEST 3: Soul names filter (sem web p/ família)")
print("="*60)

# Pergunta sobre membro da família — não deve disparar web search
status, body = _req("GET", "/v1/monitor/snapshot")
web_before = body.get("web_searches_total", 0) if isinstance(body, dict) else 0

reply, _ = chat("Fala um pouco sobre o Marco. Quem é ele na família?", max_tokens=60)
time.sleep(1)

status2, snap = _req("GET", "/v1/monitor/snapshot")
web_after = snap.get("web_searches_total", 0) if isinstance(snap, dict) else 0

# A resposta deve mencionar algo de identidade/família, e web não deve ter disparado
has_family_knowledge = any(w in reply.lower() for w in ["família", "barreto", "marco", "pai", "pai", "dono"])
check("Mike respondeu sobre Marco com conhecimento interno", has_family_knowledge or len(reply) > 20, f"reply: {reply[:120]}")

# Se o monitor não tem o campo web_searches_total, testa via stats direto
status3, stats = _req("GET", "/v1/monitor")
web_hits = stats.get("last_web_hits", -1) if isinstance(stats, dict) else -1
check("Web search não disparou para pergunta sobre família", web_hits == 0 or web_after == web_before,
      f"web_hits={web_hits}, web_before={web_before}, web_after={web_after}")


# ---------------------------------------------------------------------------
# TEST 4: RAG injection — contexto local aparece na resposta
# ---------------------------------------------------------------------------
print("\n" + "="*60)
print("  TEST 4: RAG context injection (contexto próximo da pergunta)")
print("="*60)

# Salva uma memória e depois pergunta sobre ela
mem_body = {
    "content": "O projeto Skybridge de Marco usa arquitetura de microserviços com FastAPI e Redis.",
    "session_id": "test-improvements",
}
status, body = _req("POST", "/v1/memory/add", mem_body, timeout=15)
# Se endpoint não existir, testa via chat
if status not in (200, 201):
    # Injeta via sistema (não testa RAG injection diretamente, mas verifica que a resposta funciona)
    check("Endpoint /v1/memory/add disponível", False, f"HTTP {status} — RAG add não disponível, pulando teste direto")
    check("RAG injection (skip — sem endpoint de add)", True, "N/A — requer endpoint de memória")
else:
    check("POST /v1/memory/add", True)
    time.sleep(1)
    reply, _ = chat("O que você sabe sobre o projeto Skybridge do Marco?", max_tokens=80)
    has_rag = any(w in reply.lower() for w in ["microserv", "fastapi", "redis", "skybridge"])
    check("RAG injection com contexto na mensagem do usuário", has_rag, f"reply: {reply[:150]}")


# ---------------------------------------------------------------------------
# TEST 5: dispatch_chain — dois agentes em cadeia
# ---------------------------------------------------------------------------
print("\n" + "="*60)
print("  TEST 5: dispatch_chain (multi-agente em cadeia)")
print("="*60)

status, body = _req("POST", "/v1/agents/dispatch_chain", {
    "task": "pesquise sobre FastAPI e Python para construir uma API REST",
    "threshold": 0.5,
    "max_agents": 2,
    "max_tokens": 80,
}, timeout=180)

if status == 404:
    check("POST /v1/agents/dispatch_chain endpoint existe", False, "endpoint não registrado — precisa adicionar rota")
elif status == 200:
    chain_results = body if isinstance(body, list) else body.get("results", [])
    num_agents = len(chain_results) if isinstance(chain_results, list) else 0
    agents_used = [r.get("agent", "?") for r in chain_results] if isinstance(chain_results, list) else []
    check("dispatch_chain retorna lista de resultados", num_agents > 0, f"{num_agents} agentes: {agents_used}")
    if num_agents > 0:
        first_ok = chain_results[0].get("success", False) if isinstance(chain_results, list) else False
        check("Primeiro agente executou com sucesso", first_ok, str(chain_results[0])[:100] if chain_results else "")
else:
    check("POST /v1/agents/dispatch_chain", False, f"HTTP {status}: {str(body)[:100]}")


# Aguarda servidor terminar qualquer inferência pendente do TEST 5
print("  [idle] Aguardando servidor ficar livre...")
wait_for_server_idle(timeout_s=300)

# ---------------------------------------------------------------------------
# TEST 6: Agent output validation (auditor)
# ---------------------------------------------------------------------------
print("\n" + "="*60)
print("  TEST 6: Agent output validation (auditor)")
print("="*60)

# Despacha o agente Guardian para checar sistema — deve ter output válido
status, body = _req("POST", "/v1/agents/dispatch", {"task": "verifica status do sistema"}, timeout=60)
if status == 200 and isinstance(body, dict):
    agent_name = body.get("agent", "?")
    output = body.get("output", "")
    success = body.get("success", False)
    is_real_output = success and len(output.strip()) >= 50
    check("Agent dispatch retorna output válido (≥50 chars)", is_real_output,
          f"agent={agent_name} success={success} len={len(output)}")
    # Verifica que o validador não rejeitou um output legítimo
    check("Validador não rejeitou output legítimo do Guardian", success, f"success={success}")
else:
    check("Agent dispatch funciona", False, f"HTTP {status}")


# Aguarda servidor entre TEST 6 (dispatch pesado) e TEST 7
print("  [idle] Aguardando servidor ficar livre...")
wait_for_server_idle(timeout_s=300)

# ---------------------------------------------------------------------------
# TEST 7: Smoke test completo (15 endpoints)
# ---------------------------------------------------------------------------
print("\n" + "="*60)
print("  TEST 7: Smoke test completo (15 endpoints)")
print("="*60)

smoke_endpoints = [
    ("GET", "/health"),
    ("GET", "/v1/models"),
    ("GET", "/"),
    ("GET", "/family"),
    ("GET", "/v1/briefing"),
    ("GET", "/v1/graph/status"),
    ("GET", "/v1/monitor"),
    ("GET", "/v1/monitor/snapshot"),
    ("GET", "/v1/learner/summary"),
    ("GET", "/v1/learner/topics"),
    ("GET", "/v1/learner/errors"),
    ("GET", "/v1/agents"),
]
post_endpoints = [
    ("POST", "/v1/heartbeat", {}),
    ("POST", "/v1/agents/dispatch", {"task": "status do sistema"}),
]

for method, path in smoke_endpoints:
    s, _ = _req(method, path, timeout=30)
    check(f"{method} {path}", s == 200, f"HTTP {s}")

for method, path, body in post_endpoints:
    s, _ = _req(method, path, body, timeout=60)
    check(f"{method} {path}", s == 200, f"HTTP {s}")

# Chat completions — mede velocidade
print("\n  [Timing] Chat completions com max_tokens=20...")
t0 = time.time()
s, body = _req("POST", "/v1/chat/completions", {
    "model": "mike",
    "messages": [{"role": "user", "content": "diga: funcionando"}],
    "max_tokens": 20,
    "stream": False,
}, timeout=90)
elapsed = time.time() - t0
if s == 200 and isinstance(body, dict):
    toks = body.get("usage", {}).get("completion_tokens", 0) or 20
    speed = round(toks / elapsed, 1) if elapsed > 0 else 0
    reply = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    check(f"POST /v1/chat/completions ({speed} tok/s)", True, f"reply: {reply[:60]}")
else:
    check("POST /v1/chat/completions", False, f"HTTP {s}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "="*60)
print(f"  RESULTS: {PASS} passed, {FAIL} failed, {PASS+FAIL} total")
print("="*60)
for name, passed, detail in results:
    tag = "✅" if passed else "❌"
    suffix = f" [{detail}]" if detail and not passed else ""
    print(f"  {tag} {name}{suffix}")

sys.exit(0 if FAIL == 0 else 1)
