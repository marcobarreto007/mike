param(
    [int]$Port = 8083,
    [switch]$NoBrowser,
    [switch]$SkipTunnel
)

$ErrorActionPreference = "Stop"

# Dot-source common module
. "$PSScriptRoot\mike_common.ps1"

function Write-Step {
    param([string]$Message)
    Write-Host "`n[LAUNCH] $Message" -ForegroundColor Cyan
}

function Test-QwenServer {
    try {
        $health = Invoke-WebRequest -Uri "http://127.0.0.1:8081/health" -UseBasicParsing -TimeoutSec 3
        if ($health.StatusCode -ne 200) { return $false }
        $models = Invoke-RestMethod -Uri "http://127.0.0.1:8081/v1/models" -TimeoutSec 4
        $ids = @($models.data | ForEach-Object { [string]$_.id })
        return [bool]($ids | Where-Object { $_ -match "Qwen3\.6-35B-A3B" })
    } catch {
        return $false
    }
}

function Ensure-QwenServer {
    param([string]$ProjectRoot)

    if (Test-QwenServer) {
        Write-Host "Qwen 3.6 health: OK (already running)" -ForegroundColor Green
        return
    }

    $listener = Get-NetTCPConnection -State Listen -LocalPort 8081 -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        throw "Port 8081 is occupied by PID $($listener.OwningProcess), but it is not the expected Qwen 3.6 server."
    }

    $qwenScript = Join-Path $ProjectRoot "scripts\ops\start_qwen36_server.ps1"
    $modelPath = Join-Path $ProjectRoot "llm_cache\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"
    if (-not (Test-Path $qwenScript)) { throw "Qwen launcher not found: $qwenScript" }
    if (-not (Test-Path $modelPath)) { throw "Qwen model not found: $modelPath" }

    $logsDir = Join-Path $ProjectRoot "logs"
    New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
    $stdoutLog = Join-Path $logsDir "qwen36_stdout.log"
    $stderrLog = Join-Path $logsDir "qwen36_stderr.log"
    foreach ($log in @($stdoutLog, $stderrLog)) {
        if (Test-Path $log) { Clear-Content -LiteralPath $log -ErrorAction SilentlyContinue }
    }

    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$qwenScript`"",
        "-ModelPath", "`"$modelPath`"",
        "-Port", "8081"
    )
    $process = Start-Process -FilePath "powershell.exe" `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Write-Host "Qwen 3.6 starting (launcher PID $($process.Id))..." -ForegroundColor Yellow

    $deadline = (Get-Date).AddMinutes(4)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-QwenServer) {
            Write-Host "Qwen 3.6 health: OK" -ForegroundColor Green
            return
        }
        if ($process.HasExited) {
            $tail = if (Test-Path $stderrLog) { (Get-Content $stderrLog -Tail 20) -join "`n" } else { "" }
            throw "Qwen launcher exited with code $($process.ExitCode). $tail"
        }
    }
    throw "Qwen 3.6 did not become healthy within 4 minutes. Check $stderrLog"
}

function Ensure-Admin {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) { return }

    Write-Host "Re-launching with admin privileges (UAC)..." -ForegroundColor Yellow
    $args = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"", "-Port", "$Port"
    )
    if ($NoBrowser) { $args += "-NoBrowser" }
    if ($SkipTunnel) { $args += "-SkipTunnel" }
    Start-Process powershell -Verb RunAs -ArgumentList ($args -join " ") | Out-Null
    exit 0
}

function Ensure-TunnelService {
    param([string]$ProjectRoot)
    $serviceName = "CloudflaredMikeTunnel"
    $svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if (-not $svc) {
        $cloudflaredExe = Join-Path $ProjectRoot "bin\cloudflared.exe"
        $tunnelConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
        if (-not (Test-Path -LiteralPath $cloudflaredExe) -or -not (Test-Path -LiteralPath $tunnelConfig)) {
            Write-Host "Tunnel assets not configured; skipping public tunnel. Local Mike will still start." -ForegroundColor Yellow
            return
        }

        $installScript = Join-Path $ProjectRoot "scripts\ops\install_tunnel_service.ps1"
        if (Test-Path $installScript) {
            Write-Host "Installing tunnel service..." -ForegroundColor Yellow
            & $installScript -ProjectRoot $ProjectRoot -CloudflaredExe $cloudflaredExe -TunnelConfig $tunnelConfig
            $svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        }
    }
    if ($svc) {
        try { Set-Service -Name $serviceName -StartupType Automatic -ErrorAction SilentlyContinue } catch {}
        if ($svc.Status -ne "Running") {
            Start-Service -Name $serviceName -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            $svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        }
        cmd /c "sc.exe failure $serviceName reset= 0 actions= restart/5000/restart/5000/restart/5000" | Out-Null
        cmd /c "sc.exe failureflag $serviceName 1" | Out-Null
    }
    if (-not $svc -or $svc.Status -ne "Running") {
        Write-Host "Tunnel service unavailable. Starting cloudflared process fallback..." -ForegroundColor Yellow
        Ensure-CloudflareTunnel -ProjectRoot $ProjectRoot
    }
}

