# Mike Common PowerShell Module
# Shared functions for Mike scripts. Dot-source in your script:
#   . "$PSScriptRoot\mike_common.ps1"

function Write-Banner {
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "  MIKE - Startup Sequence" -ForegroundColor Cyan
    Write-Host "============================================" -ForegroundColor Cyan
}

function Get-MikeProjectRoot {
    param([string]$ProjectRoot)
    if ($ProjectRoot) {
        return [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\', '/')
    }
    return [System.IO.Path]::GetFullPath(
        (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    ).TrimEnd('\', '/')
}

function Get-ProcessByIdSafe {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Test-MikeProcessObjectIdentity {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [string]$ProjectRoot
    )

    $processName = [string]$Process.Name
    $executablePath = [string]$Process.ExecutablePath
    $commandLine = [string]$Process.CommandLine
    if (
        $processName -notmatch '^pythonw?(?:\.exe)?$' -or
        [string]::IsNullOrWhiteSpace($executablePath) -or
        [string]::IsNullOrWhiteSpace($commandLine)
    ) {
        return $false
    }

    $root = Get-MikeProjectRoot -ProjectRoot $ProjectRoot
    $expectedServer = [System.IO.Path]::GetFullPath(
        (Join-Path $root "core\server\mike_server.py")
    ).Replace('\', '/')
    $normalizedCommandLine = $commandLine.Replace('\', '/')

    return $normalizedCommandLine.IndexOf(
        $expectedServer,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -ge 0
}

function Test-MikeProcessIdentity {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [string]$ProjectRoot
    )
    $process = Get-ProcessByIdSafe -ProcessId $ProcessId
    if (-not $process) { return $false }
    return Test-MikeProcessObjectIdentity -Process $process -ProjectRoot $ProjectRoot
}

function Get-MikeProcessDiagnostic {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    $process = Get-ProcessByIdSafe -ProcessId $ProcessId
    if (-not $process) { return "PID $ProcessId no longer exists" }
    $exe = if ($process.ExecutablePath) { $process.ExecutablePath } else { "<unknown>" }
    $cmd = if ($process.CommandLine) { $process.CommandLine } else { "<unavailable>" }
    return "PID $ProcessId; executable='$exe'; commandLine='$cmd'"
}

function Stop-MikeProcessSafely {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [string]$ProjectRoot,
        [switch]$Force
    )
    if (-not (Test-MikeProcessIdentity -ProcessId $ProcessId -ProjectRoot $ProjectRoot)) {
        $diagnostic = Get-MikeProcessDiagnostic -ProcessId $ProcessId
        throw "Refusing to stop a process that is not this Mike runtime: $diagnostic"
    }
    Stop-Process -Id $ProcessId -Force:$Force -ErrorAction Stop
}

function Get-MikeProcesses {
    param([string]$ProjectRoot)
    $root = Get-MikeProjectRoot -ProjectRoot $ProjectRoot
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            Test-MikeProcessObjectIdentity -Process $_ -ProjectRoot $root
        }
}

function Get-PortListener {
    param([int]$Port)
    Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Get-NvidiaSmiPath {
    $candidates = @(
        $env:MIKE_NVIDIA_SMI,
        (Join-Path $env:ProgramFiles "NVIDIA Corporation\NVSMI\nvidia-smi.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "NVIDIA Corporation\NVSMI\nvidia-smi.exe"),
        (Join-Path $env:SystemRoot "System32\nvidia-smi.exe")
    ) | Where-Object { $_ -and (Test-Path $_) }

    $candidate = $candidates | Select-Object -First 1
    if ($candidate) { return $candidate }

    $command = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Get-CudaMajorFromEnv {
    foreach ($value in @($env:CUDA_PATH, $env:CUDA_HOME)) {
        if ($value -and $value -match '[\\/]v?(\d+)(?:\.\d+)?(?:[\\/]|$)') {
            return [int]$Matches[1]
        }
    }
    return $null
}

function Get-ComputeCapabilityFromName {
    param([string]$Name)
    if ($Name -match "P106" -or $Name -match "GTX 10") { return "6.1" }
    if ($Name -match "Quadro K5000") { return "3.0" }
    if ($Name -match "RTX 50") { return "12.0" }
    if ($Name -match "RTX 40") { return "8.9" }
    if ($Name -match "RTX 30") { return "8.6" }
    if ($Name -match "RTX 20") { return "7.5" }
    return $null
}

function Test-CudaStackCompatible {
    param([string]$DriverVersion, [string]$ComputeCapability)
    if (-not ($DriverVersion -match '^(\d+)')) { return $false }
    $driverMajor = [int]$Matches[1]

    $compute = 0.0
    if (-not [double]::TryParse($ComputeCapability, [System.Globalization.NumberStyles]::Any, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$compute)) {
        return $false
    }

    $cudaMajor = Get-CudaMajorFromEnv
    if ($null -eq $cudaMajor) { return $driverMajor -ge 450 }
    if ($cudaMajor -ge 13) { return ($driverMajor -ge 570) -and ($compute -ge 6.1) }
    if ($cudaMajor -eq 12) { return ($driverMajor -ge 525) -and ($compute -ge 5.0) }
    if ($cudaMajor -eq 11) { return ($driverMajor -ge 450) -and ($compute -ge 3.5) }
    if ($cudaMajor -eq 10) { return ($driverMajor -ge 410) -and ($compute -ge 3.0) }
    if ($cudaMajor -eq 9)  { return ($driverMajor -ge 385) -and ($compute -ge 3.0) }
    return $driverMajor -ge 450
}

function Test-MikeHealth {
    param([int]$Port)
    $healthUrl = "http://127.0.0.1:$Port/health"
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
        return ($response.status -eq "ok" -or $response.status -eq "healthy")
    } catch {
        return $false
    }
}

function Get-RuntimeEnvValue {
    param([string]$Name, [string]$ProjectRoot)
    if (-not $ProjectRoot) {
        $ProjectRoot = $script:projectRoot
    }
    if (-not $ProjectRoot) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
        $ProjectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
    }
    $runtimeEnv = Join-Path $ProjectRoot "config\.env.runtime"
    if (-not (Test-Path $runtimeEnv)) { return "" }

    $line = Get-Content $runtimeEnv |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -First 1
    if (-not $line) { return "" }
    return $line -replace "^$([regex]::Escape($Name))=", ""
}

function Show-RecentLogs {
    param([string]$StdoutLog, [string]$StderrLog)
    Write-Host "`nRecent stdout:" -ForegroundColor Yellow
    if (-not [string]::IsNullOrWhiteSpace($StdoutLog) -and (Test-Path $StdoutLog)) { Get-Content $StdoutLog -Tail 40 }
    else { Write-Host "  <missing>" -ForegroundColor DarkYellow }

    Write-Host "`nRecent stderr:" -ForegroundColor Yellow
    if (-not [string]::IsNullOrWhiteSpace($StderrLog) -and (Test-Path $StderrLog)) { Get-Content $StderrLog -Tail 40 }
    else { Write-Host "  <missing>" -ForegroundColor DarkYellow }
}

function Stop-MikeServiceIfRunning {
    param([int]$TargetPort)
    $svc = Get-Service -Name "MikeServer" -ErrorAction SilentlyContinue
    if (-not $svc) { return }

    if ($svc.Status -eq "Running") {
        Write-Host "  MikeServer service is running; stopping to avoid port conflict on $TargetPort..." -ForegroundColor Yellow
        try { Stop-Service -Name "MikeServer" -Force -ErrorAction Stop }
        catch {
            try {
                Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -Command "Stop-Service MikeServer -Force"' -Wait
            } catch {
                Write-Host "  WARNING: Could not stop MikeServer service. Run as Admin: Stop-Service MikeServer -Force" -ForegroundColor Red
            }
        }
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Seconds 1
            if (-not (Get-PortListener -Port $TargetPort)) { break }
        }
    }
}

function Ensure-CloudflareTunnel {
    param([string]$ProjectRoot)
    $serviceName = "CloudflaredMikeTunnel"
    $cloudflaredExe = Join-Path $ProjectRoot "bin\cloudflared.exe"
    $tunnelConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"

    $svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -ne "Running") {
            Write-Host "  [Tunnel] Starting service $serviceName..." -ForegroundColor Yellow
            try { Start-Service -Name $serviceName -ErrorAction Stop; Start-Sleep -Seconds 2 }
            catch { Write-Host "  [Tunnel] WARNING: could not start service $serviceName." -ForegroundColor Yellow }
            $svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        }
        if ($svc -and $svc.Status -eq "Running") {
            Write-Host "  [Tunnel] Service running ($serviceName)." -ForegroundColor Green
            return
        }
    }

    if ((Test-Path $cloudflaredExe) -and (Test-Path $tunnelConfig)) {
        $cfRunning = Get-Process cloudflared -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cfRunning) {
            Write-Host "  [Tunnel] Starting cloudflared process fallback..." -ForegroundColor Yellow
            Start-Process -FilePath $cloudflaredExe -ArgumentList "tunnel --config `"$tunnelConfig`" run" -WindowStyle Hidden
            Start-Sleep -Seconds 3
        }
        $cfRunning = Get-Process cloudflared -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cfRunning) {
            Write-Host "  [Tunnel] Process running (PID $($cfRunning.Id))." -ForegroundColor Green
        } else {
            Write-Host "  [Tunnel] WARNING: cloudflared did not start." -ForegroundColor Yellow
        }
    } else {
        Write-Host "  [Tunnel] WARNING: cloudflared binary/config not found; skipping tunnel startup." -ForegroundColor Yellow
    }
}
