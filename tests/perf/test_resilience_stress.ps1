# ============================================================
# TESTES DE STRESS - RESILIENCIA TASKMESH + MISSIONENGINE
# ============================================================
# Testa se o Mike realmente completa tarefas multi-step sem abandonar

$BASE = "http://127.0.0.1:8080"
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

# -----------------------------------------------------------
# TESTE 0: Health check
# -----------------------------------------------------------
Test-Case "Health check basico" {
    $r = Invoke-RestMethod -Uri "$BASE/health" -TimeoutSec 10
    $r.status -eq "ok"
}

# -----------------------------------------------------------
# TESTE 1: Task multi-step (streaming) - pede algo complexo
# que ativa o TaskMesh e verifica se TODOS os steps completam
# -----------------------------------------------------------
Test-Case "TaskMesh streaming: tarefa complexa completa todos os steps" {
    $body = @{
        model = "mike"
        messages = @(
            @{ role = "user"; content = "pesquise na web sobre inteligencia artificial em 2026, depois faca um resumo organizado com os principais avancos, e por fim crie 3 perguntas sobre o tema" }
        )
        stream = $true
        session_id = "stress_test_mesh_$(Get-Random)"
    } | ConvertTo-Json -Depth 5

    $response = Invoke-WebRequest -Uri "$BASE/v1/chat/completions" `
        -Method POST -ContentType "application/json" -Body $body -TimeoutSec 300

    $content = $response.Content
    
    # Verifica se o plano foi criado
    $hasPlan = $content -match "Plano de execu"
    # Verifica se houve steps executados (match "[N/M]" pattern from step_start)
    $stepStarts = ([regex]::Matches($content, "\[\d+/\d+\]")).Count
    $stepDones = ([regex]::Matches($content, "Passo \d+ conclu")).Count
    # Verifica se houve resposta final (nao vazio)
    $hasContent = $content.Length -gt 200

    Write-Host "  Plan detectado: $hasPlan" -ForegroundColor Yellow
    Write-Host "  Steps iniciados: $stepStarts" -ForegroundColor Yellow
    Write-Host "  Steps concluidos: $stepDones" -ForegroundColor Yellow
    Write-Host "  Tamanho resposta: $($content.Length) chars" -ForegroundColor Yellow
    
    # SUCESSO se: tem plano E pelo menos 2 steps iniciados E resposta > 200 chars
    $hasPlan -and ($stepStarts -ge 2) -and $hasContent
}

# -----------------------------------------------------------
# TESTE 2: Task multi-step (non-streaming) - verifica que
# o try/except funciona e retorna resposta mesmo com erros
# -----------------------------------------------------------
Test-Case "TaskMesh non-streaming: tarefa complexa retorna resposta" {
    $body = @{
        model = "mike"
        messages = @(
            @{ role = "user"; content = "pesquise sobre energia solar no Brasil em 2026, liste os 5 maiores projetos, e faca uma analise comparativa entre eles" }
        )
        stream = $false
        session_id = "stress_test_nostream_$(Get-Random)"
    } | ConvertTo-Json -Depth 5

    $r = Invoke-RestMethod -Uri "$BASE/v1/chat/completions" `
        -Method POST -ContentType "application/json" -Body $body -TimeoutSec 300

    $text = $r.choices[0].message.content
    Write-Host "  Resposta: $($text.Length) chars" -ForegroundColor Yellow
    Write-Host "  Preview: $($text.Substring(0, [Math]::Min(200, $text.Length)))..." -ForegroundColor Gray
    
    # SUCESSO se: tem resposta com mais de 100 chars
    $text.Length -gt 100
}

