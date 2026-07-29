param(
    [int]$Port = 0,
    [int]$StartupTimeoutSec = 600,
    [switch]$Foreground,
    [switch]$ForceRestart,
    [switch]$SkipTunnel,
    [switch]$ServiceMode
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$serverPath = Join-Path $projectRoot "core\server\mike_server.py"

# Dot-source common module
. "$scriptDir\mike_common.ps1"

# Export MIKE_HOME so mike_config.py resolves PROJECT_ROOT correctly
$env:MIKE_HOME = $projectRoot

# Export PYTHONPATH so all core subsystems are importable by name
$coreDirs = @("server","memory","mcp","autonomy","comms","orchestration","integrations","chat") |
    ForEach-Object { Join-Path $projectRoot "core\$_" }
$corePaths = ($coreDirs + @($projectRoot, (Join-Path $projectRoot "core"))) -join ";"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$corePaths;$($env:PYTHONPATH)" } else { $corePaths }
$logsDir = Join-Path $projectRoot "logs"
$stdoutLog = Join-Path $logsDir "mike_stdout.log"
$stderrLog = Join-Path $logsDir "mike_stderr.log"

# Resolve port: parameter > MIKE_PORT env > .env.runtime > default 8083
if ($Port -eq 0) {
    $envFile = Join-Path $projectRoot "config\.env.runtime"
    $mikePort = $env:MIKE_PORT
    if (-not $mikePort -and (Test-Path $envFile)) {
        $mikePort = (Get-Content $envFile | Where-Object { $_ -match '^MIKE_PORT=' } | Select-Object -First 1) -replace '^MIKE_PORT=',''
    }
    $Port = if ($mikePort) { [int]$mikePort } else { 8083 }
}

$healthUrl = "http://127.0.0.1:$Port/health"
$dashboardUrl = "http://127.0.0.1:$Port/"
$apiUrl = "http://127.0.0.1:$Port/v1/chat/completions"

# Shared functions now in mike_common.ps1 (dot-sourced above):
# Write-Banner, Get-MikeProcesses, Get-PortListener, Get-NvidiaSmiPath,
# Get-CudaMajorFromEnv, Get-ComputeCapabilityFromName, Test-CudaStackCompatible,
# Test-MikeHealth, Get-RuntimeEnvValue, Show-RecentLogs,
# Stop-MikeServiceIfRunning, Ensure-CloudflareTunnel

if (-not (Test-Path $serverPath)) {
    throw "Server file not found: $serverPath"
}

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

Write-Banner
Write-Host "Workspace: $projectRoot" -ForegroundColor DarkCyan
Write-Host "Dashboard: $dashboardUrl" -ForegroundColor DarkCyan
Write-Host "API:       $apiUrl" -ForegroundColor DarkCyan
Write-Host "Health:    $healthUrl" -ForegroundColor DarkCyan

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) {
    Get-Item $venvPython
} else {
    Get-Command python -ErrorAction Stop
}
$cuda61Runtime = Join-Path $projectRoot "build\llama-cpp-python-cuda61\runtime"
Write-Host "`n[1/4] Python..." -ForegroundColor Yellow
$pythonPath = if ($python.PSObject.Properties.Match('FullName').Count -gt 0 -and $python.FullName) {
    $python.FullName
} else {
    $python.Source
}
Write-Host "  Using: $pythonPath" -ForegroundColor Green
if (Test-Path (Join-Path $cuda61Runtime "llama_cpp\__init__.py")) {
    $cuda61RuntimeBin = Join-Path $cuda61Runtime "llama_cpp\bin"
    $cuda61RuntimeLib = Join-Path $cuda61Runtime "llama_cpp\lib"
    $env:PYTHONPATH = if ($env:PYTHONPATH) {
        "$cuda61Runtime;$($env:PYTHONPATH)"
    } else {
        $cuda61Runtime
    }
    foreach ($nativeDir in @($cuda61RuntimeBin, $cuda61RuntimeLib)) {
        if (Test-Path $nativeDir) {
            $env:PATH = "$nativeDir;$($env:PATH)"
        }
    }
    Write-Host "  Using CUDA sm_61 llama_cpp runtime: $cuda61Runtime" -ForegroundColor Green
}

