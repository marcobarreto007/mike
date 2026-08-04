# =============================================================================
# MIKE - Download Qwen3.6-35B-A3B GGUF
# =============================================================================
# Baixa o modelo MoE quantizado direto do HuggingFace
# Sem dependencia de git-lfs, usa HTTP direto
#
# Hardware atual (2026-08-03):
#   GPU0 RTX 5060 Ti 16GB + GPU1 RTX 3060 12GB + 64GB RAM
# Hardware legado:
#   RTX 2070 8GB hybrid

param(
    [string]$Quant = "UD-Q3_K_M",
    [string]$OutDir = "$PSScriptRoot\..\..\llm_cache",
    [switch]$ListOnly
)

$ErrorActionPreference = "Stop"
$Repo = "unsloth/Qwen3.6-35B-A3B-MTP-GGUF"

# Todas as quants disponiveis (menor -> maior)
# VRAM = estimativa de pe para caber com KV/contexto razoavel
$Quants = [ordered]@{
    "UD-IQ2_XXS" = @{ Size = "10.76 GB"; VRAM = "12 GB" }
    "UD-IQ2_M"   = @{ Size = "11.52 GB"; VRAM = "14 GB" }
    "UD-Q2_K_XL" = @{ Size = "12.29 GB"; VRAM = "14 GB" }
    "UD-IQ3_XXS" = @{ Size = "13.21 GB"; VRAM = "16 GB" }
    "UD-Q3_K_S"  = @{ Size = "15.36 GB"; VRAM = "18 GB" }
    "UD-Q3_K_M"  = @{ Size = "16.60 GB"; VRAM = "20 GB" }
    "UD-IQ4_XS"  = @{ Size = "17.73 GB"; VRAM = "22 GB" }
}

# Detecta VRAM combinada se nvidia-smi existir
$totalVramGb = 28.0
try {
    $mem = & nvidia-smi --query-gpu=memory.total --format=csv, noheader, nounits 2>$null
    $sumMb = 0
    foreach ($m in @($mem)) {
        if ($m -match "^\s*(\d+)") { $sumMb += [int]$Matches[1] }
    }
    if ($sumMb -gt 0) { $totalVramGb = [math]::Round($sumMb / 1024.0, 1) }
}
catch {}

if ($ListOnly) {
    Write-Output "============================================"
    Write-Output "  Qwen3.6-35B-A3B Quants Disponiveis"
    Write-Output "  Repo: $Repo"
    Write-Output "============================================"
    Write-Output ""
    Write-Output "  VRAM combinada detectada: ~$totalVramGb GB"
    Write-Output "  Perfil alvo: dual-GPU (5060 Ti 16GB + 3060 12GB) ou single GPU0"
    Write-Output ""
    foreach ($key in $Quants.Keys) {
        $info = $Quants[$key]
        $need = [double]($info.VRAM -replace '[^0-9.]', '')
        $fits = if ($need -le $totalVramGb) { "CABE" } else { "APERTADO/NAO CABE" }
        $marker = if ($fits -eq "CABE") { "  <= " } else { "  X  " }
        Write-Output ("{0}{1} {2} {3} {4}" -f $marker, $key.PadRight(16), $info.Size.PadRight(10), $info.VRAM.PadRight(10), $fits)
    }
    Write-Output ""
    Write-Output "  Recomendado (dual 16+12): UD-Q3_K_M"
    Write-Output "  Qualidade validada MIKE:  UD-IQ4_XS (agora com bem menos CPU offload)"
    Write-Output "  Margem de contexto/KV:   UD-Q2_K_XL so na 5060 Ti"
    Write-Output "  Qwen3.8-Max 2.4T:        NAO e GGUF desktop - usar API ou esperar 27B"
    exit 0
}

if (-not $Quants.Contains($Quant)) {
    throw "Quant desconhecida: $Quant. Use -ListOnly para ver opcoes."
}

$FileName = "Qwen3.6-35B-A3B-$Quant.gguf"
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
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

$pyLines = @(
    "from pathlib import Path",
    "from huggingface_hub import hf_hub_download",
    "",
    "print('Conectando ao HuggingFace...')",
    "print('Repo: $Repo')",
    "print('File: $FileName')",
    "",
    "path = hf_hub_download(",
    "    repo_id='$Repo',",
    "    filename='$FileName',",
    "    local_dir=r'$OutDir',",
    ")",
    "print('')",
    "print('DOWNLOAD CONCLUIDO!')",
    "print(f'Arquivo: {path}')",
    "print(f'Tamanho: {Path(path).stat().st_size / (1024**3):.2f} GB')"
)

$TempScript = Join-Path $env:TEMP "mike_download_model.py"
Set-Content -Path $TempScript -Value ($pyLines -join "`n") -Encoding UTF8

$VenvPy = Join-Path $PSScriptRoot "..\..\.venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPy) { (Resolve-Path $VenvPy).Path } else { "python" }

try {
    & $Python $TempScript
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Remove-Item $TempScript -ErrorAction SilentlyContinue
}
