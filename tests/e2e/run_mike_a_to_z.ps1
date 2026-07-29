param(
    [int]$Port = 8080,
    [string]$TempServiceName = "MikeAI-Test",
    [string]$AuthKey = "mike-a-to-z-secret",
    [string]$ReportPath
)

$ErrorActionPreference = "Stop"

$testsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $testsRoot
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $ReportPath) {
    $ReportPath = Join-Path $projectRoot ("mike\roadmap\a_to_z_test_report_{0}.json" -f $timestamp)
}

$healthUrl = "http://127.0.0.1:$Port/health"
$statsUrl = "http://127.0.0.1:$Port/stats"
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$nssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source
$authStdout = Join-Path $projectRoot "logs\a_to_z_auth_stdout.log"
$authStderr = Join-Path $projectRoot "logs\a_to_z_auth_stderr.log"

function Get-MikeProcesses {
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match '^python(\.exe)?$' -and
            $_.CommandLine -match 'mike_server\.py'
        }
}

function Get-TestService {
    Get-Service -Name $TempServiceName -ErrorAction SilentlyContinue
}

function Invoke-Json([string]$Url, [hashtable]$Headers = @{}) {
    return Invoke-RestMethod -Uri $Url -Headers $Headers -TimeoutSec 15
}

function Invoke-HttpStatusCode([string]$Url, [hashtable]$Headers = @{}) {
    try {
        Invoke-WebRequest -Uri $Url -Headers $Headers -TimeoutSec 10 | Out-Null
        return 200
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            return [int]$_.Exception.Response.StatusCode
        }
        throw
    }
}

function Wait-ForHealth([int]$TimeoutSec = 90, [hashtable]$Headers = @{}) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-Json -Url $healthUrl -Headers $Headers
            if ($health.status -eq "ok") {
                return $health
            }
        } catch {
        }
        Start-Sleep -Milliseconds 750
    }
    throw "Health check did not become ready within $TimeoutSec seconds."
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) {
        throw $Message
    }
}

