# MIKE — contexto para o Claude Code

MIKE é um assistente pessoal de IA **100% local** da família Barreto. Este ficheiro
é carregado automaticamente pelo Claude Code em cada sessão — é a "memória de projeto".
Lê antes de agir.

## ⚠️ REGRA DE OURO (não negociável)

> **O MIKE é 100% LOCAL. O cérebro é o Qwen no `llama-server` local (porta 8081).
> NUNCA sugiras nuvem, DeepSeek, OpenAI, Anthropic ou qualquer API externa como
> LLM do MIKE.** Não há fallback silencioso para mock/cloud.

Se um pedido implicar enviar dados ou inferência para fora, recusa e explica porquê.

## Arquitetura two-process

```
[ MIKE :8083 ]  --POST /v1/chat/completions-->  [ llama-server :8081 (Qwen IQ4_XS) ]
   FastAPI: corpo (memória, agentes, tools, autonomia)     cérebro (inferência)
```

- **MIKE** = orquestrador FastAPI (`core/server/`). O "corpo". **Não gera texto.**
- **llama-server** = processo SEPARADO do `llama.cpp` que serve o
  `Qwen3.6-35B-A3B-UD-IQ4_XS.gguf` (~18 GB) em `http://127.0.0.1:8081/v1`
  (API OpenAI-compatible). O "cérebro".
- Hardware validado: RTX 2070 8GB (driver 596.49, compute 7.5), offload híbrido
  GPU+CPU (especialistas MoE na CPU).
- Mem0 e LightRAG também apontam a `:8081`.
- Sem cérebro (`:8081` em baixo): chat = 500, autonomia/monitor/learner = 503.

**Diagnóstico "MIKE não arranca":** `curl http://127.0.0.1:8081/v1/models` → se não
responde, o problema é do Qwen, não do MIKE. Ver `mike-architect` para o runbook.

## Layout

```
core/
  server/         FastAPI, auth, chat, SSE, tools locais, rotas (mike_routes_*)
  autonomy/       rotinas, missões, governança, skills, task board
  memory/         SQLite, busca híbrida, Mem0, LightRAG, reranking
  chat/           contexto virtual
  mcp/            Gmail, Calendar, Drive, Excel, GA4, Ads, Shopify...
  integrations/   Qwen/llama-server, Google OAuth, busca web
  orchestration/  TaskMesh, Agent SDK, swarm
  comms/          email, Telegram, Twilio
dashboard/        PWA em JavaScript ("Mike Operator Console")
config/           .env.runtime, requirements.txt, manifesto MCP
scripts/          setup, ops, recuperação, auditoria, janitor
skills/           catálogo YAML de skills DO PRÓPRIO MIKE (54)
tests/unit/       testes offline coletados pelo pytest (234 testes)
tests/integration/  explícitos (não coletados)
tests/e2e/        fluxos completos com runtime (run_mike_a_to_z.ps1)
runtime/          knowledge/, memory/, backups/, state/, roadmap/, cache/
```

## Comandos-chave

```powershell
# Arrancar (Qwen primeiro, MIKE depois)
.\scripts\ops\start_qwen36_server.ps1 -ModelPath "llama.cpp\models\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"
.\scripts\ops\start_mike.ps1 -Port 8083 -ForceRestart -SkipTunnel
.\scripts\ops\launch_mike.ps1 -SkipTunnel            # tudo num comando

# Estado / recuperação
.\scripts\ops\recover_mike.ps1 -Mode status
.\scripts\ops\recover_mike.ps1 -Mode restart -SkipTunnel
.\scripts\ops\check_mike_readiness.py                 # readiness do núcleo
.\scripts\ops\check_mike_readiness.py --strict        # + integrações externas

# Saúde rápida
Invoke-RestMethod http://127.0.0.1:8081/health
Invoke-RestMethod http://127.0.0.1:8083/health

# Testes
.\.venv\Scripts\python.exe -m pytest -q               # unit (offline)
.\tests\e2e\run_mike_a_to_z.ps1                       # e2e completo (inicia/para serviços)
```