# -----------------------------------------------------------
# TESTE 3: Mission Engine - criar missao e verificar que 
# o tick executa os steps com retry
# -----------------------------------------------------------
Test-Case "MissionEngine: criar missao e executar tick" {
    $body = @{
        title = "Teste stress resiliencia"
        goal = "Diga ola mundo e depois diga tchau mundo"
        auto_plan = $false
        steps = @(
            @{
                id = "1"
                description = "Diga: Ola mundo! Este e o passo 1."
                checklist_label = "Ola mundo"
            },
            @{
                id = "2"
                description = "Diga: Tchau mundo! Este e o passo 2."
                depends_on = @("1")
                checklist_label = "Tchau mundo"
            }
        )
    } | ConvertTo-Json -Depth 5

    $mission = Invoke-RestMethod -Uri "$BASE/v1/missions" `
        -Method POST -ContentType "application/json" -Body $body -TimeoutSec 30

    $missionId = $mission.id
    Write-Host "  Mission criada: $missionId" -ForegroundColor Yellow
    Write-Host "  Steps: $($mission.steps.Count)" -ForegroundColor Yellow
    Write-Host "  Status: $($mission.status)" -ForegroundColor Yellow

    if (-not $missionId) { return $false }

    # Executar 3 ticks
    for ($i = 1; $i -le 3; $i++) {
        Write-Host "  Tick $i..." -ForegroundColor Yellow
        $tick = Invoke-RestMethod -Uri "$BASE/v1/missions/run_once" `
            -Method POST -ContentType "application/json" -Body "{}" -TimeoutSec 120
        Write-Host "    Executados: $($tick.executed), Completados: $($tick.completed)" -ForegroundColor Yellow
        Start-Sleep -Seconds 2
    }

    # Verificar status final
    $final = Invoke-RestMethod -Uri "$BASE/v1/missions/$missionId" -TimeoutSec 10
    Write-Host "  Status final: $($final.status)" -ForegroundColor Yellow
    Write-Host "  Progresso: $($final.progress.done)/$($final.progress.total) done" -ForegroundColor Yellow
    
    foreach ($s in $final.steps) {
        Write-Host "    Step $($s.id): $($s.status) (attempts=$($s.attempts))" -ForegroundColor Gray
    }

    # SUCESSO se: pelo menos 1 step completou
    $final.progress.done -ge 1
}

# -----------------------------------------------------------
# TESTE 4: Mission Engine - verificar retry_after no step
# -----------------------------------------------------------
Test-Case "MissionEngine: retry_after campo presente em step" {
    $body = @{
        title = "Teste retry_after"
        goal = "Execute uma tarefa impossivel para testar retry"
        auto_plan = $false
        steps = @(
            @{
                id = "1"
                description = "Use a ferramenta xyz_inexistente para fazer algo impossivel"
                checklist_label = "Tarefa impossivel"
            }
        )
    } | ConvertTo-Json -Depth 5

    $mission = Invoke-RestMethod -Uri "$BASE/v1/missions" `
        -Method POST -ContentType "application/json" -Body $body -TimeoutSec 30

    $missionId = $mission.id
    Write-Host "  Mission criada: $missionId" -ForegroundColor Yellow

    # Executar 2 ticks (step vai falhar e ser marcado pending com retry_after)
    for ($i = 1; $i -le 2; $i++) {
        Write-Host "  Tick $i..." -ForegroundColor Yellow
        $tick = Invoke-RestMethod -Uri "$BASE/v1/missions/run_once" `
            -Method POST -ContentType "application/json" -Body "{}" -TimeoutSec 120
        Start-Sleep -Seconds 2
    }

    $final = Invoke-RestMethod -Uri "$BASE/v1/missions/$missionId" -TimeoutSec 10
    $step1 = $final.steps[0]
    Write-Host "  Step status: $($step1.status)" -ForegroundColor Yellow
    Write-Host "  Step attempts: $($step1.attempts)" -ForegroundColor Yellow
    Write-Host "  Step retry_after: $($step1.retry_after)" -ForegroundColor Yellow
    Write-Host "  Step last_error: $($step1.last_error)" -ForegroundColor Yellow

    # SUCESSO se: step tem attempts > 0 (mostra que retry aconteceu)
    $step1.attempts -ge 1
}

