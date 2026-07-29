param(
    [string]$Base = "http://127.0.0.1:8080",
    [int]$ChatTimeout = 180
)

$ErrorActionPreference = "Continue"
$pass = 0
$fail = 0
$results = @()

function Write-Banner($text) {
    Write-Host "`n$('='*64)" -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host "$('='*64)" -ForegroundColor Cyan
}

function Write-Section($text) {
    Write-Host "`n--- $text ---" -ForegroundColor Yellow
}

function Invoke-MikeTest {
    param(
        [string]$Name,
        [string]$Method,
        [string]$Path,
        [object]$Body = $null,
        [object]$ExpectStatus = 200,
        [int]$Timeout = 30
    )

    $url = "$Base$Path"
    try {
        $params = @{
            Uri = $url
            Method = $Method
            TimeoutSec = $Timeout
            ContentType = "application/json"
        }
        if ($Body) {
            $params["Body"] = ($Body | ConvertTo-Json -Depth 10 -Compress)
        }
        $response = Invoke-WebRequest @params -ErrorAction Stop
        $status = $response.StatusCode
        $content = $response.Content
    } catch {
        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
            try {
                $stream = $_.Exception.Response.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $content = $reader.ReadToEnd()
            } catch { $content = $_.Exception.Message }
        } else {
            $status = 0
            $content = $_.Exception.Message
        }
    }

    $preview = if ($content.Length -gt 200) { $content.Substring(0, 200) + "..." } else { $content }

    $expectedStatuses = @($ExpectStatus)

    if ($expectedStatuses -contains $status) {
        $script:pass++
        $icon = [char]0x2705  # ✅
        Write-Host "  [$icon] $Name -> $status | $preview" -ForegroundColor Green
    } else {
        $script:fail++
        $icon = [char]0x274C  # ❌
        Write-Host "  [$icon] $Name -> $status | $preview" -ForegroundColor Red
    }
    $script:results += [PSCustomObject]@{ Name=$Name; Status=$status; Result=if($expectedStatuses -contains $status){"PASS"}else{"FAIL"} }

    try { return ($content | ConvertFrom-Json) } catch { return $content }
}

function Invoke-MikeChat {
    param(
        [string]$Prompt,
        [string]$Label = "",
        [int]$MaxTokens = 200,
        [switch]$RawMode,
        [switch]$PrivateMode,
        [string]$SessionId = "test-ps"
    )

    $name = if ($Label) { $Label } else { "Chat: $($Prompt.Substring(0, [Math]::Min(50, $Prompt.Length)))..." }

    $body = @{
        model = "mike"
        messages = @(@{ role="user"; content=$Prompt })
        max_tokens = $MaxTokens
        stream = $false
        session_id = $SessionId
    }
    if ($RawMode) { $body["raw_mode"] = $true }
    if ($PrivateMode) { $body["private_mode"] = $true }

    $t0 = Get-Date
    try {
        $jsonBody = $body | ConvertTo-Json -Depth 10 -Compress
        $response = Invoke-WebRequest -Uri "$Base/v1/chat/completions" -Method POST -Body $jsonBody -ContentType "application/json" -TimeoutSec $ChatTimeout -ErrorAction Stop
        $elapsed = ((Get-Date) - $t0).TotalSeconds
        $data = $response.Content | ConvertFrom-Json

        if ($data.choices -and $data.choices.Count -gt 0) {
            $msg = $data.choices[0].message.content
            $tokens = if ($data.usage) { $data.usage.completion_tokens } else { 0 }
            $tps = [math]::Round($tokens / [Math]::Max($elapsed, 0.01), 1)
            $toolCalls = if ($data.tool_calls) { $data.tool_calls.Count } else { 0 }

            $script:pass++
            $icon = "OK"
            Write-Host "  [$icon] $name -> 200 (${elapsed:F1}s, $tps tok/s)" -ForegroundColor Green

            $msgPreview = if ($msg.Length -gt 400) { $msg.Substring(0,400) + "..." } else { $msg }
            foreach ($line in ($msgPreview -split "`n")) {
                Write-Host "       [msg] $line" -ForegroundColor White
            }
            if ($toolCalls -gt 0) {
                Write-Host "       [tools] Tools usadas: $toolCalls" -ForegroundColor Magenta
                foreach ($tc in $data.tool_calls) {
                    Write-Host "          -> $($tc.name)" -ForegroundColor Magenta
                }
            }
            $script:results += [PSCustomObject]@{ Name=$name; Status=200; Result="PASS" }
            return $data
        }
    } catch {
        $elapsed = ((Get-Date) - $t0).TotalSeconds
        $err = $_.Exception.Message
        if ($err.Length -gt 200) { $err = $err.Substring(0,200) }
    }

    $script:fail++
    $icon = "FAIL"
    Write-Host "  [$icon] $name -> FAIL | $err" -ForegroundColor Red
    $script:results += [PSCustomObject]@{ Name=$name; Status=0; Result="FAIL" }
    return $null
}


