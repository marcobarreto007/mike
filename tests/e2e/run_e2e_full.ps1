<#
.SYNOPSIS
    Roda o teste E2E completo do Mike e exibe o relatório.
    Uso: .\run_e2e_full.ps1 [-Port 8083] [-StressWorkers 5]
#>
param(
    [int]$Port = 8083,
    [int]$StressWorkers = 5,
    [string]$Url = ""
)

$ErrorActionPreference = "Continue"
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$testScript  = Join-Path $scriptDir "test_e2e_full.py"

# Detecta Python
$python = $null
foreach ($candidate in @("python", "python3", (Join-Path $projectRoot ".venv\Scripts\python.exe"))) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $python = $candidate; break
    }
}
if (-not $python) {
    Write-Host "ERRO: python nao encontrado." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  MIKE E2E FULL TEST" -ForegroundColor Cyan
Write-Host "  Servidor: $(if ($Url) { $Url } else { "http://127.0.0.1:$Port" })" -ForegroundColor Cyan
Write-Host "  Workers stress: $StressWorkers" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$args_list = @($testScript, "--port", $Port, "--stress-workers", $StressWorkers)
if ($Url) { $args_list += @("--url", $Url) }

& $python @args_list
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "  TODOS OS TESTES PASSARAM!" -ForegroundColor Green
} else {
    Write-Host "  FALHAS ENCONTRADAS. Veja e2e_report.json para detalhes." -ForegroundColor Red
}
Write-Host ""
exit $exitCode
