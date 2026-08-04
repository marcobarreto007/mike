# MIKE — estado real de readiness

Atualizado em **30 de julho de 2026**, a partir do runtime e não apenas do
estado do Git.

## Resultado atual

| Área | Estado | Evidência |
|---|---|---|
| Qwen local | Pronto | `8081/health` retornou `ok` |
| Modelo | Pronto | Qwen3.6-35B-A3B UD-IQ4_XS, aproximadamente 18,2 GB |
| API/dashboard | Pronto | `8083/health` retornou `healthy` |
| Memória | Pronto | SQLite, Mem0 e LightRAG inicializados |
| Autonomia | Pronto | motor ativo, 6 rotinas habilitadas |
| Governança | Pronto | loop ativo e último ciclo saudável |
| Skills | Pronto para tools | 54 carregadas e 54 com tools disponíveis |
| Tools | Pronto | 186 tools em 18 servidores |
| Cobertura de tools | Pronto | 100% dos padrões declarados resolvidos |
| Gmail | Pronto | listagem real validada via OAuth |
| Calendar | Pronto | 9 calendários encontrados |
| Drive | Pronto | listagem real validada via OAuth |
| Google Workspace/GA4 | Pronto | token com todos os escopos requeridos |
| Email local | Pronto | adaptador Gmail retornou mensagem real |
| Testes unitários | Pronto | 234 passaram; 3 manuais foram ignorados |

O readiness local passa:

```powershell
.\.venv\Scripts\python.exe scripts\ops\check_mike_readiness.py
```

## Readiness local versus estrito

O modo normal exige:

- API, Qwen, memória, autonomia e governança saudáveis;
- apenas o backend `llama_server` como cérebro;
- tools locais essenciais funcionando;
- skills ligadas a tools reais.

O modo estrito também exige todas as integrações opcionais:

```powershell
.\.venv\Scripts\python.exe scripts\ops\check_mike_readiness.py --strict
```

Não use o resultado estrito para declarar o núcleo local quebrado quando uma
integração opcional não foi contratada, autorizada ou ligada.

## Probes locais validadas

- workspace e execução PowerShell confinada;
- memória persistente;
- raciocínio sequencial;
- filesystem;
- GitHub público;
- SQLite;
- busca web local;
- Puppeteer;
- Excel;
- appointments;
- Hugging Face;
- autonomia e task board;
- Gmail, Calendar e Drive;
- email local via Gmail API;
- introspecção do MIKE.

## Integrações externas ainda opcionais

| Integração | Estado | Ação necessária |
|---|---|---|
| GA4 | OAuth pronto | configurar a propriedade quando for usar relatórios |
| Google Ads | Depende da conta | configurar developer token e customer IDs |
| Shopify | Depende da loja | configurar domínio e access token |
| Telegram | Sem credenciais confirmadas | configurar bot token e chat IDs |
| Twilio | Sem credenciais | configurar SID, token e números |
| CrawlConsole | HTTP 401 | configurar autenticação válida |
| Neo4j | Desativado/ausente | instalar e habilitar se o grafo externo for necessário |
| Cloudflare Tunnel | Não instalado | instalar somente para acesso público |
| GitHub escrita | Leitura pública | configurar token de menor privilégio |
| Agente remoto | Offline/sem chave | ligar `192.168.40.60:3000` e configurar chave |
| Perfis familiares | 5 senhas ausentes | configurar Raphael, Alice, Matheus, Marilene e Visitante |

O token combinado de Google Workspace foi renovado com Gmail, Calendar, Drive
e `analytics.readonly`.

## Limitações conhecidas

- O Qwen atual é text-only; visão permanece desabilitada.
- Hardware atual: RTX 5060 Ti 16 GB + RTX 3060 12 GB (ver [HARDWARE.md](HARDWARE.md)).
- No perfil dual, o alvo é GPU-puro (`n_cpu_moe=0` + `tensor-split`); o hybrid
  CPU/GPU (`legacy2070`) ficou só como fallback.
- Qwen3.8-Max 2.4T não roda local neste desktop; candidatos locais: Qwen3.6-35B
  agora e Qwen3.8-27B quando o GGUF open weights sair.
- O agente remoto demora até o timeout quando o computador remoto está fora.
- Alguns servidores MCP opcionais podem estar carregados, mas exigem conta ou
  credenciais específicas para executar operações reais.
- O inventário de skills pode mudar quando novos YAMLs são adicionados; use o
  endpoint de coverage como fonte atual.

## Comandos de auditoria

```powershell
Invoke-RestMethod http://127.0.0.1:8081/health
Invoke-RestMethod http://127.0.0.1:8083/health
Invoke-RestMethod http://127.0.0.1:8083/v1/health/models
Invoke-RestMethod http://127.0.0.1:8083/v1/skills/coverage
Invoke-RestMethod http://127.0.0.1:8083/v1/autonomy/status
Invoke-RestMethod http://127.0.0.1:8083/v1/governance

.\.venv\Scripts\python.exe scripts\ops\check_mike_readiness.py
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\ops\test_ops_hardening.ps1
```

## Critério para atualizar este documento

Atualize a tabela somente depois de executar as probes correspondentes. Não
copie contagens ou estados de uma sessão antiga: portas, credenciais, skills e
servidores MCP são estado de runtime e podem mudar sem alterar o Git.