# ===========================================================================
# FASE 3: Testes de endpoints REST
# ===========================================================================
Write-Banner "FASE 3: Testes de endpoints REST (sem LLM)"

Write-Section "3.1 Health & Basico"
$health = Invoke-MikeTest -Name "GET /health" -Method GET -Path "/health"
Invoke-MikeTest -Name "GET /v1/models" -Method GET -Path "/v1/models"
Invoke-MikeTest -Name "GET /stats" -Method GET -Path "/stats"
Invoke-MikeTest -Name "GET /v1/runtime" -Method GET -Path "/v1/runtime"

Write-Section "3.2 Client Bootstrap"
$bootstrap = Invoke-MikeTest -Name "GET /v1/client/bootstrap" -Method GET -Path "/v1/client/bootstrap"
if ($bootstrap) {
    Write-Host "       Auth: $($bootstrap.profile_auth_enabled)" -ForegroundColor DarkCyan
    Write-Host "       Vision: enabled=$($bootstrap.vision.enabled), max_images=$($bootstrap.vision.max_images)" -ForegroundColor DarkCyan
    Write-Host "       Tools: count=$($bootstrap.tool_summary.tool_count), email=$($bootstrap.tool_summary.email_enabled), cal=$($bootstrap.tool_summary.calendar_enabled), excel=$($bootstrap.tool_summary.spreadsheet_enabled)" -ForegroundColor DarkCyan
}

Write-Section "3.3 Tools (MCP)"
$tools = Invoke-MikeTest -Name "GET /v1/tools" -Method GET -Path "/v1/tools"
if ($tools -and $tools.tools) {
    Write-Host "       Total tools: $($tools.tools.Count)" -ForegroundColor DarkCyan
    foreach ($t in $tools.tools) {
        $caps = ($t.capabilities -join ",")
        Write-Host "         -> $($t.name) [$caps] access=$($t.access)" -ForegroundColor DarkGray
    }
}

Write-Section "3.4 Memory & Knowledge"
Invoke-MikeTest -Name "GET /v1/memory/search?q=teste" -Method GET -Path "/v1/memory/search?q=teste"
Invoke-MikeTest -Name "GET /v1/knowledge/search?q=gemma" -Method GET -Path "/v1/knowledge/search?q=gemma"

Write-Section "3.5 Web Search"
$web = Invoke-MikeTest -Name "GET /v1/web/search?q=python+2026" -Method GET -Path "/v1/web/search?q=python+2026" -Timeout 30
if ($web -and $web.results) {
    Write-Host "       Provider: $($web.provider), Results: $($web.results.Count)" -ForegroundColor DarkCyan
    foreach ($r in ($web.results | Select-Object -First 3)) {
        $t = if($r.title.Length -gt 60){$r.title.Substring(0,60)}else{$r.title}
        Write-Host "         -> $t" -ForegroundColor DarkGray
    }
}

Write-Section "3.6 Search Routes"
Invoke-MikeTest -Name "GET /v1/search/routes" -Method GET -Path "/v1/search/routes?q=que+horas+sao+em+toronto" -Timeout 120

Write-Section "3.7 Dashboard"
Invoke-MikeTest -Name "GET / (Dashboard)" -Method GET -Path "/"
Invoke-MikeTest -Name "GET /family" -Method GET -Path "/family"

