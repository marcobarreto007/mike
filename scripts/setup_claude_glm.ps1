# Configura o Claude Code para o backend GLM (glm-5.2) via bigmodel.cn
# Resolve o erro 1211 (模型不存在 / "model does not exist") que parte TODOS os subagentes
# (SWAT, specialists, general-purpose) quando o processo arranca com OPUS/SONNET/HAIKU/SUBAGENT
# ainda apontados para deepseek-v4-pro.
#
# Uso:  .\scripts\setup_claude_glm.ps1
# Depois: FECHA e REABRE o Claude Code (o novo processo tem de herdar estes valores).
# Finalmente a frota SWAT inteira fica operante para "ataca em paralelo".

$baseUrl = "https://open.bigmodel.cn/api/anthropic"

# Preserva o token ja configurado (nao hardcodear por seguranca)
$token = [System.Environment]::GetEnvironmentVariable("ANTHROPIC_AUTH_TOKEN", "User")
if (-not $token) { $token = $env:ANTHROPIC_AUTH_TOKEN }
if (-not $token) {
    Write-Warning "ANTHROPIC_AUTH_TOKEN nao encontrado. Define primeiro:"
    Write-Warning '  setx ANTHROPIC_AUTH_TOKEN "<a-tua-key-bigmodel>"'
    Write-Warning "e volta a correr este script."
    exit 1
}

$envVars = [ordered]@{
    "ANTHROPIC_BASE_URL"             = $baseUrl
    "ANTHROPIC_AUTH_TOKEN"           = $token
    "ANTHROPIC_MODEL"                = "glm-5.2"
    "ANTHROPIC_DEFAULT_OPUS_MODEL"   = "glm-5.2"
    "ANTHROPIC_DEFAULT_SONNET_MODEL" = "glm-5.2"
    "ANTHROPIC_DEFAULT_HAIKU_MODEL"  = "glm-5.2"
    "CLAUDE_CODE_SUBAGENT_MODEL"     = "glm-5.2"
}

Write-Host "Configurando Claude Code para GLM (glm-5.2) em $baseUrl..." -ForegroundColor Cyan
foreach ($name in $envVars.Keys) {
    $value = $envVars[$name]
    [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
    [System.Environment]::SetEnvironmentVariable($name, $value, "User")
    try { [System.Environment]::SetEnvironmentVariable($name, $value, "Machine") } catch { }
    $shown = if ($name -like "*TOKEN*") { "***" } else { $value }
    Write-Host ("  {0} = {1}" -f $name, $shown)
}

Write-Host ""
Write-Host "Pronto. AGORA REINICIA o Claude Code (fecha e reabre o terminal/app)." -ForegroundColor Green
Write-Host "So assim o novo processo herda glm-5.2 em TODOS os modelos (incl. subagentes)." -ForegroundColor Green
Write-Host "Depois do restart, a frota SWAT (swat-architect, swat-debug, ...) fica 100%% operante." -ForegroundColor Green
