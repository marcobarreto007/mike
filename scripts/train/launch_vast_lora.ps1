# =============================================================================
# MIKE LoRA Training — Vast.ai Launch Script
# =============================================================================
# RTX 5090 32GB ~$0.058/h | Com $14 credit = ~240 horas de treino
# =============================================================================

param(
    [string]$DatasetPath = "dados_treino.jsonl",
    [string]$BaseModel = "openai/gpt-oss-20b",
    [int]$Epochs = 3,
    [int]$LoraRank = 16,
    [float]$MaxPrice = 0.15
)

$API_KEY = $env:VAST_API_KEY
if ([string]::IsNullOrWhiteSpace($API_KEY)) {
    Write-Error "VAST_API_KEY is required. Set it in the process environment before launching."
    exit 1
}
$VAST_API = "https://cloud.vast.ai/api/v0"

# =============================================================================
# 1. ENCONTRAR MELHOR GPU
# =============================================================================
Write-Host "[1/5] Searching best GPU < $MaxPrice/h..." -ForegroundColor Yellow

$response = curl -s "$VAST_API/bundles/" -H "Authorization: Bearer $API_KEY" | ConvertFrom-Json

$best_gpu = $response `
    | Where-Object { $_.dph_total -lt $MaxPrice -and $_.gpu_ram -ge 24000 } `
    | Sort-Object dph_total `
    | Select-Object -First 1

if (-not $best_gpu) {
    Write-Host "ERROR: No GPU found under $MaxPrice/h. Increase MaxPrice." -ForegroundColor Red
    exit 1
}

Write-Host "  GPU: $($best_gpu.gpu_name) | $([math]::Round($best_gpu.gpu_ram/1024))GB VRAM" -ForegroundColor Green
Write-Host "  Price: $($best_gpu.dph_total)/h | Disk: $($best_gpu.disk_space)GB" -ForegroundColor Green
Write-Host "  Location: $($best_gpu.geolocation)" -ForegroundColor Green

# =============================================================================
# 2. CRIAR INSTÂNCIA
# =============================================================================
Write-Host "[2/5] Creating instance..." -ForegroundColor Yellow

$instance = @{
    client_id = "mike-lora-train"
    image = "pytorch/pytorch:2.6.0-cuda12.9-cudnn9-runtime"
    disk = 100
    extra = ""
    env = @{
        HF_HUB_ENABLE_HF_TRANSFER = "1"
    }
    onstart = @"
#!/bin/bash
set -e
echo "=== MIKE LoRA Training ==="

# Install dependencies
pip install --quiet transformers peft accelerate bitsandbytes datasets \
    trl sentencepiece huggingface_hub[hf_transfer]

# Upload your dataset (or use the one from the instance)
# curl -o dados.jsonl YOUR_DATASET_URL

# Run LoRA training
python train_lora_gptoss.py \
    --dataset dados.jsonl \
    --model $BaseModel \
    --epochs $Epochs \
    --lora_r $LoraRank

# Pack results
tar -czf lora_output.tar.gz lora_output/final_lora/
echo "=== DONE ==="
"@
}

# ... continuação do script de deployment
Write-Host "Script ready. Launch manually or via Vast.ai CLI." -ForegroundColor Cyan
Write-Host "  vastai create instance <bundle_id> --image pytorch/pytorch --disk 100" -ForegroundColor Cyan
