[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$ServiceName = "MikeServer",
    [string]$QwenServiceName = "MikeQwen36",
    [int]$MikePort = 8083,
    [int]$QwenPort = 8081,
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Message)
    Write-Host "[Mike Service Setup] $Message" -ForegroundColor Cyan
}

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\', '/')
$startMikeScript = Join-Path $ProjectRoot "scripts\ops\start_mike.ps1"
$startQwenScript = Join-Path $ProjectRoot "scripts\ops\start_qwen36_server.ps1"
$qwenModel = Join-Path $ProjectRoot "llm_cache\Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"
$logsDir = Join-Path $ProjectRoot "logs"

foreach ($requiredPath in @($startMikeScript, $startQwenScript, $qwenModel)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required service asset not found: $requiredPath"
    }
}

if ($WhatIfPreference) {
    Write-Log "WhatIf validation for workspace $ProjectRoot"
    $PSCmdlet.ShouldProcess($QwenServiceName, "Install Qwen 3.6 dependency service") | Out-Null
    $PSCmdlet.ShouldProcess($ServiceName, "Install Mike service depending on $QwenServiceName") | Out-Null
    return
}

$nssmCommand = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssmCommand) {
    throw "nssm was not found in PATH. Install NSSM before installing Windows services."
}
$nssmPath = $nssmCommand.Source

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Log "Requesting administrator privileges..."
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-ProjectRoot", "`"$ProjectRoot`"",
        "-ServiceName", "`"$ServiceName`"",
        "-QwenServiceName", "`"$QwenServiceName`"",
        "-MikePort", "$MikePort",
        "-QwenPort", "$QwenPort"
    )
    if ($StartNow) { $arguments += "-StartNow" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList ($arguments -join " ") | Out-Null
    return
}

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Prepare-NssmService {
    param([Parameter(Mandatory = $true)][string]$Name)
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $existing) { return $false }

    $serviceRecord = Get-CimInstance Win32_Service -Filter "Name = '$Name'" -ErrorAction Stop
    if (-not ([string]$serviceRecord.PathName -match '(?i)(^|[\\/"''])nssm(?:\.exe)?([\\/"'']|\s|$)')) {
        throw "Service name collision: $Name exists but is not managed by NSSM. Refusing to replace it. PathName='$($serviceRecord.PathName)'"
    }

    Write-Log "Updating existing NSSM service $Name in place..."
    if ($existing.Status -ne "Stopped") {
        Stop-Service -Name $Name -Force -ErrorAction Stop
        $existing.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
    }
    return $true
}

function Assert-NssmSuccess {
    param([Parameter(Mandatory = $true)][string]$Action)
    if ($LASTEXITCODE -ne 0) {
        throw "NSSM failed to $Action (exit $LASTEXITCODE)."
    }
}

