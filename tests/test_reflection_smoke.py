"""Smoke test for mike_reflection.py — EpisodicReflectionStore"""
import tempfile
from pathlib import Path

def main():
    tmpdir = Path(tempfile.mkdtemp())
    from core.autonomy.mike_reflection import EpisodicReflectionStore, EpisodicReflection

    store = EpisodicReflectionStore(db_path=tmpdir / "test_reflections.db")
    store.initialize()
    print("[1] Store initialized")

    r = EpisodicReflection(
        id="test_001",
        goal="Enviar email para cliente",
        output="Email enviado com sucesso",
        success=True,
        reflection_text="Funcionou bem, lembrar de incluir assunto descritivo",
        lessons_learned=["Incluir assunto descritivo", "Verificar anexos"],
        what_worked=["SMTP configurado", "Template HTML funcionou"],
        tool_calls=["email.send"],
        iterations=2,
        elapsed_sec=3.5,
        tags=["email", "sucesso"],
    )
    assert store.save(r), "Save failed"
    print("[2] Reflection saved")

    r2 = EpisodicReflection(
        id="test_002",
        goal="Ler arquivo grande de 500MB",
        output="Timeout ao processar",
        success=False,
        reflection_text="Arquivos grandes precisam ser processados em chunks",
        lessons_learned=["Dividir arquivos grandes em chunks de 50MB"],
        mistakes_made=["Tentou carregar 500MB de uma vez"],
        tool_calls=["file.read"],
        iterations=1,
        elapsed_sec=30.0,
        tags=["arquivo", "falha"],
    )
    store.save(r2)
    print("[3] Second reflection saved")

    hits = store.search_similar("preciso enviar um email urgente", limit=3)
    # Without embedder, FTS search may return 0 or more results
    print(f"[4] Search results: {len(hits)} (without embedder, FTS-only)")

    stats = store.stats()
    assert stats["total_reflections"] == 2, f"Expected 2, got {stats['total_reflections']}"
    print(f"[5] Stats: {stats['total_reflections']} reflections, {stats['success_rate']}% success")

    recent = store.get_recent(limit=10)
    assert len(recent) == 2, f"Expected 2 recent, got {len(recent)}"
    print(f"[6] Recent: {len(recent)} reflections")

    single = store.get_by_id("test_001")
    assert single is not None, "Get by id failed"
    assert single.goal == "Enviar email para cliente"
    print(f"[7] Get by id: OK")

    # Cleanup
    store.clear()
    assert store.stats()["total_reflections"] == 0

    print("\n*** ALL REFLECTION TESTS PASSED ***")
    return True

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    sys.path.insert(0, "core/autonomy")
    success = main()
    sys.exit(0 if success else 1)
