# Aumenta pagefile no D: para 32 GB (necessario para carregar FLUX)
# Rode como Administrador

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
Write-Host "=== Configurando Pagefile no D: ===" -ForegroundColor Cyan

# Desabilita pagefile no C: primeiro
Write-Host "[1/3] Desabilitando pagefile no C:..."
$cDrive = Get-CimInstance -ClassName Win32_PageFileSetting -Filter "Name='C:\\pagefile.sys'"
if ($cDrive) {
    Remove-CimInstance -InputObject $cDrive
    Write-Host "  C: pagefile removido"
} else {
    Write-Host "  C: pagefile nao encontrado"
}

# Cria pagefile no D: (32 GB)
Write-Host "[2/3] Criando pagefile de 32 GB no D:..."
$pf = Get-CimInstance -ClassName Win32_PageFileSetting -Filter "Name='D:\\pagefile.sys'"
if ($pf) {
    $pf.InitialSize = 32768
    $pf.MaximumSize = 65536
    Set-CimInstance -InputObject $pf
    Write-Host "  D: pagefile atualizado para 32-64 GB"
} else {
    Set-CimInstance -Namespace root/cimv2 -ClassName Win32_PageFileSetting -Property @{
        Name = "D:\\pagefile.sys"
        InitialSize = 32768
        MaximumSize = 65536
    }
    Write-Host "  D: pagefile criado"
}

Write-Host "[3/3] Verificando..."
Get-CimInstance -ClassName Win32_PageFileSetting | Format-Table Name, InitialSize, MaximumSize

Write-Host ""
Write-Host "=== PRONTO ===" -ForegroundColor Green
Write-Host "REINICIE o PC para aplicar as mudancas."
Write-Host "  shutdown /r /t 0"
