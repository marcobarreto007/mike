# Testes TaskMesh complexos (parte 2 da bateria)
$baseUrl = "http://localhost:8080"
$headers = @{ "Content-Type" = "application/json" }

function Send-Chat {
    param([string]$Message, [string]$SessionId = "test-mesh", [int]$TimeoutSec = 300)
    $body = @{
        model    = "mike"
        messages = @(@{ role = "user"; content = $Message })
        stream   = $false
        session_id = $SessionId
        max_tokens = 2048
    } | ConvertTo-Json -Depth 5
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-RestMethod -Uri "$baseUrl/v1/chat/completions" -Method POST -Headers $headers -Body $body -TimeoutSec $TimeoutSec
        $sw.Stop()
        $text = $resp.choices[0].message.content
        return @{ ok = $true; text = $text; time_s = [math]::Round($sw.Elapsed.TotalSeconds, 1); tokens = $resp.usage.completion_tokens }
    } catch {
        $sw.Stop()
        return @{ ok = $false; text = $_.Exception.Message; time_s = [math]::Round($sw.Elapsed.TotalSeconds, 1); tokens = 0 }
    }
}

function Show-Result {
    param([int]$Num, [string]$Name, [hashtable]$R)
    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor Cyan
    Write-Host "  TESTE $Num : $Name" -ForegroundColor Yellow
    Write-Host ("=" * 70) -ForegroundColor Cyan
    if ($R.ok) {
        Write-Host "  Tempo: $($R.time_s)s | Tokens: $($R.tokens)" -ForegroundColor Gray
        $preview = $R.text
        if ($preview.Length -gt 500) { $preview = $preview.Substring(0, 500) + "..." }
        Write-Host "  Resposta:" -ForegroundColor White
        Write-Host $preview -ForegroundColor White
        Write-Host "  STATUS: OK" -ForegroundColor Green
    } else {
        Write-Host "  ERRO: $($R.text)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "###  TESTES TASKMESH COMPLEXOS  ###" -ForegroundColor Magenta
Write-Host ""

# TESTE 9: Decomposicao inbox + resumo
$r9 = Send-Chat -Message "Faz uma revisao do meu inbox. Lista os emails recentes e me da um resumo do que e importante." -SessionId "mesh-9"
Show-Result -Num 9 -Name "Decomposicao: inbox + resumo" -R $r9

# TESTE 10: Buscar pessoa + ler email completo
$r10 = Send-Chat -Message "Encontra o email mais recente de uma pessoa real e me mostra o conteudo completo dele." -SessionId "mesh-10"
Show-Result -Num 10 -Name "Buscar pessoa + ler email" -R $r10

# TESTE 11: TaskMesh - listar pessoas reais dos ultimos 10 dias
$r11 = Send-Chat -Message "Liste TODAS as pessoas reais que me mandaram email nos ultimos 10 dias. Filtre: so nomes de pessoas, sem empresas, newsletters ou sistemas automaticos." -SessionId "mesh-11"
Show-Result -Num 11 -Name "TaskMesh: listar pessoas 10 dias" -R $r11

# TESTE 12: Cross-tool email + web
$r12 = Send-Chat -Message "Verifica meus emails recentes e identifica o mais importante. Depois faz uma busca na web sobre o assunto desse email pra me dar contexto." -SessionId "mesh-12"
Show-Result -Num 12 -Name "TaskMesh: cross-tool email+web" -R $r12

# TESTE 13: Relatorio completo
$r13 = Send-Chat -Message "Faz um relatorio completo do meu inbox dos ultimos 7 dias com: quantos emails recebi, lista de remetentes unicos, os 3 mais importantes com resumo, e emails que precisam de resposta urgente." -SessionId "mesh-13"
Show-Result -Num 13 -Name "TaskMesh: relatorio completo" -R $r13

# TESTE 14: Multi-pessoa search
$r14 = Send-Chat -Message "Busca separadamente emails do rapha, da ana, do frederico e do marcelo nos ultimos 30 dias. Me diz de quem voce encontrou emails e de quem nao." -SessionId "mesh-14"
Show-Result -Num 14 -Name "TaskMesh: multi-pessoa search" -R $r14

Write-Host ""
Write-Host "###  FIM DOS TESTES TASKMESH  ###" -ForegroundColor Magenta
Write-Host ""

# Resumo
$all = @($r9, $r10, $r11, $r12, $r13, $r14)
$okCount = ($all | Where-Object { $_.ok }).Count
$totalTime = 0
foreach ($r in $all) { $totalTime += $r.time_s }
Write-Host "Resultado: $okCount/6 responderam OK | Tempo total: $([math]::Round($totalTime, 0))s" -ForegroundColor $(if ($okCount -eq 6) { "Green" } else { "Yellow" })
