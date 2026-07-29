# =============================================================================
# MIKE — Qwen3.6-35B-A3B IQ4_XS Server (O Modelo Certo)
# RTX 2070 8GB + 40GB RAM | IQ4_XS ~18GB | 128 experts pequenos
# Alvo: 22-27 tok/s comprovado pela comunidade
# =============================================================================

param(
    [string]$ModelPath,
    [int]$Port = 8081,
    [int]$CtxSize = 16384,
    [int]$GpuLayers = 999,
    [int]$CpuMoe = 99,
    [int]$Threads = 4,
    [int]$BatchSize = 1024,
    [int]$UbatchSize = 512
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

if (-not $ModelPath) {
    $ModelPath = Join-Path $ProjectRoot "llm_cache\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"
}
$ModelPath = [System.IO.Path]::GetFullPath($ModelPath)

$LlamaDir = Join-Path $ProjectRoot "llama.cpp\build\bin"
$ServerExe = Join-Path $LlamaDir "llama-server.exe"
if (-not (Test-Path -LiteralPath $ServerExe)) {
    throw "llama-server.exe not found: $ServerExe"
}
if (-not (Test-Path -LiteralPath $ModelPath)) {
    throw "Qwen 3.6 model not found: $ModelPath"
}
$env:PATH = "$LlamaDir;$env:PATH"

Write-Host "============================================"
Write-Host "  MIKE - Qwen3.6-35B-A3B IQ4_XS (22-27 tok/s)"
Write-Host "============================================"
Write-Host "  Modelo : Qwen3.6-35B-A3B (35B MoE, 3B ativos)"
Write-Host "  Quant  : IQ4_XS (~18 GB, 128 experts)"
Write-Host "  GPU    : RTX 2070 (8GB) + 40GB RAM"
Write-Host "  Config : n_cpu_moe=99, FA=on, KV=q8_0"
Write-Host "  API    : http://127.0.0.1:${Port}/v1"
Write-Host "============================================"

& $ServerExe --model $ModelPath --host 127.0.0.1 --port $Port `
  --n-gpu-layers $GpuLayers --n-cpu-moe $CpuMoe `
  --ctx-size $CtxSize --threads $Threads `
  --batch-size $BatchSize --ubatch-size $UbatchSize `
  --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 `
  --parallel 1 --cont-batching --no-warmup --no-mmap