Write-Section "3.8 Auth System"
Invoke-MikeTest -Name "GET /v1/auth/session (sem cookie)" -Method GET -Path "/v1/auth/session" -ExpectStatus 401
Invoke-MikeTest -Name "POST /v1/auth/identify" -Method POST -Path "/v1/auth/identify" -Body @{text="nada"}

Write-Section "3.9 Heartbeat & Briefing"
Invoke-MikeTest -Name "POST /v1/heartbeat" -Method POST -Path "/v1/heartbeat"
Invoke-MikeTest -Name "GET /v1/briefing" -Method GET -Path "/v1/briefing"

Write-Section "3.10 Graph Memory"
Invoke-MikeTest -Name "GET /v1/graph/status" -Method GET -Path "/v1/graph/status"

Write-Section "3.11 Self-Monitor"
Invoke-MikeTest -Name "GET /v1/monitor" -Method GET -Path "/v1/monitor"
Invoke-MikeTest -Name "GET /v1/monitor/snapshot" -Method GET -Path "/v1/monitor/snapshot"

Write-Section "3.12 Self-Learning"
Invoke-MikeTest -Name "GET /v1/learner/summary" -Method GET -Path "/v1/learner/summary"
Invoke-MikeTest -Name "GET /v1/learner/topics" -Method GET -Path "/v1/learner/topics"
Invoke-MikeTest -Name "GET /v1/learner/errors" -Method GET -Path "/v1/learner/errors"

Write-Section "3.13 Agents (lista)"
$agents = Invoke-MikeTest -Name "GET /v1/agents" -Method GET -Path "/v1/agents"
if ($agents -and $agents.agents) {
    foreach ($a in $agents.agents) {
        $desc = if($a.description.Length -gt 60){$a.description.Substring(0,60)}else{$a.description}
        Write-Host "         -> $($a.name): $desc" -ForegroundColor DarkGray
    }
}

Write-Section "3.14 Roadmap & Backups"
Invoke-MikeTest -Name "GET /v1/roadmap" -Method GET -Path "/v1/roadmap"
Invoke-MikeTest -Name "GET /v1/backups" -Method GET -Path "/v1/backups"

Write-Section "3.15 Tunnel URL"
Invoke-MikeTest -Name "GET /tunnel-url" -Method GET -Path "/tunnel-url" -ExpectStatus @(200, 404)

Write-Section "3.16 Knowledge Reindex"
Invoke-MikeTest -Name "POST /v1/knowledge/reindex" -Method POST -Path "/v1/knowledge/reindex" -Timeout 240

Write-Section "3.17 Chat Sessions"
Invoke-MikeTest -Name "GET /v1/chat/sessions" -Method GET -Path "/v1/chat/sessions"