function Stop-MikeRuntime {
    $service = Get-TestService
    if ($service -and $service.Status -ne "Stopped") {
        Stop-Service -Name $TempServiceName -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    $procs = @(Get-MikeProcesses)
    foreach ($proc in $procs) {
        Stop-Process -Id $proc.ProcessId -Force
    }
    if ($procs.Count -gt 0) {
        Start-Sleep -Seconds 2
    }
}

function Start-NormalMike {
    & (Join-Path $projectRoot "scripts\start_mike.ps1") -Port $Port -ForceRestart | Out-Null
    return Wait-ForHealth
}

function Remove-TestService {
    if (-not $nssmPath) {
        return
    }

    $service = Get-TestService
    if (-not $service) {
        return
    }

    if ($service.Status -ne "Stopped") {
        & $nssmPath stop $TempServiceName | Out-Null
        Start-Sleep -Seconds 2
    }
    & $nssmPath remove $TempServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}

function Run-Check([string]$Id, [scriptblock]$Action) {
    $started = Get-Date
    try {
        $details = & $Action
        $status = "passed"
        if ($details -is [hashtable] -and $details.Contains("__status")) {
            $status = $details["__status"]
            $details.Remove("__status")
        }
        return [ordered]@{
            id = $Id
            status = $status
            started_at = $started.ToUniversalTime().ToString("o")
            finished_at = (Get-Date).ToUniversalTime().ToString("o")
            details = $details
        }
    } catch {
        return [ordered]@{
            id = $Id
            status = "failed"
            started_at = $started.ToUniversalTime().ToString("o")
            finished_at = (Get-Date).ToUniversalTime().ToString("o")
            error = $_.Exception.Message
        }
    }
}

$results = New-Object System.Collections.Generic.List[object]
$authProcess = $null

try {
    $results.Add((Run-Check "bootstrap_normal_runtime" {
        $health = Start-NormalMike
        @{
            bind_host = $health.bind_host
            auth_enabled = $health.auth_enabled
            web_search_provider = $health.web_search_provider
        }
    }))

    $results.Add((Run-Check "py_compile" {
        & $pythonPath -m py_compile .\src\mike_server.py .\src\mike_memory.py .\src\mike_web.py .\tests\test_mike_server.py .\tests\test_mike_memory.py .\tests\test_mike_web.py
        if ($LASTEXITCODE -ne 0) {
            throw "py_compile failed with exit code $LASTEXITCODE"
        }
        @{ command = "python -m py_compile"; status = "ok" }
    }))

    $results.Add((Run-Check "unit_tests" {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        & $pythonPath -m unittest discover -s tests -v
        $exitCode = $LASTEXITCODE
        $sw.Stop()
        if ($exitCode -ne 0) {
            throw "unittest failed with exit code $exitCode"
        }
        @{
            command = "python -m unittest discover -s tests -v"
            duration_seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2)
        }
    }))

    $results.Add((Run-Check "integration_levels_1_to_5" {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $output = & $pythonPath .\tests\run_mike_levels.py 2>&1
        $exitCode = $LASTEXITCODE
        $sw.Stop()
        if ($exitCode -ne 0) {
            throw ("run_mike_levels.py failed with exit code {0}: {1}" -f $exitCode, (($output | Select-Object -Last 20) -join "`n"))
        }
        @{
            command = "python .\tests\run_mike_levels.py"
            duration_seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2)
            tail = ($output | Select-Object -Last 12)
        }
    }))

    $results.Add((Run-Check "core_endpoints" {
        $health = Invoke-Json $healthUrl
        $stats = Invoke-Json $statsUrl
        $runtime = Invoke-Json ("http://127.0.0.1:$Port/v1/runtime")
        $bootstrap = Invoke-Json ("http://127.0.0.1:$Port/v1/client/bootstrap")
        $sessions = Invoke-Json ("http://127.0.0.1:$Port/v1/chat/sessions?profile=visitante")
        $roadmap = Invoke-Json ("http://127.0.0.1:$Port/v1/roadmap")
        $backups = Invoke-Json ("http://127.0.0.1:$Port/v1/backups")
        $models = Invoke-Json ("http://127.0.0.1:$Port/v1/models")

        Assert-True ($health.status -eq "ok") "Health endpoint returned non-ok status."
        Assert-True ($stats.cuda_detected -eq $true) "Stats should report CUDA detected."
        Assert-True ($runtime.supports_text_streaming -eq $true) "Runtime should advertise text streaming."
        Assert-True ($null -ne $bootstrap.tool_summary) "Bootstrap missing tool summary."
        Assert-True ($bootstrap.chat.default_max_tokens -ge 256) "Bootstrap default_max_tokens unexpectedly low."
        Assert-True ($null -ne $sessions.sessions) "Chat sessions endpoint missing sessions payload."
        Assert-True ($roadmap.summary.roadmap_items_total -ge 15) "Roadmap total items unexpectedly low."
        Assert-True ($models.data.Count -ge 1) "Models endpoint returned no models."

        @{
            health = $health
            bootstrap = @{
                ops_access = $bootstrap.ops_access
                tool_count = $bootstrap.tool_summary.tool_count
                supports_text_streaming = $bootstrap.chat.supports_text_streaming
            }
            stats_subset = @{
                total_requests = $stats.total_requests
                total_tokens_generated = $stats.total_tokens_generated
                backup_archives = $stats.backup_archives
            }
            roadmap_completion_percent = $roadmap.summary.roadmap_completion_percent
            backup_summary = $backups.summary
        }
    }))

    $results.Add((Run-Check "dashboard_assets" {
        $dashboardHome = Invoke-WebRequest -Uri ("http://127.0.0.1:$Port/") -TimeoutSec 15
        $appJs = Invoke-WebRequest -Uri ("http://127.0.0.1:$Port/static/app.js") -TimeoutSec 15
        $styleCss = Invoke-WebRequest -Uri ("http://127.0.0.1:$Port/static/style.css") -TimeoutSec 15

        Assert-True ($dashboardHome.Content -match "Mike Operator Console") "Dashboard HTML missing operator console title."
        Assert-True ($appJs.Content -match "refreshBootstrap") "Dashboard JS missing bootstrap refresh hook."
        Assert-True ($appJs.Content -match "/v1/chat/history") "Dashboard JS missing backend history rehydration."
        Assert-True ($styleCss.Content -match "session-btn") "Dashboard CSS missing session sidebar styles."

        @{
            html_bytes = $dashboardHome.RawContentLength
            js_bytes = $appJs.RawContentLength
            css_bytes = $styleCss.RawContentLength
        }
    }))

    $results.Add((Run-Check "backup_export_restore_cycle" {
        $before = @(Get-ChildItem .\mike\backups -Filter "*.zip" -File -ErrorAction SilentlyContinue)
        & .\scripts\backup_mike.ps1 -Mode backup -Label a-to-z | Out-Null
        $backupExit = $LASTEXITCODE
        if ($backupExit -ne 0) {
            throw "backup_mike.ps1 backup failed with exit code $backupExit"
        }
        $afterBackup = @(Get-ChildItem .\mike\backups -Filter "*.zip" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
        Assert-True ($afterBackup.Count -gt $before.Count) "Backup archive count did not increase."

        $exportPath = Join-Path $projectRoot ("mike\backups\mike-export-{0}.zip" -f $timestamp)
        & .\scripts\backup_mike.ps1 -Mode export -Label a-to-z -ArchivePath $exportPath | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "backup_mike.ps1 export failed with exit code $LASTEXITCODE"
        }
        Assert-True (Test-Path $exportPath) "Export archive was not created."

        $guardWorked = $false
        try {
            & .\scripts\backup_mike.ps1 -Mode restore -ArchivePath $afterBackup[0].FullName | Out-Null
        } catch {
            if ($_.Exception.Message -match "Mike is running") {
                $guardWorked = $true
            } else {
                throw
            }
        }
        Assert-True $guardWorked "Restore guard did not block while Mike was running."

        & .\scripts\backup_mike.ps1 -Mode restore -ArchivePath $afterBackup[0].FullName -StopMike -RestartMike | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "backup_mike.ps1 restore failed with exit code $LASTEXITCODE"
        }
        $health = Wait-ForHealth

        @{
            latest_backup = $afterBackup[0].Name
            export_archive = Split-Path -Leaf $exportPath
            restore_guard = $guardWorked
            health_after_restore = $health.status
        }
    }))

    $results.Add((Run-Check "operational_scripts" {
        & .\scripts\recover_mike.ps1 -Mode status | Out-Null
        & .\scripts\recover_mike.ps1 -Mode logs -Tail 5 | Out-Null
        & .\scripts\recover_mike.ps1 -Mode restart | Out-Null
        $health = Wait-ForHealth
        @{
            status = "ok"
            health_after_restart = $health.status
        }
    }))

    $results.Add((Run-Check "auth_hardening_cycle" {
        Stop-MikeRuntime

        $oldApi = $env:MIKE_API_KEY
        $oldTrust = $env:MIKE_TRUST_LOCALHOST
        try {
            $env:MIKE_API_KEY = $AuthKey
            $env:MIKE_TRUST_LOCALHOST = "false"

            if (Test-Path $authStdout) { Remove-Item $authStdout -Force }
            if (Test-Path $authStderr) { Remove-Item $authStderr -Force }

            $authProcess = Start-Process -FilePath $pythonPath `
                -ArgumentList ".\src\mike_server.py" `
                -WorkingDirectory $projectRoot `
                -PassThru `
                -WindowStyle Hidden `
                -RedirectStandardOutput $authStdout `
                -RedirectStandardError $authStderr

            $health = Wait-ForHealth
            $unauthStatus = Invoke-HttpStatusCode -Url $statsUrl
            $authStatus = Invoke-HttpStatusCode -Url $statsUrl -Headers @{ Authorization = "Bearer $AuthKey" }

            Assert-True ($health.auth_enabled -eq $true) "Auth probe runtime should report auth enabled."
            Assert-True ($unauthStatus -eq 401) "Unauthenticated stats should return 401."
            Assert-True ($authStatus -eq 200) "Authenticated stats should return 200."

            @{
                health = $health
                unauthenticated_status = $unauthStatus
                authenticated_status = $authStatus
            }
        } finally {
            if ($authProcess -and -not $authProcess.HasExited) {
                Stop-Process -Id $authProcess.Id -Force
                Start-Sleep -Seconds 2
            }
            $authProcess = $null

            if ($null -eq $oldApi) {
                Remove-Item Env:MIKE_API_KEY -ErrorAction SilentlyContinue
            } else {
                $env:MIKE_API_KEY = $oldApi
            }

            if ($null -eq $oldTrust) {
                Remove-Item Env:MIKE_TRUST_LOCALHOST -ErrorAction SilentlyContinue
            } else {
                $env:MIKE_TRUST_LOCALHOST = $oldTrust
            }

            Start-NormalMike | Out-Null
        }
    }))

    $results.Add((Run-Check "windows_service_cycle" {
        Assert-True ([bool]$nssmPath) "NSSM is not available in PATH."

        Stop-MikeRuntime
        Remove-TestService

        try {
            & .\scripts\install_mike_service.ps1 -ServiceName $TempServiceName -StartNow | Out-Null
        } catch {
            $message = $_.Exception.Message
            if ($message -match "Administrator" -or $message -match "Cannot open .* service") {
                Start-NormalMike | Out-Null
                return @{
                    __status = "blocked"
                    reason = "Windows service install requires elevated Service Control Manager access in this shell."
                    raw_error = $message
                }
            }
            throw
        }
        $service = Get-TestService
        Assert-True ([bool]$service) "Temporary Mike service was not installed."
        $health = Wait-ForHealth
        $service = Get-TestService
        Assert-True ($service.Status -eq "Running") "Temporary Mike service is not running."

        Remove-TestService
        Start-NormalMike | Out-Null

        @{
            service_name = $TempServiceName
            service_status = "running_then_removed"
            health_via_service = $health.status
        }
    }))
} finally {
    if ($authProcess -and -not $authProcess.HasExited) {
        Stop-Process -Id $authProcess.Id -Force
    }
    Remove-TestService
    Start-NormalMike | Out-Null
}

$failed = @($results | Where-Object { $_.status -eq "failed" })
$blocked = @($results | Where-Object { $_.status -eq "blocked" })
$report = [ordered]@{
    schema_version = "mike.a_to_z.v1"
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    project_root = $projectRoot
    overall_status = if ($failed.Count -eq 0 -and $blocked.Count -eq 0) { "passed" } elseif ($failed.Count -eq 0) { "passed_with_blockers" } else { "failed" }
    total_checks = $results.Count
    passed_checks = @($results | Where-Object { $_.status -eq "passed" }).Count
    blocked_checks = $blocked.Count
    failed_checks = @($results | Where-Object { $_.status -eq "failed" }).Count
    checks = $results
    final_stats = Invoke-Json $statsUrl
}

$reportDir = Split-Path -Parent $ReportPath
if ($reportDir) {
    New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
}
$report | ConvertTo-Json -Depth 8 | Set-Content -Path $ReportPath -Encoding UTF8
Get-Content $ReportPath -Raw

if (@($results | Where-Object { $_.status -eq "failed" }).Count -gt 0) {
    exit 1
}
