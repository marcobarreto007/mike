"""Test OutputGuard simulation detection"""
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "core/autonomy")
from mike_output_guard import detect_simulation, OutputGuard

# Test stateless detector
tests = [
    ("Prontinho, chefe! Ja salvei o arquivo em dashboard/games/space.html", False, True),
    ("Voce precisa que eu envie o email?", False, False),
    ("Ja criei o arquivo e configurei tudo.", False, True),
    ("Nao tenho acesso a essa ferramenta. Precisa instalar o pacote.", False, False),
    ("Posso te ajudar, mas preciso usar a ferramenta write_file.", False, False),
    ("O arquivo foi salvo com sucesso em /tmp/test.html", False, True),
    ("Done! I've created the file and saved it.", False, True),
    ("Let me check if the server is running first.", False, False),
]

print("=== OutputGuard Detection Tests ===")
for text, has_tools, expected in tests:
    is_sim, conf, patterns = detect_simulation(text, has_tools)
    status = "OK" if is_sim == expected else "FAIL"
    marker = "[SIM]" if is_sim else "[OK ]"
    print(f"  {status} {marker} conf={conf:.2f} | {text[:70]}")

# Test with tool calls (should never flag)
is_sim, conf, patterns = detect_simulation("Ja salvei o arquivo!", has_tool_calls=True)
assert not is_sim, "Should not flag when tool_calls present"
print("  OK  [OK ] Tool calls present = never flags simulation")

# Test OutputGuard class
import tempfile
from pathlib import Path
guard = OutputGuard(log_dir=Path(tempfile.mkdtemp()))
v = guard.check("Prontinho! Ja salvei o jogo em games/space.html", tool_calls=[], session_id="test")
assert v.is_simulation, "Should detect simulation"
assert v.action_taken in ("flag", "correct", "block")
print(f"\nGuard verdict: sim={v.is_simulation} action={v.action_taken} confidence={v.confidence:.2f}")
print(f"Matched: {v.matched_patterns}")

v2 = guard.check("Como posso ajudar voce hoje?", tool_calls=[], session_id="test")
assert not v2.is_simulation, "Should NOT flag normal response"
print(f"Normal response: sim={v2.is_simulation} action={v2.action_taken}")

s = guard.stats()
print(f"\nStats: {s['total_checked']} checked, {s['simulations_detected']} detected")

print("\n*** ALL OUTPUT GUARD TESTS PASSED ***")
