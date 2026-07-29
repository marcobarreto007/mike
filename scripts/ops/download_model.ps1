# =============================================================================
# MIKE - Download Qwen3.6-35B-A3B GGUF (GPU Puro)
# =============================================================================
# Baixa o modelo MoE quantizado direto do HuggingFace
# Sem dependencia de git-lfs, usa HTTP direto

param(
    [string]$Quant = "UD-Q2_K_XL",
    [string]$OutDir = "$PSScriptRoot\..\..\llm_cache",
    [switch]$ListOnly
)

$Repo = "unsloth/Qwen3.6-35B-A3B-MTP-GGUF"

# Todas as quants disponiveis (menor -> maior)
$Quants = @{
    "UD-IQ2_XXS"  = @{ Size = "10.76 GB"; VRAM = "12 GB" }
    "UD-IQ2_M"    = @{ Size = "11.52 GB"; VRAM = "14 GB" }
    "UD-Q2_K_XL"  = @{ Size = "12.29 GB"; VRAM = "14 GB" }
    "UD-IQ3_XXS"  = @{ Size = "13.21 GB"; VRAM = "16 GB" }
    "UD-Q3_K_S"   = @{ Size = "15.36 GB"; VRAM = "18 GB" }
    "UD-Q3_K_M"   = @{ Size = "16.60 GB"; VRAM = "20 GB" }
    "UD-IQ4_XS"   = @{ Size = "17.73 GB"; VRAM = "20 GB" }
}

if ($ListOnly) {
    Write-Output "============================================"
    Write-Output "  Qwen3.6-35B-A3B Quants Disponiveis"
    Write-Output "  Repo: $Repo"
    Write-Output "============================================"
    Write-Output ""
    Write-Output "  SUAS GPUs: 14 GB VRAM (RTX 2070 8GB + P106 6GB)"
    Write-Output "  Modo: GPU PURO (zero CPU offload)"
    Write-Output ""
    foreach ($q in $Quants.GetEnumerator() | Sort-Object { [double]($_.Value.Size -replace '[^0-9.]','') }) {
        $fits = if ([double]($_.Value.VRAM -replace '[^0-9.]','') -le 14) { "CABE" } else { "NAO CABE" }
        $marker = if ($fits -eq "CABE") { "  <= " } else { "  X  " }
        Write-Output "$marker $($q.Key.PadRight(16)) $($q.Value.Size.PadRight(10)) $($q.Value.VRAM.PadRight(10)) $fits"
    }
    Write-Output ""
    Write-Output "  Recomendado: UD-Q2_K_XL (melhor qualidade que cabe em 14GB)"
    Write-Output "  Alternativa: UD-IQ2_M   (mais leve, mais rapida)"
    exit 0
}

$FileName = "Qwen3.6-35B-A3B-$Quant.gguf"
$OutPath = Join-Path $OutDir $FileName

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
}

Write-Output "============================================"
Write-Output "  Baixando Qwen3.6-35B-A3B MoE"
Write-Output "============================================"
Write-Output "  Quant    : $Quant"
Write-Output "  Arquivo  : $FileName"
Write-Output "  Tamanho  : $($Quants[$Quant].Size)"
Write-Output "  Destino  : $OutPath"
Write-Output "============================================"

# Usa huggingface_hub (ja instalado no .venv)
$Script = @"
import sys
from pathlib import Path
from huggingface_hub import hf_hub_download

print(f"Conectando ao HuggingFace...")
print(f"Repo: $Repo")
print(f"File: $FileName")

path = hf_hub_download(
    repo_id="$Repo",
    filename="$FileName",
    local_dir=r"$OutDir",
    resume=True,
)
print(f"")
print(f"DOWNLOAD CONCLUIDO!")
print(f"Arquivo: {path}")
print(f"Tamanho: {Path(path).stat().st_size / (1024**3):.2f} GB")
"@

$TempScript = Join-Path $env:TEMP "mike_download_model.py"
$Script | Set-Content -Path $TempScript -Encoding UTF8

& "$PSScriptRoot\..\..\.venv\Scripts\python.exe" $TempScript
Remove-Item $TempScript -ErrorAction SilentlyContinue
