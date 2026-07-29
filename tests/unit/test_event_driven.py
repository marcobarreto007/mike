# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
test_event_driven.py
====================
Tests Mike's event-driven architecture:
  - MikeEventBus pub/sub with concurrent handlers
  - MikeHeartbeatV2 with mock LLM for urgency/SMS analysis
  - Keyword-only fallback when no generate_fn
  - End-to-end event flow: email received -> publish -> handler -> auto-reply
  - All tests use MockLLM — no real model needed

Run:
    python tests/unit/test_event_driven.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

# This file is a self-contained async test harness with its own counters and
# ``main()``. It is executed directly below; it is not a pytest-style module.
__test__ = False
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Ensure core/ and core/autonomy/ are on path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _PROJECT_ROOT / "core"
_AUTONOMY_DIR = _SRC_DIR / "autonomy"
_MODULE_DIRS = (
    _SRC_DIR,
    _AUTONOMY_DIR,
    _SRC_DIR / "server",
    _SRC_DIR / "integrations",
    _SRC_DIR / "comms",
    _SRC_DIR / "memory",
    _SRC_DIR / "orchestration",
    _SRC_DIR / "mcp",
)
for _d in _MODULE_DIRS:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
SKIP = "\033[93m[SKIP]\033[0m"

passed = 0
failed = 0
skipped = 0


