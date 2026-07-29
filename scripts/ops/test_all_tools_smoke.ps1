param(
    [string]$BaseUrl = "http://127.0.0.1:8083",
    [int]$TimeoutSec = 30,
    [string]$ReportPath = "",
    [switch]$IncludeGithubWrite,
    [switch]$IncludePuppeteerInteractive
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"

if (-not $ReportPath) {
    $ReportPath = "runtime\\roadmap\\tools_smoke_$stamp.json"
}
if (-not [System.IO.Path]::IsPathRooted($ReportPath)) {
    $ReportPath = Join-Path $projectRoot $ReportPath
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ReportPath) | Out-Null

$workspaceScratchRel = "scratch/tool_smoke_$stamp"
$workspaceScratchAbs = Join-Path $projectRoot $workspaceScratchRel
New-Item -ItemType Directory -Force -Path $workspaceScratchAbs | Out-Null
$workspaceFileRel = "$workspaceScratchRel/proof.txt"

$filesystemRoot = $projectRoot
$filesystemScratchAbs = Join-Path $filesystemRoot "scratch\\tool_smoke_$stamp"
New-Item -ItemType Directory -Force -Path $filesystemScratchAbs | Out-Null
$filesystemFileAbs = Join-Path $filesystemScratchAbs "proof.txt"

$mediaPath = $null
$mediaCandidates = @(
    (Join-Path $filesystemRoot "dashboard\\icons\\icon-192.png"),
    (Join-Path $filesystemRoot "dashboard\\icons\\icon-512.png")
)
foreach ($candidate in $mediaCandidates) {
    if (Test-Path $candidate) {
        $mediaPath = $candidate
        break
    }
}
if (-not $mediaPath) {
    $found = Get-ChildItem -Path (Join-Path $filesystemRoot "dashboard") -Recurse -File -Include *.png,*.jpg,*.jpeg,*.webp -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $mediaPath = $found.FullName }
}

$sqliteTable = "mike_smoke_" + ((Get-Date).ToString("HHmmss"))
$memoryEntity = "mike_smoke_entity_$stamp"
$checkpointId = $null
$emailUid = $null

$githubReadOwner = "octocat"
$githubReadRepo = "Hello-World"
$githubPrOwner = "microsoft"
$githubPrRepo = "vscode"
$githubPullNumber = $null

function New-Result {
    param(
        [string]$Name,
        [string]$Status,
        [bool]$Ok,
        [hashtable]$Arguments,
        [string]$Text,
        [string]$Http = "ok",
        [string]$Reason = ""
    )

    if ($null -eq $Arguments) { $Arguments = @{} }
    $msg = [string]$Text
    if ($msg.Length -gt 400) {
        $msg = $msg.Substring(0, 400) + " ..."
    }

    return [ordered]@{
        name = $Name
        status = $Status
        http = $Http
        ok = $Ok
        arguments = $Arguments
        text = $msg
        reason = $Reason
    }
}

function Invoke-ToolCall {
    param(
        [string]$Name,
        [hashtable]$Arguments
    )

    $payload = @{ name = $Name; arguments = $Arguments }
    try {
        $response = Invoke-RestMethod -Uri "$BaseUrl/v1/tools/call" -Method Post -ContentType "application/json" -Body ($payload | ConvertTo-Json -Depth 20) -TimeoutSec $TimeoutSec
        $result = $response.result
        $ok = $false
        $text = ""
        if ($null -ne $result) {
            $ok = [bool]$result.ok
            $text = [string]$result.text
        }
        return (New-Result -Name $Name -Status $(if ($ok) { "ok" } else { "tool_error" }) -Ok $ok -Arguments $Arguments -Text $text)
    }
    catch {
        $msg = if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
        return (New-Result -Name $Name -Status "http_error" -Ok $false -Arguments $Arguments -Text $msg -Http "error")
    }
}

function Get-SchemaType {
    param(
        $Tool,
        [string]$PropertyName
    )

    if (-not $Tool.input_schema) { return $null }
    if (-not $Tool.input_schema.properties) { return $null }

    $propMatch = $Tool.input_schema.properties.PSObject.Properties | Where-Object { $_.Name -eq $PropertyName } | Select-Object -First 1
    if (-not $propMatch) { return $null }

    $v = $propMatch.Value
    if ($v.type) {
        if ($v.type -is [System.Array]) {
            return [string]($v.type[0])
        }
        return [string]$v.type
    }

    if ($v.anyOf) {
        foreach ($item in $v.anyOf) {
            if ($item.type -and $item.type -ne "null") {
                return [string]$item.type
            }
        }
    }

    return $null
}

