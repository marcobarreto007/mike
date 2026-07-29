# ============================================================
# Test Suite: Skill System — SkillRegistry + DynamicSkillAgent
# ============================================================
# Testa: YAML loading, matching, composição, endpoints REST,
#         backward compatibility com agents legados
# ============================================================

$ErrorActionPreference = "Stop"
$base = "http://localhost:8080"
$pass = 0
$fail = 0
$total = 0

function Test($name, $block) {
    $script:total++
    try {
        & $block
        Write-Host "  PASS  $name" -ForegroundColor Green
        $script:pass++
    } catch {
        Write-Host "  FAIL  $name — $_" -ForegroundColor Red
        $script:fail++
    }
}

# Wait for server
$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $h = Invoke-RestMethod "$base/health" -TimeoutSec 3
        if ($h.status -eq "ok") { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}
if (-not $ready) { Write-Host "Server not ready"; exit 1 }

# ============================================================
# 1. GET /v1/skills — listar skills carregadas
# ============================================================
Test "Listar skills" {
    $r = Invoke-RestMethod "$base/v1/skills"
    if ($r.count -lt 10) { throw "Expected >=10 skills, got $($r.count)" }
    if ($r.domains.Count -lt 5) { throw "Expected >=5 domains, got $($r.domains.Count)" }
}

# ============================================================
# 2. GET /v1/skills?domain=email — filtrar por domínio
# ============================================================
Test "Filtrar skills por dominio" {
    $r = Invoke-RestMethod "$base/v1/skills?domain=email"
    if ($r.count -lt 2) { throw "Expected >=2 email skills, got $($r.count)" }
    foreach ($s in $r.skills) {
        if ($s.domain -ne "email") { throw "Skill $($s.name) has domain $($s.domain)" }
    }
}

# ============================================================
# 3. GET /v1/skills/{name} — detalhe de uma skill
# ============================================================
Test "Detalhe de skill por nome" {
    $r = Invoke-RestMethod "$base/v1/skills/email_send"
    if ($r.name -ne "email_send") { throw "Expected email_send, got $($r.name)" }
    if ($r.tools.Count -lt 1) { throw "Expected >=1 tools" }
    if (-not $r.PSObject.Properties["matched_tool_count"]) { throw "Missing matched_tool_count" }
}

# ============================================================
# 4. GET /v1/skills/packs — listar packs
# ============================================================
Test "Listar skill packs" {
    $r = Invoke-RestMethod "$base/v1/skills/packs"
    if ($r.count -lt 8) { throw "Expected >=8 packs, got $($r.count)" }
    $emailPack = $r.packs | Where-Object { $_.name -eq "email" }
    if (-not $emailPack) { throw "email pack not found" }
    if ($emailPack.resolved_skills.Count -lt 2) { throw "email pack should resolve >=2 skills" }
}

# ============================================================
# 5. POST /v1/skills/match — matching de skills por tarefa
# ============================================================
Test "Match skills por tarefa email" {
    $body = @{ task = "enviar email para marco@test.com" } | ConvertTo-Json
    $r = Invoke-RestMethod "$base/v1/skills/match" -Method POST -Body $body -ContentType "application/json"
    if ($r.count -lt 1) { throw "Expected >=1 match" }
    $top = $r.matches[0]
    if ($top.domain -ne "email") { throw "Top match domain should be email, got $($top.domain)" }
    if ($top.score -lt 0.5) { throw "Top match score too low: $($top.score)" }
}

# ============================================================
# 6. POST /v1/skills/match — matching de skills por tarefa github
# ============================================================
Test "Match skills por tarefa github" {
    $body = @{ task = "liste meus repositorios no github" } | ConvertTo-Json
    $r = Invoke-RestMethod "$base/v1/skills/match" -Method POST -Body $body -ContentType "application/json"
    if ($r.count -lt 1) { throw "Expected >=1 match" }
    $hasGithub = $false
    foreach ($m in $r.matches) {
        if ($m.domain -eq "github") { $hasGithub = $true }
    }
    if (-not $hasGithub) { throw "No github skill matched" }
}

# ============================================================
# 7. GET /v1/agents/sdk — agents agora incluem tipo dynamic
# ============================================================
Test "Agents SDK com tipo dynamic" {
    $r = Invoke-RestMethod "$base/v1/agents/sdk"
    if ($r.count -lt 9) { throw "Expected >=9 agents, got $($r.count)" }
    $dynamicAgents = @($r.agents | Where-Object { $_.type -eq "dynamic" })
    if ($dynamicAgents.Count -lt 5) { throw "Expected >=5 dynamic agents, got $($dynamicAgents.Count)" }
    # check that dynamic agents have skills field
    $first = $dynamicAgents[0]
    if (-not $first.skills) { throw "Dynamic agent missing skills field" }
}

# ============================================================
# 8. POST /v1/agents/sdk/dispatch — dispatch com skill-based agent
# ============================================================
Test "Dispatch task via skill-based agent" {
    $body = @{ task = "liste meus eventos da agenda"; threshold = 0.5 } | ConvertTo-Json
    $r = Invoke-RestMethod "$base/v1/agents/sdk/dispatch" -Method POST -Body $body -ContentType "application/json"
    if ($r.agent -ne "calendar") { throw "Expected calendar agent, got $($r.agent)" }
    if (-not $r.PSObject.Properties["success"]) { throw "Missing success field" }
}

# ============================================================
# 9. POST /v1/skills/reload — hot reload
# ============================================================
Test "Hot reload skills" {
    $r = Invoke-RestMethod "$base/v1/skills/reload" -Method POST -ContentType "application/json"
    if ($r.status -ne "ok") { throw "Expected ok, got $($r.status)" }
    if (-not $r.PSObject.Properties["total_skills"]) { throw "Missing total_skills" }
}

# ============================================================
# 10. Backward compat — legacy agent routing still works
# ============================================================
Test "Backward compat routing" {
    $body = @{ task = "busque modelos de text-generation no HuggingFace"; threshold = 0.5 } | ConvertTo-Json
    $r = Invoke-RestMethod "$base/v1/agents/sdk/dispatch" -Method POST -Body $body -ContentType "application/json"
    if ($r.agent -ne "huggingface") { throw "Expected huggingface agent, got $($r.agent)" }
}

# ============================================================
# Summary
# ============================================================
Write-Host ""
Write-Host "========================================="
Write-Host "  Skills System: $pass/$total PASS" -ForegroundColor $(if ($fail -eq 0) { "Green" } else { "Yellow" })
if ($fail -gt 0) { Write-Host "  $fail FAILED" -ForegroundColor Red }
Write-Host "========================================="
exit $fail
