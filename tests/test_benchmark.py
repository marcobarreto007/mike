"""
MikeScore Benchmark Suite
==========================
15 standardized tasks measuring Mike's capabilities across 8 dimensions.
Score 0-100. Run with: python tests/test_benchmark.py

Dimensions:
  MEM  - Memory & retrieval
  REA  - Reasoning
  MAT  - Math
  COD  - Code generation
  FAC  - Factual knowledge
  LANG - Portuguese language quality
  TOOL - Tool use
  MULTI - Multi-step reasoning
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

API = os.getenv("MIKE_API_URL", "http://127.0.0.1:8083")
CHAT_URL = f"{API}/v1/chat/completions"
TOOLS_URL = f"{API}/v1/tools"
MEM_URL = f"{API}/v1/memory/search"
HEALTH_URL = f"{API}/health"
TIMEOUT = 60


def _api_post(url, body, timeout=TIMEOUT):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def _api_get(url, timeout=TIMEOUT):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def _safe(text: str) -> str:
    """Strip emojis and non-ASCII chars that break Windows console."""
    import re
    return re.sub(r'[^\x00-\x7F]+', '?', text)[:200]


def chat(prompt: str, max_tokens: int = 100) -> dict:
    payload = {
        "model": "mike",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    first = _api_post(CHAT_URL, payload)
    if first.get("error") or not first.get("assistant_text"):
        time.sleep(0.4)
        second = _api_post(CHAT_URL, payload)
        if not second.get("error"):
            return second
    return first


# ---------------------------------------------------------------------------
# Benchmark Tasks
# ---------------------------------------------------------------------------

class BenchmarkTask:
    def __init__(self, id: str, name: str, dimension: str, weight: int = 5):
        self.id = id
        self.name = name
        self.dimension = dimension
        self.weight = weight  # out of 100 total
        self.score = 0.0
        self.detail = ""

    def run(self) -> float:
        raise NotImplementedError

    def result(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "dimension": self.dimension,
            "weight": self.weight,
            "score": round(self.score, 1),
            "detail": self.detail,
        }


class HealthCheck(BenchmarkTask):
    def run(self):
        r = _api_get(HEALTH_URL, timeout=5)
        if r.get("status") in ("healthy", "ok"):
            self.score = self.weight
            self.detail = f"Status: {r['status']}".encode('ascii', errors='replace').decode()
        else:
            self.score = 0
            self.detail = f"Falha: {r}"


class MemorySearch(BenchmarkTask):
    def run(self):
        r = _api_get(f"{MEM_URL}?q=projeto+mike&limit=3", timeout=10)
        if "error" in str(r):
            self.score = 0
            self.detail = f"Erro: {r.get('error', r)}"
        else:
            self.score = self.weight
            self.detail = "OK — memory search funciona"


class SimpleMath(BenchmarkTask):
    def run(self):
        r = chat("Quanto e 15 + 27? Responda apenas com o numero.", max_tokens=20)
        text = r.get("assistant_text", "")
        if "42" in text:
            self.score = self.weight
            self.detail = f"Correto: 42 | Resposta: {text[:100]}"
        elif any(n in text for n in ["42", "quarenta e dois"]):
            self.score = self.weight * 0.8
            self.detail = f"Parcial | Resposta: {text[:100]}"
        else:
            self.score = 0
            self.detail = f"Incorreto | Resposta: {text[:100]}"


class PortugueseGreeting(BenchmarkTask):
    def run(self):
        r = chat("Oi Mike! Tudo bem?", max_tokens=80)
        text = r.get("assistant_text", "").lower()
        score = 0.0
        if any(w in text for w in ["oi", "ola", "tudo bem", "bom"]):
            score += self.weight * 0.4
        if any(w in text for w in ["marco", "familia", "ajudar", "aqui"]):
            score += self.weight * 0.3
        if len(text) > 10 and len(text) < 500:
            score += self.weight * 0.3
        self.score = min(self.weight, score)
        self.detail = f"Resposta: {text[:150]}"


class CodeGeneration(BenchmarkTask):
    def run(self):
        r = chat(
            "Escreva uma funcao Python que recebe uma lista e retorna apenas os numeros pares. "
            "Apenas o codigo, sem explicacoes.",
            max_tokens=200,
        )
        text = r.get("assistant_text", "")
        score = 0.0
        if "def " in text:
            score += self.weight * 0.35
        if "return" in text:
            score += self.weight * 0.25
        if "% 2" in text or "mod" in text.lower():
            score += self.weight * 0.25
        if "lambda" in text or "filter" in text or "for " in text:
            score += self.weight * 0.15
        self.score = min(self.weight, score)
        self.detail = f"Codigo: {text[:200]}"


class FactualRecall(BenchmarkTask):
    def run(self):
        r = chat(
            "Qual e a capital do Brasil? Responda apenas com o nome da cidade.",
            max_tokens=30,
        )
        text = r.get("assistant_text", "").lower()
        if "brasilia" in text or "brasília" in text:
            self.score = self.weight
            self.detail = f"Correto: Brasilia | {text[:80]}"
        else:
            self.score = 0
            self.detail = f"Incorreto: {text[:80]}"


class MultiStepReasoning(BenchmarkTask):
    def run(self):
        r = chat(
            "Se um trem sai de Sao Paulo as 8h e viaja a 80km/h, e outro sai do Rio de Janeiro "
            "as 9h a 100km/h na mesma direcao, a que horas o segundo trem alcancaria o primeiro? "
            "Considere que a distancia entre SP e RJ e 430km. Mostre seu raciocinio em passos.",
            max_tokens=300,
        )
        text = r.get("assistant_text", "")
        score = 0.0
        if len(text) > 50:
            score += self.weight * 0.3  # Engajou
        if any(w in text.lower() for w in ["distancia", "tempo", "velocidad", "calculo", "hora"]):
            score += self.weight * 0.3  # Usou conceitos certos
        if any(c in text for c in "0123456789"):
            score += self.weight * 0.4  # Gerou numeros
        self.score = min(self.weight, score)
        self.detail = f"Raciocinio: {text[:250]}"


class ToolManifest(BenchmarkTask):
    def run(self):
        r = _api_get(TOOLS_URL, timeout=5)
        if isinstance(r, list) and len(r) > 0:
            self.score = self.weight
            self.detail = f"OK — {len(r)} tools disponiveis"
        elif isinstance(r, dict) and r.get("tools"):
            self.score = self.weight
            self.detail = f"OK — {len(r['tools'])} tools disponiveis"
        else:
            self.score = self.weight * 0.5
            self.detail = f"Parcial: {str(r)[:100]}"


class SelfAwareness(BenchmarkTask):
    def run(self):
        r = chat("Quem e voce? Responda em 2 frases.", max_tokens=100)
        text = r.get("assistant_text", "").lower()
        score = 0.0
        if "mike" in text:
            score += self.weight * 0.5
        if any(w in text for w in ["familia", "barreto", "yorkshire", "escudeiro", "ajudante"]):
            score += self.weight * 0.5
        self.score = min(self.weight, score)
        self.detail = f"Identidade: {text[:150]}"


class TranslationAbility(BenchmarkTask):
    def run(self):
        r = chat(
            "Traduza para portugues: 'The quick brown fox jumps over the lazy dog'",
            max_tokens=50,
        )
        text = r.get("assistant_text", "").lower()
        score = 0.0
        if "raposa" in text:
            score += self.weight * 0.4
        if "cachorro" in text or "cao" in text:
            score += self.weight * 0.3
        if "rapid" in text or "marrom" in text or "preguicos" in text:
            score += self.weight * 0.3
        self.score = min(self.weight, score)
        self.detail = f"Traducao: {text[:100]}"


class LongContextMemory(BenchmarkTask):
    def run(self):
        key = f"MIKE_BENCH_{int(time.time())}"
        add_resp = _api_post(
            f"{API}/v1/memory/add",
            {
                "content": f"memorize este codigo secreto: {key}",
                "assistant_text": f"Codigo secreto {key} registrado.",
            },
        )
        if add_resp.get("status") != "ok":
            self.score = 0
            self.detail = f"Falha ao adicionar memoria: {add_resp}"
            return

        time.sleep(2.0)  # let async persistence complete
        r = _api_get(f"{MEM_URL}?q={key}&limit=5", timeout=10)
        results = str(r)
        if key in results:
            self.score = self.weight
            self.detail = f"Memoria funciona — codigo recuperado"
        else:
            self.score = self.weight * 0.3
            self.detail = f"Codigo nao encontrado em resultados imediatos"


class CreativeWriting(BenchmarkTask):
    def run(self):
        r = chat(
            "Escreva um micro-conto de 3 frases sobre um cachorro Yorkshire que sonha em voar.",
            max_tokens=200,
        )
        text = r.get("assistant_text", "")
        score = 0.0
        if len(text) > 50:
            score += self.weight * 0.4
        if any(w in text.lower() for w in ["yorkshire", "cachorro", "cao", "mike"]):
            score += self.weight * 0.3
        if any(w in text.lower() for w in ["voar", "voar", "ceu", "asas", "nuvens"]):
            score += self.weight * 0.3
        self.score = min(self.weight, score)
        self.detail = f"Conto: {text[:200]}"


class ErrorRecovery(BenchmarkTask):
    def run(self):
        r = chat(
            "Qual e a raiz quadrada de -16? Resposta curta.",
            max_tokens=50,
        )
        text = r.get("assistant_text", "").lower()
        score = 0.0
        if any(w in text for w in ["imaginario", "complexo", "4i", "impossivel", "real"]):
            score += self.weight * 0.7  # Reconheceu o caso especial
        if "4" in text:
            score += self.weight * 0.3
        self.score = min(self.weight, score)
        self.detail = f"Resposta: {text[:100]}"


class StructuredOutput(BenchmarkTask):
    def run(self):
        r = chat(
            'Liste 3 frutas brasileiras no formato JSON: [{"nome": "...", "cor": "..."}] '
            'Apenas o JSON, sem explicacoes.',
            max_tokens=150,
        )
        text = r.get("assistant_text", "")
        score = 0.0
        if "[" in text and "]" in text:
            score += self.weight * 0.3
        if "{" in text and "}" in text:
            score += self.weight * 0.3
        if "nome" in text.lower():
            score += self.weight * 0.2
        if any(f in text.lower() for f in ["banana", "manga", "abacaxi", "laranja",
                                             "acerola", "goiaba", "caju", "maracuja",
                                             "jabuticaba", "cupuacu", "caja", "graviola",
                                             "pitanga", "coco", "melancia", "mamao"]):
            score += self.weight * 0.2
        self.score = min(self.weight, score)
        self.detail = f"JSON: {text[:200]}"


# ---------------------------------------------------------------------------
# Benchmark Runner
# ---------------------------------------------------------------------------

ALL_TASKS = [
    # (weight, task_class, name, dimension)
    (5,  HealthCheck,        "Health Check",             "SYS"),
    (7,  MemorySearch,       "Memory Search",            "MEM"),
    (7,  LongContextMemory,  "Long-term Memory",          "MEM"),
    (7,  SimpleMath,         "Simple Math (15+27)",      "MAT"),
    (7,  PortugueseGreeting, "Portuguese Greeting",     "LANG"),
    (7,  CodeGeneration,     "Code Generation",          "COD"),
    (7,  FactualRecall,      "Factual Knowledge",        "FAC"),
    (7,  MultiStepReasoning, "Multi-step Reasoning",    "MULTI"),
    (7,  ToolManifest,       "Tool Manifest",           "TOOL"),
    (7,  SelfAwareness,      "Self-Awareness",           "LANG"),
    (7,  TranslationAbility,  "Translation PT",          "LANG"),
    (7,  CreativeWriting,    "Creative Writing",         "LANG"),
    (6,  ErrorRecovery,      "Error Recovery (sqrt(-16))","REA"),
    (6,  StructuredOutput,   "Structured Output (JSON)", "COD"),
    (6,  HealthCheck,        "Final Health Check",       "SYS"),
]

# Verify total weight = 100
TOTAL_WEIGHT = sum(w for w, _, _, _ in ALL_TASKS)
assert TOTAL_WEIGHT == 100, f"Total weight must be 100, got {TOTAL_WEIGHT}"


def run_benchmark(verbose: bool = True) -> dict:
    """Run all benchmark tasks and return results."""
    results = []
    total_score = 0.0
    dim_scores = {}
    dim_weights = {}
    passed = 0
    failed = 0

    for weight, task_cls, name, dim in ALL_TASKS:
        task = task_cls(f"bench_{task_cls.__name__}", name, dim, weight)
        t0 = time.time()
        try:
            task.run()
            elapsed = time.time() - t0
        except Exception as exc:
            task.score = 0
            task.detail = f"Exception: {exc}"
            elapsed = time.time() - t0

        status = "PASS" if task.score >= weight * 0.5 else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1

        total_score += task.score
        dim_scores[dim] = dim_scores.get(dim, 0) + task.score
        dim_weights[dim] = dim_weights.get(dim, 0) + weight

        if verbose:
            detail_safe = task.detail.encode('ascii', errors='replace').decode()[:120]
            print(f"  [{status}] {task.name} ({dim}) - {task.score:.1f}/{weight} - {detail_safe}")

        results.append(task.result())

    # Dimension breakdown
    dim_breakdown = {}
    for dim in sorted(dim_scores):
        dim_breakdown[dim] = {
            "score": round(dim_scores[dim], 1),
            "max": dim_weights[dim],
            "pct": round(dim_scores[dim] / dim_weights[dim] * 100, 1),
        }

    mikescore = round(total_score, 1)

    if verbose:
        print(f"\n  MikeScore: {mikescore}/100")
        print(f"  Passed: {passed}/{len(ALL_TASKS)} | Failed: {failed}")
        print(f"\n  Dimension Breakdown:")
        for dim, info in dim_breakdown.items():
            bar = "#" * int(info["pct"] / 10) + "." * (10 - int(info["pct"] / 10))
            bar_safe = bar.encode('ascii', errors='replace').decode()
            print(f"    {dim:6s} {bar_safe} {info['pct']:.0f}%")

    return {
        "mikescore": mikescore,
        "passed": passed,
        "failed": failed,
        "total_tasks": len(ALL_TASKS),
        "dimensions": dim_breakdown,
        "tasks": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  MikeScore Benchmark Suite")
    print(f"  API: {API}")
    print("=" * 60)

    # Quick health check first
    health = _api_get(HEALTH_URL, timeout=5)
    if "error" in str(health):
        print(f"\n  ERROR: Server not reachable at {API}")
        print(f"  {health}")
        sys.exit(1)

    print(f"  Server: {health.get('status', '?')} | Backend: {health.get('llm_backend', '?')}")
    print(f"  Model: {health.get('active_model', '?')}")
    print()

    result = run_benchmark(verbose=True)

    print(f"\n{'='*60}")
    if result["mikescore"] >= 80:
        print("  MIKE SUPREMO - Agente de classe mundial!")
    elif result["mikescore"] >= 60:
        print("  Mike funcional - pronto para producao")
    elif result["mikescore"] >= 40:
        print("  Mike basico - precisa de melhorias")
    else:
        print("  Mike precisa de atencao urgente")

    # Save result
    out_path = "runtime/roadmap/mikescore_result.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  Resultado salvo: {out_path}")
    print(f"{'='*60}")

    sys.exit(0 if result["mikescore"] >= 50 else 1)