function Get-GenericArgs {
    param($Tool)

    $args = @{}
    $required = @()
    if ($Tool.input_schema -and $Tool.input_schema.required) {
        $required = @($Tool.input_schema.required)
    }

    foreach ($req in $required) {
        $schemaType = Get-SchemaType -Tool $Tool -PropertyName $req
        switch ($schemaType) {
            "integer" { $args[$req] = 1; continue }
            "number"  { $args[$req] = 1; continue }
            "boolean" { $args[$req] = $false; continue }
            "array"   { $args[$req] = @(); continue }
            "object"  { $args[$req] = @{}; continue }
            default    { $args[$req] = "smoke"; continue }
        }
    }

    return $args
}

function Get-SkipReason {
    param([string]$Name)

    if ($Name -eq "filesystem.read_media_file" -and -not $mediaPath) {
        return "Nenhum arquivo de imagem encontrado para teste de mídia."
    }

    if ($Name -eq "memory.checkpoint_restore" -and -not $checkpointId) {
        return "checkpoint_restore depende de checkpoint_id valido da chamada checkpoint_save."
    }

    if ($Name -eq "email.read" -and -not $emailUid) {
        return "email.read depende de UID real obtido de email.list_inbox."
    }

    if ($Name -like "github.*") {
        $writeOps = @(
            "github.create_or_update_file",
            "github.create_repository",
            "github.push_files",
            "github.create_issue",
            "github.create_pull_request",
            "github.fork_repository",
            "github.create_branch",
            "github.update_issue",
            "github.add_issue_comment",
            "github.create_pull_request_review",
            "github.merge_pull_request",
            "github.update_pull_request_branch"
        )
        if (-not $IncludeGithubWrite -and ($writeOps -contains $Name)) {
            return "Operacao de escrita GitHub ignorada por padrao (use -IncludeGithubWrite)."
        }

        if ($Name -in @("github.get_pull_request_comments", "github.get_pull_request_reviews") -and -not $githubPullNumber) {
            return "Sem pull request de referencia para comments/reviews; aguardando retorno de github.list_pull_requests."
        }
    }

    if ($Name -in @("puppeteer.puppeteer_click", "puppeteer.puppeteer_fill", "puppeteer.puppeteer_select", "puppeteer.puppeteer_hover") -and -not $IncludePuppeteerInteractive) {
        return "Interacoes de seletor desativadas por padrao (dependem de sessao browser persistente); use -IncludePuppeteerInteractive."
    }

    return $null
}

