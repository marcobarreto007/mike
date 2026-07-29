<#
.SYNOPSIS
    Starts Neo4j Community Server for Mike's Graphiti graph memory.
.DESCRIPTION
    Starts Neo4j as a hidden background process (console mode).
    Neo4j bolt: localhost:7687, HTTP: localhost:7474
    User: neo4j, Pass: MikeBarreto2026

    To install as Windows service (requires admin):
        Start-Process powershell -Verb RunAs -ArgumentList "-Command & 'C:\neo4j\bin\neo4j.bat' windows-service install"
#>
param(
    [int]$StartupTimeoutSec = 60
)

$ErrorActionPreference = "Stop"

$neo4jHome = "C:\neo4j"
$javaHome  = "C:\Program Files\Eclipse Adoptium\jdk-21.0.10.7-hotspot"
$neo4jLog  = "$neo4jHome\logs\neo4j_stdout.log"

if (-not (Test-Path "$neo4jHome\bin\neo4j.bat")) {
    throw "Neo4j nao encontrado em $neo4jHome"
}
if (-not (Test-Path $javaHome)) {
    throw "Java nao encontrado em $javaHome"
}

# Ja esta rodando?
$bolt = Get-NetTCPConnection -State Listen -LocalPort 7687 -ErrorAction SilentlyContinue
if ($bolt) {
    Write-Host "Neo4j ja rodando (bolt://localhost:7687, PID $($bolt.OwningProcess))" -ForegroundColor Green
    exit 0
}

# Verificar se ja tem processo neo4j.bat rodando
$existing = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "neo4j" -and $_.CommandLine -match "console" }
if ($existing.Count -gt 0) {
    Write-Host "Processo Neo4j encontrado (PID $($existing[0].ProcessId)), aguardando bolt..." -ForegroundColor Yellow
} else {
    # Preparar ambiente
    $env:JAVA_HOME = $javaHome
    $env:PATH = "$javaHome\bin;$env:PATH"

    New-Item -ItemType Directory -Force -Path "$neo4jHome\logs" | Out-Null
    if (Test-Path $neo4jLog) { Remove-Item $neo4jLog -Force }

    Write-Host "Iniciando Neo4j em background..." -ForegroundColor Cyan
    Write-Host "  JAVA_HOME: $javaHome" -ForegroundColor DarkCyan
    Write-Host "  Bolt:      bolt://localhost:7687" -ForegroundColor DarkCyan
    Write-Host "  HTTP:      http://localhost:7474" -ForegroundColor DarkCyan
    Write-Host "  Log:       $neo4jLog" -ForegroundColor DarkCyan

    $proc = Start-Process -FilePath "$neo4jHome\bin\neo4j.bat" `
        -ArgumentList "console" `
        -WorkingDirectory $neo4jHome `
        -WindowStyle Hidden `
        -PassThru `
        -RedirectStandardOutput $neo4jLog `
        -RedirectStandardError "$neo4jHome\logs\neo4j_stderr.log" `
        -Environment @{ JAVA_HOME = $javaHome; PATH = "$javaHome\bin;$env:PATH" }

    Write-Host "  PID: $($proc.Id)" -ForegroundColor Green
}

Write-Host "  Aguardando bolt:7687..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $bolt = Get-NetTCPConnection -State Listen -LocalPort 7687 -ErrorAction SilentlyContinue
    if ($bolt) {
        Write-Host "  Neo4j pronto! (bolt://localhost:7687)" -ForegroundColor Green
        exit 0
    }
}

Write-Host "  Neo4j nao respondeu em $StartupTimeoutSec segundos." -ForegroundColor Red
if (Test-Path $neo4jLog) { Get-Content $neo4jLog -Tail 20 }
exit 1
