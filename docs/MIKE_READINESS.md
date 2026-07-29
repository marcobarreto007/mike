# Mike — Estado de prontidão

Atualizado em 2026-07-24. Este documento separa capacidade operacional de
integração apenas instalada/configurada.

## Núcleo operacional

| Componente | Estado | Evidência |
|---|---|---|
| Cérebro | Pronto | Somente `llama_server`, modelo Qwen3.6-35B-A3B IQ4_XS |
| API/dashboard | Pronto | FastAPI saudável em `http://127.0.0.1:8083` |
| Tools | Pronto | 153 tools em 16 servidores, incluindo tools locais |
| Skills | Pronto | 48/48 empacotadas e executáveis; cobertura de padrões 100% |
| TaskMesh | Pronto | Planejamento, escopo semântico e uma tool por passo |
| Autonomia | Pronto | Motor ativo, 6/6 rotinas habilitadas |
| Governança | Pronto | Loop ativo e último ciclo saudável |
| Memória | Pronto | Mem0 OSS + SQLite + BGE-M3 + Memory Mesh |
| LightRAG | Pronto | Índice criado e consulta de validação concluída |
| Busca web | Pronto | DDGS e Fetch funcionais sem chave paga |

O launcher oficial valida/inicia primeiro o Qwen na porta 8081 e recusa outro
modelo nessa porta. A API não cai silenciosamente para mock ou DeepSeek.

## MCPs validados

- workspace: listagem dos diretórios permitidos;
- filesystem: leitura/listagem dentro das raízes autorizadas;
- memory-persistent: leitura do grafo;
- sequential-thinking: execução de raciocínio sequencial;
- SQLite: listagem de tabelas e consultas;
- Fetch: leitura HTTP de página externa;
- Puppeteer: abertura do dashboard local;
- GitHub: busca pública de repositórios;
- Excel: listagem/leitura/escrita local controlada;
- Appointments: agenda persistente e máquina de estados;
- Hugging Face: identidade pública, modelos, datasets e model cards;
- Autonomia: status e lousa de tarefas;
- Workspace PowerShell: execução não interativa, confinada ao `cwd` autorizado;
- Qwen tool loop: `autonomy_status` e `execute_powershell` escolhidas e
  executadas pelo próprio Qwen em conversas reais.

## Dependências externas pendentes

Estas integrações não podem ser ativadas corretamente sem credenciais ou
software do proprietário:

| Integração | Estado atual | Necessário |
|---|---|---|
| Google Workspace | OAuth ausente | Executar `scripts/setup/setup_google_workspace_oauth.py` |
| Email IMAP/SMTP | Credencial rejeitada | Gerar/configurar senha de app válida |
| Telegram | Token ausente | Bot token e chat IDs |
| Twilio | Credenciais ausentes | Account SID, auth token e números |
| CrawlConsole | HTTP 401 | Chave/header de autenticação |
| Brave Search | Chave ausente | `BRAVE_API_KEY` (DDGS continua disponível) |
| GitHub privado/escrita | Token ausente | Token com o menor escopo necessário |
| Agente Windows remoto | Host fora do ar/chave ausente | Ligar o agente em `192.168.40.60:3000` e configurar `MIKE_REMOTE_AGENT_KEY` |
| Neo4j | Serviço ausente | Instalar, configurar e habilitar o grafo |
| Cloudflare Tunnel | Binário/config ausente | Instalar `cloudflared` e configurar túnel |
| Perfis familiares | 5 senhas ausentes | Configurar senhas de Raphael, Alice, Matheus, Marilene e Visitante |

## Limitações honestas

- Qwen3.6-35B-A3B é text-only. Visão fica desabilitada até existir um caminho
  compatível que preserve a regra de um único cérebro.
- A RTX 2070 funciona, é detectada como GPU0 e é observada pela governança.
- LightRAG foi validado com o README. A base principal já possui busca híbrida
  sobre 179 documentos e mais de 63 mil chunks; uma reconstrução integral do
  grafo LightRAG deve ser agendada, pois é uma operação longa.

## Verificações rápidas

```powershell
Invoke-RestMethod http://127.0.0.1:8083/health
Invoke-RestMethod http://127.0.0.1:8083/v1/health/models
Invoke-RestMethod http://127.0.0.1:8083/v1/tools
Invoke-RestMethod http://127.0.0.1:8083/v1/skills/coverage
Invoke-RestMethod http://127.0.0.1:8083/v1/autonomy/status
Invoke-RestMethod http://127.0.0.1:8083/v1/governance
python scripts/ops/check_mike_readiness.py
```

O verificador retorna `0` quando cérebro, memória, autonomia, governança,
skills e ferramentas locais estão prontos. Use `--strict` para exigir também
OAuth, tokens, túnel, Neo4j e o computador remoto.