function Build-Args {
    param($Tool)

    $name = [string]$Tool.name

    switch ($name) {
        "list_allowed_directories" { return @{} }
        "create_directory" { return @{ path = $workspaceScratchRel } }
        "list_directory" { return @{ path = "." } }
        "read_text_file" { return @{ path = "LICENSE"; head = 20 } }
        "write_file" { return @{ path = $workspaceFileRel; content = "MIKE_TOOL_SMOKE_OK" } }
        "edit_file" { return @{ path = $workspaceFileRel; old_text = "MIKE_TOOL_SMOKE_OK"; new_text = "MIKE_TOOL_SMOKE_EDITED"; replace_all = $false; dry_run = $false } }
        "delete_file" { return @{ path = "$workspaceScratchRel/tmp_delete.txt"; missing_ok = $true } }
        "delete_directory" { return @{ path = "$workspaceScratchRel/tmpdir"; recursive = $true; missing_ok = $true } }
        "move_path" { return @{ source = $workspaceFileRel; destination = "$workspaceScratchRel/proof_renamed.txt" } }
        "get_path_info" { return @{ path = "." } }

        "memory-persistent.create_entities" { return @{ entities = @(@{ name = $memoryEntity; entityType = "smoke"; observations = @("created-by-smoke-test") }) } }
        "memory-persistent.create_relations" { return @{ relations = @(@{ from = $memoryEntity; to = $memoryEntity; relationType = "self_test" }) } }
        "memory-persistent.add_observations" { return @{ observations = @(@{ entityName = $memoryEntity; contents = @("second-observation") }) } }
        "memory-persistent.delete_entities" { return @{ entityNames = @($memoryEntity) } }
        "memory-persistent.delete_observations" { return @{ deletions = @(@{ entityName = $memoryEntity; observations = @("second-observation") }) } }
        "memory-persistent.delete_relations" { return @{ relations = @(@{ from = $memoryEntity; to = $memoryEntity; relationType = "self_test" }) } }
        "memory-persistent.read_graph" { return @{} }
        "memory-persistent.search_nodes" { return @{ query = $memoryEntity } }
        "memory-persistent.open_nodes" { return @{ names = @($memoryEntity) } }

        "sequential-thinking.sequentialthinking" {
            return @{ thought = "Smoke test thought"; nextThoughtNeeded = $false; thoughtNumber = 1; totalThoughts = 1 }
        }

        "filesystem.read_file" { return @{ path = (Join-Path $filesystemRoot "LICENSE"); head = 20 } }
        "filesystem.read_text_file" { return @{ path = (Join-Path $filesystemRoot "LICENSE"); head = 20 } }
        "filesystem.read_media_file" { return @{ path = $mediaPath } }
        "filesystem.read_multiple_files" { return @{ paths = @((Join-Path $filesystemRoot "LICENSE"), (Join-Path $filesystemRoot "CLAUDE.md")) } }
        "filesystem.write_file" { return @{ path = $filesystemFileAbs; content = "filesystem-smoke" } }
        "filesystem.edit_file" { return @{ path = $filesystemFileAbs; edits = @(@{ oldText = "filesystem-smoke"; newText = "filesystem-smoke-edited" }); dryRun = $false } }
        "filesystem.create_directory" { return @{ path = $filesystemScratchAbs } }
        "filesystem.list_directory" { return @{ path = $filesystemRoot } }
        "filesystem.list_directory_with_sizes" { return @{ path = $filesystemRoot; sortBy = "name" } }
        "filesystem.directory_tree" { return @{ path = $filesystemRoot; excludePatterns = @("**/.venv/**") } }
        "filesystem.move_file" { return @{ source = $filesystemFileAbs; destination = (Join-Path $filesystemScratchAbs "proof_moved.txt") } }
        "filesystem.search_files" { return @{ path = $filesystemRoot; pattern = "**/*.md"; excludePatterns = @("**/.venv/**") } }
        "filesystem.get_file_info" { return @{ path = $filesystemRoot } }
        "filesystem.list_allowed_directories" { return @{} }

        "sqlite.query" { return @{ sql = "SELECT name FROM sqlite_master WHERE type='table' LIMIT 5;" } }
        "sqlite.execute" { return @{ sql = "CREATE TABLE IF NOT EXISTS $sqliteTable (id INTEGER PRIMARY KEY, note TEXT);" } }
        "sqlite.describe-table" { return @{ tableName = $sqliteTable } }
        "sqlite.list-tables" { return @{} }
        "sqlite.create-table" { return @{ name = $sqliteTable; columns = @(@{ name = "id"; type = "INTEGER"; primaryKey = $true }, @{ name = "note"; type = "TEXT" }); ifNotExists = $true } }
        "sqlite.drop-table" { return @{ name = "${sqliteTable}_drop"; ifExists = $true } }
        "sqlite.insert-record" { return @{ table = $sqliteTable; data = @{ note = "hello" } } }
        "sqlite.update-record" { return @{ table = $sqliteTable; data = @{ note = "updated" }; where = "note='hello'" } }
        "sqlite.delete-record" { return @{ table = $sqliteTable; where = "1=1" } }
        "sqlite.transaction" { return @{ statements = @("CREATE TABLE IF NOT EXISTS $sqliteTable (id INTEGER PRIMARY KEY, note TEXT)", "INSERT INTO $sqliteTable (note) VALUES ('tx')") } }

        "puppeteer.puppeteer_navigate" { return @{ url = "about:blank" } }
        "puppeteer.puppeteer_screenshot" { return @{ name = "smoke_shot_$stamp"; encoded = $true; width = 800; height = 600 } }
        "puppeteer.puppeteer_click" { return @{ selector = "body" } }
        "puppeteer.puppeteer_fill" { return @{ selector = "input"; value = "smoke" } }
        "puppeteer.puppeteer_select" { return @{ selector = "select"; value = "smoke" } }
        "puppeteer.puppeteer_hover" { return @{ selector = "body" } }
        "puppeteer.puppeteer_evaluate" { return @{ script = "() => document.title" } }

        "web.search_and_cache" { return @{ query = "Mike server health check"; limit = 1 } }
        "memory.checkpoint_save" { return @{ label = "smoke-$stamp" } }
        "memory.checkpoint_list" { return @{ limit = 3 } }
        "memory.checkpoint_restore" { return @{ checkpoint_id = $checkpointId } }
        "memory.session_summary" { return @{ summary = "smoke summary"; topics = @("smoke", "tools") } }

        "expert.consult_deepseek" { return @{ prompt = "responda apenas: ok"; mode = "reasoning" } }

        "email.send" { return @{ to = "noreply@example.invalid"; subject = "smoke"; body = "smoke"; html = $false } }
        "email.list_inbox" { return @{ limit = 1; folder = "INBOX"; unread_only = $false } }
        "email.read" { return @{ uid = $emailUid; folder = "INBOX" } }
        "email.search" { return @{ query = "smoke"; folder = "INBOX"; limit = 1 } }

        "mike.introspect" { return @{ section = "all" } }
        "mike.hot_cache_list" { return @{} }
        "mike.hot_cache_add" { return @{ key = "smoke-$stamp"; content = "ok"; tags = "smoke,test" } }

        "github.list_pull_requests" { return @{ owner = $githubPrOwner; repo = $githubPrRepo; state = "open"; per_page = 1 } }
        "github.get_pull_request_comments" { return @{ owner = $githubPrOwner; repo = $githubPrRepo; pull_number = $githubPullNumber } }
        "github.get_pull_request_reviews" { return @{ owner = $githubPrOwner; repo = $githubPrRepo; pull_number = $githubPullNumber } }

        default {
            if ($name -like "github.*") {
                $args = Get-GenericArgs -Tool $Tool
                if ($args.ContainsKey("owner")) { $args["owner"] = $githubReadOwner }
                if ($args.ContainsKey("repo")) { $args["repo"] = $githubReadRepo }
                if ($args.ContainsKey("branch")) { $args["branch"] = "main" }
                if ($args.ContainsKey("path")) { $args["path"] = "README" }
                if ($args.ContainsKey("message")) { $args["message"] = "smoke test" }
                if ($args.ContainsKey("title")) { $args["title"] = "smoke" }
                if ($args.ContainsKey("head")) { $args["head"] = "main" }
                if ($args.ContainsKey("base")) { $args["base"] = "main" }
                if ($args.ContainsKey("q")) { $args["q"] = "mike" }
                if ($args.ContainsKey("query")) { $args["query"] = "mike" }
                return $args
            }
            return (Get-GenericArgs -Tool $Tool)
        }
    }
}

