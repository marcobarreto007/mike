param(
    [int]$Port = 8083,
    [int]$StartupTimeoutSec = 75,
    [switch]$IncludeFlash,
    [string]$ReportPath
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$runtimeFile = Join-Path $projectRoot "config\\.env.runtime"
$stdoutLog = Join-Path $projectRoot "logs\\tuning_stdout.log"
$stderrLog = Join-Path $projectRoot "logs\\tuning_stderr.log"
$healthUrl = "http://127.0.0.1:$Port/health"
$chatUrl = "http://127.0.0.1:$Port/v1/chat/completions"
$statsUrl = "http://127.0.0.1:$Port/stats"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $ReportPath) {
    $ReportPath = Join-Path $projectRoot ("mike\\roadmap\\runtime_tuning_report_{0}.json" -f $timestamp)
}

. (Join-Path $scriptDir "mike_common.ps1")

function Stop-MikeProcesses {
    $procs = @(Get-MikeProcesses -ProjectRoot $projectRoot)
    foreach ($proc in $procs) {
        Stop-MikeProcessSafely -ProcessId $proc.ProcessId -ProjectRoot $projectRoot -Force
    }
    if ($procs.Count -gt 0) {
        Start-Sleep -Seconds 2
    }
}

function Wait-ForHealth([int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
            if ($health.status -eq "ok") {
                return $true
            }
        } catch {
        }
        Start-Sleep -Milliseconds 750
    }
    return $false
}

function Invoke-BenchmarkChat([string]$ProfileName) {
    $body = @{
        model = "mike"
        session_id = "runtime-bench-$ProfileName"
        messages = @(
            @{
                role = "user"
                content = "Explique em duas frases curtas por que latencia e memoria importam para o Mike."
            }
        )
        max_tokens = 96
        temperature = 0.1
        stream = $false
    } | ConvertTo-Json -Depth 6

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-RestMethod -Method Post -Uri $chatUrl -Body $body -ContentType "application/json" -TimeoutSec 180
    $sw.Stop()
    $stats = Invoke-RestMethod -Uri $statsUrl -TimeoutSec 15

    return [pscustomobject]@{
        generation_seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2)
        response_text = $response.choices[0].message.content
        last_speed_tps = $stats.last_speed_tps
        total_tokens_generated = $stats.total_tokens_generated
    }
}

function Set-ProfileEnv([hashtable]$Profile) {
    $keys = @(
        "MIKE_RUNTIME_PROFILE",
        "MIKE_CTX_SIZE",
        "MIKE_GPU_LAYERS",
        "MIKE_N_BATCH",
        "MIKE_N_UBATCH",
        "MIKE_FLASH_ATTN"
    )
    $previous = @{}
    foreach ($key in $keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
    }
    foreach ($entry in $Profile.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, "Process")
    }
    return $previous
}