$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "mike_common.ps1")
$healthUrl = "http://127.0.0.1:$Port/health"
$dashboardUrl = "http://127.0.0.1:$Port/"
$publicHealth = "https://mike.supereziorealtime.com/health"

$tunnelService = Get-Service -Name "CloudflaredMikeTunnel" -ErrorAction SilentlyContinue
$tunnelBinary = Join-Path $projectRoot "bin\cloudflared.exe"
$tunnelConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
$tunnelAvailable = $null -ne $tunnelService -or (
    (Test-Path -LiteralPath $tunnelBinary) -and (Test-Path -LiteralPath $tunnelConfig)
)
$effectiveSkipTunnel = $SkipTunnel -or -not $tunnelAvailable
$needsTunnelAdmin = -not $effectiveSkipTunnel -and (
    $null -ne $tunnelService -or
    ((Test-Path -LiteralPath $tunnelBinary) -and (Test-Path -LiteralPath $tunnelConfig))
)
if ($needsTunnelAdmin) {
    Ensure-Admin
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  MIKE - Official Launcher" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "Workspace: $projectRoot"
Write-Host "Local:     $dashboardUrl"
Write-Host "Public:    https://mike.supereziorealtime.com"

Write-Step "1/5 Ensuring tunnel persistence"
if ($SkipTunnel) {
    Write-Host "Public tunnel skipped by request. Local Mike will still start." -ForegroundColor Yellow
} elseif (-not $tunnelAvailable) {
    Write-Host "Tunnel assets not configured; skipping public tunnel. Local Mike will still start." -ForegroundColor Yellow
} else {
    Ensure-TunnelService -ProjectRoot $projectRoot
}

Write-Step "2/5 Ensuring the single Qwen 3.6 brain"
Ensure-QwenServer -ProjectRoot $projectRoot

Write-Step "3/5 Clearing server conflicts"
$mikeSvc = Get-Service MikeServer -ErrorAction SilentlyContinue
if ($mikeSvc -and $mikeSvc.Status -eq "Running") {
    Stop-Service -Name MikeServer -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
if ($mikeSvc) {
    try { Set-Service -Name MikeServer -StartupType Manual -ErrorAction SilentlyContinue } catch {}
}

$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    if (-not (Test-MikeProcessIdentity -ProcessId $listener.OwningProcess -ProjectRoot $projectRoot)) {
        $diagnostic = Get-MikeProcessDiagnostic -ProcessId $listener.OwningProcess
        throw "Port $Port is occupied by a process that is not this Mike runtime. Refusing to stop it. $diagnostic"
    }
    Stop-MikeProcessSafely -ProcessId $listener.OwningProcess -ProjectRoot $projectRoot -Force
    Start-Sleep -Seconds 2
}

Write-Step "4/5 Starting Mike runtime"
$startScript = Join-Path $projectRoot "scripts\ops\start_mike.ps1"
if ($effectiveSkipTunnel) {
    & $startScript -Port $Port -ForceRestart -SkipTunnel
} else {
    & $startScript -Port $Port -ForceRestart
}

Write-Step "5/5 Validating local/public health"
$localOk = $false
for ($i = 0; $i -lt 15; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 4
        if ($r.StatusCode -eq 200) {
            $localOk = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 2
}

if ($localOk) {
    Write-Host "Local health: OK" -ForegroundColor Green
} else {
    Write-Host "Local health: FAILED" -ForegroundColor Red
    exit 1
}

if ($effectiveSkipTunnel) {
    Write-Host "Public tunnel health: SKIPPED" -ForegroundColor Yellow
} else {
    try {
        $pub = Invoke-WebRequest -Uri $publicHealth -UseBasicParsing -TimeoutSec 8
        if ($pub.StatusCode -eq 200) {
            Write-Host "Public tunnel health: OK" -ForegroundColor Green
        } else {
            Write-Host "Public tunnel health returned $($pub.StatusCode)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "Public tunnel health check failed (may still be warming up)." -ForegroundColor Yellow
    }
}

if (-not $NoBrowser) {
    Start-Process $dashboardUrl | Out-Null
}

Write-Host "Launcher completed." -ForegroundColor Green
