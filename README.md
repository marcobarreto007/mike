# MIKE — assistente local da família Barreto

MIKE é um assistente pessoal de IA executado localmente no Windows. O cérebro
de produção é um único modelo Qwen3.6-35B-A3B em GGUF, servido por
`llama-server`. A API e o dashboard usam FastAPI e uma PWA em JavaScript.

## Estado validado

Última validação de produto local: **30 de julho de 2026** (stack em outra
máquina). Hardware e scripts de boot atualizados em **3 de agosto de 2026**.

- cérebro de produção: Qwen3.6-35B-A3B GGUF via `llama-server` em `:8081`;
- API/dashboard: FastAPI + PWA em `:8083`;
- quant validada no projeto: `Qwen3.6-35B-A3B-UD-IQ4_XS.gguf`;
- quant recomendada no hardware atual: `UD-Q3_K_M` (dual-GPU);
- hardware atual: RTX 5060 Ti 16 GB + RTX 3060 12 GB + 64 GB RAM + i7-12700K.

Neste workspace, o runtime ainda precisa de bootstrap (`.venv`,
`llama-server.exe`, GGUF). Veja [docs/HARDWARE.md](docs/HARDWARE.md) e
[docs/MIKE_READINESS.md](docs/MIKE_READINESS.md).

## Requisitos

- Windows 10 ou 11;
- PowerShell 5.1 ou superior;
- Python 3.11+ (validado com 3.12);
- Node.js 18 ou superior;
- GPU NVIDIA compatível com CUDA 12.x;
- pelo menos 32 GB de RAM recomendados (64 GB no hardware atual);
- aproximadamente 20–25 GB livres para o modelo e arquivos auxiliares.

Hardware atual (probe 2026-08-03): **RTX 5060 Ti 16 GB + RTX 3060 12 GB**,
driver 595.97, CUDA Toolkit 12.8, ~28 GB VRAM combinada. O launcher dual-GPU
usa `tensor-split 16,12` e `n_cpu_moe=0` por padrão.

## Instalação

```powershell
git clone <repo-url> mike
cd mike

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r config\requirements.txt

Copy-Item config\.env.example config\.env.runtime
```

Configure `config\.env.runtime` sem versionar tokens ou senhas.

Para baixar o modelo quando ele ainda não existir:

```powershell
# recomendado no par 5060 Ti + 3060
.\scripts\ops\download_model.ps1 -Quant UD-Q3_K_M

# quant histórica validada no projeto
.\scripts\ops\download_model.ps1 -Quant UD-IQ4_XS

# listar quants e o que cabe na VRAM detectada
.\scripts\ops\download_model.ps1 -ListOnly
```

## Localização do modelo

O launcher resolve `MIKE_MODEL_FILE` nesta ordem:

1. caminho absoluto passado por `-ModelPath`;
Exemplos de caminho:

```text
llm_cache\Qwen3.6-35B-A3B-UD-Q3_K_M.gguf
llama.cpp\models\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf
```

Não copie o arquivo grande só para satisfazer um path antigo. O resolver
centralizado permite manter uma única cópia.

Sobre Qwen 3.8-Max (2.4T): é API/cloud ou multi-node. Nesta máquina o caminho
local realista da linha 3.8 é o futuro **Qwen3.8-27B** open weights, não o Max

```text
llama.cpp\models\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf
```

Não copie o arquivo de 18,2 GB apenas para satisfazer um caminho antigo. O
resolver centralizado permite manter uma única cópia.

## Iniciar e parar

Inicialização local recomendada:

```powershell
.\scripts\ops\launch_mike.ps1 -SkipTunnel
```

Sem abrir o navegador:

```powershell
.\scripts\ops\launch_mike.ps1 -SkipTunnel -NoBrowser
```

Consultar o estado:

```powershell
.\scripts\ops\recover_mike.ps1 -Mode status
```

Reiniciar o conjunto:

