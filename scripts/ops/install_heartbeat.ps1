<#
.SYNOPSIS
    Installs Mike Heartbeat and Morning Briefing tasks in Windows Task Scheduler.
.DESCRIPTION
    Creates two scheduled tasks:
      1. "Mike Heartbeat"       — every 30 minutes
      2. "Mike Morning Briefing" — daily at 7:00 AM
#>
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$HeartbeatRunner = Join-Path $ProjectRoot "scripts\ops\mike_heartbeat.ps1"
$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source

if (-not (Test-Path -LiteralPath $HeartbeatRunner)) {
    Write-Error "Heartbeat runner not found at $HeartbeatRunner"
    exit 1
}

# --- Heartbeat: every 30 minutes ---
$taskName = "Mike Heartbeat"
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Removing existing task '$taskName'..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $PowerShellExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$HeartbeatRunner`" -Mode once" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $taskName `
    -Description "Mike AI - Heartbeat monitoring every 30 min" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Limited

Write-Host "Task '$taskName' registered (every 30 min)." -ForegroundColor Green


# --- Morning Briefing: daily at 7:00 AM ---
$briefingTask = "Mike Morning Briefing"
$existingBriefing = Get-ScheduledTask -TaskName $briefingTask -ErrorAction SilentlyContinue
if ($existingBriefing) {
    Write-Host "Removing existing task '$briefingTask'..."
    Unregister-ScheduledTask -TaskName $briefingTask -Confirm:$false
}

$briefingAction = New-ScheduledTaskAction `
    -Execute $PowerShellExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$HeartbeatRunner`" -Mode briefing" `
    -WorkingDirectory $ProjectRoot

$briefingTrigger = New-ScheduledTaskTrigger -Daily -At "07:00"

Register-ScheduledTask `
    -TaskName $briefingTask `
    -Description "Mike AI - Morning briefing at 7 AM" `
    -Action $briefingAction `
    -Trigger $briefingTrigger `
    -Settings $settings `
    -RunLevel Limited

Write-Host "Task '$briefingTask' registered (daily 7:00 AM)." -ForegroundColor Green
Write-Host ""
Write-Host "Done! Both tasks are now active." -ForegroundColor Cyan
Write-Host "  - Heartbeat: every 30 min"
Write-Host "  - Briefing:  daily 7:00 AM"
