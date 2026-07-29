# Script para configurar o Claude Code com a API do DeepSeek
# Uso: .\setup_claude_deepseek.ps1 [-ApiKey "sua-chave-aqui"]

param (
    [Parameter(Mandatory=$true, HelpMessage="DeepSeek API key")]
    [string]$ApiKey
)

$envVars = @{
    "ANTHROPIC_BASE_URL" = "https://api.deepseek.com/anthropic"
    "ANTHROPIC_AUTH_TOKEN" = $ApiKey
    "ANTHROPIC_MODEL" = "deepseek-v4-pro"
    "ANTHROPIC_DEFAULT_OPUS_MODEL" = "deepseek-v4-pro"
    "ANTHROPIC_DEFAULT_SONNET_MODEL" = "deepseek-v4-pro"
    "ANTHROPIC_DEFAULT_HAIKU_MODEL" = "deepseek-v4-pro"
    "CLAUDE_CODE_SUBAGENT_MODEL" = "deepseek-v4-pro"
    "CLAUDE_CODE_EFFORT_LEVEL" = "max"
}

Write-Host "Configurando variáveis de ambiente do Claude Code (DeepSeek v4 pro)..." -ForegroundColor Cyan

foreach ($name in $envVars.Keys) {
    $value = $envVars[$name]
    [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
    [System.Environment]::SetEnvironmentVariable($name, $value, "User")
    try {
        [System.Environment]::SetEnvironmentVariable($name, $value, "Machine")
    } catch {
        # Em caso de falha por permissão de Admin, prossegue com User
    }
    Write-Host "  $name = $value"
}

Write-Host "`nPronto! Claude Code configurado com DeepSeek v4 pro." -ForegroundColor Green

