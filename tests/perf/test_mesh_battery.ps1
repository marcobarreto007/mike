# ============================================================
# Bateria de Testes do Mike — Simples → Complexo (TaskMesh)
# ============================================================
# Testa: resposta direta, tool simples, nova search_emails,
#        decomposição multi-step, e TaskMesh complexo.
# ============================================================

$baseUrl = "http://localhost:8080"
$headers = @{ "Content-Type" = "application/json" }
$results = @()
$testNum = 0

function Send-Chat {
    param(
        [string]$Message,
        [string]$SessionId = "test-battery",
        [int]$TimeoutSec = 120
    )
    $body = @{
        model    = "mike"
        messages = @(@{ role = "user"; content = $Message })
        stream   = $false
        session_id = $SessionId
        max_tokens = 2048
    } | ConvertTo-Json -Depth 5

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-RestMethod -Uri "$baseUrl/v1/chat/completions" `
            -Method POST -Headers $headers -Body $body -TimeoutSec $TimeoutSec
        $sw.Stop()
        $text = $resp.choices[0].message.content
        return @{
            ok       = $true
            text     = $text
            time_s   = [math]::Round($sw.Elapsed.TotalSeconds, 1)
            tokens   = $resp.usage.completion_tokens
            tools    = if ($resp.tool_calls) { $resp.tool_calls } else { $null }
        }
    } catch {
        $sw.Stop()
        return @{
            ok     = $false
            text   = $_.Exception.Message
            time_s = [math]::Round($sw.Elapsed.TotalSeconds, 1)
            tokens = 0
            tools  = $null
        }
    }
}

function Run-Test {
    param(
        [string]$Name,
        [string]$Category,
        [string]$Prompt,
        [scriptblock]$Validate,
        [int]$TimeoutSec = 120
    )
    $script:testNum++
    Write-Host ""
    Write-Host "=" * 70 -ForegroundColor Cyan
    Write-Host "  TESTE $($script:testNum): $Name" -ForegroundColor Yellow
    Write-Host "  Categoria: $Category" -ForegroundColor DarkGray
    Write-Host "  Prompt: $Prompt" -ForegroundColor DarkGray
    Write-Host "=" * 70 -ForegroundColor Cyan

    $r = Send-Chat -Message $Prompt -SessionId "test-battery-$($script:testNum)" -TimeoutSec $TimeoutSec

    if ($r.ok) {
        Write-Host "  Tempo: $($r.time_s)s | Tokens: $($r.tokens)" -ForegroundColor Gray
        $preview = if ($r.text.Length -gt 300) { $r.text.Substring(0, 300) + "..." } else { $r.text }
        Write-Host "  Resposta: $preview" -ForegroundColor White

        # Run validation
        $passed = & $Validate $r
        if ($passed) {
            Write-Host "  RESULTADO: PASSOU" -ForegroundColor Green
        } else {
            Write-Host "  RESULTADO: FALHOU" -ForegroundColor Red
        }
    } else {
        Write-Host "  ERRO: $($r.text)" -ForegroundColor Red
        $passed = $false
    }

    $script:results += @{
        num      = $script:testNum
        name     = $Name
        category = $Category
        passed   = $passed
        time_s   = $r.time_s
        tokens   = $r.tokens
        response = if ($r.text.Length -gt 500) { $r.text.Substring(0, 500) } else { $r.text }
    }
}

Write-Host ""
Write-Host "###############################################################" -ForegroundColor Magenta
Write-Host "#  BATERIA DE TESTES DO MIKE - TaskMesh e Tools             #" -ForegroundColor Magenta
Write-Host "#  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')                             #" -ForegroundColor Magenta
Write-Host "###############################################################" -ForegroundColor Magenta

# ────────────────────────────────────────────────────────────
# NIVEL 1: Resposta direta (sem tools)
# ────────────────────────────────────────────────────────────

Run-Test -Name "Resposta direta simples" `
    -Category "Nivel 1 - Sem Tool" `
    -Prompt "Oi Mike, tudo bem? Me diz que dia e hoje." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 10 -and ($r.text -match "16|abril|2026|quarta")
    }

Run-Test -Name "Conhecimento da familia" `
    -Category "Nivel 1 - Sem Tool" `
    -Prompt "Mike, quem sao os membros da familia? Lista todos os nomes." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 30 -and ($r.text -match "(?i)marco|raphael|ana|alice|matheus")
    }

# ────────────────────────────────────────────────────────────
# NIVEL 2: Tool simples (1 chamada)
# ────────────────────────────────────────────────────────────

Run-Test -Name "list_inbox basico" `
    -Category "Nivel 2 - Tool Simples" `
    -Prompt "Lista os 5 emails mais recentes da minha caixa de entrada." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 50 -and ($r.text -match "(?i)email|inbox|assunto|subject|de=|remetente")
    }

Run-Test -Name "search_emails (nova tool)" `
    -Category "Nivel 2 - Tool Simples" `
    -Prompt "Busca emails que eu recebi do rapha nos ultimos 30 dias." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 20 -and -not ($r.text -match "(?i)nao (tenho|consigo|posso) acessar|erro de auth|drive")
    }

Run-Test -Name "get_email_address" `
    -Category "Nivel 2 - Tool Simples" `
    -Prompt "Qual o email configurado no sistema?" `
    -Validate {
        param($r)
        $r.ok -and ($r.text -match "(?i)@|oauth|email|gmail")
    }

# ────────────────────────────────────────────────────────────
# NIVEL 3: Tool com query complexa
# ────────────────────────────────────────────────────────────

Run-Test -Name "list_inbox com query Gmail" `
    -Category "Nivel 3 - Query Complexa" `
    -Prompt "Mostra emails nao lidos que tenham anexo, dos ultimos 7 dias." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 20
    }

