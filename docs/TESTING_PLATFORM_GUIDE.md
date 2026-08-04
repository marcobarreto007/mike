# Guia de Testes por Plataforma

## Visão Geral

O sistema Mike foi projetado para ser multi-plataforma (Windows e Linux/Unix). Este documento descreve como os testes são adaptados para cada plataforma e as melhores práticas para escrever testes compatíveis.

## Detecção de Plataforma

Os testes usam `sys.platform` para detectar o sistema operacional:

```python
import sys

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
```

## Decoradores de Skip por Plataforma

Use os decoradores do `unittest` para pular testes específicos de plataforma:

### Pular em Windows
```python
@unittest.skipIf(IS_WINDOWS, "Teste requer Linux/Unix")
def test_linux_feature(self):
    ...
```

### Pular em Linux
```python
@unittest.skipUnless(IS_WINDOWS, "Teste requer Windows")
def test_powershell_feature(self):
    ...
```

### Pular em macOS
```python
@unittest.skipIf(IS_MACOS, "Teste não suporta macOS")
def test_platform_specific(self):
    ...
```

## Exemplos de Testes Adaptados

### 1. Teste de Comandos Shell

**Arquivo:** `tests/unit/test_mcp_extended_tools.py`

```python
class WorkspaceCommandTests(unittest.TestCase):
    @unittest.skipUnless(IS_WINDOWS, "PowerShell tests require Windows")
    def test_command_runs_in_allowed_root_and_returns_exit_code(self):
        """Testa comandos PowerShell no Windows."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = workspace_mcp.run_command(
                "Write-Output 'QWEN_COMMAND_OK'",
                cwd=temp_dir,
                timeout_seconds=10,
            )
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("QWEN_COMMAND_OK", result["output"])

    @unittest.skipIf(IS_WINDOWS, "Shell command tests require non-Windows platform")
    def test_command_runs_in_allowed_root_linux(self):
        """Testa comandos bash no Linux."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = workspace_mcp.run_command(
                "echo 'QWEN_COMMAND_OK'",
                cwd=temp_dir,
                timeout_seconds=10,
            )
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("QWEN_COMMAND_OK", result["output"])
```

### 2. Limpeza de Processos

**Arquivo:** `tests/integration/test_mike_full_isolated.py`

```python
def kill_mike_processes():
    """Mata processos Mike anteriores de forma multi-plataforma."""
    if sys.platform == "win32":
        # Windows: usa PowerShell
        subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -match '^python' } | "
             "Stop-Process -Force"],
            capture_output=True, text=True, timeout=10
        )
    else:
        # Linux: usa pkill
        os.system("pkill -f mike_server.py 2>/dev/null || true")
```

### 3. Verificação de Porta

**Arquivo:** `tests/integration/test_mike_full_isolated.py`

```python
# Verifica porta (apenas no Windows)
if sys.platform == "win32":
    result = subprocess.run(
        ["powershell", "-Command",
         "$c = Get-NetTCPConnection -State Listen -LocalPort 8080; "
         "if ($c) { Stop-Process -Id $c.OwningProcess -Force }"],
        capture_output=True, text=True, timeout=5
    )
else:
    # Linux: usa lsof
    result = subprocess.run(
        ["lsof", "-i", ":8080", "-t"],
        capture_output=True, text=True, timeout=5
    )
    if result.stdout.strip():
        for pid in result.stdout.strip().split('\n'):
            os.kill(int(pid), signal.SIGKILL)
```

## Implementação Multi-Plataforma no Código

### Execução de Comandos

**Arquivo:** `core/mcp/mike_workspace_mcp.py`

```python
def run_command(command: str, cwd: str = ".", timeout_seconds: int = 60) -> dict:
    # Platform-specific command execution
    if sys.platform == "win32":
        cmd_args = [
            "powershell.exe",
            "-NoLogo", "-NoProfile", "-NonInteractive",
            "-Command", str(command),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        # Linux/Unix: use shell command
        cmd_args = ["/bin/bash", "-c", str(command)]
        creationflags = 0
    
    completed = subprocess.run(
        cmd_args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        creationflags=creationflags,
    )
    # ... processa resultado
```

