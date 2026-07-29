# Instalacao limpa NVIDIA 566.36 + P106-100
# Rode como Administrador
# Uso: .\install_nv566_clean.ps1

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$DriverDir = "C:\NVIDIA\566.36_extracted"

Write-Host "=== LIMPEZA E INSTALACAO NVIDIA 566.36 ===" -ForegroundColor Cyan

# 1. Limpar drivers antigos do Driver Store
Write-Host "`n[1/4] Removendo drivers NVIDIA antigos do Driver Store..." -ForegroundColor Yellow
$oldDrivers = pnputil /enum-drivers 2>&1 | Select-String -Pattern "NVIDIA" -Context 0,3
$publishedNames = @()
foreach ($line in $oldDrivers) {
    if ($line -match "Published Name:\s+(.+\.inf)") {
        $publishedNames += $matches[1]
    }
}
foreach ($name in $publishedNames) {
    Write-Host "  Removendo: $name"
    pnputil /delete-driver $name /uninstall /force 2>&1 | Out-Null
}

# 2. Desinstalar pacotes NVIDIA antigos via programas instalados
Write-Host "`n[2/4] Desinstalando pacotes NVIDIA antigos..." -ForegroundColor Yellow
$nvPackages = @(
    "NVIDIA Graphics Driver 596.36",
    "NVIDIA Graphics Driver 432.00"
)
foreach ($pkg in $nvPackages) {
    $entry = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*" | Where-Object { $_.DisplayName -eq $pkg } | Select-Object -First 1
    if ($entry -and $entry.UninstallString) {
        Write-Host "  Desinstalando: $pkg"
        $uninst = $entry.UninstallString
        if ($uninst -match '"(.*)"') {
            Start-Process -FilePath $matches[1] -ArgumentList "/S" -Wait -NoNewWindow
        }
    }
}

# 3. Matar processos NVIDIA residuais
Write-Host "`n[3/4] Parando processos NVIDIA..." -ForegroundColor Yellow
$nvProcs = Get-Process -Name "nv*", "*nvidia*" -ErrorAction SilentlyContinue
foreach ($p in $nvProcs) {
    Write-Host "  Parando: $($p.Name)"
    Stop-Process -Name $p.Name -Force -ErrorAction SilentlyContinue
}

# 4. Instalar 566.36 com clean install
Write-Host "`n[4/4] Instalando NVIDIA 566.36 (suporte nativo a P106-100)..." -ForegroundColor Green
$setupExe = Join-Path $DriverDir "setup.exe"
if (-not (Test-Path $setupExe)) {
    Write-Error "setup.exe nao encontrado em $DriverDir"
    exit 1
}

Write-Host "  Flags: -clean -noeula -nofinish -nosplash -passive"
$proc = Start-Process -FilePath $setupExe -ArgumentList "-clean -noeula -nofinish -nosplash -passive" -Wait -PassThru -NoNewWindow

Write-Host ""
if ($proc.ExitCode -eq 0) {
    Write-Host "=== INSTALACAO CONCLUIDA ===" -ForegroundColor Green
    Write-Host "Reinicie o PC para finalizar: shutdown /r /t 0"
} elseif ($proc.ExitCode -eq 1) {
    Write-Host "=== INSTALACAO CONCLUIDA (reboot pendente) ===" -ForegroundColor Yellow
    Write-Host "Reinicie o PC agora: shutdown /r /t 0"
} else {
    Write-Host "=== ATENCAO: ExitCode $($proc.ExitCode) ===" -ForegroundColor Red
    Write-Host "Pode ser necessario reiniciar e rodar novamente."
}
