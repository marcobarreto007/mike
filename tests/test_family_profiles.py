"""
Test suite: Family profile injection — 20 real interactions.
Each test sends a chat request as a specific family member and checks
that Mike recognizes them correctly via the family_profiles.json injection.
"""
import json
import urllib.request
import urllib.error
import sys
import os
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

API = os.environ.get("MIKE_TEST_API", "http://127.0.0.1:8083")
TIMEOUT = 120

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def test(name, session_id, message, expect_contains=None, expect_not_contains=None):
    global PASS, FAIL
    expect_contains = expect_contains or []
    expect_not_contains = expect_not_contains or []

    body = json.dumps({
        "model": "mike",
        "session_id": session_id,
        "messages": [{"role": "user", "content": message}],
        "stream": False,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"{API}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        data = json.loads(resp.read().decode("utf-8"))
        text = data.get("assistant_text", "") or ""

        if not text or len(text) < 3:
            FAIL += 1
            ERRORS.append(f"FAIL [{name}]: empty (len={len(text)})")
            return

        # expect_contains: ANY term must match (OR logic)
        if expect_contains:
            if not any(term.lower() in text.lower() for term in expect_contains):
                FAIL += 1
                ERRORS.append(f"FAIL [{name}]: none of {expect_contains} found")
                print(f"         Reply: {text[:200]}...")
                return

        for term in expect_not_contains:
            if term.lower() in text.lower():
                FAIL += 1
                ERRORS.append(f"FAIL [{name}]: forbidden '{term}' found")
                print(f"         Reply: {text[:200]}...")
                return

        PASS += 1
        print(f"  [PASS] {name}")
        print(f"         {text[:150]}...")
        time.sleep(0.5)
    except urllib.error.HTTPError as e:
        FAIL += 1
        ERRORS.append(f"FAIL [{name}]: HTTP {e.code}")
    except Exception as e:
        FAIL += 1
        ERRORS.append(f"FAIL [{name}]: {str(e)[:100]}")


# ═══════════════════════════════════════════════════════════════
print("=== 1-4: ANA PAULA ===")
# ═══════════════════════════════════════════════════════════════
test("Ana Paula — identidade",
     "anapaula-main",
     "Me diga meu nome completo. 1 frase.",
     expect_contains=["Ana Paula"],
     expect_not_contains=["Marco Barreto"])

test("Ana Paula — trabalho",
     "anapaula-main",
     "Onde eu trabalho? Curto.",
     expect_contains=["ONF", "NFB"])

test("Ana Paula — filhos",
     "anapaula-main",
     "Quem sao meus filhos? 1 frase.",
     expect_contains=["Rapha", "Alice"])

test("Ana Paula — irmao Matheus",
     "anapaula-main",
     "Fala do meu irmao Matheus. Curto.",
     expect_contains=["Matheus", "Brasil"])


# ═══════════════════════════════════════════════════════════════
print("=== 5-8: RAPHAEL ===")
# ═══════════════════════════════════════════════════════════════
test("Raphael — identidade",
     "raphael-main",
     "Me diga meu nome completo. 1 frase.",
     expect_contains=["Raphael"],
     expect_not_contains=["Marco Barreto"])

test("Raphael — universidade",
     "raphael-main",
     "O que estudo? 1 frase.",
     expect_contains=["Ciências", "Políticas"])

test("Raphael — Direito",
     "raphael-main",
     "Qual carreira quero? 1 frase.",
     expect_contains=["Direito", "advogado"])

test("Raphael — interesses",
     "raphael-main",
     "Do que eu gosto? Curto.",
     expect_contains=["League of Legends", "MMA", "PS5"])


# ═══════════════════════════════════════════════════════════════
print("=== 9-12: ALICE ===")
# ═══════════════════════════════════════════════════════════════
test("Alice — identidade",
     "alice-main",
     "Quem sou eu? 1 frase.",
     expect_contains=["Alice"],
     expect_not_contains=["Marco Barreto"])

test("Alice — dentista",
     "alice-main",
     "O que quero ser? Curto.",
     expect_contains=["dentista"])

test("Alice — musica",
     "alice-main",
     "Que musica gosto? Curto.",
     expect_contains=["Laufey"])

test("Alice — tom respeitoso",
     "alice-main",
     "Me descreve. 1 frase.",
     expect_not_contains=["constrangedor", "vulgar"])


# ═══════════════════════════════════════════════════════════════
print("=== 13-16: MATHEUS ===")
# ═══════════════════════════════════════════════════════════════
test("Matheus — identidade",
     "matheus-main",
     "Quem sou eu? 1 frase.",
     expect_contains=["Matheus"],
     expect_not_contains=["Marco Barreto"])

test("Matheus — localizacao",
     "matheus-main",
     "Onde moro? Curto.",
     expect_contains=["Brasil"])

test("Matheus — tom",
     "matheus-main",
     "Como devem me tratar? Curto.",
     expect_not_contains=["infantil", "problema"])

test("Matheus — rotina Ana Paula",
     "matheus-main",
     "Quando Ana Paula me liga? Curto.",
     expect_contains=["20h", "20:00"])


# ═══════════════════════════════════════════════════════════════
print("=== 17-20: MARILENE ===")
# ═══════════════════════════════════════════════════════════════
test("Marilene — identidade",
     "marilene-main",
     "Quem sou eu? 1 frase.",
     expect_contains=["Marilene"],
     expect_not_contains=["Marco Barreto"])

test("Marilene — idade",
     "marilene-main",
     "Quantos anos tenho? Curto.",
     expect_contains=["76"])

test("Marilene — saude",
     "marilene-main",
     "O que sabe da minha saude? Curto.",
     expect_contains=["AVC"])

test("Marilene — prudencia medica",
     "marilene-main",
     "Dor de cabeca forte, o que tomo? Curto.",
     expect_contains=["médico"])


# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"  {PASS}/{PASS+FAIL} passed")
if ERRORS:
    for e in ERRORS:
        print(f"  {e}")
print(f"{'='*50}")
sys.exit(0 if FAIL == 0 else 1)
