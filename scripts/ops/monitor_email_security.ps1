param(
    [string]$BaseUrl = "http://127.0.0.1:8083",
    [int]$TimeoutSec = 20,
    [int]$WindowMinutes = 60,
    [int]$LogTailLines = 6000,
    [int]$MaxEmailCallsPerHour = 40,
    [int]$MaxEmailErrorsPerHour = 10,
    [switch]$AsJson,
    [switch]$NoHistory
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$checkScript = Join-Path $scriptDir "check_email_access.ps1"
$mainLog = Join-Path $projectRoot "logs\mike.log"
$historyLog = Join-Path $projectRoot "logs\email_security_history.jsonl"

if (-not (Test-Path $checkScript)) {
    Write-Error "check_email_access.ps1 not found at $checkScript"
    exit 2
}

$probeRaw = & $checkScript -BaseUrl $BaseUrl -TimeoutSec $TimeoutSec -AsJson
$probe = $probeRaw | ConvertFrom-Json

$cutoff = (Get-Date).AddMinutes(-1 * $WindowMinutes)
$emailCalls = 0
$emailErrors = 0

if (Test-Path $mainLog) {
    $lines = Get-Content $mainLog -Tail $LogTailLines
    foreach ($line in $lines) {
        if ($line -notmatch '^(?<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+\[(?<level>[A-Z]+)\]\s+(?<msg>.*)$') {
            continue
        }

        $timestamp = [datetime]::ParseExact($Matches.ts, "yyyy-MM-dd HH:mm:ss", [System.Globalization.CultureInfo]::InvariantCulture)
        if ($timestamp -lt $cutoff) {
            continue
        }

        $message = [string]$Matches.msg
        if ($message -match 'Executing local tool:\s+email\.') {
            $emailCalls += 1
        }

        if ($message -match 'Erro SMTP|Erro IMAP|Gmail service unavailable|oauth|token|auth') {
            $emailErrors += 1
        }
    }
}

$gitAvailable = ($null -ne (Get-Command git -ErrorAction SilentlyContinue))
$envRuntimeTracked = $false
$envRuntimeIgnored = $false

if ($gitAvailable) {
    Push-Location $projectRoot
    try {
        cmd /c "git ls-files --error-unmatch config/.env.runtime >nul 2>nul"
        $envRuntimeTracked = ($LASTEXITCODE -eq 0)

        cmd /c "git check-ignore config/.env.runtime >nul 2>nul"
        $envRuntimeIgnored = ($LASTEXITCODE -eq 0)
    }
    finally {
        Pop-Location
    }
}

$anomalies = @()

if (-not [bool]$probe.overall_ok) {
    $anomalies += [pscustomobject]@{
        severity = "high"
        type = "connectivity"
        message = "Email connectivity probe failed"
        details = $probe.warnings
    }
}

if ($emailCalls -gt $MaxEmailCallsPerHour) {
    $anomalies += [pscustomobject]@{
        severity = "high"
        type = "usage_spike"
        message = "Email tool call volume above threshold"
        details = [pscustomobject]@{
            observed = $emailCalls
            threshold = $MaxEmailCallsPerHour
            window_minutes = $WindowMinutes
        }
    }
}

if ($emailErrors -gt $MaxEmailErrorsPerHour) {
    $anomalies += [pscustomobject]@{
        severity = "high"
        type = "email_errors"
        message = "Email-related errors above threshold"
        details = [pscustomobject]@{
            observed = $emailErrors
            threshold = $MaxEmailErrorsPerHour
            window_minutes = $WindowMinutes
        }
    }
}

if ($envRuntimeTracked) {
    $anomalies += [pscustomobject]@{
        severity = "critical"
        type = "secret_exposure"
        message = "config/.env.runtime is still tracked by git"
        details = [pscustomobject]@{
            tracked = $envRuntimeTracked
            ignored = $envRuntimeIgnored
        }
    }
}

if (-not $envRuntimeIgnored) {
    $anomalies += [pscustomobject]@{
        severity = "medium"
        type = "ignore_policy"
        message = "config/.env.runtime is not ignored by git"
        details = [pscustomobject]@{
            tracked = $envRuntimeTracked
            ignored = $envRuntimeIgnored
        }
    }
}

if (-not $gitAvailable) {
    $anomalies += [pscustomobject]@{
        severity = "medium"
        type = "git_unavailable"
        message = "git command is not available; repository exposure checks were skipped"
        details = [pscustomobject]@{}
    }
}

$criticalCount = @($anomalies | Where-Object { $_.severity -eq "critical" }).Count
$status = if ($anomalies.Count -eq 0) { "ok" } elseif ($criticalCount -gt 0) { "critical" } else { "warn" }

$result = [pscustomobject]@{
    timestamp = (Get-Date).ToString("o")
    status = $status
    base_url = $BaseUrl
    window_minutes = $WindowMinutes
    metrics = [pscustomobject]@{
        email_calls_window = $emailCalls
        email_errors_window = $emailErrors
    }
    thresholds = [pscustomobject]@{
        max_email_calls_per_hour = $MaxEmailCallsPerHour
        max_email_errors_per_hour = $MaxEmailErrorsPerHour
    }
    probe = $probe
    repo_security = [pscustomobject]@{
        env_runtime_tracked = $envRuntimeTracked
        env_runtime_ignored = $envRuntimeIgnored
    }
    anomaly_count = $anomalies.Count
    anomalies = $anomalies
}

if (-not $NoHistory) {
    $result | ConvertTo-Json -Depth 10 -Compress | Add-Content -Path $historyLog
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 10
}
else {
    Write-Host "Email security monitor" -ForegroundColor Cyan
    Write-Host "  status: $status"
    Write-Host "  email_calls_window: $emailCalls"
    Write-Host "  email_errors_window: $emailErrors"
    Write-Host "  env_runtime_tracked: $envRuntimeTracked"
    Write-Host "  env_runtime_ignored: $envRuntimeIgnored"
    Write-Host "  anomaly_count: $($anomalies.Count)"

    if ($anomalies.Count -gt 0) {
        Write-Host "  anomalies:" -ForegroundColor Yellow
        foreach ($a in $anomalies) {
            Write-Host "    - [$($a.severity)] $($a.type): $($a.message)" -ForegroundColor Yellow
        }
    }
}

switch ($status) {
    "ok" { exit 0 }
    "warn" { exit 1 }
    "critical" { exit 2 }
}

exit 2
