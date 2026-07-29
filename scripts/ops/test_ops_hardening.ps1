[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$failures = [System.Collections.Generic.List[string]]::new()

function Test-Condition {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        $script:failures.Add($Message)
        Write-Host "[FAIL] $Message" -ForegroundColor Red
    } else {
        Write-Host "[PASS] $Message" -ForegroundColor Green
    }
}

$scripts = @(Get-ChildItem -LiteralPath $scriptDir -Filter "*.ps1" -File)
foreach ($script in $scripts) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $script.FullName,
        [ref]$tokens,
        [ref]$parseErrors
    )
    Test-Condition -Condition ($parseErrors.Count -eq 0) -Message "$($script.Name) parses without errors"
}

$productionScripts = $scripts | Where-Object { $_.Name -ne "test_ops_hardening.ps1" }
$productionText = ($productionScripts | ForEach-Object {
    Get-Content -LiteralPath $_.FullName -Raw
}) -join "`n"

$legacyDrivePattern = '(?i)\bd:\\mike(?:\\|["'']|\s|$)'
$legacyServicePattern = '(?i)\bMike' + 'AI\b'
$legacyPortPattern = '\b80' + '80\b'
Test-Condition -Condition ($productionText -notmatch $legacyDrivePattern) -Message "No fixed legacy D:\mike workspace remains"
Test-Condition -Condition ($productionText -notmatch $legacyServicePattern) -Message "No legacy Mike service name remains"
Test-Condition -Condition ($productionText -notmatch $legacyPortPattern) -Message "No legacy Mike port remains in operational scripts"

$heartbeatPath = Join-Path $projectRoot "core\autonomy\mike_heartbeat.py"
Test-Condition -Condition (Test-Path -LiteralPath $heartbeatPath) -Message "Heartbeat resolves inside the current workspace"

$serviceInstaller = Get-Content -LiteralPath (Join-Path $scriptDir "install_mike_service.ps1") -Raw
Test-Condition -Condition ($serviceInstaller -match 'DependOnService",\s*\$QwenServiceName') -Message "Mike Windows service declares its Qwen dependency"
Test-Condition -Condition ($serviceInstaller -match '\[int\]\$MikePort\s*=\s*8083') -Message "Mike Windows service defaults to port 8083"

$launcherText = Get-Content -LiteralPath (Join-Path $scriptDir "launch_mike.ps1") -Raw
$starterText = Get-Content -LiteralPath (Join-Path $scriptDir "start_mike.ps1") -Raw
$recoveryText = Get-Content -LiteralPath (Join-Path $scriptDir "recover_mike.ps1") -Raw
Test-Condition -Condition ($launcherText -match 'Stop-MikeProcessSafely') -Message "Official launcher uses identity-checked process termination"
Test-Condition -Condition ($starterText -match 'Stop-MikeProcessSafely') -Message "Runtime starter uses identity-checked process termination"
Test-Condition -Condition ($recoveryText -match 'Stop-MikeProcessSafely') -Message "Recovery uses identity-checked process termination"

. (Join-Path $scriptDir "mike_common.ps1")
Test-Condition -Condition (-not (Test-MikeProcessIdentity -ProcessId $PID -ProjectRoot $projectRoot)) -Message "An unrelated PowerShell process is rejected as Mike"
$safeStopRefused = $false
try {
    Stop-MikeProcessSafely -ProcessId $PID -ProjectRoot $projectRoot -Force
} catch {
    $safeStopRefused = $_.Exception.Message -match "Refusing to stop"
}
Test-Condition -Condition $safeStopRefused -Message "Safe stop refuses to terminate an unrelated process"
Test-Condition -Condition ($null -ne (Get-Process -Id $PID -ErrorAction SilentlyContinue)) -Message "Rejected process remains alive"

$listener = Get-PortListener -Port 8083
if ($listener) {
    Test-Condition `
        -Condition (Test-MikeProcessIdentity -ProcessId $listener.OwningProcess -ProjectRoot $projectRoot) `
        -Message "Current port 8083 listener is positively identified as this Mike runtime"
} else {
    Write-Host "[SKIP] Port 8083 is not listening; live Mike identity check skipped." -ForegroundColor Yellow
}

if ($failures.Count -gt 0) {
    Write-Host "`nOperational hardening validation failed ($($failures.Count) failure(s))." -ForegroundColor Red
    exit 1
}

Write-Host "`nOperational hardening validation passed." -ForegroundColor Green
exit 0
