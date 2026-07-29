# Test battery for Mike Mission Engine

$BASE = "http://localhost:8080"
$pass = 0
$fail = 0
$total = 0
$missionId = $null
$scheduledMissionId = $null

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

Test-Case "Criar missao com checklist" {
    $body = @{
        title = "Missao teste GitHub"
        goal = "Listar repositorios e resumir"
        auto_plan = $false
        steps = @(
            @{
                id = "1"
                description = "liste meus repositorios no GitHub"
                agent_hint = "github"
                checklist_label = "Listar repositorios"
            },
            @{
                id = "2"
                description = "faca um resumo curto do que foi encontrado"
                depends_on = @("1")
                checklist_label = "Gerar resumo"
            }
        )
        metadata = @{
            source = "test_mission_engine.ps1"
        }
    } | ConvertTo-Json -Depth 8

    $r = Invoke-RestMethod -Uri "$BASE/v1/missions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 120
    $script:missionId = $r.id
    Write-Host "  Mission ID: $($r.id)"
    Write-Host "  Status: $($r.status)"
    Write-Host "  Steps: $($r.steps.Count)"
    ($r.id) -and ($r.steps.Count -ge 2) -and ($r.checklist.Count -ge 2)
}

Test-Case "Buscar missao por ID" {
    if (-not $script:missionId) { return $false }
    $r = Invoke-RestMethod -Uri "$BASE/v1/missions/$($script:missionId)" -Method Get -TimeoutSec 60
    Write-Host "  Title: $($r.title)"
    Write-Host "  Progress: $($r.progress.done)/$($r.progress.total)"
    ($r.id -eq $script:missionId) -and ($r.progress.total -ge 2)
}

Test-Case "Executar tick manual" {
    $r = Invoke-RestMethod -Uri "$BASE/v1/missions/run_once" -Method Post -ContentType "application/json" -Body "{}" -TimeoutSec 180
    Write-Host "  Tick executed: $($r.tick.executed)"
    Write-Host "  Tick completed: $($r.tick.completed)"
    $r.status -eq "ok"
}

Test-Case "Ver progresso apos ticks" {
    if (-not $script:missionId) { return $false }

    # Multiple ticks to allow sequential steps to run
    for ($i = 0; $i -lt 3; $i++) {
        Invoke-RestMethod -Uri "$BASE/v1/missions/run_once" -Method Post -ContentType "application/json" -Body "{}" -TimeoutSec 180 | Out-Null
    }

    $r = Invoke-RestMethod -Uri "$BASE/v1/missions/$($script:missionId)" -Method Get -TimeoutSec 60
    Write-Host "  Status: $($r.status)"
    Write-Host "  Done: $($r.progress.done)/$($r.progress.total)"
    $r.progress.done -ge 1
}

Test-Case "Criar missao agendada e cancelar" {
    $future = (Get-Date).ToUniversalTime().AddDays(1).ToString("o")
    $body = @{
        title = "Missao agendada de teste"
        goal = "Executar amanha"
        trigger_at = $future
        auto_plan = $false
        steps = @(
            @{
                id = "1"
                description = "liste meus eventos de hoje"
                agent_hint = "calendar"
            }
        )
    } | ConvertTo-Json -Depth 6

    $created = Invoke-RestMethod -Uri "$BASE/v1/missions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 120
    $script:scheduledMissionId = $created.id
    Write-Host "  Scheduled mission ID: $($created.id)"
    Write-Host "  Status: $($created.status)"

    $cancelBody = @{ reason = "teste concluido" } | ConvertTo-Json
    $cancelled = Invoke-RestMethod -Uri "$BASE/v1/missions/$($script:scheduledMissionId)/cancel" -Method Post -ContentType "application/json" -Body $cancelBody -TimeoutSec 60
    Write-Host "  Cancelled status: $($cancelled.status)"

    ($created.status -eq "scheduled") -and ($cancelled.status -eq "cancelled")
}

Write-Host "`n========================================"
Write-Host " RESULTADO: $pass PASS / $fail FAIL / $total TOTAL"
Write-Host "========================================"

if ($fail -gt 0) { exit 1 }
exit 0
