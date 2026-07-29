<#
.SYNOPSIS
    Mike Heartbeat runner — single cycle
.DESCRIPTION
    Activates the venv and runs mike_heartbeat.py once.
    Designed to be called by Windows Task Scheduler.
#>
param(
    [ValidateSet("once", "briefing")]
    [string]$Mode = "once",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Script = Join-Path $ProjectRoot "core\autonomy\mike_heartbeat.py"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Mike virtual-environment Python not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $Script)) {
    throw "Mike heartbeat script not found: $Script"
}

$env:MIKE_HOME = $ProjectRoot
$coreDirs = @("server", "memory", "mcp", "autonomy", "comms", "orchestration", "integrations", "chat") |
    ForEach-Object { Join-Path $ProjectRoot "core\$_" }
$mikePythonPath = ($coreDirs + @($ProjectRoot, (Join-Path $ProjectRoot "core"))) -join ";"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$mikePythonPath;$($env:PYTHONPATH)" } else { $mikePythonPath }
$modeArgument = if ($Mode -eq "briefing") { "--briefing" } else { "--once" }

Push-Location $ProjectRoot
try {
    if ($ValidateOnly) {
        & $PythonExe -c "import mike_heartbeat; print(mike_heartbeat._PROJECT_ROOT)"
        exit $LASTEXITCODE
    }
    & $PythonExe $Script $modeArgument
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
