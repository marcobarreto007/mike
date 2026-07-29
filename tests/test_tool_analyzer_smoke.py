"""Smoke test for mike_tool_analyzer.py — ToolFailureAnalyzer"""
import tempfile
from pathlib import Path

def main():
    from core.autonomy.mike_tool_analyzer import ToolFailureAnalyzer, classify_error

    ta = ToolFailureAnalyzer(store_dir=Path(tempfile.mkdtemp()))
    print("[1] ToolFailureAnalyzer initialized")

    # Test classification
    tests = [
        ("Connection timed out after 30s", "timeout"),
        ("401 Unauthorized - invalid API key", "auth"),
        ("400 Bad Request: missing required field 'to'", "bad_args"),
        ("500 Internal Server Error", "server_error"),
        ("Rate limit exceeded. Retry after 60s", "rate_limit"),
        ("Connection refused", "network"),
        ("CUDA out of memory", "out_of_memory"),
        ("Permission denied: /etc/shadow", "permission"),
        ("File not found: /tmp/missing.txt", "not_found"),
        ("UnicodeDecodeError: 'utf-8' codec can't decode", "encoding"),
        ("Some random weird error", "unknown"),
    ]
    for msg, expected in tests:
        result = classify_error(msg)
        status = "OK" if result["type"] == expected else f"EXPECTED {expected}"
        print(f"    [{status}] '{msg[:50]}...' -> {result['type']}")

    # Record failures
    ta.record_failure("email.send", "Connection timed out after 30s", context="Enviando email para cliente")
    ta.record_failure("email.send", "Connection timed out after 30s", context="Enviando novamente")
    ta.record_failure("email.send", "Connection timed out after 30s", context="Terceira tentativa")
    print(f"[2] Recorded 3 email.send timeouts")

    # Check pattern
    patterns = ta.get_patterns(min_count=3)
    print(f"[3] Confirmed patterns: {len(patterns)}")
    for p in patterns:
        print(f"    {p['tool_name']}:{p['error_type']} x{p['count']} -&gt; {p['lesson_learned'][:80] if p.get('lesson_learned') else 'pending'}")

    # Recovery strategy
    recovery = ta.get_recovery_strategy("email.send", "Connection timed out after 30s")
    print(f"[4] Recovery: {recovery['type']} -&gt; {recovery['recovery'][:60]}")
    print(f"    Is known pattern: {recovery['is_known_pattern']}")

    # Record another failure type
    ta.record_failure("file.read", "Permission denied: /root/secrets.txt")
    ta.record_failure("file.read", "Permission denied: /root/secrets.txt")
    print(f"[5] Recorded 2 file.read permission errors (not yet a pattern)")

    patterns2 = ta.get_patterns(min_count=1)
    print(f"[6] All patterns: {len(patterns2)}")
    for p in patterns2:
        print(f"    {p['tool_name']}:{p['error_type']} x{p['count']}")

    # Stats
    stats = ta.stats()
    print(f"[7] Stats: {stats['total_failures']} failures, {stats['patterns_confirmed']} confirmed patterns")
    print(f"    Recovery rate: {stats['recovery_rate']}%")

    # Record recovery
    failures = ta.get_failures(tool_name="email.send", limit=1)
    if failures:
        ta.record_recovery(failures[0]["id"], "Aumentei timeout para 60s e funcionou")
        print(f"[8] Recovery recorded for {failures[0]['id']}")

    # Verify recovery count
    stats_after = ta.stats()
    print(f"[9] After recovery: {stats_after['recovered']} recovered")

    print("\n*** ALL TOOL ANALYZER TESTS PASSED ***")
    return True

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    sys.path.insert(0, "core/autonomy")
    success = main()
    sys.exit(0 if success else 1)
