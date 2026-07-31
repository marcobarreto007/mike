# Estratégia de testes

## Suite padrão: unitária e offline

O arquivo `pytest.ini` limita a coleta padrão a `tests\unit`.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Essa suíte não exige Qwen, Mike, internet nem credenciais externas. Resultado
validado em 30 de julho de 2026:

```text
234 passed, 3 skipped
```

Os três skips são testes manuais ou de smoke explicitamente desabilitados.

## Integração

Arquivos em `tests\integration` exercitam vários componentes e podem criar
processos ou usar recursos locais. Execute explicitamente:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration -q
```

Leia o cabeçalho do arquivo antes de executar: alguns testes gerenciam o
servidor por conta própria.

## Runtime e smoke

Com Qwen e Mike ativos:

```powershell
.\.venv\Scripts\python.exe tests\test_smoke.py
.\.venv\Scripts\python.exe tests\test_tools_comprehensive.py
```

Esses arquivos não fazem parte da coleta padrão porque dependem de portas,
credenciais ou estado persistente.

## E2E

```powershell
.\.venv\Scripts\python.exe tests\e2e\test_e2e_full.py
.\tests\e2e\run_mike_a_to_z.ps1
```

Testes E2E podem ser demorados e alterar estado de runtime. Use uma workspace
de teste quando a prova envolver escrita em email, calendário ou arquivos.

## Operação

Os scripts PowerShell têm uma auditoria não destrutiva:

```powershell
.\scripts\ops\test_ops_hardening.ps1
```

## Regra de organização

- teste sem rede e sem runtime: `tests\unit`;
- teste entre componentes locais: `tests\integration`;
- teste que exige portas 8081/8083: `tests\e2e` ou script de smoke explícito;
- benchmark ou stress: `tests\perf`;
- teste manual: marque `__test__ = False` ou use uma variável de opt-in.

Nenhum módulo coletado pelo pytest deve executar requisições, imprimir um
relatório próprio ou chamar `sys.exit()` durante o import.