Write-Host "`n[2/4] GPU..." -ForegroundColor Yellow
$cudaUsable = $false
$forceCpuEnv = (Get-RuntimeEnvValue "MIKE_FORCE_CPU").Trim().ToLowerInvariant()
$disableGpuEnv = (Get-RuntimeEnvValue "MIKE_DISABLE_GPU").Trim().ToLowerInvariant()
$gpuForbidden = @("1", "true", "yes", "on").Contains($forceCpuEnv) -or @("1", "true", "yes", "on").Contains($disableGpuEnv)

if ($gpuForbidden) {
    $env:MIKE_FORCE_CPU = "true"
    $env:MIKE_DISABLE_GPU = "true"
    $env:CUDA_VISIBLE_DEVICES = "-1"
    Write-Host "  GPU usage is forbidden by runtime config. CPU-only mode is locked." -ForegroundColor Yellow
} else {
    $userCuda129 = Join-Path $env:USERPROFILE "cuda\v12.9"
    if (Test-Path (Join-Path $userCuda129 "bin\nvcc.exe")) {
        $env:CUDA_PATH = $userCuda129
        $env:CUDA_HOME = $userCuda129
        $env:CUDA_PATH_V12_9 = $userCuda129
        $env:PATH = (Join-Path $userCuda129 "bin") + ";" + (Join-Path $userCuda129 "libnvvp") + ";" + $env:PATH
        Write-Host "  Using local CUDA 12.9: $userCuda129" -ForegroundColor Green
    }

    $nvidiaSmi = Get-NvidiaSmiPath
    if ($nvidiaSmi) {
        $env:MIKE_NVIDIA_SMI = $nvidiaSmi
        try {
            # This workstation has a stale GPU1/P106 driver entry. Query the
            # primary RTX directly so stderr from the unavailable device does
            # not make PowerShell classify the whole CUDA stack as failed.
            $gpu = & $nvidiaSmi -i 0 --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader 2>$null
        } catch {
            $gpu = $null
        }
        if ($gpu) {
            foreach ($line in @($gpu)) {
                $parts = $line -split ","
                $computeCapability = if ($parts.Count -ge 4) {
                    $parts[3].Trim()
                } else {
                    Get-ComputeCapabilityFromName -Name $parts[0].Trim()
                }
                if (
                    $parts.Count -ge 3 -and
                    $parts[1].Trim() -match "^\d+" -and
                    (Test-CudaStackCompatible -DriverVersion $parts[2].Trim() -ComputeCapability $computeCapability)
                ) {
                    $cudaUsable = $true
                    break
                }
            }

            if ($cudaUsable) {
                Write-Host "  GPU Found: $gpu" -ForegroundColor Green
            } else {
                Write-Host "  GPU present, but this CUDA/driver stack is not compatible. Forcing CPU-safe startup." -ForegroundColor Yellow
                Write-Host "  Detected: $gpu" -ForegroundColor DarkYellow
            }
        } else {
            Write-Host "  WARNING: GPU detected but nvidia-smi query failed." -ForegroundColor Red
        }
    } else {
        Write-Host "  WARNING: nvidia-smi not found in this PowerShell environment." -ForegroundColor Yellow
    }
}

# --- Neo4j auto-start (if MIKE_GRAPH_ENABLED=true) ---
$graphEnabled = (Get-Content (Join-Path $projectRoot "config\.env.runtime") -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^MIKE_GRAPH_ENABLED=' } | Select-Object -First 1) -replace '^MIKE_GRAPH_ENABLED=',''
if ($graphEnabled -eq "true") {
    $neo4jBolt = $false
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", 7687)
        $neo4jBolt = $true
        $tcp.Close()
    } catch {}

    if (-not $neo4jBolt) {
        Write-Host "`n[Neo4j] Starting Neo4j..." -ForegroundColor Yellow
        $neo4jLog = "C:\neo4j\logs\neo4j-console.log"
        $lockFile = "C:\neo4j\data\databases\store_lock"
        if (Test-Path $lockFile) { Remove-Item $lockFile -Force }
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c C:\neo4j\bin\neo4j.bat console > `"$neo4jLog`" 2>&1" -WindowStyle Hidden
        $waited = 0
        while ($waited -lt 30) {
            Start-Sleep -Seconds 2; $waited += 2
            try {
                $t = New-Object System.Net.Sockets.TcpClient; $t.Connect("127.0.0.1", 7687); $t.Close()
                Write-Host "  Neo4j ready on bolt://localhost:7687" -ForegroundColor Green
                break
            } catch {}
        }
        if ($waited -ge 30) { Write-Host "  Neo4j did not start in time - continuing anyway" -ForegroundColor Yellow }
    } else {
        Write-Host "`n[Neo4j] Already running on bolt://localhost:7687" -ForegroundColor Green
    }
}

