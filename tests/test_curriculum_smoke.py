"""Smoke test for mike_curriculum.py — AutoCurriculum"""
import tempfile
from pathlib import Path

def main():
    from core.autonomy.mike_curriculum import AutoCurriculum, ExplorationGoal

    c = AutoCurriculum(store_dir=Path(tempfile.mkdtemp()))
    print("[1] AutoCurriculum initialized")

    # Generate goals
    goals = c.generate_goals(count=5)
    print(f"[2] Generated {len(goals)} goals:")
    for g in goals:
        print(f"    [{g.difficulty}] {g.description[:60]}")

    assert len(goals) > 0, "Should generate at least some goals"

    # List
    all_goals = c.list_goals()
    print(f"[3] Total goals: {len(all_goals)}")

    # Next goal
    next_g = c.next_goal()
    assert next_g is not None
    print(f"[4] Next goal: {next_g.description[:60]}")

    # Start and complete
    started = c.start_goal(next_g.id)
    assert started is not None
    assert started.status == "active"
    print(f"[5] Started goal: {started.id}")

    completed = c.complete_goal(next_g.id, score=0.9)
    assert completed is not None
    assert completed.status == "completed"
    print(f"[6] Completed goal: score={completed.score}")

    # Progress
    prog = c.progress()
    print(f"[7] Progress: {prog['completed']}/{prog['total_goals']} ({prog['completion_rate']}%)")

    # Detect gaps (should be empty with no failures)
    gaps = c.detect_gaps()
    print(f"[8] Gaps detected: {len(gaps)}")

    # Idle action
    action = c.idle_action()
    print(f"[9] Idle action: {action}")

    # Fail a goal
    pending = [g for g in c._goals.values() if g.status == "pending"]
    if pending:
        started2 = c.start_goal(pending[0].id)
        failed = c.fail_goal(pending[0].id, error="Teste de falha")
        print(f"[10] Failed goal: attempts={failed.attempts}")

    print("\n*** ALL CURRICULUM TESTS PASSED ***")
    return True

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    sys.path.insert(0, "core/autonomy")
    success = main()
    sys.exit(0 if success else 1)
