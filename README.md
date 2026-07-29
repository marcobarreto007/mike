# MIKE — Yorkshire Operator Console

Assistente pessoal de IA com personalidade de Yorkshire Terrier para a familia Barreto.
LLM local (Qwen3.6 35B MoE) via `llama-server`, com Qwen como unico cerebro.

## Features

- **Chat multi-perfil** — Marco, Ana Paula, Raphael, Alice, Matheus, Marilene, Visitante
- **LLM local** — Qwen3.6-35B-A3B GGUF com GPU offload, zero custo de API
- **Backend unico** — sem fallback silencioso para mock, DeepSeek ou outro modelo
- **Dashboard PWA** — Chat, Lousa de Tarefas, Status, voz, camera, TTS
- **Motor de Autonomia** — briefing matinal, email tracking, missoes, governanca
- **RAG / Memoria** — busca hibrida (BM25 + embeddings + reranker), LightRAG, Mem0
- **Email SMTP/IMAP** — Gmail integrado para envio e leitura
- **153 Tools / 16 servidores** — workspace e PowerShell controlado, memoria,
  raciocinio sequencial, filesystem, GitHub, SQLite, Fetch, Puppeteer, Excel,
  agendamentos, Hugging Face, Google Workspace e agente Windows remoto
- **48 Skills executaveis** — 48/48 ligadas a ferramentas reais, com 100% dos
  padroes declarados resolvidos no manifesto
- **Twilio SMS** — envio de SMS e voice calls (requer token)
- **Telegram** — notificacoes proativas (requer token)
- **Google Workspace** — Gmail API, Calendar, Drive (requer OAuth)

## Pre-requisitos

- Windows 10/11 com PowerShell 5.1+
- Python 3.11+ (com venv)
- NVIDIA GPU com 8GB+ VRAM (RTX 2070 ou superior)
- NVIDIA Driver 450+ com CUDA 12.x
- [llama.cpp](https://github.com/ggerganov/llama.cpp) compilado com CUDA
- Node.js 18+ (para MCP servers)
- Git

## Instalacao Rapida

```powershell
# 1. Clona o repo
git clone <repo-url> mike
cd mike

# 2. Cria virtualenv
python -m venv .venv
.venv\Scripts\activate

# 3. Instala dependencias
pip install -r config/requirements.txt

# 4. Configura ambiente
copy config/.env.example config/.env.runtime
# Edita config/.env.runtime com os teus tokens e paths

# 5. Descarrega o modelo Qwen GGUF (~18,2 GB)
scripts/ops/download_model.ps1

# 6. Inicia Qwen, Mike e as verificacoes de saude
scripts/ops/launch_mike.ps1
```

Abre http://localhost:8083 no browser.

## Acesso em Familia (LAN)

Com `MIKE_HOST=0.0.0.0`, o Mike fica acessivel em toda a rede local.
Dispositivos na mesma rede acedem via `http://<ip-do-pc>:8083`.

Para acesso externo seguro, configura o Cloudflare Tunnel:
```powershell
scripts/ops/install_tunnel_service.ps1
```

## Arquitetura

```
core/
  server/       FastAPI + chat + streaming + auth
  autonomy/     Motor proativo (heartbeat, missoes, task board)
  memory/       RAG hibrido (SQLite + embeddings + LightRAG + Mem0)
  chat/         Contexto virtual multi-perfil (MemGPT-style)
  mcp/          MCPs: workspace, email, calendar, Drive, Excel, appointments,
                Hugging Face e agente remoto
  integrations/ Qwen/llama-server, Google OAuth, Drive Indexer, Web Search
  orchestration/ Swarm multi-agente + Agent SDK + Task Mesh
  comms/        Email SMTP/IMAP, Twilio SMS, Telegram
  shared/       Utilitarios partilhados (time, etc.)

dashboard/      SPA (vanilla JS, PWA, voice, camera, TTS)
config/         .env.runtime, identity, family profiles, MCP servers
scripts/        PowerShell (launch, tunnel, email, setup)
skills/         Skills YAML catalog (48 skills)
tests/          Smoke, unit, integration, E2E, performance
```

## Variaveis de Ambiente Principais

| Variavel | Default | Descricao |
|----------|---------|-----------|
| `MIKE_HOST` | `0.0.0.0` | Interface de rede |
| `MIKE_PORT` | `8083` | Porta do servidor |
| `MIKE_LLM_BACKEND` | `llama_server` | Backend unico de inferencia |
| `MIKE_LLM_BACKENDS` | `llama_server` | Cadeia permitida (somente Qwen) |
| `MIKE_LLAMA_SERVER_URL` | `http://127.0.0.1:8081/v1` | API do Qwen |
| `MIKE_LLAMA_SERVER_MODEL` | `mike` | Alias OpenAI-compatible |
| `MIKE_QWEN_ENABLE_THINKING` | `false` | Desativa blocos de raciocinio na resposta |
| `MIKE_PROFILE_*_PASSWORD` | — | Password por perfil |
| `MIKE_ENABLE_VISION` | `false` | Desabilitado: o Qwen atual e text-only |
| `MIKE_ENABLE_MCP_TOOLS` | `true` | MCP tools ativas |
| `MIKE_GRAPH_ENABLED` | `false` | Neo4j knowledge graph |
| `MIKE_CLEANUP_ON_BOOT` | `false` | Limpeza no startup |

Ver `config/.env.example` para a lista completa.

## Prontidao das Integracoes

O nucleo local funciona sem servicos pagos. Integracoes externas aparecem como
indisponiveis, em vez de simularem sucesso, ate receberem credenciais validas:

- Google Workspace: execute
  `python scripts/setup/setup_google_workspace_oauth.py` e configure o token OAuth;
- email IMAP/SMTP: configure uma senha de app valida;
- Telegram e Twilio: configure os respectivos tokens;
- CrawlConsole e Brave: configure as chaves de API;
- agente Windows remoto: configure `MIKE_REMOTE_AGENT_KEY` e mantenha
  `MIKE_REMOTE_AGENT_URL` acessivel;
- Neo4j: instale o servico e habilite `MIKE_GRAPH_ENABLED`;
- acesso publico: instale e configure `cloudflared`.

O endpoint `GET /v1/tools` mostra o inventario e o erro de cada servidor MCP.
`GET /v1/skills/coverage` confirma se todas as skills estao alcancaveis.

Auditoria completa e nao destrutiva:

```powershell
python scripts/ops/check_mike_readiness.py
```

O comando retorna sucesso quando o nucleo e todas as tools locais passam.
Use `--strict` para tambem exigir credenciais e integracoes externas.

## Testes

```powershell
# Smoke test (servidor precisa estar a correr)
python tests/test_smoke.py

# Suite completa
python tests/e2e/test_e2e_full.py

# PowerShell
tests/e2e/run_mike_a_to_z.ps1
```

## Seguranca

- HMAC-SHA256 para sessoes com timing-attack protection
- PBKDF2-HMAC-SHA256 (200K iteracoes) para passwords
- Rate limiting: 60 req/min global
- Headers: X-Content-Type-Options, X-Frame-Options, Referrer-Policy
- SQL parameterizado (sem injection)
- DOMPurify no dashboard (XSS protection)
- Cloudflare Tunnel para HTTPS externo
- `mike_verifier.py` — scan estatico de anti-patterns

## Licenca

Proprietary software — see LICENSE file in project root.
Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