```powershell
.\scripts\ops\recover_mike.ps1 -Mode restart -SkipTunnel
```

Reiniciar somente a API, mantendo o Qwen carregado:

```powershell
.\scripts\ops\start_mike.ps1 -Port 8083 -ForceRestart -SkipTunnel
```

Os scripts recusam encerrar processos que não sejam o MIKE desta workspace.

## Verificação

Saúde rápida:

```powershell
Invoke-RestMethod http://127.0.0.1:8081/health
Invoke-RestMethod http://127.0.0.1:8083/health
Invoke-RestMethod http://127.0.0.1:8083/v1/health/models
```

Readiness do núcleo e das tools locais:

```powershell
.\.venv\Scripts\python.exe scripts\ops\check_mike_readiness.py
```

Auditoria incluindo todas as integrações externas:

```powershell
.\.venv\Scripts\python.exe scripts\ops\check_mike_readiness.py --strict
```

O primeiro comando deve passar para operação local. O segundo somente passa
quando todas as integrações opcionais também estiverem configuradas.

## Testes

`pytest` executa somente testes unitários e offline:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Testes dependentes do runtime ficam em `tests\integration`, `tests\e2e` e nos
scripts de smoke da raiz de `tests`. Eles não são coletados automaticamente.

Exemplos:

```powershell
.\.venv\Scripts\python.exe tests\test_smoke.py
.\.venv\Scripts\python.exe tests\e2e\test_e2e_full.py
.\tests\e2e\run_mike_a_to_z.ps1
```

Veja [docs/TESTING.md](docs/TESTING.md).

## Google Workspace

O mesmo token OAuth atende Gmail, Calendar, Drive e, quando autorizado, GA4:

```powershell
$env:PYTHONPATH = (Resolve-Path core\integrations).Path
.\.venv\Scripts\python.exe scripts\setup\setup_google_workspace_oauth.py
```

Quando novos escopos forem adicionados, o script abre o navegador novamente.
O token é armazenado em `config\google_workspace_token.json` e não deve ser
versionado.

## Arquitetura

```text
core/
  server/          FastAPI, autenticação, chat, SSE e tools locais
  autonomy/        rotinas, missões, governança, skills e task board
  memory/          SQLite, busca híbrida, Mem0, LightRAG e reranking
  chat/            contexto virtual e montagem de contexto
  mcp/             Gmail, Calendar, Drive, Excel, GA4, Ads, Shopify etc.
  integrations/    Qwen/llama-server, Google OAuth e busca web
  orchestration/   TaskMesh, Agent SDK e swarm
  comms/           email, Telegram e Twilio

dashboard/         PWA em JavaScript
config/            configuração local e manifesto MCP
scripts/           setup, operação, recuperação e auditoria
skills/            catálogo YAML de skills
tests/unit/        testes offline coletados pelo pytest
tests/integration/ testes de integração explícitos
tests/e2e/         fluxos completos com runtime
runtime/           memória, índices, estado e conhecimento gerados
```

## Segurança

- sessões assinadas com HMAC-SHA256;
- senhas de perfil com PBKDF2-HMAC-SHA256;
- comparação constante de chaves e hashes;
- rate limiting e headers de segurança;
- isolamento de memória e sessões por perfil;
- tools sensíveis restritas ao proprietário;
- execução PowerShell confinada às raízes permitidas;
- integrações indisponíveis retornam erro real, sem simular sucesso.

O acesso LAN usa `MIKE_HOST=0.0.0.0`. Não exponha a porta 8083 diretamente à
internet. Para acesso público, use HTTPS e um túnel autenticado.

## Documentação

- [Estado de readiness](docs/MIKE_READINESS.md)
- [Operação no Windows](docs/WINDOWS_OPERATIONS.md)
- [Estratégia de testes](docs/TESTING.md)
- [Pipeline MBJ](docs/MBJ-PIPELINE.md)

## Licença

Software proprietário. Copyright © 2025–2026 Marco Barreto.
