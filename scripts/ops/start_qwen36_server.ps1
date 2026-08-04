# =============================================================================
# MIKE — Qwen3.6-35B-A3B Server
# Perfil atual: RTX 5060 Ti 16GB (GPU0) + RTX 3060 12GB (GPU1) + 64GB RAM
# Perfil legado (fallback): RTX 2070 8GB hybrid n_cpu_moe=99
# =============================================================================

param(
    [string]$ModelPath,
    [int]$Port = 8081,
    [int]$CtxSize = 16384,
    [int]$GpuLayers = 999,
    [int]$CpuMoe = -1,
    [int]$Threads = 0,
    [int]$BatchSize = 1024,
    [int]$UbatchSize = 512,
    [string]$TensorSplit = "",
    [string]$CudaDevices = "0,1",
    [ValidateSet("auto", "dual", "gpu0", "legacy2070")]
    [string]$Profile = "auto"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)
. (Join-Path $PSScriptRoot "mike_common.ps1")

$ModelPath = Resolve-MikeQwenModelPath -ProjectRoot $ProjectRoot -ModelPath $ModelPath

$LlamaDir = Join-Path $ProjectRoot "llama.cpp\build\bin"
$ServerExe = Join-Path $LlamaDir "llama-server.exe"
if (-not (Test-Path -LiteralPath $ServerExe)) {
    throw "llama-server.exe not found: $ServerExe. Build llama.cpp with CUDA (SM 8.6 + 12.0) first."
}
$env:PATH = "$LlamaDir;$env:PATH"

function Get-MikeGpuInventory {
    $rows = @()
    try {
        $csv = & nvidia-smi --query-gpu=index, name, memory.total --format=csv, noheader, nounits 2>$null
        foreach ($line in @($csv)) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $parts = $line.Split(",") | ForEach-Object { $_.Trim() }
            if ($parts.Count -lt 3) { continue }
            $rows += [pscustomobject]@{
                Index  = [int]$parts[0]
                Name   = $parts[1]
                VramMb = [int]$parts[2]
            }
        }
    }
    catch {}
    return $rows
}

$gpus = Get-MikeGpuInventory
$has5060 = $gpus | Where-Object { $_.Name -match "5060" }
$has3060 = $gpus | Where-Object { $_.Name -match "3060" }
$has2070 = $gpus | Where-Object { $_.Name -match "2070" }

if ($Profile -eq "auto") {
    if ($has5060 -and $has3060) { $Profile = "dual" }
    elseif ($has5060) { $Profile = "gpu0" }
    elseif ($has2070) { $Profile = "legacy2070" }
    elseif ($gpus.Count -ge 2) { $Profile = "dual" }
    else { $Profile = "gpu0" }
}

if ($Threads -le 0) {
    $Threads = [Math]::Max(4, [Math]::Min(12, [int]$env:NUMBER_OF_PROCESSORS))
}

switch ($Profile) {
    "dual" {
        if ($CpuMoe -lt 0) { $CpuMoe = 0 }
        if ([string]::IsNullOrWhiteSpace($TensorSplit)) { $TensorSplit = "16,12" }
        if ([string]::IsNullOrWhiteSpace($CudaDevices)) { $CudaDevices = "0,1" }
        $modeLabel = "dual-GPU tensor-split (5060 Ti + 3060)"
    }
    "gpu0" {
        if ($CpuMoe -lt 0) { $CpuMoe = 0 }
        $TensorSplit = ""
        if ([string]::IsNullOrWhiteSpace($CudaDevices)) { $CudaDevices = "0" }
        $modeLabel = "single-GPU (GPU0)"
    }
    "legacy2070" {
        if ($CpuMoe -lt 0) { $CpuMoe = 99 }
        $TensorSplit = ""
        if ([string]::IsNullOrWhiteSpace($CudaDevices)) { $CudaDevices = "0" }
        $modeLabel = "legacy hybrid CPU/GPU (2070-class)"
    }
}

if (-not [string]::IsNullOrWhiteSpace($CudaDevices)) {
    $env:CUDA_VISIBLE_DEVICES = $CudaDevices
}

$modelName = [System.IO.Path]::GetFileName($ModelPath)

Write-Host "============================================"
Write-Host "  MIKE - Qwen local brain"
Write-Host "============================================"
Write-Host "  Modelo : $modelName"
Write-Host "  Path   : $ModelPath"
Write-Host "  Perfil : $Profile — $modeLabel"
Write-Host "  CUDA   : CUDA_VISIBLE_DEVICES=$($env:CUDA_VISIBLE_DEVICES)"
Write-Host "  Config : ngl=$GpuLayers n_cpu_moe=$CpuMoe threads=$Threads ctx=$CtxSize"
if (-not [string]::IsNullOrWhiteSpace($TensorSplit)) {
    Write-Host "  Split  : tensor-split=$TensorSplit"
}
Write-Host "  FA/KV  : flash-attn=on cache=q8_0"
Write-Host "  API    : http://127.0.0.1:${Port}/v1"
Write-Host "============================================"

$argList = @(
    "--model", $ModelPath,
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--n-gpu-layers", "$GpuLayers",
    "--n-cpu-moe", "$CpuMoe",
    "--ctx-size", "$CtxSize",
    "--threads", "$Threads",
    "--batch-size", "$BatchSize",
    "--ubatch-size", "$UbatchSize",
    "--flash-attn", "on",
    "--cache-type-k", "q8_0",
    "--cache-type-v", "q8_0",
    "--parallel", "1",
    "--cont-batching",
    "--no-warmup",
    "--no-mmap"
)
if (-not [string]::IsNullOrWhiteSpace($TensorSplit)) {
    $argList += @("--tensor-split", $TensorSplit)
}

& $ServerExe @argList
