# Bootstrap do knowledge dropzone

- **Dropzone:** `runtime/knowledge/` — indexado no startup e via `POST /v1/knowledge/reindex`.
- **Indexador:** memory service (`_ms()`), embedder local + LightRAG. Rota: `core/server/mike_routes_knowledge.py`.
- **Formatos indexáveis:** `.md .txt .json .jsonl .yaml .yml`. **Não-indexáveis diretamente:** `.pdf .docx .csv` (precisam extração/normalização p/ `.md`/`.txt`).
- **Endpoints:** `POST /v1/knowledge/reindex` (→ `indexed_sources`), `POST /v1/knowledge/upsert` (1 ficheiro, flags `enable_vector`/`enable_lightrag`), `POST /v1/drive/index` (Drive → reindexa; owner-only).
- **Subpastas e propósito:** `current_docs/` (docs de API atuais), `drive_docs/` (docs do Google Drive do utilizador — muitos `.pdf/.docx/.csv`), `harvested/` (pesquisa web → `.md`), `learnings/` (JSON diários), `public_domain_fused/` (livros), `web_cache/` (cache de busca).
- **Janitor:** `scripts/janitor/cleanup_memory.py`, `deep_analyze.py`.
- **Auth:** se `MIKE_API_KEY` definido, endpoints precisam `Authorization: Bearer <key>` (a menos que `MIKE_TRUST_LOCALHOST=true`).
- **Regra:** nunca indexar conteúdo privado do utilizador sem contexto — o dropzone é RAG pessoal da família.