## Knowledge (RAG dropzone)

`runtime/knowledge/` é indexado no startup e via `POST /v1/knowledge/reindex`
(rota em `core/server/mike_routes_knowledge.py`). O indexador é o **memory service**
(embedder local + LightRAG).

- **Formatos indexáveis:** `.md .txt .json .jsonl .yaml .yml`
- **Subpastas:** `current_docs/`, `drive_docs/` (docs do Google Drive do utilizador),
  `harvested/` (pesquisa web), `learnings/` (JSON), `public_domain_fused/` (livros),
  `web_cache/`.
- `.pdf/.docx/.csv` **não são indexados diretamente** → precisam de extração/normalização.
- Endpoints: `POST /v1/knowledge/reindex`, `POST /v1/knowledge/upsert`,
  `POST /v1/drive/index` (Drive → reindexa).
- Janitor: `scripts/janitor/cleanup_memory.py`, `deep_analyze.py`.
- Ver `mike-knowledge` para stewardship do dropzone.

## Memória

SQLite + Mem0 + LightRAG + busca híbrida com reranking. Dados em `runtime/memory/`
(incl. `mem0/` e backups). Backups em `runtime/backups/`. Ver `mike-memory-janitor`.

## Segurança (padrões a respeitar em qualquer alteração)

- Sessões assinadas com **HMAC-SHA256**; senhas de perfil com **PBKDF2-HMAC-SHA256**;
  comparação constante de chaves/hashes.
- Rate limiting + headers de segurança.
- **Isolamento de memória e sessões por perfil.**
- Tools sensíveis restritas ao proprietário; `_can_view_operational_details`.
- **Execução PowerShell confinada às raízes permitidas** (não alargar confinamento
  sem motivo forte).
- **Integrações indisponíveis devolvem erro real — nunca simular sucesso.**
- Acesso LAN via `MIKE_HOST=0.0.0.0`; NUNCA expor a 8083 diretamente à internet sem
  HTTPS + túnel autenticado.

## Agentes Claude Code (`.claude/agents/`)

Orquestração de topo via **`mike-orchestrator`** — lê a conversa, roteia para o subagente
certo (mike-*, swat-*, gpu-*, mbj-*) e sintetiza resultados. Para implementação multi-ficheiro
genérica, o orquestrador delega a **`swat-lead`** (fan-out com Agent tool, max 4-6 concorrentes,
file-ownership único por iteração). Famílias:

- **`mike-*`** (específicos do Mike): `mike-architect`, `mike-launch`, `mike-offload`,
  `mike-cuda`, `mike-knowledge`, `mike-e2e`, `mike-review`, `mike-memory-janitor`,
  `mike-mcp`, `mike-skills`.
- **`swat-*`** (disciplina genérica): `swat-architect/backend/frontend/database/devops/
  security/qa/performance/review/debug/git/lead`.
- **`gpu-*`**, **`mbj-*`** (GPU/quantização e pipeline MBJ).

**Memória por agente:** `.claude/agent-memory/<nome>/MEMORY.md` (índice → notas).
Lê/carrega conforme o `memory:` do frontmatter.

## ⚠️ NÃO confundir sistemas

- **`.claude/agents/`** = agentes do **Claude Code** (este CLI). ← onde se mexe em agentes.
- **`agents/`** (top-level) = personas internas do **próprio MIKE** (architect, planner,
  code-reviewer...). Sistema diferente.
- **`skills/*.yaml`** = catálogo de skills do **próprio MIKE** (não confundir com skills
  do Claude Code).

## Convenções de código

- Python 3.11; venv em `.venv`; deps em `config/requirements.txt`.
- FastAPI com `APIRouter`; rotas extraídas em `core/server/mike_routes_*.py` e helpers
  em `mike_*_helpers.py`. Estado partilhado via `shared_state`.
- Testes unitários em `tests/unit` (offline, coletados pelo pytest); dependentes do
  runtime em `tests/integration` e `tests/e2e` (não coletados por defeito).
- Software proprietário © 2025–2026 Marco Barreto.