## Melhores Práticas

### 1. Sempre Detectar a Plataforma
Nunca assuma que o teste está rodando em uma plataforma específica. Sempre verifique:

```python
# ❌ Ruim: assume Linux
subprocess.run(["bash", "-c", "echo test"])

# ✅ Bom: detecta plataforma
if sys.platform == "win32":
    subprocess.run(["powershell", "-Command", "Write-Output test"])
else:
    subprocess.run(["bash", "-c", "echo test"])
```

### 2. Use Decoradores de Skip Appropriadamente
Marque claramente quais testes são específicos de plataforma:

```python
@unittest.skipUnless(sys.platform == "win32", "Requires Windows")
def test_registry_access(self):
    ...
```

### 3. Forneça Alternativas
Sempre que possível, forneça implementações alternativas para diferentes plataformas:

```python
def get_user_home():
    if sys.platform == "win32":
        return os.environ.get("USERPROFILE")
    else:
        return os.path.expanduser("~")
```

### 4. Documente Dependências de Plataforma
No docstring do teste, documente dependências de plataforma:

```python
def test_windows_feature(self):
    """
    Testa feature específica do Windows.
    
    Requer:
        - Windows 10 ou superior
        - PowerShell 5.1+
        - Permissões de administrador (para alguns casos)
    
    Skip: Linux, macOS
    """
```

### 5. Mock de Dependências de Plataforma
Para testes unitários, use mock para isolar dependências de plataforma:

```python
from unittest.mock import patch

@patch('sys.platform', 'win32')
def test_mocked_windows_behavior(self):
    # Testa comportamento Windows sem precisar do OS real
    ...
```

## Configuração do Ambiente de Teste

### Variáveis de Ambiente Úteis

```bash
# Forçar detecção de plataforma (para debugging)
export MIKE_TEST_PLATFORM=linux  # ou 'windows'

# Habilitar testes manuais
export MIKE_RUN_VISION_SMOKE=1

# Configurar diretório de testes
export MIKE_HOME=/path/to/test/home
```

### Executando Testes

```bash
# Todos os testes (auto-detecta plataforma)
python -m pytest tests/unit/ -v

# Apenas testes Linux
python -m pytest tests/unit/ -v -k "not powershell"

# Apenas testes Windows (em ambiente Windows)
python -m pytest tests/unit/ -v -k "powershell"

# Ver quais testes serão pulados
python -m pytest tests/unit/ -v --collect-only | grep SKIP
```

## Matriz de Suporte

| Feature | Windows | Linux | macOS |
|---------|---------|-------|-------|
| PowerShell commands | ✅ | ❌ | ❌ |
| Bash commands | ❌ | ✅ | ✅ |
| Kill processes (native) | ✅ | ✅ | ✅ |
| Port checking | ✅ | ✅ | ✅ |
| File system ops | ✅ | ✅ | ✅ |
| Environment vars | ✅ | ✅ | ✅ |

## Troubleshooting

### Erro: `FileNotFoundError: powershell.exe`
**Causa:** Tentando executar PowerShell no Linux.

**Solução:** Adicione verificação de plataforma:
```python
if sys.platform == "win32":
    # usa PowerShell
else:
    # usa bash
```

### Erro: `FileNotFoundError: /bin/bash`
**Causa:** Sistema sem bash (ex: Windows puro).

**Solução:** Use `shell=True` ou detecte shell disponível:
```python
import shutil
shell = shutil.which("bash") or shutil.which("sh") or "cmd.exe"
```

### Teste falha apenas em CI/CD
**Causa:** Ambiente CI pode ter plataforma diferente.

**Solução:** 
1. Verifique logs do CI para plataforma
2. Adicione skips apropriados
3. Use mocks para isolar dependências

## Referências

- [Python sys.platform docs](https://docs.python.org/3/library/sys.html#sys.platform)
- [unittest.skipIf docs](https://docs.python.org/3/library/unittest.html#unittest.skipIf)
- [subprocess module](https://docs.python.org/3/library/subprocess.html)
- [pytest skip markers](https://docs.pytest.org/en/latest/how-to/skipping.html)
