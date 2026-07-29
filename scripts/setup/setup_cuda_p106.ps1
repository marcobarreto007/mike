# setup_cuda_p106.ps1
# Consolidated NVIDIA/CUDA Environment Setup & Diagnostic
# Target Hardware: GTX 10-series, P106-100 (Pascal sm_61)

$ErrorActionPreference = 'Continue'
$PROJECT_ROOT = Resolve-Path "$PSScriptRoot\.."
$VENV_PYTHON = "$PROJECT_ROOT\.venv\Scripts\python.exe"

Write-Host ''
Write-Host '==> [1/4] Hardware Diagnostic' -ForegroundColor Cyan

$nvsmi = where.exe nvidia-smi 2>$null
if (-not $nvsmi) {
    Write-Host '  ERROR: nvidia-smi not found. Please install NVIDIA drivers (560+).' -ForegroundColor Red
    exit 1
}

$gpus = nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
Write-Host '  Detected GPUs:' -ForegroundColor White
foreach ($gpu in $gpus) { Write-Host "    - $gpu" -ForegroundColor Gray }

Write-Host ''
Write-Host '==> [2/4] Python Environment' -ForegroundColor Cyan
if (-not (Test-Path $VENV_PYTHON)) {
    Write-Host "  ERROR: Virtual environment not found at $VENV_PYTHON" -ForegroundColor Red
    exit 1
}

$pyVer = & $VENV_PYTHON --version
Write-Host "  Python: $pyVer" -ForegroundColor Gray

Write-Host ''
Write-Host '==> [3/4] CUDA Runtime Validation' -ForegroundColor Cyan

$tmpDir = "$PROJECT_ROOT\scripts\tmp"
if (-not (Test-Path $tmpDir)) { New-Item -ItemType Directory -Path $tmpDir | Out-Null }

$testPyPath = "$tmpDir\cuda_validate.py"
$testPyCode = @'
import json, sys
try:
    import llama_cpp
    from llama_cpp import llama_cpp as low
    info = llama_cpp.llama_print_system_info().decode('utf-8', errors='ignore')
    gpu_ok = False
    if hasattr(low, 'llama_supports_gpu_offload'):
        gpu_ok = low.llama_supports_gpu_offload()
    elif 'CUDA' in info and '1' in info:
        gpu_ok = True
    print(json.dumps({'version': llama_cpp.__version__, 'gpu_offload': bool(gpu_ok), 'info': info.strip()}))
except Exception as e:
    print(json.dumps({'error': str(e)}))
'@
Set-Content -Path $testPyPath -Value $testPyCode -Encoding UTF8

$resRaw = & $VENV_PYTHON $testPyPath
$needsFix = $true

if ($null -ne $resRaw) {
    $jsonOnly = ($resRaw | Select-String '\{.*\}').ToString()
    if ($jsonOnly) {
        $resJson = $jsonOnly | ConvertFrom-Json
        if ($resJson.error) {
            Write-Host "  Status: $($resJson.error)" -ForegroundColor Red
        } else {
            Write-Host "  llama-cpp-python: $($resJson.version)" -ForegroundColor White
            if ($resJson.gpu_offload) {
                Write-Host '  ✓ CUDA INITIALIZED' -ForegroundColor Green
                $needsFix = $false
            } else {
                Write-Host '  ✗ CUDA NOT INITIALIZED (CPU Fallback)' -ForegroundColor Yellow
            }
        }
    }
}

Write-Host ''
Write-Host '==> [4/4] Environment Repair' -ForegroundColor Cyan

if (-not $needsFix) {
    Write-Host '  System is healthy. CUDA is correctly configured.' -ForegroundColor Green
} else {
    Write-Host '  Action required to enable GPU offloading.' -ForegroundColor Yellow
    Write-Host '  1. Install pre-compiled Pascal (sm_61) wheel' -ForegroundColor White
    Write-Host '  2. Exit' -ForegroundColor White
    
    $choice = Read-Host 'Selection'
    
    if ($choice -eq '1') {
        Write-Host '  Installing llama-cpp-python (cu124/sm_61)...' -ForegroundColor Cyan
        $url = 'https://github.com/JamePeng/llama-cpp-python/releases/download/v0.3.36-cu124-Basic-win-20260417/llama_cpp_python-0.3.36+cu124.basic-cp311-cp311-win_amd64.whl'
        & $VENV_PYTHON -m pip install $url --force-reinstall --no-deps
        Write-Host '  Repair complete. Please restart the application.' -ForegroundColor Green
    }
}

Remove-Item -Path $testPyPath -ErrorAction SilentlyContinue
Write-Host ''
Write-Host 'Diagnostic complete.' -ForegroundColor Cyan
