# Operação do MIKE no Windows

Este é o procedimento operacional para iniciar, validar e recuperar o MIKE.
Todos os scripts calculam a raiz do repositório a partir da própria localização.

## Endpoints

| Serviço | URL |
|---|---|
| Qwen/llama-server | `http://127.0.0.1:8081` |
| API e dashboard | `http://127.0.0.1:8083` |
| Saúde do Qwen | `http://127.0.0.1:8081/health` |
| Saúde do MIKE | `http://127.0.0.1:8083/health` |

## Inicialização normal

```powershell
.\scripts\ops\launch_mike.ps1 -SkipTunnel
```

O launcher:

1. verifica se a porta 8081 já contém o Qwen esperado;
2. resolve o arquivo do modelo;
3. inicia o `llama-server` quando necessário;
4. verifica conflitos na porta 8083 sem matar processos alheios;
5. inicia o FastAPI;
6. valida o health local.

Use `-NoBrowser` em automações ou sessões remotas.

## Resolução do modelo

`Resolve-MikeQwenModelPath`, em `mike_common.ps1`, aceita caminho absoluto ou
nome relativo. Os diretórios pesquisados são:

```text
<raiz>\
<raiz>\llm_cache\
<raiz>\llama.cpp\models\
<raiz>\llama.cpp-turboquant\models\
```

Valor atualmente validado:

```text
C:\Users\Admin\Desktop\mike\llama.cpp\models\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf
```

Para conferir sem iniciar o runtime:

```powershell
. .\scripts\ops\mike_common.ps1
Resolve-MikeQwenModelPath -ProjectRoot (Get-Location).Path
```

## Estado e recuperação

Consulta sem alteração:

```powershell
.\scripts\ops\recover_mike.ps1 -Mode status
```

Restart completo:

```powershell
.\scripts\ops\recover_mike.ps1 -Mode restart -SkipTunnel
```

Restart apenas da API:

```powershell
.\scripts\ops\start_mike.ps1 -Port 8083 -ForceRestart -SkipTunnel
```

Os scripts somente encerram um `python.exe` cuja linha de comando aponta para
`core\server\mike_server.py` dentro desta workspace.

## Logs

| Arquivo | Conteúdo |
|---|---|
| `logs\qwen36_stdout.log` | banner e saída padrão do Qwen |
| `logs\qwen36_stderr.log` | carregamento do GGUF, CUDA e servidor |
| `logs\mike.log` | eventos funcionais do MIKE |
| `logs\mike_stdout.log` | saída do processo FastAPI/MCP |
| `logs\mike_stderr.log` | tracebacks e erros do processo |

Comandos úteis:

```powershell
Get-Content logs\qwen36_stderr.log -Tail 80
Get-Content logs\mike.log -Tail 120
Get-Content logs\mike_stderr.log -Tail 120
```

## Diagnóstico de portas

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8081,8083 |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

Se uma porta estiver ocupada por outro aplicativo, o launcher para e mostra o
PID. Identifique o processo; não encerre PIDs cegamente.

## Readiness

```powershell
.\.venv\Scripts\python.exe scripts\ops\check_mike_readiness.py
```

O modo normal ignora falhas de integrações opcionais, mas não ignora falhas do
núcleo ou de tools locais.

```powershell
.\.venv\Scripts\python.exe scripts\ops\check_mike_readiness.py --strict
```

O modo estrito é apropriado para uma implantação que promete todas as
integrações externas.

## Google OAuth

```powershell
$env:PYTHONPATH = (Resolve-Path core\integrations).Path
.\.venv\Scripts\python.exe scripts\setup\setup_google_workspace_oauth.py
```

O script valida o arquivo de credenciais, abre o navegador, grava o token e
executa probes reais de Gmail e Calendar. Quando o conjunto de escopos muda, é
necessário consentir novamente.

## Serviços Windows opcionais

O instalador usa NSSM e cria:

1. `MikeQwen36`, porta 8081;
2. `MikeServer`, porta 8083, dependente de `MikeQwen36`.

Pré-visualização:

```powershell
.\scripts\ops\install_mike_service.ps1 -WhatIf
```

Instalação:

```powershell
.\scripts\ops\install_mike_service.ps1
```

Não instale os serviços enquanto processos manuais ocuparem as mesmas portas.
O Cloudflare Tunnel é um serviço separado e não é requisito local.

## Tarefas agendadas

```powershell
.\scripts\ops\install_heartbeat.ps1
.\scripts\ops\mike_heartbeat.ps1 -ValidateOnly
```

O heartbeat deve usar a mesma workspace e o mesmo ambiente Python do MIKE.

## Validação dos scripts

```powershell
.\scripts\ops\test_ops_hardening.ps1
```

Essa verificação analisa sintaxe, caminhos, nomes de serviço, dependências,
portas e proteção contra encerramento de processos não relacionados.

## Encerramento seguro

Para parar somente a API em modo processo, descubra o PID na porta 8083 e use
as funções de identidade de `mike_common.ps1`. Para operação normal, prefira
`recover_mike.ps1` em vez de `Stop-Process` manual.
