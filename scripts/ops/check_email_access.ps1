param(
    [string]$BaseUrl = "http://127.0.0.1:8083",
    [int]$TimeoutSec = 20,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

function Invoke-MikeApi {
    param(
        [string]$Method,
        [string]$Path,
        [object]$Body = $null
    )

    $uri = if ($Path.StartsWith("http")) { $Path } else { "$BaseUrl$Path" }
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

    try {
        if ($null -ne $Body) {
            $bodyJson = $Body | ConvertTo-Json -Depth 8 -Compress
            $data = Invoke-RestMethod -Uri $uri -Method $Method -ContentType "application/json" -Body $bodyJson -TimeoutSec $TimeoutSec
        }
        else {
            $data = Invoke-RestMethod -Uri $uri -Method $Method -TimeoutSec $TimeoutSec
        }

        $stopwatch.Stop()
        return [ordered]@{
            ok = $true
            elapsed_ms = [math]::Round($stopwatch.Elapsed.TotalMilliseconds, 2)
            data = $data
            error = $null
        }
    }
    catch {
        $stopwatch.Stop()
        return [ordered]@{
            ok = $false
            elapsed_ms = [math]::Round($stopwatch.Elapsed.TotalMilliseconds, 2)
            data = $null
            error = $_.Exception.Message
        }
    }
}

$healthRes = Invoke-MikeApi -Method "GET" -Path "/health"
$healthOk = $false
if ($healthRes.ok -and $null -ne $healthRes.data -and ($healthRes.data.status -in @("ok", "healthy"))) {
    $healthOk = $true
}

$toolsRes = Invoke-MikeApi -Method "GET" -Path "/v1/tools"
$emailTools = @()
$toolsEmailOk = $false

if ($toolsRes.ok -and $null -ne $toolsRes.data) {
    $toolsPayload = if ($null -ne $toolsRes.data.tools) { $toolsRes.data.tools } else { $toolsRes.data }

    foreach ($tool in @($toolsPayload)) {
        $name = [string]$tool.name
        if ($name -match '^email\.') {
            $emailTools += $name
        }
    }

    $emailTools = @($emailTools | Sort-Object -Unique)
    $requiredTools = @("email.list_inbox", "email.read", "email.search", "email.send")
    $foundRequired = @($requiredTools | Where-Object { $emailTools -contains $_ })
    $toolsEmailOk = ($foundRequired.Count -eq $requiredTools.Count)
}

$probeBody = @{
    name = "email.search"
    parameters = @{
        query = "__mike_email_probe__"
        limit = 1
        folder = "INBOX"
    }
}
$probeRes = Invoke-MikeApi -Method "POST" -Path "/v1/tools/call" -Body $probeBody

$probeText = ""
if ($probeRes.ok -and $null -ne $probeRes.data) {
    if ($null -ne $probeRes.data.result -and $null -ne $probeRes.data.result.text) {
        $probeText = [string]$probeRes.data.result.text
    }
    elseif ($null -ne $probeRes.data.text) {
        $probeText = [string]$probeRes.data.text
    }
}

$authIssueSuspected = $false
if (-not [string]::IsNullOrWhiteSpace($probeText)) {
    $lower = $probeText.ToLowerInvariant()
    if ($lower -match 'auth|oauth|token|credencial|senha|password|imap|smtp|not configured|nao configurado|login') {
        $authIssueSuspected = $true
    }
}

$probeOk = ($probeRes.ok -and -not $authIssueSuspected)
$overallOk = ($healthOk -and $toolsEmailOk -and $probeOk)

$warnings = New-Object System.Collections.Generic.List[string]
if (-not $healthOk) {
    $warnings.Add("health endpoint did not return ok/healthy")
}
if (-not $toolsEmailOk) {
    $warnings.Add("required email tools are missing from /v1/tools")
}
if (-not $probeRes.ok) {
    $warnings.Add("email probe request failed")
}
if ($authIssueSuspected) {
    $warnings.Add("email probe text suggests auth/config issue")
}

$result = [ordered]@{
    timestamp = (Get-Date).ToString("o")
    base_url = $BaseUrl
    health_ok = $healthOk
    tools_email_ok = $toolsEmailOk
    probe_ok = $probeOk
    auth_issue_suspected = $authIssueSuspected
    email_tools_found = $emailTools
    overall_ok = $overallOk
    warnings = @($warnings)
    latency_ms = [ordered]@{
        health = $healthRes.elapsed_ms
        tools = $toolsRes.elapsed_ms
        probe = $probeRes.elapsed_ms
    }
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 8
}
else {
    Write-Host "Email access check" -ForegroundColor Cyan
    Write-Host "  base_url: $BaseUrl"
    Write-Host "  health_ok: $healthOk"
    Write-Host "  tools_email_ok: $toolsEmailOk"
    Write-Host "  probe_ok: $probeOk"
    Write-Host "  auth_issue_suspected: $authIssueSuspected"
    Write-Host "  overall_ok: $overallOk"

    if ($warnings.Count -gt 0) {
        Write-Host "  warnings:" -ForegroundColor Yellow
        foreach ($w in $warnings) {
            Write-Host "    - $w" -ForegroundColor Yellow
        }
    }
}

if ($overallOk) {
    exit 0
}
exit 1