function Invoke-Nssm {
    param(
        [Parameter(Mandatory = $true)][string]$Action,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $nssmPath @Arguments | Out-Null
    Assert-NssmSuccess -Action $Action
}

if (-not $PSCmdlet.ShouldProcess(
    "$QwenServiceName and $ServiceName",
    "Install two automatic Windows services in $ProjectRoot"
)) {
    return
}

# Mike is prepared first because it depends on Qwen.
$mikeExists = Prepare-NssmService -Name $ServiceName
$qwenExists = Prepare-NssmService -Name $QwenServiceName

$qwenArguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", "`"$startQwenScript`"",
    "-ModelPath", "`"$qwenModel`"",
    "-Port", "$QwenPort"
) -join " "

Write-Log "Installing $QwenServiceName (Qwen 3.6 on port $QwenPort)..."
if (-not $qwenExists) {
    Invoke-Nssm -Action "install $QwenServiceName" -Arguments @(
        "install", $QwenServiceName, "powershell.exe", $qwenArguments
    )
} else {
    Invoke-Nssm -Action "set $QwenServiceName application" -Arguments @(
        "set", $QwenServiceName, "Application", "powershell.exe"
    )
    Invoke-Nssm -Action "set $QwenServiceName arguments" -Arguments @(
        "set", $QwenServiceName, "AppParameters", $qwenArguments
    )
}
Invoke-Nssm -Action "set $QwenServiceName AppDirectory" -Arguments @(
    "set", $QwenServiceName, "AppDirectory", $ProjectRoot
)
Invoke-Nssm -Action "set $QwenServiceName AppStdout" -Arguments @(
    "set", $QwenServiceName, "AppStdout", (Join-Path $logsDir "qwen36_service_stdout.log")
)
Invoke-Nssm -Action "set $QwenServiceName AppStderr" -Arguments @(
    "set", $QwenServiceName, "AppStderr", (Join-Path $logsDir "qwen36_service_stderr.log")
)
Invoke-Nssm -Action "set $QwenServiceName restart policy" -Arguments @(
    "set", $QwenServiceName, "AppExit", "Default", "Restart"
)
Invoke-Nssm -Action "set $QwenServiceName restart delay" -Arguments @(
    "set", $QwenServiceName, "AppRestartDelay", "5000"
)
Invoke-Nssm -Action "set $QwenServiceName automatic start" -Arguments @(
    "set", $QwenServiceName, "Start", "SERVICE_AUTO_START"
)

$mikeArguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", "`"$startMikeScript`"",
    "-Port", "$MikePort",
    "-Foreground",
    "-ForceRestart",
    "-ServiceMode",
    "-SkipTunnel"
) -join " "

Write-Log "Installing $ServiceName (Mike on port $MikePort)..."
if (-not $mikeExists) {
    Invoke-Nssm -Action "install $ServiceName" -Arguments @(
        "install", $ServiceName, "powershell.exe", $mikeArguments
    )
} else {
    Invoke-Nssm -Action "set $ServiceName application" -Arguments @(
        "set", $ServiceName, "Application", "powershell.exe"
    )
    Invoke-Nssm -Action "set $ServiceName arguments" -Arguments @(
        "set", $ServiceName, "AppParameters", $mikeArguments
    )
}
Invoke-Nssm -Action "set $ServiceName AppDirectory" -Arguments @(
    "set", $ServiceName, "AppDirectory", $ProjectRoot
)
Invoke-Nssm -Action "set $ServiceName AppStdout" -Arguments @(
    "set", $ServiceName, "AppStdout", (Join-Path $logsDir "mike_service_stdout.log")
)
Invoke-Nssm -Action "set $ServiceName AppStderr" -Arguments @(
    "set", $ServiceName, "AppStderr", (Join-Path $logsDir "mike_service_stderr.log")
)
Invoke-Nssm -Action "set $ServiceName restart policy" -Arguments @(
    "set", $ServiceName, "AppExit", "Default", "Restart"
)
Invoke-Nssm -Action "set $ServiceName restart delay" -Arguments @(
    "set", $ServiceName, "AppRestartDelay", "5000"
)
Invoke-Nssm -Action "set $ServiceName automatic start" -Arguments @(
    "set", $ServiceName, "Start", "SERVICE_AUTO_START"
)
Invoke-Nssm -Action "set $ServiceName dependency" -Arguments @(
    "set", $ServiceName, "DependOnService", $QwenServiceName
)

if ($StartNow) {
    $qwenListener = Get-NetTCPConnection -State Listen -LocalPort $QwenPort -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $mikeListener = Get-NetTCPConnection -State Listen -LocalPort $MikePort -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($qwenListener -or $mikeListener) {
        throw "Services were installed but not started because port $QwenPort or $MikePort is already occupied. Stop the existing runtimes explicitly, then start $ServiceName."
    }
    Start-Service -Name $QwenServiceName
    Start-Service -Name $ServiceName
}

Write-Host "Services installed successfully:" -ForegroundColor Green
Write-Host "  $QwenServiceName -> Qwen 3.6 on 127.0.0.1:$QwenPort" -ForegroundColor Green
Write-Host "  $ServiceName -> Mike on 127.0.0.1:$MikePort (depends on $QwenServiceName)" -ForegroundColor Green
if (-not $StartNow) {
    Write-Host "Services were not started. Start with: Start-Service $ServiceName" -ForegroundColor Yellow
}
