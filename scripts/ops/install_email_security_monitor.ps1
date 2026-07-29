param(
    [int]$IntervalMinutes = 15,
    [string]$TaskName = "Mike Email Security Monitor",
    [string]$BaseUrl = "http://127.0.0.1:8083",
    [int]$MaxEmailCallsPerHour = 40,
    [int]$MaxEmailErrorsPerHour = 10,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

if ($IntervalMinutes -lt 5) {
    Write-Error "IntervalMinutes must be >= 5"
    exit 1
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$monitorScript = Join-Path $scriptDir "monitor_email_security.ps1"
$powershellExe = (Get-Command powershell.exe).Source

if (-not (Test-Path $monitorScript)) {
    Write-Error "monitor_email_security.ps1 not found at $monitorScript"
    exit 1
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$monitorScript`" -BaseUrl `"$BaseUrl`" -WindowMinutes 60 -MaxEmailCallsPerHour $MaxEmailCallsPerHour -MaxEmailErrorsPerHour $MaxEmailErrorsPerHour"

$action = New-ScheduledTaskAction `
    -Execute $powershellExe `
    -Argument $argument `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Mike email connectivity and anomaly monitor" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Limited

Write-Host "Task '$TaskName' registered (every $IntervalMinutes min)." -ForegroundColor Green
Write-Host "Monitor script: $monitorScript"
Write-Host "Thresholds: calls/h=$MaxEmailCallsPerHour errors/h=$MaxEmailErrorsPerHour"
Write-Host "History log: $(Join-Path $projectRoot 'logs\email_security_history.jsonl')"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started now." -ForegroundColor Cyan
}