Write-Host "`n[3/4] Model cache..." -ForegroundColor Yellow
$llmBackend = (Get-RuntimeEnvValue "MIKE_LLM_BACKEND").Trim().ToLowerInvariant()
if (-not $llmBackend) { $llmBackend = "local" }

if ($llmBackend -eq "deepseek") {
    $deepseekModel = (Get-RuntimeEnvValue "DEEPSEEK_CHAT_MODEL").Trim()
    if (-not $deepseekModel) { $deepseekModel = "deepseek-v4-pro" }
    Write-Host "  Backend: DeepSeek API ($deepseekModel)" -ForegroundColor Green
    Write-Host "  Local GGUF cache is not used for chat generation." -ForegroundColor DarkGray
} else {
    $modelFileLine = Get-RuntimeEnvValue "MIKE_MODEL_FILE"
    $modelFile = if ($modelFileLine -and [System.IO.Path]::IsPathRooted($modelFileLine)) {
        $modelFileLine
    } elseif ($modelFileLine) {
        Join-Path $projectRoot "llm_cache\$modelFileLine"
    } else {
        Join-Path $projectRoot "llm_cache\model.gguf"
    }
    if ((Test-Path $modelFile) -and ((Get-Item $modelFile).Length -gt 0)) {
        Write-Host "  Model cached: OK ($([math]::Round((Get-Item $modelFile).Length/1GB, 1)) GB) - $([System.IO.Path]::GetFileName($modelFile))" -ForegroundColor Green
    } elseif (Test-Path $modelFile) {
        Write-Host "  Model cache is invalid: $modelFile (0 bytes)" -ForegroundColor Red
        Write-Host "  Mike will remove it and download the model from Hugging Face on startup." -ForegroundColor Yellow
    } else {
        Write-Host "  Model not found: $modelFile" -ForegroundColor Red
        Write-Host "  Mike will download it from Hugging Face on startup; first boot can take a while." -ForegroundColor Yellow
    }
}

Write-Host "`n[4/5] Tunnel..." -ForegroundColor Yellow
if ($SkipTunnel) {
    Write-Host "  [Tunnel] Skipped. Local Mike remains available." -ForegroundColor Yellow
} else {
    Ensure-CloudflareTunnel -ProjectRoot $projectRoot
}

$existing = @(Get-MikeProcesses -ProjectRoot $projectRoot)
if ($existing.Count -gt 0) {
    if (-not $ForceRestart -and (Test-MikeHealth -Port $Port)) {
        Write-Host "`n[4/4] Mike is already running and healthy." -ForegroundColor Green
        Write-Host "  Dashboard: $dashboardUrl" -ForegroundColor Green
        Write-Host "  API:       $apiUrl" -ForegroundColor Green
        exit 0
    }

    Write-Host "`n[4/4] Restarting existing Mike process..." -ForegroundColor Yellow
    foreach ($proc in $existing) {
        Write-Host "  Stopping PID $($proc.ProcessId)" -ForegroundColor Yellow
        Stop-MikeProcessSafely -ProcessId $proc.ProcessId -ProjectRoot $projectRoot -Force
    }
    Start-Sleep -Seconds 2
}

