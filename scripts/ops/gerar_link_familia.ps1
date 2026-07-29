<#
.SYNOPSIS
    Gera links magicos (sem senha) para cada membro da familia acessar o Mike.
    O link e enviado pelo WhatsApp e abre o perfil da pessoa automaticamente.

.COMO USAR
    .\gerar_link_familia.ps1                     # gera links para todos
    .\gerar_link_familia.ps1 -Perfil anapaula    # gera so para a Ana Paula
    .\gerar_link_familia.ps1 -Perfil alice -Dias 30  # validade de 30 dias
#>
param(
    [string]$Perfil = "",    # deixe vazio para gerar para todos
    [int]$Dias = 90,         # validade do link em dias (padrao: 90 dias)
    [int]$Port = 8083
)

$baseUrl   = "http://127.0.0.1:$Port"
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$dataDir = Join-Path $projectRoot "data"
$tunnelFile = Join-Path $dataDir "tunnel_url_atual.txt"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

# Usa o link publico do tunnel se disponivel
if (Test-Path $tunnelFile) {
    $publicBase = (Get-Content $tunnelFile -Raw).Trim().Split("`n")[0].Trim()
} else {
    $publicBase = $baseUrl
}

# Perfis da familia
$todosPerfis = @{
    "marco"     = @{ nome = "Marco";     whatsapp = "" }
    "anapaula"  = @{ nome = "Ana Paula"; whatsapp = "" }
    "raphael"   = @{ nome = "Raphael";   whatsapp = "" }
    "alice"     = @{ nome = "Alice";     whatsapp = "" }
    "matheus"   = @{ nome = "Matheus";   whatsapp = "" }
    "visitante" = @{ nome = "Visitante"; whatsapp = "" }
}

if ($Perfil) {
    $perfilKey = $Perfil.Trim().ToLower()
    if (-not $todosPerfis.ContainsKey($perfilKey)) {
        Write-Warning "Perfil '$Perfil' nao encontrado. Opcoes: $($todosPerfis.Keys -join ', ')"
        exit 1
    }
    $perfilParaGerar = @{ $perfilKey = $todosPerfis[$perfilKey] }
} else {
    $perfilParaGerar = $todosPerfis
}

# Verifica se o Mike esta rodando
$health = $null
try {
    $health = Invoke-RestMethod -Uri "$baseUrl/health" -TimeoutSec 5 -ErrorAction Stop
} catch {
    Write-Warning "Mike nao esta respondendo em $baseUrl. Inicie com start_mike.ps1"
    exit 1
}

Clear-Host
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  MIKE - Links Magicos para a Familia"           -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Validade: $Dias dias | Base: $publicBase"       -ForegroundColor DarkGray
Write-Host ""

$resultados = @()

foreach ($key in $perfilParaGerar.Keys) {
    $info = $perfilParaGerar[$key]
    Write-Host "Gerando link para $($info.nome)..." -ForegroundColor DarkGray

    try {
        $resp = Invoke-RestMethod `
            -Uri "$baseUrl/v1/auth/magic/generate" `
            -Method POST `
            -ContentType "application/json" `
            -Body (@{ profile = $key; ttl_days = $Dias } | ConvertTo-Json) `
            -ErrorAction Stop
    } catch {
        Write-Warning "Erro ao gerar link para $($info.nome): $_"
        continue
    }

    $magicUrl = $resp.magic_url
    $qrUrl    = $resp.qr_image_url

    # Exibe resultado
    Write-Host ""
    Write-Host "  $($info.nome.ToUpper())" -ForegroundColor Green
    Write-Host "  Link: $magicUrl"         -ForegroundColor Yellow
    Write-Host "  QR:   $qrUrl"            -ForegroundColor DarkGray

    $resultados += [PSCustomObject]@{
        Perfil   = $info.nome
        MagicUrl = $magicUrl
        QrUrl    = $qrUrl
    }
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan

# Salva todos os links em arquivo
$linksFile = Join-Path $dataDir "links_familia.txt"
$linhas = @("Links Magicos - Mike", "Gerados em: $(Get-Date -Format 'dd/MM/yyyy HH:mm')", "Validade: $Dias dias", "")
foreach ($r in $resultados) {
    $linhas += "[$($r.Perfil)]"
    $linhas += "Link: $($r.MagicUrl)"
    $linhas += "QR:   $($r.QrUrl)"
    $linhas += ""
}
$linhas | Set-Content $linksFile -Encoding UTF8
Write-Host "  Links salvos em: links_familia.txt" -ForegroundColor DarkGray
Write-Host ""

# Menu interativo
if ($resultados.Count -eq 0) {
    Write-Warning "Nenhum link foi gerado."
    exit 1
}

Write-Host "O que deseja fazer?" -ForegroundColor White
Write-Host "  [1] Abrir QR code no navegador (para escanear com o celular)"
Write-Host "  [2] Enviar pelo WhatsApp Web"
Write-Host "  [3] Copiar link para o clipboard"
Write-Host "  [4] Fazer tudo automaticamente"
Write-Host "  [Enter] Sair"
Write-Host ""

$escolha = Read-Host "Escolha"

switch ($escolha.Trim()) {
    "1" {
        foreach ($r in $resultados) {
            Write-Host "Abrindo QR de $($r.Perfil)..." -ForegroundColor DarkGray
            Start-Process $r.QrUrl
            Start-Sleep -Milliseconds 800
        }
    }
    "2" {
        foreach ($r in $resultados) {
            $msg = [Uri]::EscapeDataString(
                "Oi $($r.Perfil)! Aqui esta seu acesso pessoal ao Mike.`nSo clicar no link abaixo - nao precisa de senha!`n`n$($r.MagicUrl)`n`nSalva este link nos Favoritos do celular!")
            Write-Host "Abrindo WhatsApp para $($r.Perfil)..." -ForegroundColor DarkGray
            Start-Process "https://wa.me/?text=$msg"
            Start-Sleep -Milliseconds 1000
        }
    }
    "3" {
        $tudo = $resultados | ForEach-Object { "$($_.Perfil): $($_.MagicUrl)" }
        $tudo -join "`n" | Set-Clipboard
        Write-Host "Links copiados para o clipboard!" -ForegroundColor Green
    }
    "4" {
        foreach ($r in $resultados) {
            # Abre QR
            Start-Process $r.QrUrl
            # Abre WhatsApp
            $msg = [Uri]::EscapeDataString(
                "Oi $($r.Perfil)! Aqui esta seu acesso pessoal ao Mike.`nSo clicar no link abaixo - nao precisa de senha!`n`n$($r.MagicUrl)`n`nSalva este link nos Favoritos do celular!")
            Start-Process "https://wa.me/?text=$msg"
            Start-Sleep -Milliseconds 1200
        }
        Write-Host "Tudo aberto!" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "Pronto! Os links valem por $Dias dias." -ForegroundColor Green
Write-Host "Para revogar um link: DELETE $baseUrl/v1/auth/magic/<token>" -ForegroundColor DarkGray
Write-Host ""
