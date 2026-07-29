param(
    [string]$ModelPath,
    [int]$Port = 8081,
    [int]$CtxSize = 16384,
    [int]$GpuLayers = 999,
    [int]$CpuMoe = 20,
    [int]$Threads = 8,
    [int]$BatchSize = 1024,
    [int]$UbatchSize = 512
)

if (-not $ModelPath) {
    $ModelPath = Join-Path $PSScriptRoot "..\..\llm_cache\gpt-oss-20b-abliterated\OpenAI-20B-NEO-Uncensored2-IQ4_NL.gguf"
}

$LlamaDir = Join-Path $PSScriptRoot "..\..\llama.cpp\build\bin"
$ServerExe = Join-Path $LlamaDir "llama-server.exe"

$env:PATH = "$LlamaDir;$env:PATH"

Write-Host "============================================"
Write-Host "  MIKE - gpt-oss-20b Abliterated Server"
Write-Host "============================================"
Write-Host "  Modelo : gpt-oss-20b (21B MoE, 3.6B ativos)"
Write-Host "  Quant  : IQ4_NL (~12 GB)"
Write-Host "  GPU    : RTX 2070 (8GB) + 40GB RAM"
Write-Host "  Port   : $Port"
Write-Host "============================================"

& $ServerExe --model $ModelPath --host 127.0.0.1 --port $Port --n-gpu-layers $GpuLayers --n-cpu-moe $CpuMoe --ctx-size $CtxSize --threads $Threads --batch-size $BatchSize --ubatch-size $UbatchSize --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --parallel 1 --cont-batching --no-warmup --no-mmap
