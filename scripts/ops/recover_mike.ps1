param(
    [ValidateSet("status", "start", "stop", "restart", "logs", "install-service", "repair")]
    [string]$Mode = "status",
    [string]$ServiceName = "MikeServer",
    [string]$QwenServiceName = "MikeQwen36",
    [int]$Port = 8083,
    [int]$StartupTimeoutSec = 600,
    [int]$Tail = 60,
    [switch]$StartNow,
    [switch]$SkipTests,
    [switch]$SkipTunnel
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
. (Join-Path $scriptDir "mike_common.ps1")
$healthUrl = "http://127.0.0.1:$Port/health"
$qwenHealthUrl = "http://127.0.0.1:8081/health"
$stdoutLog = Join-Path $projectRoot "logs\mike_stdout.log"
$stderrLog = Join-Path $projectRoot "logs\mike_stderr.log"
$serviceStdoutLog = Join-Path $projectRoot "logs\mike_service_stdout.log"
$serviceStderrLog = Join-Path $projectRoot "logs\mike_service_stderr.log"
$qwenStdoutLog = Join-Path $projectRoot "logs\qwen36_service_stdout.log"
$qwenStderrLog = Join-Path $projectRoot "logs\qwen36_service_stderr.log"

function Write-Section([string]$Title) {
    Write-Host "`n=== $Title ===" -ForegroundColor Cyan
}

function Get-MikeService {
    Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
}

function Get-QwenService {
    Get-Service -Name $QwenServiceName -ErrorAction SilentlyContinue
}

function Get-MikeHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
    } catch {
        return $null
    }
}

function Get-QwenHealth {
    try {
        return Invoke-RestMethod -Uri $qwenHealthUrl -TimeoutSec 5
    } catch {
        return $null
    }
}

function Wait-ForEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$TimeoutSec = 600
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) { return }
        } catch {}
        Start-Sleep -Seconds 2
    }
    throw "$Name did not become healthy at $Url within $TimeoutSec seconds."
}

function Stop-MikeRuntime {
    $service = Get-MikeService
    if ($service -and $service.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force -ErrorAction Stop
        Start-Sleep -Seconds 2
    }

    $processes = @(Get-MikeProcesses -ProjectRoot $projectRoot)
    foreach ($process in $processes) {
        Stop-MikeProcessSafely -ProcessId $process.ProcessId -ProjectRoot $projectRoot -Force
    }

    if ($processes.Count -gt 0) {
        Start-Sleep -Seconds 2
    }

    $listener = Get-PortListener -Port $Port
    if ($listener) {
        $diagnostic = Get-MikeProcessDiagnostic -ProcessId $listener.OwningProcess
        throw "Port $Port remains occupied after stopping Mike. Refusing to stop an unverified process. $diagnostic"
    }
}