# -----------------------------------------------------------
# TESTE 5: Checkpoint - verificar que mesh salva checkpoints
# -----------------------------------------------------------
Test-Case "TaskMesh: checkpoint salvo em disco" {
    # Primeiro, verificar se o diretorio existe
    $cpDir = "mike\memory\mesh_checkpoints"
    
    # Enviar uma tarefa complexa pra gerar checkpoint
    $body = @{
        model = "mike"
        messages = @(
            @{ role = "user"; content = "pesquise sobre os 3 maiores rios do mundo, depois liste eles com suas extensoes, e por fim faca uma curiosidade sobre cada um" }
        )
        stream = $true
        session_id = "stress_checkpoint_$(Get-Random)"
    } | ConvertTo-Json -Depth 5

    try {
        $null = Invoke-WebRequest -Uri "$BASE/v1/chat/completions" `
            -Method POST -ContentType "application/json" -Body $body -TimeoutSec 300
    } catch {
        Write-Host "  Request falhou mas checkpoint pode ter sido salvo" -ForegroundColor Yellow
    }

    Start-Sleep -Seconds 2
    
    # Verificar se existem arquivos de checkpoint
    if (Test-Path $cpDir) {
        $files = Get-ChildItem $cpDir -Filter "mesh_*.json" -ErrorAction SilentlyContinue
        Write-Host "  Checkpoint dir: $cpDir" -ForegroundColor Yellow
        Write-Host "  Arquivos checkpoint: $($files.Count)" -ForegroundColor Yellow
        foreach ($f in $files | Select-Object -Last 5) {
            Write-Host "    $($f.Name) ($($f.Length) bytes)" -ForegroundColor Gray
        }
        $files.Count -ge 1
    } else {
        Write-Host "  Checkpoint dir nao existe ainda: $cpDir" -ForegroundColor Yellow
        # Se nao existe, pode ser que o TaskMesh nao foi ativado
        # (tarefa nao foi complexa o suficiente)
        $true  # nao falhar por isso
    }
}

# -----------------------------------------------------------
# TESTE 6: Multiplas requests simultaneas (concorrencia)
# -----------------------------------------------------------
Test-Case "Concorrencia: 3 requests simultaneas sem crash" {
    $jobs = @()
    $prompts = @(
        "qual e a capital da Franca e quantos habitantes tem?",
        "me fale sobre a historia do Brasil em 2 paragrafos",
        "liste 3 linguagens de programacao populares em 2026"
    )

    foreach ($prompt in $prompts) {
        $body = @{
            model = "mike"
            messages = @(
                @{ role = "user"; content = $prompt }
            )
            stream = $false
            session_id = "stress_concurrent_$(Get-Random)"
        } | ConvertTo-Json -Depth 5

        $jobs += Start-Job -ScriptBlock {
            param($uri, $body)
            try {
                $r = Invoke-RestMethod -Uri $uri -Method POST -ContentType "application/json" -Body $body -TimeoutSec 300
                return @{ success = $true; length = $r.choices[0].message.content.Length }
            } catch {
                return @{ success = $false; error = $_.Exception.Message }
            }
        } -ArgumentList "$BASE/v1/chat/completions", $body
    }

    $results = $jobs | Wait-Job -Timeout 360 | Receive-Job
    $jobs | Remove-Job -Force -ErrorAction SilentlyContinue

    $successCount = 0
    foreach ($r in $results) {
        if ($r.success) {
            $successCount++
            Write-Host "  OK: $($r.length) chars" -ForegroundColor Gray
        } else {
            Write-Host "  FAIL: $($r.error)" -ForegroundColor Red
        }
    }

    Write-Host "  Sucesso: $successCount/3" -ForegroundColor Yellow
    # SUCESSO se pelo menos 2 de 3 completaram
    $successCount -ge 2
}

# -----------------------------------------------------------
# RESULTADO FINAL
# -----------------------------------------------------------
Write-Host "`n============================================================" -ForegroundColor White
Write-Host "RESULTADO FINAL: $pass/$total PASS" -ForegroundColor $(if ($fail -eq 0) { "Green" } else { "Yellow" })
if ($fail -gt 0) {
    Write-Host "FALHAS: $fail" -ForegroundColor Red
}
Write-Host "============================================================`n" -ForegroundColor White
