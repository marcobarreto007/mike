# =============================================================================
# MIKE - llama.cpp Server Launcher (GPU Puro, Zero CPU, Zero Nuvem)
# =============================================================================
# GPUs atuais: RTX 5060 Ti 16GB + RTX 3060 12GB (ver start_qwen36_server.ps1)
# Legado: RTX 2070 8GB / P106
# Modelo: Qwen3.6-35B-A3B MoE (35B total, ~3B ativos por token)
# Arquitetura: Pipeline paralelo (layer split) - unico modo compativel com MoE
# =============================================================================

param(
    [string]$ModelPath = "$PSScriptRoot\..\..\llm_cache\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ3_M.gguf",
    [int]$Port = 8081,
    [int]$ContextSize = 4096,
    [string]$TensorSplit = "6.2,4.2",
    [int]$GpuLayers = 30,
    [int]$Threads = 8,
    [int]$BatchSize = 2048,
    [int]$UbatchSize = 512
)

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$LlamaDir = Join-Path $ProjectRoot "llama.cpp\build\bin"
$ServerExe = Get-ChildItem -LiteralPath $LlamaDir -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1

if (-not $ServerExe) {
    Write-Error "llama-server.exe nao encontrado em $LlamaDir"
    Write-Output "Execute primeiro: scripts/ops/download_llama_binary.ps1"
    exit 1
}

if (-not (Test-Path $ModelPath)) {
    Write-Error "Modelo GGUF nao encontrado: $ModelPath"
    Write-Output "Execute primeiro: scripts/ops/download_model.ps1"
    exit 1
}

Write-Output "============================================"
Write-Output "  MIKE - llama-server (GPU Puro)"
Write-Output "============================================"
Write-Output "  Binario : $($ServerExe.FullName)"
Write-Output "  Modelo  : $ModelPath"
Write-Output "  Porta   : $Port"
Write-Output "  GPUs    : RTX 2070 (8GB) + P106-100 (6GB)"
Write-Output "  Split   : $TensorSplit (proporcional)"
Write-Output "  Contexto: $ContextSize tokens"
Write-Output "  Modo    : layer pipeline (MoE compat)"
Write-Output "  API     : http://127.0.0.1:${Port}/v1"
Write-Output "============================================"

& $ServerExe.FullName `
    --model $ModelPath `
    --host 127.0.0.1 `
    --port $Port `
    --n-gpu-layers $GpuLayers `
    --split-mode layer `
    --tensor-split $TensorSplit `
    --main-gpu 0 `
    --ctx-size $ContextSize `
    --threads $Threads `
    --batch-size $BatchSize `
    --ubatch-size $UbatchSize `
    --flash-attn off `
    --cache-type-k f16 `
    --cache-type-v f16 `
    --parallel 1 `
    --cont-batching `
    --no-warmup `
    --mlock `
    --no-mmap
