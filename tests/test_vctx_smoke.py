"""Smoke test for mike_context_virtual.py — VirtualContextManager"""
import tempfile
from pathlib import Path

def main():
    from core.chat.mike_context_virtual import VirtualContextManager

    vctx = VirtualContextManager(
        store_dir=Path(tempfile.mkdtemp()),
        max_fast_pages=10,
        page_max_chars=500,
        swap_threshold=0.7,
    )
    print("[1] VirtualContextManager initialized")

    vctx.add_page("Sistema: voce e o Mike.", role="system", priority=1.0)
    vctx.add_page("Marco pediu para verificar emails.", role="user", priority=0.7, importance=0.8)
    vctx.add_conversation_turn(
        "Mike, quantos emails nao lidos?",
        "Voce tem 5 emails nao lidos."
    )
    vctx.add_tool_result("email.list_inbox", "5 emails encontrados")
    print(f"[2] Pages: {len(vctx._fast_memory)} in fast memory")

    stats = vctx.stats()
    assert stats["fast_pages"] > 0
    print(f"[3] Stats: {stats['fast_pages']} fast pages")

    # Add many pages to trigger swap
    for i in range(12):
        vctx.add_page(f"Mensagem {i}: " + "x" * 200, role="conversation", priority=0.3, importance=0.2)

    print(f"[4] After bulk add: {len(vctx._fast_memory)} fast, {len(vctx._slow_memory)} slow")
    assert len(vctx._slow_memory) > 0 or vctx._swap_count > 0, "Swap should have occurred"

    ctx = vctx.assemble_context(max_tokens=500)
    assert len(ctx) > 0
    print(f"[5] Assembled context: {len(ctx)} chars")

    result = vctx.compact()
    print(f"[6] Compact: fast={result['fast_pages']}, slow={result['slow_pages']}")

    print("\n*** ALL VIRTUAL CONTEXT TESTS PASSED ***")
    return True

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    sys.path.insert(0, "core/chat")
    success = main()
    sys.exit(0 if success else 1)
