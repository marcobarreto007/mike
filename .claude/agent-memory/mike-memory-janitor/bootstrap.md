# Bootstrap do stack de memória

- **Stack:** SQLite (memória estruturada/perfis) + Mem0 (memória semântica, vetorial) + LightRAG (grafo de conhecimento) + busca híbrida com reranking.
- **Backend de embeddings/LLM para Mem0 e LightRAG:** aponta ao Qwen local `:8081` (`MIKE_MEM0_OPENAI_BASE_URL`, `MIKE_LIGHTRAG_LLM_BASE_URL`).
- **Dados:** `runtime/memory/` (incl. `mem0/` com `qdrant_store` e `backups/`), SQLite em `database.db`.
- **Janitor:** `scripts/janitor/cleanup_memory.py` (GC/limpeza), `scripts/janitor/deep_analyze.py` (análise profunda). Ler antes de executar — confirmar flags.
- **Backups:** `runtime/backups/` (`.zip`) + `runtime/memory/mem0/backups/` (snapshots de reset, ex. `mem0_reset_..._1024d_to_384d`).
- **Reset de dimensionalidade:** houve reset 1024d→384d (2026-07-24) — não recriar store com dimensão errada.
- **Cuidado:** operações destrutivas (reset/wipe) só com confirmação explícita do utilizador e após backup.
