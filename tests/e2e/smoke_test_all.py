# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""Quick smoke test for all Mike endpoints."""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8080"
PASS = 0
FAIL = 0
RESULTS = []


def test(name, method, path, body=None, expect_status=200, timeout=120):
    global PASS, FAIL
    url = f"{BASE}{path}"
    try:
        if body is not None:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        req.get_method = lambda: method
        r = urllib.request.urlopen(req, timeout=timeout)
        status = r.status
        content = r.read().decode("utf-8", errors="replace")
        try:
            jdata = json.loads(content)
        except Exception:
            jdata = None

        if status == expect_status:
            PASS += 1
            tag = "PASS"
        else:
            FAIL += 1
            tag = "FAIL"
        preview = content[:200] if jdata is None else json.dumps(jdata, ensure_ascii=False)[:200]
        print(f"  [{tag}] {name} -> {status} | {preview}")
        RESULTS.append((name, tag, status))
        return jdata or content
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")[:200] if e.fp else ""
        if e.code == expect_status:
            PASS += 1
            tag = "PASS"
        else:
            FAIL += 1
            tag = "FAIL"
        print(f"  [{tag}] {name} -> HTTP {e.code} | {body_err}")
        RESULTS.append((name, tag, e.code))
        return None
    except Exception as exc:
        FAIL += 1
        print(f"  [FAIL] {name} -> {exc}")
        RESULTS.append((name, "FAIL", str(exc)))
        return None


print("=" * 60)
print("  MIKE FULL SMOKE TEST")
print("=" * 60)

# === Basic ===
print("\n--- Basic Endpoints ---")
test("GET /health", "GET", "/health")
test("GET /v1/models", "GET", "/v1/models")

# === Dashboard ===
print("\n--- Dashboards ---")
test("GET / (dashboard)", "GET", "/")
test("GET /family", "GET", "/family")

# === Phase 3: Briefing ===
print("\n--- Phase 3: Briefing & Heartbeat ---")
test("GET /v1/briefing", "GET", "/v1/briefing")
test("POST /v1/heartbeat", "POST", "/v1/heartbeat")

# === Phase 4: Graph ===
print("\n--- Phase 4: Graph ---")
test("GET /v1/graph/status", "GET", "/v1/graph/status")

# === Phase 6: Monitor ===
print("\n--- Phase 6: Monitor ---")
test("GET /v1/monitor", "GET", "/v1/monitor")
test("GET /v1/monitor/snapshot", "GET", "/v1/monitor/snapshot")

# === Phase 6: Learner ===
print("\n--- Phase 6: Learner ---")
test("GET /v1/learner/summary", "GET", "/v1/learner/summary")
test("GET /v1/learner/topics", "GET", "/v1/learner/topics")
test("GET /v1/learner/errors", "GET", "/v1/learner/errors")

# === Phase 6: Agents ===
print("\n--- Phase 6: Agents ---")
test("GET /v1/agents", "GET", "/v1/agents")
test("POST /v1/agents/dispatch", "POST", "/v1/agents/dispatch",
     body={"task": "qual o status do sistema?"})

# === Chat Completions (the big one) ===
print("\n--- Chat Completions ---")
t0 = time.time()
resp = test("POST /v1/chat/completions", "POST", "/v1/chat/completions",
            body={
                "model": "mike",
                "messages": [{"role": "user", "content": "Oi Mike! Diga uma frase curta confirmando que voce esta funcionando."}],
                "max_tokens": 80,
                "stream": False,
            })
elapsed = time.time() - t0
if resp and isinstance(resp, dict) and resp.get("choices"):
    msg = resp["choices"][0].get("message", {}).get("content", "")
    usage = resp.get("usage", {})
    tps = usage.get("completion_tokens", 0) / max(elapsed, 0.01)
    print(f"  -> Mike: {msg[:200]}")
    print(f"  -> Tokens: {usage}, Time: {elapsed:.1f}s, Speed: {tps:.1f} tok/s")

# === Summary ===
print("\n" + "=" * 60)
print(f"  RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
print("=" * 60)

for name, tag, st in RESULTS:
    icon = "✅" if tag == "PASS" else "❌"
    print(f"  {icon} {name} [{st}]")

print()
sys.exit(0 if FAIL == 0 else 1)
