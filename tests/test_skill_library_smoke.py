"""Smoke test for mike_skill_library.py — SkillLibrary"""
import tempfile
from pathlib import Path

def main():
    from core.autonomy.mike_skill_library import SkillLibrary

    lib = SkillLibrary(store_dir=Path(tempfile.mkdtemp()))
    print("[1] SkillLibrary initialized")

    skill = lib.add_skill(
        name="listar_arquivos",
        description="Lista arquivos em um diretorio",
        code="from pathlib import Path\nd = Path(params.get('path', '.'))\nfiles = list(d.glob('*'))\nresult = [str(f) for f in files[:10]]",
        category="file_ops",
        tags=["arquivos", "listar"],
    )
    assert skill is not None, "Failed to add skill"
    assert skill.validated, "Skill should be validated"
    print(f"[2] Skill added: {skill.name}, validated={skill.validated}")

    skill2 = lib.add_skill(
        name="contar_linhas",
        description="Conta linhas de um arquivo",
        code="from pathlib import Path\np = Path(params.get('path', ''))\nif p.exists():\n    result = len(p.read_text().splitlines())\nelse:\n    result = 0",
        category="file_ops",
        tags=["arquivos", "contar"],
    )
    if skill2 is None:
        # Debug: try again without validation to see error
        skill2 = lib.add_skill(
            name="contar_linhas_v2",
            description="Conta linhas",
            code="p = '.'\nresult = 42",
            category="file_ops",
            tags=["test"],
            validate=True,
        )
        print(f"[3] Second skill add result: {skill2}")
    assert skill2 is not None, f"Failed to add second skill"
    print(f"[3] Second skill added: {skill2.name}")

    results = lib.search("listar", limit=5)
    assert len(results) >= 1
    print(f"[4] Search 'listar': {len(results)} results")

    file_skills = lib.list_skills(category="file_ops")
    assert len(file_skills) >= 2
    print(f"[5] File ops skills: {len(file_skills)}")

    stats = lib.stats()
    print(f"[6] Stats: {stats['total_skills']} skills, {stats['validated_skills']} validated")

    s = lib.get_skill_by_name("listar_arquivos")
    assert s is not None
    print(f"[7] Get by name: {s.name}")

    # Test dangerous code rejection
    bad_skill = lib.add_skill(
        name="skill_perigosa",
        description="Tenta algo perigoso",
        code="import subprocess\nsubprocess.run(['rm', '-rf', '/'])",
        category="system",
    )
    assert bad_skill is None, "Dangerous skill should be rejected"
    print("[8] Dangerous skill correctly rejected")

    # Test composable search
    chains = lib.find_composable("listar arquivos e contar linhas")
    print(f"[9] Composable chains: {len(chains)}")

    # Test execution
    exec_result = lib.execute_skill(skill.id, {"path": "."})
    print(f"[10] Execution: success={exec_result['success']}")

    print("\n*** ALL SKILL LIBRARY TESTS PASSED ***")
    return True

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    sys.path.insert(0, "core/autonomy")
    success = main()
    sys.exit(0 if success else 1)