Run-Test -Name "search_emails multi-filtro" `
    -Category "Nivel 3 - Query Complexa" `
    -Prompt "Busca emails sobre 'meeting' ou 'reuniao' dos ultimos 15 dias." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 20
    }

# ────────────────────────────────────────────────────────────
# NIVEL 4: Decomposição simples (2-3 steps)
# ────────────────────────────────────────────────────────────

Run-Test -Name "Decompor: inbox + resumo" `
    -Category "Nivel 4 - Decomposicao Simples" `
    -Prompt "Faz uma revisao do meu inbox — lista os emails recentes e me da um resumo do que e importante." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 100
    } -TimeoutSec 180

Run-Test -Name "Decompor: buscar pessoa + ler email" `
    -Category "Nivel 4 - Decomposicao Simples" `
    -Prompt "Encontra o ultimo email que recebi de uma pessoa real (nao empresa) e me mostra o conteudo completo dele." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 100
    } -TimeoutSec 180

# ────────────────────────────────────────────────────────────
# NIVEL 5: TaskMesh complexo (multi-tool, multi-step)
# ────────────────────────────────────────────────────────────

Run-Test -Name "TaskMesh: listar pessoas dos ultimos 10 dias" `
    -Category "Nivel 5 - TaskMesh Complexo" `
    -Prompt "Liste TODAS as pessoas reais (nao empresas, nao newsletters, nao sistemas automaticos) que me mandaram email nos ultimos 10 dias. Quero so nomes de pessoas." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 50
    } -TimeoutSec 240

Run-Test -Name "TaskMesh: analise cross-tool (email + web)" `
    -Category "Nivel 5 - TaskMesh Complexo" `
    -Prompt "Verifica meus emails recentes e identifica se tem algum email urgente ou importante. Depois faz uma busca na web sobre o assunto do email mais importante pra me dar contexto." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 100
    } -TimeoutSec 300

Run-Test -Name "TaskMesh: relatorio completo do inbox" `
    -Category "Nivel 5 - TaskMesh Complexo" `
    -Prompt "Faz um relatorio completo do meu inbox dos ultimos 7 dias: (1) quantos emails recebi, (2) lista de remetentes unicos, (3) os 3 emails mais importantes com resumo, (4) emails que precisam de resposta urgente." `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 200
    } -TimeoutSec 300

# ────────────────────────────────────────────────────────────
# NIVEL 6: Stress — tarefa que exige raciocínio + tools
# ────────────────────────────────────────────────────────────

Run-Test -Name "Stress: comparar emails de 2 periodos" `
    -Category "Nivel 6 - Stress" `
    -Prompt "Compara meu inbox da ultima semana com a semana anterior. Recebi mais ou menos emails? De quem?" `
    -Validate {
        param($r)
        $r.ok -and $r.text.Length -gt 100
    } -TimeoutSec 300

# ============================================================
# RELATORIO FINAL
# ============================================================

Write-Host ""
Write-Host ""
Write-Host "###############################################################" -ForegroundColor Magenta
Write-Host "#                    RELATORIO FINAL                          #" -ForegroundColor Magenta
Write-Host "###############################################################" -ForegroundColor Magenta
Write-Host ""

$passed = ($results | Where-Object { $_.passed }).Count
$failed = ($results | Where-Object { -not $_.passed }).Count
$total  = $results.Count
$totalTime = ($results | Measure-Object -Property time_s -Sum).Sum

Write-Host "  Total: $total testes | Passou: $passed | Falhou: $failed" -ForegroundColor $(if ($failed -eq 0) { "Green" } else { "Yellow" })
Write-Host "  Tempo total: $([math]::Round($totalTime, 1))s" -ForegroundColor Gray
Write-Host ""

foreach ($r in $results) {
    $icon = if ($r.passed) { "[OK]" } else { "[FAIL]" }
    $color = if ($r.passed) { "Green" } else { "Red" }
    Write-Host "  $icon Teste $($r.num): $($r.name) ($($r.time_s)s, $($r.tokens) tokens)" -ForegroundColor $color
}

Write-Host ""
Write-Host "  Detalhes dos falhos:" -ForegroundColor Yellow
foreach ($r in ($results | Where-Object { -not $_.passed })) {
    Write-Host "    Teste $($r.num) ($($r.name)):" -ForegroundColor Red
    Write-Host "    $($r.response)" -ForegroundColor DarkRed
    Write-Host ""
}

Write-Host "###############################################################" -ForegroundColor Magenta