Write-Host "============================================"
Write-Host "  Mike Tools Smoke Test (all tools)"
Write-Host "============================================"
Write-Host "Base URL:   $BaseUrl"
Write-Host "Project:    $projectRoot"
Write-Host "Report:     $ReportPath"

$manifest = Invoke-RestMethod -Uri "$BaseUrl/v1/tools" -TimeoutSec $TimeoutSec
$tools = @($manifest.tools)
if (-not $tools.Count) {
    throw "Nenhuma tool encontrada em /v1/tools"
}

Write-Host "Tools detectadas: $($tools.Count)"

$results = @()
$index = 0

foreach ($tool in $tools) {
    $index++
    $toolName = [string]$tool.name
    Write-Host ("[{0}/{1}] {2}" -f $index, $tools.Count, $toolName)

    $skip = Get-SkipReason -Name $toolName
    if ($skip) {
        $results += [pscustomobject](New-Result -Name $toolName -Status "skipped" -Ok $false -Arguments @{} -Text "skip" -Reason $skip)
        continue
    }

    $args = Build-Args -Tool $tool
    $entry = Invoke-ToolCall -Name $toolName -Arguments $args
    $results += [pscustomobject]$entry

    if ($toolName -eq "memory.checkpoint_save" -and $entry.status -eq "ok") {
        if ($entry.text -match "(ckpt-[A-Za-z0-9_-]+)") {
            $checkpointId = $matches[1]
        }
    }

    if ($toolName -eq "email.list_inbox" -and $entry.status -eq "ok") {
        if ($entry.text -match '"uid"\s*:\s*"?([0-9]+)"?') {
            $emailUid = $matches[1]
        }
    }

    if ($toolName -eq "github.list_pull_requests" -and $entry.status -eq "ok") {
        if ($entry.text -match '"number"\s*:\s*([0-9]+)') {
            $githubPullNumber = [int]$matches[1]
        }
    }
}

$okCount = @($results | Where-Object { $_.status -eq "ok" }).Count
$toolErrCount = @($results | Where-Object { $_.status -eq "tool_error" }).Count
$httpErrCount = @($results | Where-Object { $_.status -eq "http_error" }).Count
$skipCount = @($results | Where-Object { $_.status -eq "skipped" }).Count

$report = [ordered]@{
    timestamp = (Get-Date).ToString("o")
    base_url = $BaseUrl
    tool_count = $tools.Count
    ok_count = $okCount
    tool_error_count = $toolErrCount
    http_error_count = $httpErrCount
    skipped_count = $skipCount
    results = $results
}

($report | ConvertTo-Json -Depth 20) | Set-Content -Path $ReportPath -Encoding UTF8

Write-Host ""
Write-Host "Resumo:" -ForegroundColor Cyan
Write-Host "  OK:         $okCount"
Write-Host "  Tool error: $toolErrCount"
Write-Host "  HTTP error: $httpErrCount"
Write-Host "  Skipped:    $skipCount"
Write-Host "  Report:     $ReportPath"

exit 0