function Start-MikeRuntime {
    $service = Get-MikeService
    if ($service) {
        $qwenService = Get-QwenService
        if ($qwenService -and $qwenService.Status -ne "Running") {
            Start-Service -Name $QwenServiceName
        }
        if ($qwenService) {
            Wait-ForEndpoint -Url $qwenHealthUrl -Name "Qwen 3.6" -TimeoutSec $StartupTimeoutSec
        } elseif (-not (Get-QwenHealth)) {
            throw "$ServiceName is installed, but $QwenServiceName is missing and Qwen is not healthy at $qwenHealthUrl."
        }
        if ($service.Status -ne "Running") {
            Start-Service -Name $ServiceName
        }
        Wait-ForEndpoint -Url $healthUrl -Name "Mike" -TimeoutSec $StartupTimeoutSec
        return
    }

    $launcher = Join-Path $scriptDir "launch_mike.ps1"
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$launcher`"",
        "-Port", "$Port",
        "-NoBrowser"
    )
    if ($SkipTunnel) { $arguments += "-SkipTunnel" }
    $launcherProcess = Start-Process -FilePath "powershell.exe" `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -Wait `
        -PassThru
    if ($launcherProcess.ExitCode -ne 0) {
        throw "Mike launcher failed with exit code $($launcherProcess.ExitCode)"
    }
}

function Show-MikeLogs {
    foreach ($logPath in @(
        $stdoutLog, $stderrLog, $serviceStdoutLog, $serviceStderrLog,
        $qwenStdoutLog, $qwenStderrLog
    )) {
        Write-Section $logPath
        if (Test-Path $logPath) {
            Get-Content -LiteralPath $logPath -Tail $Tail
        } else {
            Write-Host "<missing>" -ForegroundColor DarkYellow
        }
    }
}

function Show-MikeStatus {
    $service = Get-MikeService
    $qwenService = Get-QwenService
    $processes = @(Get-MikeProcesses -ProjectRoot $projectRoot)
    $health = Get-MikeHealth
    $qwenHealth = Get-QwenHealth
    $listener = Get-PortListener -Port $Port

    Write-Section "Services"
    if ($service) {
        $service | Select-Object Name, Status, StartType | Format-List
    } else {
        Write-Host "$ServiceName not installed; process mode is supported." -ForegroundColor Yellow
    }
    if ($qwenService) {
        $qwenService | Select-Object Name, Status, StartType | Format-List
    } else {
        Write-Host "$QwenServiceName not installed; launcher-managed Qwen is supported." -ForegroundColor Yellow
    }

    Write-Section "Process"
    if ($processes.Count -gt 0) {
        $processes | Select-Object ProcessId, CommandLine | Format-List
    } else {
        Write-Host "No Mike process detected." -ForegroundColor Yellow
    }
    if ($listener -and -not (Test-MikeProcessIdentity -ProcessId $listener.OwningProcess -ProjectRoot $projectRoot)) {
        Write-Host "WARNING: port $Port is owned by a non-Mike process." -ForegroundColor Red
        Write-Host (Get-MikeProcessDiagnostic -ProcessId $listener.OwningProcess) -ForegroundColor Yellow
    }

    Write-Section "Mike Health"
    if ($health) {
        $health | ConvertTo-Json -Depth 6
    } else {
        Write-Host "Health endpoint unavailable at $healthUrl" -ForegroundColor Yellow
    }

    Write-Section "Qwen Health"
    if ($qwenHealth) {
        $qwenHealth | ConvertTo-Json -Depth 6
    } else {
        Write-Host "Health endpoint unavailable at $qwenHealthUrl" -ForegroundColor Yellow
    }
}

function Install-MikeService {
    & (Join-Path $scriptDir "install_mike_service.ps1") `
        -ServiceName $ServiceName `
        -QwenServiceName $QwenServiceName `
        -ProjectRoot $projectRoot `
        -StartNow:$StartNow
}

function Repair-Mike {
    Write-Section "Preflight"
    Push-Location $projectRoot
    try {
        $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
        $python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }
        & $python -m compileall -q core
        if ($LASTEXITCODE -ne 0) { throw "Python compile validation failed." }

        if (-not $SkipTests) {
            Write-Section "Operational Tests"
            & (Join-Path $scriptDir "test_ops_hardening.ps1")
            if ($LASTEXITCODE -ne 0) { throw "Operational hardening tests failed." }
        }
    } finally {
        Pop-Location
    }

    Write-Section "Restart"
    Stop-MikeRuntime
    try {
        Start-MikeRuntime
    } catch {
        Write-Host "Repair restart failed; attempting one recovery start." -ForegroundColor Yellow
        Start-MikeRuntime
    }

    Write-Section "Status"
    Show-MikeStatus
}

switch ($Mode) {
    "status" {
        Show-MikeStatus
    }
    "start" {
        Start-MikeRuntime
        Show-MikeStatus
    }
    "stop" {
        Stop-MikeRuntime
        Show-MikeStatus
    }
    "restart" {
        Stop-MikeRuntime
        Start-MikeRuntime
        Show-MikeStatus
    }
    "logs" {
        Show-MikeLogs
    }
    "install-service" {
        Install-MikeService
    }
    "repair" {
        Repair-Mike
    }
}