function Restore-ProfileEnv([hashtable]$Previous) {
    foreach ($entry in $Previous.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot "logs") | Out-Null

$profiles = @(
    @{
        name = "baseline-balanced"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 33
        MIKE_N_BATCH = 1024
        MIKE_N_UBATCH = 512
        MIKE_FLASH_ATTN = "false"
    },
    @{
        name = "low-latency"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 35
        MIKE_N_BATCH = 768
        MIKE_N_UBATCH = 384
        MIKE_FLASH_ATTN = "false"
    },
    @{
        name = "balanced-plus"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 35
        MIKE_N_BATCH = 896
        MIKE_N_UBATCH = 448
        MIKE_FLASH_ATTN = "false"
    },
    @{
        name = "stability"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 33
        MIKE_N_BATCH = 768
        MIKE_N_UBATCH = 384
        MIKE_FLASH_ATTN = "false"
    },
    @{
        name = "context-4608"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4608
        MIKE_GPU_LAYERS = 35
        MIKE_N_BATCH = 896
        MIKE_N_UBATCH = 448
        MIKE_FLASH_ATTN = "false"
    },
    @{
        name = "context"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 6144
        MIKE_GPU_LAYERS = 33
        MIKE_N_BATCH = 768
        MIKE_N_UBATCH = 384
        MIKE_FLASH_ATTN = "false"
    },
    @{
        name = "aggressive"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 35
        MIKE_N_BATCH = 1024
        MIKE_N_UBATCH = 512
        MIKE_FLASH_ATTN = "false"
    },
    @{
        name = "throughput-1152"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 35
        MIKE_N_BATCH = 1152
        MIKE_N_UBATCH = 576
        MIKE_FLASH_ATTN = "false"
    },
    @{
        name = "throughput-1280"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 35
        MIKE_N_BATCH = 1280
        MIKE_N_UBATCH = 640
        MIKE_FLASH_ATTN = "false"
    }
)

if ($IncludeFlash) {
    $profiles += @{
        name = "flash-35"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 35
        MIKE_N_BATCH = 1024
        MIKE_N_UBATCH = 512
        MIKE_FLASH_ATTN = "true"
    }
    $profiles += @{
        name = "flash-33"
        MIKE_RUNTIME_PROFILE = "manual"
        MIKE_CTX_SIZE = 4096
        MIKE_GPU_LAYERS = 33
        MIKE_N_BATCH = 1024
        MIKE_N_UBATCH = 512
        MIKE_FLASH_ATTN = "true"
    }
}

$results = @()
Stop-MikeProcesses

foreach ($profile in $profiles) {
    Write-Host "`nTesting profile: $($profile.name)" -ForegroundColor Cyan
    $previous = Set-ProfileEnv -Profile $profile
    try {
        if (Test-Path $stdoutLog) { Remove-Item $stdoutLog -Force }
        if (Test-Path $stderrLog) { Remove-Item $stderrLog -Force }

        $bootWatch = [System.Diagnostics.Stopwatch]::StartNew()
        $process = Start-Process -FilePath $pythonPath `
            -ArgumentList ("`"{0}`"" -f (Join-Path $projectRoot "core\server\mike_server.py")) `
            -WorkingDirectory $projectRoot `
            -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog

        $healthy = Wait-ForHealth -TimeoutSec $StartupTimeoutSec
        $bootWatch.Stop()

        if (-not $healthy) {
            $results += [pscustomobject]@{
                profile = $profile.name
                status = "failed-startup"
                boot_seconds = [math]::Round($bootWatch.Elapsed.TotalSeconds, 2)
                generation_seconds = $null
                last_speed_tps = 0
                ctx_size = [int]$profile.MIKE_CTX_SIZE
                gpu_layers = [int]$profile.MIKE_GPU_LAYERS
                n_batch = [int]$profile.MIKE_N_BATCH
                n_ubatch = [int]$profile.MIKE_N_UBATCH
                flash_attn = $profile.MIKE_FLASH_ATTN
            }
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force
            }
            continue
        }

        $bench = Invoke-BenchmarkChat -ProfileName $profile.name
        $results += [pscustomobject]@{
            profile = $profile.name
            status = "ok"
            boot_seconds = [math]::Round($bootWatch.Elapsed.TotalSeconds, 2)
            generation_seconds = $bench.generation_seconds
            last_speed_tps = $bench.last_speed_tps
            ctx_size = [int]$profile.MIKE_CTX_SIZE
            gpu_layers = [int]$profile.MIKE_GPU_LAYERS
            n_batch = [int]$profile.MIKE_N_BATCH
            n_ubatch = [int]$profile.MIKE_N_UBATCH
            flash_attn = $profile.MIKE_FLASH_ATTN
            response_text = $bench.response_text
        }
    } catch {
        $results += [pscustomobject]@{
            profile = $profile.name
            status = "error"
            boot_seconds = $null
            generation_seconds = $null
            last_speed_tps = 0
            ctx_size = [int]$profile.MIKE_CTX_SIZE
            gpu_layers = [int]$profile.MIKE_GPU_LAYERS
            n_batch = [int]$profile.MIKE_N_BATCH
            n_ubatch = [int]$profile.MIKE_N_UBATCH
            flash_attn = $profile.MIKE_FLASH_ATTN
            error = $_.Exception.Message
        }
    } finally {
        Stop-MikeProcesses
        Restore-ProfileEnv -Previous $previous
    }
}

$winner = $results |
    Where-Object { $_.status -eq "ok" } |
    Sort-Object -Property @{Expression = "last_speed_tps"; Descending = $true}, @{Expression = "generation_seconds"; Descending = $false} |
    Select-Object -First 1

if (-not $winner) {
    Write-Host "`nNo runtime profile completed successfully." -ForegroundColor Red
    $results | Format-Table -AutoSize
    exit 1
}

$runtimeLines = @(
    "# Auto-generated by tune_mike_runtime.ps1",
    "MIKE_RUNTIME_PROFILE=manual",
    "MIKE_CTX_SIZE=$($winner.ctx_size)",
    "MIKE_GPU_LAYERS=$($winner.gpu_layers)",
    "MIKE_N_BATCH=$($winner.n_batch)",
    "MIKE_N_UBATCH=$($winner.n_ubatch)",
    "MIKE_FLASH_ATTN=$($winner.flash_attn)"
)

Set-Content -LiteralPath $runtimeFile -Value $runtimeLines -Encoding UTF8

$report = [ordered]@{
    schema_version = "mike.runtime_tuning.v1"
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    project_root = $projectRoot
    winner = $winner
    results = $results
}

$reportDir = Split-Path -Parent $ReportPath
if ($reportDir) {
    New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
}
$report | ConvertTo-Json -Depth 8 | Set-Content -Path $ReportPath -Encoding UTF8

Write-Host "`nBenchmark results:" -ForegroundColor Cyan
$results | Format-Table -AutoSize

Write-Host "`nWinner: $($winner.profile)" -ForegroundColor Green
Write-Host "Runtime file written to: $runtimeFile" -ForegroundColor Green
Write-Host "Report written to: $ReportPath" -ForegroundColor Green

& (Join-Path $scriptDir "start_mike.ps1") -Port $Port -ForceRestart | Out-Null