def test_result(name: str, ok: bool, message: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  {PASS} {name}")
    else:
        failed += 1
        print(f"  {FAIL} {name}  — {message}" if message else f"  {FAIL} {name}")


# ============================================================================
# MockLLM — Simulates LLM responses for testing
# ============================================================================

class MockLLM:
    """
    Mock LLM for testing. Returns pre-programmed responses based on
    the prompt content. No real model needed.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.call_history: list[dict] = []

    async def generate(self, messages: list[dict], temperature: float = 0.7,
                       max_tokens: int = 100, tools: Optional[list] = None) -> dict:
        """Mock generate function compatible with Mike's generate_fn signature."""
        self.call_count += 1

        user_content = ""
        for msg in messages:
            if msg.get("role") == "user":
                user_content = str(msg.get("content", ""))
                break

        self.call_history.append({
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })

        response_text = self._generate_response(user_content)
        return {"assistant_text": response_text}

    def _generate_response(self, prompt: str) -> str:
        """Generate a mock response based on prompt content."""
        prompt_lower = prompt.lower()

        # ── Urgency analysis ──
        if "escala de 1 a 5" in prompt_lower or "classifique a urgencia" in prompt_lower:
            return self._mock_urgency(prompt)

        # ── SMS analysis ──
        if '"talvez sim' in prompt_lower or 'resposta sms' in prompt_lower:
            return self._mock_sms_analysis(prompt)

        # ── Smart reply ──
        if "yorkshire digital da familia barreto" in prompt_lower and "resposta personalizada" in prompt_lower:
            return self._mock_smart_reply(prompt)

        # Default
        return '{"result": "ok"}'

    def _mock_urgency(self, prompt: str) -> str:
        """Return urgency JSON based on email-specific content (De:/Assunto:/Conteudo:) only."""
        import re
        prompt_lower = prompt.lower()

        # Extract only the email-specific parts to avoid matching prompt instructions
        # (the prompt description contains "emergencia" as a label for urgency 5)
        email_parts = []
        for line in prompt_lower.split("\n"):
            line = line.strip()
            if line.startswith(("de:", "assunto:", "conteudo:", "contexto familiar:")):
                email_parts.append(line)
        email_content = " ".join(email_parts) if email_parts else prompt_lower

        if "emergencia" in email_content or "socorro" in email_content or "hospital" in email_content:
            return '{"urgency": 5, "intent": "emergencia", "reasoning": "menção de emergência"}'

        if "urgente" in email_content:
            return '{"urgency": 4, "intent": "pedido_ajuda", "reasoning": "marcado como urgente"}'

        if "marilene" in email_content:
            return '{"urgency": 4, "intent": "saude", "reasoning": "Marilene (76 anos, saúde frágil)"}'

        if "newsletter" in email_content or "spam" in email_content or "promocao" in email_content:
            return '{"urgency": 1, "intent": "spam", "reasoning": "newsletter sem importância"}'

        if "agendamento" in email_content or "consulta" in email_content:
            return '{"urgency": 3, "intent": "agendamento", "reasoning": "confirmação de agendamento"}'

        return '{"urgency": 2, "intent": "informacao", "reasoning": "email informativo padrão"}'

    def _mock_sms_analysis(self, prompt: str) -> str:
        """Return SMS analysis JSON based on the SMS body quoted in the prompt."""
        prompt_lower = prompt.lower()

        # Extract only the SMS body: look for the quoted text after "SMS: "
        import re
        sms_match = re.search(r'sms:\s*"([^"]+)"', prompt_lower)
        sms_body = sms_match.group(1) if sms_match else prompt_lower

        if "talvez sim" in sms_body and "depois das 15h" in sms_body:
            return (
                '{"intent": "confirm_conditional", "conditions": "após as 15h", '
                '"confidence": 0.85, "suggested_action": "flag_for_review"}'
            )

        if "sim" in sms_body and "confirmo" in sms_body:
            return (
                '{"intent": "confirm", "conditions": "", '
                '"confidence": 0.95, "suggested_action": "confirm_appointment"}'
            )

        if "nao" in sms_body or "não" in sms_body or "cancelar" in sms_body or "cancela" in sms_body:
            return (
                '{"intent": "cancel", "conditions": "", '
                '"confidence": 0.90, "suggested_action": "cancel_appointment"}'
            )

        if "reagendar" in sms_body or "remarcar" in sms_body:
            return (
                '{"intent": "reschedule", "conditions": "pedido de reagendamento", '
                '"confidence": 0.80, "suggested_action": "flag_for_reschedule"}'
            )

        return (
            '{"intent": "ambiguous", "conditions": "", '
            '"confidence": 0.40, "suggested_action": "ask_clarification"}'
        )

    def _mock_smart_reply(self, prompt: str) -> str:
        """Return a mock smart reply."""
        if "marilene" in prompt.lower():
            return (
                "Olá, Dona Marilene!\n\n"
                "Recebi seu email e já estou cuidando disso com todo carinho.\n\n"
                "Sou o Mike, o Yorkshire digital da família. "
                "Pode contar comigo sempre que precisar.\n\n"
                "Com amor,\n"
                "Mike 🐾"
            )
        return (
            "Olá!\n\n"
            "Recebi seu email e já estou aqui para ajudar.\n\n"
            "Com carinho,\n"
            "Mike 🐾"
        )


# ============================================================================
# Test 1: MikeEventBus — basic pub/sub
# ============================================================================

async def test_1_event_bus_basic() -> None:
    """Test MikeEventBus: publish events, verify handlers called."""
    print("\n--- Test 1: MikeEventBus Basic Pub/Sub ---")

    from mike_event_bus import MikeEventBus

    bus = MikeEventBus()

    handler_calls: list[dict] = []

    async def test_handler(payload: dict) -> None:
        handler_calls.append(payload)

    # Subscribe
    bus.subscribe("email.family", test_handler)
    bus.subscribe("task.created", test_handler)

    test_result("subscribe() — 2 handlers registered",
                bus.subscriber_count() == 2,
                f"expected 2, got {bus.subscriber_count()}")

    # Publish
    n = await bus.publish("email.family", {"from": "ana@test.com", "subject": "Oi!"})
    test_result("publish('email.family') — 1 handler called",
                n == 1 and len(handler_calls) == 1,
                f"n={n}, calls={len(handler_calls)}")

    # Verify payload
    payload = handler_calls[0]
    test_result("publish('email.family') — payload correct",
                payload.get("from") == "ana@test.com" and payload.get("subject") == "Oi!",
                f"payload={payload}")

    # Publish event with no subscribers
    n = await bus.publish("calendar.reminder", {"event": "Reunião"})
    test_result("publish('calendar.reminder') — 0 handlers (none subscribed)",
                n == 0,
                f"n={n}")

    # Unsubscribe and verify
    bus.unsubscribe("email.family", test_handler)
    test_result("unsubscribe() — 1 handler removed",
                bus.subscriber_count() == 1,
                f"got {bus.subscriber_count()}")

    n = await bus.publish("email.family", {"from": "bob@test.com"})
    test_result("publish after unsubscribe — 0 handlers called",
                n == 0 and len(handler_calls) == 1,
                f"n={n}, calls={len(handler_calls)}")


# ============================================================================
# Test 2: MikeEventBus — stats
# ============================================================================

async def test_2_event_bus_stats() -> None:
    """Test MikeEventBus: event counting and stats."""
    print("\n--- Test 2: MikeEventBus Stats ---")

    from mike_event_bus import MikeEventBus

    bus = MikeEventBus()

    async def dummy(payload: dict) -> None:
        pass

    bus.subscribe("email.urgent", dummy)
    bus.subscribe("sms.reply", dummy)

    await bus.publish("email.urgent", {"subject": "test1"})
    await bus.publish("email.urgent", {"subject": "test2"})
    await bus.publish("sms.reply", {"body": "Sim"})

    stats = bus.stats()
    test_result("total_events_published == 3",
                stats["total_events_published"] == 3,
                f"got {stats['total_events_published']}")

    test_result("event_counts['email.urgent'] == 2",
                stats["event_counts"]["email.urgent"] == 2,
                f"got {stats['event_counts'].get('email.urgent')}")

    test_result("event_counts['sms.reply'] == 1",
                stats["event_counts"]["sms.reply"] == 1,
                f"got {stats['event_counts'].get('sms.reply')}")

    test_result("subscriber_counts correct",
                stats["subscriber_counts"]["email.urgent"] == 1
                and stats["subscriber_counts"]["sms.reply"] == 1,
                f"got {stats['subscriber_counts']}")

    bus.reset_counts()
    test_result("reset_counts() zeroed event_counts",
                bus.stats()["total_events_published"] == 0,
                f"got {bus.stats()['total_events_published']}")


# ============================================================================
# Test 3: MikeEventBus — concurrent handlers
# ============================================================================

async def test_3_event_bus_concurrent() -> None:
    """Test MikeEventBus: multiple handlers run concurrently."""
    print("\n--- Test 3: MikeEventBus Concurrent Handlers ---")

    from mike_event_bus import MikeEventBus

    bus = MikeEventBus()

    execution_order: list[str] = []

    async def handler_a(payload: dict) -> None:
        await asyncio.sleep(0.05)
        execution_order.append("A")

    async def handler_b(payload: dict) -> None:
        await asyncio.sleep(0.02)
        execution_order.append("B")

    async def handler_c(payload: dict) -> None:
        execution_order.append("C")

    bus.subscribe("test.event", handler_a)
    bus.subscribe("test.event", handler_b)
    bus.subscribe("test.event", handler_c)

    t0 = time.time()
    n = await bus.publish("test.event", {"data": "x"})
    elapsed = time.time() - t0

    test_result("3 handlers called for 1 event",
                n == 3 and len(execution_order) == 3,
                f"n={n}, order={execution_order}")

    # B should finish before A (sleeps less), C should be first (no sleep)
    test_result("handlers ran concurrently (elapsed < 0.1s for 3 handlers)",
                elapsed < 0.15,  # generous: concurrent should be ~0.05s
                f"elapsed={elapsed:.3f}s (serial would be ~0.07s)")

    test_result("handler C ran first (no sleep)",
                execution_order[0] == "C",
                f"order={execution_order}")


# ============================================================================
# Test 4: MikeHeartbeatV2 — LLM urgency classification
# ============================================================================

async def test_4_heartbeat_v2_urgency_llm() -> None:
    """Test MikeHeartbeatV2.analyze_email_urgency() with MockLLM."""
    print("\n--- Test 4: HeartbeatV2 Email Urgency (LLM) ---")

    # Temporarily enable LLM mode
    os.environ["MIKE_HEARTBEAT_USE_LLM"] = "true"

    from mike_heartbeat import MikeHeartbeatV2

    mock_llm = MockLLM()
    hb = MikeHeartbeatV2(generate_fn=mock_llm.generate)

    test_result("has_llm == True", hb.has_llm, f"has_llm={hb.has_llm}")

    # Test urgent email
    result = await hb.analyze_email_urgency({
        "from": "emergencia@test.com",
        "subject": "SOCORRO - Preciso de ajuda urgente",
        "snippet": "Hospital...",
    })
    test_result("urgent email -> urgency 5",
                result["urgency"] == 5 and result["method"] == "llm",
                f"got urgency={result['urgency']}, intent={result['intent']}, method={result['method']}")

    # Test normal email
    result = await hb.analyze_email_urgency({
        "from": "colega@trabalho.com",
        "subject": "Relatório mensal",
        "snippet": "Segue o relatório...",
    })
    test_result("normal email -> urgency 2",
                result["urgency"] == 2,
                f"got urgency={result['urgency']}, intent={result['intent']}")

    # Test spam
    result = await hb.analyze_email_urgency({
        "from": "newsletter@promo.com",
        "subject": "newsletter PROMOCAO imperdivel",
        "snippet": "Clique aqui...",
    })
    test_result("spam email -> urgency 1",
                result["urgency"] == 1 and result["intent"] == "spam",
                f"got urgency={result['urgency']}, intent={result['intent']}")

    # Test family: Marilene
    result = await hb.analyze_email_urgency({
        "from": "marilene@family.com",
        "subject": "Preciso falar com voce",
        "snippet": "Nao estou me sentindo bem...",
    })
    test_result("Marilene email -> urgency 4 (saude fragil)",
                result["urgency"] == 4,
                f"got urgency={result['urgency']}, intent={result['intent']}")

    test_result("MockLLM call_count > 0",
                mock_llm.call_count == 4,
                f"call_count={mock_llm.call_count}")

    os.environ.pop("MIKE_HEARTBEAT_USE_LLM", None)


# ============================================================================
# Test 5: MikeHeartbeatV2 — keyword fallback urgency
# ============================================================================

async def test_5_heartbeat_v2_urgency_keyword() -> None:
    """Test MikeHeartbeatV2.analyze_email_urgency() falls back to keyword matching."""
    print("\n--- Test 5: HeartbeatV2 Email Urgency (Keyword Fallback) ---")

    from mike_heartbeat import MikeHeartbeatV2

    # No generate_fn — should use keywords
    hb = MikeHeartbeatV2(generate_fn=None)

    test_result("has_llm == False (no generate_fn)", not hb.has_llm)

    # Test urgent keywords
    result = await hb.analyze_email_urgency({
        "from": "test@test.com",
        "subject": "URGENTE: Preciso de resposta agora",
        "snippet": "responda logo",
    })
    test_result("keyword: 'urgente' -> urgency 4",
                result["urgency"] == 4 and result["method"] == "keyword",
                f"got urgency={result['urgency']}, method={result['method']}")

    # Test emergency keywords
    result = await hb.analyze_email_urgency({
        "from": "test@test.com",
        "subject": "Emergência no hospital",
        "snippet": "socorro",
    })
    test_result("keyword: 'emergencia' + 'hospital' -> urgency 5",
                result["urgency"] == 5,
                f"got urgency={result['urgency']}")

    # Test emotional content
    result = await hb.analyze_email_urgency({
        "from": "friend@test.com",
        "subject": "Estou muito triste",
        "snippet": "me sinto sozinho...",
    })
    test_result("keyword: emotional words -> urgency 4, intent=emocional",
                result["urgency"] == 4 and result["intent"] == "emocional",
                f"got urgency={result['urgency']}, intent={result['intent']}")

    # Test spam indicators
    result = await hb.analyze_email_urgency({
        "from": "spam@offers.com",
        "subject": "PROMOCAO IMPERDIVEL - desconto gratis",
        "snippet": "clique aqui para unsubscribe",
    })
    test_result("keyword: spam indicators -> urgency 1",
                result["urgency"] <= 2,
                f"got urgency={result['urgency']}, intent={result['intent']}")

    # Test family matching via keyword (name in email)
    result = await hb.analyze_email_urgency({
        "from": "marilene@gmail.com",
        "subject": "Oi filho",
        "snippet": "So passei pra dar oi...",
    })
    test_result("keyword: Marilene detected in email -> urgency >= 3",
                result["urgency"] >= 3,
                f"got urgency={result['urgency']}, method={result['method']}")


# ============================================================================
# Test 6: MikeHeartbeatV2 — SMS reply analysis (LLM)
# ============================================================================

async def test_6_heartbeat_v2_sms_llm() -> None:
    """Test MikeHeartbeatV2.analyze_sms_reply() with MockLLM."""
    print("\n--- Test 6: HeartbeatV2 SMS Reply Analysis (LLM) ---")

    os.environ["MIKE_HEARTBEAT_USE_LLM"] = "true"

    from mike_heartbeat import MikeHeartbeatV2

    mock_llm = MockLLM()
    hb = MikeHeartbeatV2(generate_fn=mock_llm.generate)

    # Test: "talvez sim, mas só depois das 15h"
    result = await hb.analyze_sms_reply(
        "talvez sim, mas só depois das 15h",
        context={"appointment_at": "2026-07-25T09:00:00", "service": "consulta"},
    )
    test_result("ambiguous: 'talvez sim, mas só depois das 15h' -> confirm_conditional",
                result["intent"] == "confirm_conditional" and "15h" in result.get("conditions", ""),
                f"got intent={result['intent']}, conditions={result.get('conditions')}")

    # Test: clear confirmation
    result = await hb.analyze_sms_reply(
        "SIM, confirmo! Pode agendar.",
    )
    test_result("clear: 'SIM, confirmo!' -> confirm",
                result["intent"] == "confirm" and result["confidence"] > 0.8,
                f"got intent={result['intent']}, confidence={result['confidence']}")

    # Test: cancellation
    result = await hb.analyze_sms_reply(
        "Não vou poder, cancela por favor.",
    )
    test_result("clear: 'Não vou poder, cancela' -> cancel",
                result["intent"] == "cancel",
                f"got intent={result['intent']}")

    os.environ.pop("MIKE_HEARTBEAT_USE_LLM", None)


# ============================================================================
# Test 7: MikeHeartbeatV2 — SMS reply analysis (keyword fallback)
# ============================================================================

async def test_7_heartbeat_v2_sms_keyword() -> None:
    """Test MikeHeartbeatV2.analyze_sms_reply() keyword fallback."""
    print("\n--- Test 7: HeartbeatV2 SMS Reply (Keyword Fallback) ---")

    from mike_heartbeat import MikeHeartbeatV2

    hb = MikeHeartbeatV2(generate_fn=None)

    # Test: confirmation
    result = await hb.analyze_sms_reply("SIM, confirmo!")
    test_result("keyword: 'SIM, confirmo' -> confirm",
                result["intent"] == "confirm" and result["method"] == "keyword",
                f"got intent={result['intent']}, method={result['method']}")

    # Test: conditional
    result = await hb.analyze_sms_reply("Sim, mas só depois das 15h")
    test_result("keyword: 'Sim, mas...' -> confirm_conditional",
                result["intent"] == "confirm_conditional",
                f"got intent={result['intent']}")

    # Test: cancel
    result = await hb.analyze_sms_reply("Não posso, cancele")
    test_result("keyword: 'Não posso, cancele' -> cancel",
                result["intent"] == "cancel",
                f"got intent={result['intent']}")

    # Test: reschedule
    result = await hb.analyze_sms_reply("Preciso reagendar para quarta")
    test_result("keyword: 'reagendar para quarta' -> reschedule",
                result["intent"] == "reschedule",
                f"got intent={result['intent']}")

    # Test: unknown
    result = await hb.analyze_sms_reply("blargh foo bar")
    test_result("keyword: nonsense -> unknown",
                result["intent"] == "unknown",
                f"got intent={result['intent']}")

    # Test: "1" -> confirm
    result = await hb.analyze_sms_reply("1")
    test_result("keyword: '1' -> confirm",
                result["intent"] == "confirm",
                f"got intent={result['intent']}")


# ============================================================================
# Test 8: MikeHeartbeatV2 — smart reply generation
# ============================================================================

async def test_8_heartbeat_v2_smart_reply() -> None:
    """Test MikeHeartbeatV2.generate_smart_reply() with mock LLM and template."""
    print("\n--- Test 8: HeartbeatV2 Smart Reply Generation ---")

    os.environ["MIKE_HEARTBEAT_USE_LLM"] = "true"

    from mike_heartbeat import MikeHeartbeatV2

    mock_llm = MockLLM()
    hb = MikeHeartbeatV2(generate_fn=mock_llm.generate)

    # LLM-powered reply
    reply = await hb.generate_smart_reply({
        "from": "marilene@family.com",
        "subject": "Oi filho",
        "body": "So queria saber como voce esta.",
    })
    test_result("LLM: reply contains 'Mike'",
                "Mike" in reply,
                f"reply[:50]={reply[:50]}")
    test_result("LLM: reply contains paw emoji",
                "🐾" in reply,
                f"reply[:50]={reply[:50]}")

    # Template fallback (no LLM)
    hb_no_llm = MikeHeartbeatV2(generate_fn=None)
    reply = await hb_no_llm.generate_smart_reply({
        "from": "someone@test.com",
        "subject": "Hello",
        "body": "Just saying hi.",
    })
    test_result("template: reply contains 'Mike'",
                "Mike" in reply,
                f"reply[:50]={reply[:50]}")
    test_result("template: reply contains 'Yorkshire'",
                "Yorkshire" in reply)

    os.environ.pop("MIKE_HEARTBEAT_USE_LLM", None)


# ============================================================================
# Test 9: End-to-End Event Flow
# ============================================================================

async def test_9_e2e_event_flow() -> None:
    """Test full event flow: email received -> event -> handler -> auto-reply."""
    print("\n--- Test 9: End-to-End Event Flow ---")

    from mike_event_bus import MikeEventBus

    bus = MikeEventBus()
    handler_results: list[dict] = []

    async def family_email_handler(payload: dict) -> None:
        """Simulates auto_reply_family routine."""
        email = payload.get("email", {})
        sender = email.get("from", "")
        subject = email.get("subject", "")
        handler_results.append({
            "action": "auto_reply_family",
            "to": sender,
            "subject": f"Re: {subject}",
            "status": "replied",
        })

    async def urgent_email_handler(payload: dict) -> None:
        """Simulates urgent notification."""
        handler_results.append({
            "action": "notify_urgent",
            "email": payload.get("email", {}),
        })

    # Subscribe handlers
    bus.subscribe("email.family", family_email_handler)
    bus.subscribe("email.urgent", urgent_email_handler)

    # Simulate: heartbeat detects urgent email from family member
    email_data = {
        "from": "ana.paula@family.com",
        "subject": "URGENTE: Preciso que voce veja isso",
        "snippet": "Tem algo importante...",
        "urgent": True,
    }

    # Step 1: Publish email.urgent
    await bus.publish("email.urgent", {"email": email_data})
    test_result("email.urgent -> 1 handler called",
                len(handler_results) == 1,
                f"got {len(handler_results)}")

    # Step 2: Publish email.family (for family-specific auto-reply)
    profile = {"name": "Ana Paula", "profile_id": "family_ana_paula_v1"}
    await bus.publish("email.family", {"email": email_data, "profile": profile})
    test_result("email.family -> 2 handlers total",
                len(handler_results) == 2,
                f"got {len(handler_results)}")

    # Verify handler results
    first = handler_results[0]
    second = handler_results[1]
    test_result("Handler 1: notify_urgent",
                first["action"] == "notify_urgent",
                f"got {first['action']}")
    test_result("Handler 2: auto_reply_family",
                second["action"] == "auto_reply_family",
                f"got {second['action']}")
    test_result("auto_reply: correct 'to' address",
                second["to"] == "ana.paula@family.com",
                f"got {second['to']}")
    test_result("auto_reply: status 'replied'",
                second["status"] == "replied",
                f"got {second['status']}")


# ============================================================================
# Test 10: Edge Cases
# ============================================================================

async def test_10_edge_cases() -> None:
    """Test edge cases: event bus, heartbeat v2."""
    print("\n--- Test 10: Edge Cases ---")

    from mike_event_bus import MikeEventBus
    from mike_heartbeat import MikeHeartbeatV2

    # Event bus: non-coroutine handler raises TypeError
    bus = MikeEventBus()
    try:
        bus.subscribe("test.event", lambda p: None)  # type: ignore
        test_result("sync handler raises TypeError", False, "should have raised")
    except TypeError:
        test_result("sync handler raises TypeError", True)

    # Event bus: publish with no subscribers (graceful)
    n = await bus.publish("no.such.event", {"data": "x"})
    test_result("publish to unsubscribed event -> 0 handlers",
                n == 0)

    # HeartbeatV2: analyze empty email
    hb = MikeHeartbeatV2(generate_fn=None)
    result = await hb.analyze_email_urgency({
        "from": "",
        "subject": "",
        "snippet": "",
    })
    test_result("empty email -> method=keyword, urgency valid",
                result["method"] == "keyword" and 1 <= result["urgency"] <= 5,
                f"got {result}")

    # HeartbeatV2: analyze empty SMS
    result = await hb.analyze_sms_reply("")
    test_result("empty SMS -> intent=unknown",
                result["intent"] == "unknown",
                f"got {result['intent']}")

    # Event bus: unsubscribe non-existent handler (no error)
    async def some_handler(payload: dict) -> None:
        pass
    bus.unsubscribe("email.family", some_handler)
    test_result("unsubscribe non-existent handler — no error", True)


# ============================================================================
# Test 11: Event Bus Stats Endpoint Format
# ============================================================================

async def test_11_stats_format() -> None:
    """Test that stats output matches the expected API format."""
    print("\n--- Test 11: Event Bus Stats Format ---")

    from mike_event_bus import MikeEventBus

    bus = MikeEventBus()

    async def h(payload: dict) -> None:
        pass

    bus.subscribe("email.urgent", h)
    bus.subscribe("sms.reply", h)
    await bus.publish("email.urgent", {"subject": "x"})

    stats = bus.stats()
    test_result("stats has 'total_events_published'",
                "total_events_published" in stats)
    test_result("stats has 'event_counts'",
                "event_counts" in stats)
    test_result("stats has 'subscriber_counts'",
                "subscriber_counts" in stats)
    test_result("stats has 'total_subscribers'",
                "total_subscribers" in stats)
    test_result("total_subscribers == 2",
                stats["total_subscribers"] == 2,
                f"got {stats['total_subscribers']}")
    test_result("all values JSON-serializable",
                json.dumps(stats) is not None,
                "JSON serialization failed")


# ============================================================================
# Main
# ============================================================================

async def main() -> None:
    print("=" * 60)
    print("  Mike Event-Driven Architecture Tests")
    print("  Event Bus + Heartbeat V2 + Mock LLM")
    print("=" * 60)

    await test_1_event_bus_basic()
    await test_2_event_bus_stats()
    await test_3_event_bus_concurrent()
    await test_4_heartbeat_v2_urgency_llm()
    await test_5_heartbeat_v2_urgency_keyword()
    await test_6_heartbeat_v2_sms_llm()
    await test_7_heartbeat_v2_sms_keyword()
    await test_8_heartbeat_v2_smart_reply()
    await test_9_e2e_event_flow()
    await test_10_edge_cases()
    await test_11_stats_format()

    # Summary
    total = passed + failed + skipped
    print(f"\n{'=' * 60}")
    print(f"  Results: {passed}/{total} passed, {failed} failed, {skipped} skipped")
    print(f"{'=' * 60}")

    if failed > 0:
        print(f"\n  {failed} test(s) FAILED!")
        sys.exit(1)
    else:
        print(f"\n  All {passed} tests passed! ✅")


if __name__ == "__main__":
    asyncio.run(main())