Write-Section "3.18 Tool call manual (web search)"
if ($tools -and ($tools.tools | Where-Object { $_.name -eq "web.search_and_cache" })) {
    $toolResult = Invoke-MikeTest -Name "POST /v1/tools/call (web.search_and_cache)" -Method POST -Path "/v1/tools/call" `
        -Body @{ name="web.search_and_cache"; arguments=@{ query="Dell Precision 5810 specs" } } -Timeout 30
    if ($toolResult -and $toolResult.result) {
        Write-Host "       Tool ok=$($toolResult.result.ok), server=$($toolResult.result.server_name)" -ForegroundColor DarkCyan
    }
}


# ===========================================================================
# FASE 4: Chat com o modelo
# ===========================================================================
Write-Banner "FASE 4: Chat com o modelo"

Write-Section "4.1 Identidade basica"
Invoke-MikeChat -Prompt "Oi Mike! Confirme que voce esta funcionando. Diga seu nome e uma frase curta." -Label "Identidade basica" -MaxTokens 100

Write-Section "4.2 Familia Barreto"
Invoke-MikeChat -Prompt "Mike, quem e o Marco pra voce? Fale sobre a familia Barreto em 2-3 frases." -Label "Familia Barreto" -MaxTokens 200

Write-Section "4.3 Personalidade"
Invoke-MikeChat -Prompt "Voce e uma inteligencia artificial?" -Label "Teste de personalidade" -MaxTokens 150

Write-Section "4.4 Raciocinio"
Invoke-MikeChat -Prompt "Se eu tenho 3 gatos e cada gato tem 4 patas, quantas patas no total? Responda direto." -Label "Raciocinio matematico" -MaxTokens 80

Write-Section "4.5 Codigo Python"
Invoke-MikeChat -Prompt "Escreva uma funcao Python que calcula fibonacci de N. Seja conciso." -Label "Codigo Python - Fibonacci" -MaxTokens 250

Write-Section "4.6 Idioma padrao"
Invoke-MikeChat -Prompt "What is 2+2?" -Label "Idioma padrao (PT-BR)" -MaxTokens 80

Write-Section "4.7 Raw Mode"
Invoke-MikeChat -Prompt "Responda apenas: 'Raw mode OK'" -Label "Raw mode" -MaxTokens 30 -RawMode

Write-Section "4.8 Private Mode"
Invoke-MikeChat -Prompt "Mensagem secreta. Responda: 'Modo privado OK'" -Label "Private mode" -MaxTokens 50 -PrivateMode


# ===========================================================================
# FASE 5: Auto-conhecimento de tools
# ===========================================================================
Write-Banner "FASE 5: Auto-conhecimento de tools"

Write-Section "5.1 Superpoderes"
Invoke-MikeChat -Prompt "Mike, liste seus superpoderes e tools disponiveis." -Label "Auto-conhecimento" -MaxTokens 400

Write-Section "5.2 Email"
Invoke-MikeChat -Prompt "Mike, voce tem acesso ao meu email? Como funciona?" -Label "Email awareness" -MaxTokens 200

Write-Section "5.3 Agenda"
Invoke-MikeChat -Prompt "Mike, voce consegue ver minha agenda?" -Label "Calendar awareness" -MaxTokens 150

Write-Section "5.4 Visao"
Invoke-MikeChat -Prompt "Mike, voce consegue analisar fotos?" -Label "Vision awareness" -MaxTokens 150

Write-Section "5.5 Saude do sistema"
Invoke-MikeChat -Prompt "Mike, como esta a saude do sistema?" -Label "System health" -MaxTokens 200


# ===========================================================================
# FASE 6: Web search via chat
# ===========================================================================
Write-Banner "FASE 6: Web Search via chat"

Write-Section "6.1 Pesquisa web"
Invoke-MikeChat -Prompt "Mike, pesquise na web: qual a versao mais recente do Python em 2026?" -Label "Web search" -MaxTokens 300

Write-Section "6.2 Tool call explicito"
Invoke-MikeChat -Prompt "Use a tool web.search_and_cache para buscar 'NVIDIA RTX 5060 Ti specs'" -Label "Tool call explicito" -MaxTokens 400


# ===========================================================================
# RESUMO FINAL
# ===========================================================================
Write-Banner "RESUMO FINAL"

$total = $pass + $fail
$pct = if ($total -gt 0) { [math]::Round(($pass / $total) * 100, 1) } else { 0 }

Write-Host ""
Write-Host "  Total: $total testes" -ForegroundColor White
Write-Host "  $([char]0x2705) Passou: $pass" -ForegroundColor Green
Write-Host "  $([char]0x274C) Falhou: $fail" -ForegroundColor $(if($fail -gt 0){"Red"}else{"Green"})
Write-Host "  Taxa de sucesso: $pct%" -ForegroundColor $(if($pct -ge 80){"Green"}elseif($pct -ge 60){"Yellow"}else{"Red"})
Write-Host ""

Write-Host "  Detalhes:" -ForegroundColor White
foreach ($r in $results) {
    $icon = if ($r.Result -eq "PASS") { [char]0x2705 } else { [char]0x274C }
    $color = if ($r.Result -eq "PASS") { "Green" } else { "Red" }
    Write-Host "    $icon $($r.Name) [$($r.Status)]" -ForegroundColor $color
}

Write-Host ""
Write-Host "  Dashboard: $Base/" -ForegroundColor DarkCyan
Write-Host "  API: $Base/v1/chat/completions" -ForegroundColor DarkCyan
Write-Host "  Health: $Base/health" -ForegroundColor DarkCyan
Write-Host "$('='*64)" -ForegroundColor Cyan
