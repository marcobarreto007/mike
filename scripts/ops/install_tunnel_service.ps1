param(
    [string]$ProjectRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$CloudflaredExe,
    [string]$TunnelConfig
)

$ErrorActionPreference = "Stop"
$serviceName = "CloudflaredMikeTunnel"

function Write-Log {
    param([string]$Message)
    Write-Host "[Tunnel Setup] $Message" -ForegroundColor Cyan
}

if ([string]::IsNullOrWhiteSpace($CloudflaredExe)) {
    $CloudflaredExe = Join-Path $ProjectRoot "bin\cloudflared.exe"
}
if ([string]::IsNullOrWhiteSpace($TunnelConfig)) {
    $TunnelConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
}
$logsDir = Join-Path $ProjectRoot "logs"

if (-not (Test-Path -LiteralPath $CloudflaredExe)) {
    Write-Host "Aviso: cloudflared.exe não encontrado em $CloudflaredExe. Instalação ignorada." -ForegroundColor Yellow
    return
}
if (-not (Test-Path -LiteralPath $TunnelConfig)) {
    Write-Host "Aviso: config.yml não encontrado em $TunnelConfig. Instalação ignorada." -ForegroundColor Yellow
    return
}

$nssmCommand = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssmCommand) {
    Write-Host "Aviso: nssm não encontrado no PATH. Instalação do serviço ignorada." -ForegroundColor Yellow
    return
}
$nssmPath = $nssmCommand.Source

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Log "Solicitando privilégios de administrador..."
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-ProjectRoot", "`"$ProjectRoot`"",
        "-CloudflaredExe", "`"$CloudflaredExe`"",
        "-TunnelConfig", "`"$TunnelConfig`""
    )
    Start-Process powershell.exe -Verb RunAs -ArgumentList ($arguments -join " ") | Out-Null
    return
}

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

Write-Log "Instalando serviço $serviceName com NSSM..."
$existing = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Log "Serviço existente encontrado; substituindo a configuração..."
    Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
    & $nssmPath remove $serviceName confirm
    Start-Sleep -Seconds 2
}

$normalizedTunnelConfig = [System.IO.Path]::GetFullPath($TunnelConfig).Replace('\', '/')
$cloudflaredProcesses = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $commandLine = [string]$_.CommandLine
        $commandLine.Replace('\', '/').IndexOf(
            $normalizedTunnelConfig,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -ge 0
    }
if ($cloudflaredProcesses) {
    Write-Log "Encerrando apenas processos cloudflared vinculados a esta configuração..."
    $cloudflaredProcesses | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
    }
}

& $nssmPath install $serviceName "$CloudflaredExe" tunnel --config "$TunnelConfig" run
& $nssmPath set $serviceName AppDirectory "$ProjectRoot"
& $nssmPath set $serviceName AppStdout (Join-Path $logsDir "tunnel_service_stdout.log")
& $nssmPath set $serviceName AppStderr (Join-Path $logsDir "tunnel_service_stderr.log")

Write-Log "Iniciando o serviço..."
Start-Service -Name $serviceName
Start-Sleep -Seconds 3

$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -eq "Running") {
    Write-Host "Sucesso: o túnel está ativo e configurado para iniciar com o Windows." -ForegroundColor Green
} else {
    Write-Host "O serviço foi instalado, mas não iniciou. Consulte $logsDir\tunnel_service_stderr.log" -ForegroundColor Yellow
}
