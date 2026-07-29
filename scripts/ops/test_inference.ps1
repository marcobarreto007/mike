# =============================================================================
# MIKE - Teste de Inferencia Otimizada GPU Puro
# =============================================================================
# Objetivo: Maxima velocidade (min 20 tok/s) com Qwen3.6-35B-A3B
# GPUs: RTX 2070 8GB + P106-100 6GB (14GB VRAM, PCIe x16)
# =============================================================================

param(
    [string]$ModelPath = "$PSScriptRoot\..\..\llm_cache\Qwen3.6-35B-A3B-UD-Q2_K_XL.gguf",
    [int]$Threads = 8,
    [int]$GpuLayers = 999,
    [string]$TensorSplit = "8,6",
    [int]$CtxSize = 16384,
    [int]$BatchSize = 1024,
    [int]$UbatchSize = 256,
    [string]$Prompt = "Explique o que e um modelo MoE (Mixture of Experts) em 3 frases."
)

$BinDir = "$PSScriptRoot\..\..\llama.cpp\build\bin"
$LlamaCli = Join-Path $BinDir "llama-cli.exe"

$env:PATH = "$BinDir;$env:PATH"

Write-Output "============================================"
Write-Output "  MIKE - Inferencia Otimizada (GPU Puro)"
Write-Output "============================================"
Write-Output "  Modelo  : Qwen3.6-35B-A3B MoE"
Write-Output "  Quant   : UD-Q2_K_XL (12.3 GB)"
Write-Output "  GPUs    : RTX 2070 (8GB) + P106 (6GB)"
Write-Output "  Split   : layer mode, tensor_split=$TensorSplit"
Write-Output "  Contexto: $CtxSize tokens"
Write-Output "  Batch   : $BatchSize / ubatch=$UbatchSize"
Write-Output "============================================"

# Parametros otimizados para maxima velocidade:
# --flash-attn      : Atencao otimizada (menos VRAM, mais rapido)
# --split-mode layer: Obrigatorio pra MoE (unico compativel)
# --tensor-split    : Proporcional ao VRAM de cada GPU
# --main-gpu 0      : RTX 2070 como GPU principal
# --cache-type-k/q  : KV cache quantizado 4-bit (economiza VRAM pra contexto)
# --cont-batching   : Batching continuo (melhor throughput)
# --mlock           : Trava memoria RAM (evita swap)
# --threads         : Threads de CPU (so pra partes nao-GPU)
# --batch-size      : Batch grande = prompt processing mais rapido
# --ubatch-size     : Micro-batch menor = decode mais rapido
# --temp            : Temperatura 0 pra teste deterministico
# --repeat-penalty  : Penalidade leve pra evitar repeticoes

$Args = @(
    "--model", $ModelPath,
    "--n-gpu-layers", $GpuLayers,
    "--split-mode", "layer",
    "--tensor-split", $TensorSplit,
    "--main-gpu", "0",
    "--ctx-size", $CtxSize,
    "--threads", $Threads,
    "--batch-size", $BatchSize,
    "--ubatch-size", $UbatchSize,
    "--cache-type-k", "q4_0",
    "--cache-type-v", "q4_0",
    "--flash-attn",
    "--cont-batching",
    "--mlock",
    "--temp", "0.0",
    "--repeat-penalty", "1.05",
    "--no-display-prompt",
    "--prompt", $Prompt
)

Write-Output ""
Write-Output "  Executando teste de inferencia..."
Write-Output ""

$sw = [System.Diagnostics.Stopwatch]::StartNew()
& $LlamaCli $Args 2>&1
$sw.Stop()

Write-Output ""
Write-Output "============================================"
Write-Output "  Tempo total: $([math]::Round($sw.Elapsed.TotalSeconds,1))s"
Write-Output "============================================"
