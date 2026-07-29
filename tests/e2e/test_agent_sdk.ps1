# Test battery for Mike Agent SDK
# Tests: agent registry, routing, tool filtering, sub-agent spawning

$BASE = "http://localhost:8080"
$pass = 0
$fail = 0
$total = 0

function Test-Case {
    param([string]$Name, [scriptblock]$Block)
    $script:total++
    Write-Host "`n=== TESTE $($script:total): $Name ===" -ForegroundColor Cyan
    try {
        $result = & $Block
        if ($result) {
            Write-Host "[PASS] $Name" -ForegroundColor Green
            $script:pass++
        } else {
            Write-Host "[FAIL] $Name" -ForegroundColor Red
            $script:fail++
        }
    } catch {
        Write-Host "[FAIL] $Name - ERRO: $_" -ForegroundColor Red
        $script:fail++
    }
}

# ============================================================
# TESTE 1: Agent SDK endpoint - list agents
# ============================================================
Test-Case "SDK Agent List" {
    $r = Invoke-RestMethod -Uri "$BASE/v1/agents/sdk" -Method Get
    Write-Host "  Agents encontrados: $($r.count)"
    foreach ($a in $r.agents) {
        Write-Host "    - $($a.name): $($a.description)" -ForegroundColor DarkGray
    }
    $r.count -ge 8
}

# ============================================================
# TESTE 2: SDK dispatch - email task
# ============================================================
Test-Case "SDK Dispatch - Email Agent" {
    $body = @{
        task = "busque meus emails do Raphael dos ultimos 5 dias"
        threshold = 0.5
    } | ConvertTo-Json

    $r = Invoke-RestMethod -Uri "$BASE/v1/agents/sdk/dispatch" -Method Post `
        -ContentType "application/json" -Body $body
    Write-Host "  Agent: $($r.agent)"
    Write-Host "  Success: $($r.success)"
    Write-Host "  Tools: $($r.tools_used -join ', ')"
    Write-Host "  Output (preview): $($r.output.Substring(0, [Math]::Min(200, $r.output.Length)))..."
    $r.agent -eq "email" -and $r.success -eq $true
}

# ============================================================
# TESTE 3: SDK dispatch - GitHub task
# ============================================================
Test-Case "SDK Dispatch - GitHub Agent" {
    $body = @{
        task = "liste meus repositorios do GitHub"
        threshold = 0.5
    } | ConvertTo-Json

    $r = Invoke-RestMethod -Uri "$BASE/v1/agents/sdk/dispatch" -Method Post `
        -ContentType "application/json" -Body $body
    Write-Host "  Agent: $($r.agent)"
    Write-Host "  Success: $($r.success)"
    Write-Host "  Tools: $($r.tools_used -join ', ')"
    Write-Host "  Output (preview): $($r.output.Substring(0, [Math]::Min(200, $r.output.Length)))..."
    $r.agent -eq "github" -and $r.success -eq $true
}

# ============================================================
# TESTE 4: SDK dispatch - Research task
# ============================================================
Test-Case "SDK Dispatch - Research Agent" {
    $body = @{
        task = "pesquise na web sobre as eleições no Brasil em 2026"
        threshold = 0.5
    } | ConvertTo-Json

    $r = Invoke-RestMethod -Uri "$BASE/v1/agents/sdk/dispatch" -Method Post `
        -ContentType "application/json" -Body $body -TimeoutSec 120
    Write-Host "  Agent: $($r.agent)"
    Write-Host "  Success: $($r.success)"
    Write-Host "  Tools: $($r.tools_used -join ', ')"
    Write-Host "  Output (preview): $($r.output.Substring(0, [Math]::Min(200, $r.output.Length)))..."
    $r.agent -eq "researcher" -and $r.success -eq $true
}

# ============================================================
# TESTE 5: TaskMesh complex task (should trigger sub-agents)
# ============================================================
Test-Case "TaskMesh com Sub-Agents" {
    $body = @{
        model = "mike"
        messages = @(
            @{ role = "user"; content = "primeiro busque meus emails do Raphael, depois liste meus repositorios no GitHub" }
        )
        max_tokens = 2048
        temperature = 0.7
        stream = $false
    } | ConvertTo-Json -Depth 5

    $r = Invoke-RestMethod -Uri "$BASE/v1/chat/completions" -Method Post `
        -ContentType "application/json" -Body $body -TimeoutSec 180
    $text = $r.choices[0].message.content
    Write-Host "  Response length: $($text.Length)"
    Write-Host "  Tool calls: $($r.usage.tool_calls.Count)"
    Write-Host "  Preview: $($text.Substring(0, [Math]::Min(300, $text.Length)))..."
    $text.Length -gt 50
}

# ============================================================
# TESTE 6: Agent routing precision
# ============================================================
Test-Case "Agent Routing Precision" {
    $tasks = @(
        @{ task = "envie email para marco@test.com"; expected = "email" },
        @{ task = "busque repos de Python no GitHub"; expected = "github" },
        @{ task = "liste meus eventos da agenda"; expected = "calendar" },
        @{ task = "abra a planilha de vendas"; expected = "data" },
        @{ task = "execute Get-Process no servidor remoto"; expected = "system" },
        @{ task = "busque modelos de text-generation no HuggingFace"; expected = "huggingface" }
    )

    $correct = 0
    foreach ($t in $tasks) {
        $body = @{ task = $t.task; threshold = 0.3 } | ConvertTo-Json
        try {
            $r = Invoke-RestMethod -Uri "$BASE/v1/agents/sdk/dispatch" -Method Post `
                -ContentType "application/json" -Body $body -TimeoutSec 60
            $matched = $r.agent -eq $t.expected
            $icon = if ($matched) { "OK" } else { "WRONG" }
            Write-Host "    [$icon] '$($t.task)' -> $($r.agent) (expected: $($t.expected))" -ForegroundColor $(if ($matched) { "Green" } else { "Yellow" })
            if ($matched) { $correct++ }
        } catch {
            Write-Host "    [ERR] '$($t.task)' -> $_" -ForegroundColor Red
        }
    }
    Write-Host "  Routing accuracy: $correct/$($tasks.Count)"
    $correct -ge 4  # at least 4/6 correct
}


# ============================================================
# RESULTADO FINAL
# ============================================================
Write-Host "`n" -NoNewline
Write-Host "========================================" -ForegroundColor White
Write-Host " RESULTADO: $pass PASS / $fail FAIL / $total TOTAL" -ForegroundColor $(if ($fail -eq 0) { "Green" } else { "Yellow" })
Write-Host "========================================" -ForegroundColor White