$listener = Get-PortListener -Port $Port
if ($listener) {
    $ownerId = $listener.OwningProcess
    if (-not $ForceRestart -and (Test-MikeHealth -Port $Port)) {
        Write-Host "`n[4/4] Mike is already running on port $Port." -ForegroundColor Green
        Write-Host "  Dashboard: $dashboardUrl" -ForegroundColor Green
        Write-Host "  API:       $apiUrl" -ForegroundColor Green
        exit 0
    }

    if (-not (Test-MikeProcessIdentity -ProcessId $ownerId -ProjectRoot $projectRoot)) {
        $diagnostic = Get-MikeProcessDiagnostic -ProcessId $ownerId
        Write-Host "  ERROR: port $Port is owned by a process that is not this Mike runtime." -ForegroundColor Red
        Write-Host "  $diagnostic" -ForegroundColor Yellow
        Write-Host "  Refusing to stop it. Free the port explicitly or select another port." -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  Liberando porta $Port (Mike PID $ownerId)..." -ForegroundColor Yellow
    Stop-MikeProcessSafely -ProcessId $ownerId -ProjectRoot $projectRoot -Force
    Start-Sleep -Seconds 2
    $listener = Get-PortListener -Port $Port
    if ($listener) {
        if (Test-MikeHealth -Port $Port) {
            Write-Host "  Porta $Port continua ocupada por PID $($listener.OwningProcess), mas Mike esta saudavel. Mantendo processo atual." -ForegroundColor Yellow
            Write-Host "  Dashboard: $dashboardUrl" -ForegroundColor Green
            Write-Host "  API:       $apiUrl" -ForegroundColor Green
            exit 0
        }

        Write-Host "  ERRO: porta $Port ainda ocupada por PID $($listener.OwningProcess)." -ForegroundColor Red
        Write-Host "  Use PowerShell como Admin para encerrar o processo dono da porta." -ForegroundColor Yellow
        exit 1
    }
}

Write-Host "`n[5/5] Starting Mike..." -ForegroundColor Yellow

# Força uso do cache local do HuggingFace (evita protocolo XET que tenta baixar/verificar o modelo pela internet)
$env:HF_HUB_DISABLE_XET = "1"

# Force UTF-8 encoding for Python stdout/stderr (prevents cp1252 crash with emoji/unicode)
$env:PYTHONIOENCODING = "utf-8"

# Offline mode for Hugging Face (prevents hangs if models are already cached)
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"

if (-not $cudaUsable) {
    # Keep Mike away from unstable or incompatible CUDA driver stacks.
    $env:MIKE_FORCE_CPU = "true"
    $env:CUDA_VISIBLE_DEVICES = "-1"
}

if ($Foreground) {
    if (-not $ServiceMode) {
        Stop-MikeServiceIfRunning -TargetPort $Port
    }

    $listener = Get-PortListener -Port $Port
    if ($listener) {
        Write-Host "  ERROR: port $Port is still occupied by PID $($listener.OwningProcess)." -ForegroundColor Red
        Write-Host "  Stop MikeServer first (admin): Stop-Service MikeServer -Force" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  Running in foreground. Press Ctrl+C to stop." -ForegroundColor Cyan
    & $pythonPath $serverPath
    exit $LASTEXITCODE
}

foreach ($logFile in @($stdoutLog, $stderrLog)) {
    if (Test-Path $logFile) {
        try {
            # Tenta limpar o conteúdo. Se falhar, tentamos identificar quem está travando.
            Clear-Content $logFile -ErrorAction Stop
        } catch {
            Write-Host "  WARNING: Log $logFile is locked. Attempting to force release..." -ForegroundColor Yellow
            $lockers = @(Get-MikeProcesses -ProjectRoot $projectRoot) |
                Where-Object { $_.ProcessId -ne $PID }
            foreach ($l in $lockers) {
                Stop-MikeProcessSafely -ProcessId $l.ProcessId -ProjectRoot $projectRoot -Force
            }
            Start-Sleep -Seconds 1
            Clear-Content $logFile -ErrorAction SilentlyContinue
        }
    }
}

$process = Start-Process -FilePath $pythonPath `
    -ArgumentList "`"$serverPath`"" `
    -WorkingDirectory $projectRoot `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog

Write-Host "  PID: $($process.Id)" -ForegroundColor Green
Write-Host "  Waiting for healthcheck..." -ForegroundColor Cyan

$deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2

    if ($process.HasExited) {
        Write-Host "`nMike exited during startup with code $($process.ExitCode)." -ForegroundColor Red
        Show-RecentLogs
        exit 1
    }

    if (Test-MikeHealth -Port $Port) {
        Write-Host "`nMike is awake." -ForegroundColor Green
        Write-Host "  Dashboard: $dashboardUrl" -ForegroundColor Green
        Write-Host "  API:       $apiUrl" -ForegroundColor Green
        Write-Host "  Logs:      $stdoutLog" -ForegroundColor Green

        if (-not $SkipTunnel) {
            $cfRunning = Get-Process cloudflared -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($cfRunning) {
                Write-Host "`n[Tunnel] Running (PID $($cfRunning.Id)) - https://mike.supereziorealtime.com" -ForegroundColor Green
            } else {
                Write-Host "`n[Tunnel] Not running after startup." -ForegroundColor Yellow
            }
        }

        exit 0
    }
}

Write-Host "`nMike did not answer the healthcheck within $StartupTimeoutSec seconds." -ForegroundColor Red
Show-RecentLogs
exit 1
