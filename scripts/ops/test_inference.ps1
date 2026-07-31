# Teste de inferência do Qwen já carregado pelo launcher.

param(
    [string]$BaseUrl = "http://127.0.0.1:8081",
    [string]$Prompt = "Responda exatamente com MIKE_LOCAL_OK e nada mais.",
    [int]$MaxTokens = 64
)

$ErrorActionPreference = "Stop"

$health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 10
if ($health.status -ne "ok") {
    throw "Qwen health check failed at $BaseUrl"
}

$models = Invoke-RestMethod -Uri "$BaseUrl/v1/models" -TimeoutSec 15
$modelId = @($models.data)[0].id
if ([string]::IsNullOrWhiteSpace($modelId)) {
    throw "Qwen returned no model ID."
}

$payload = @{
    model = $modelId
    messages = @(
        @{
            role = "user"
            content = $Prompt
        }
    )
    temperature = 0
    max_tokens = $MaxTokens
    stream = $false
    chat_template_kwargs = @{
        enable_thinking = $false
    }
} | ConvertTo-Json -Depth 8

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$response = Invoke-RestMethod `
    -Uri "$BaseUrl/v1/chat/completions" `
    -Method Post `
    -ContentType "application/json" `
    -Body $payload `
    -TimeoutSec 120
$stopwatch.Stop()

$answer = [string]$response.choices[0].message.content
if ([string]::IsNullOrWhiteSpace($answer)) {
    throw "Qwen returned an empty completion."
}

[pscustomobject]@{
    status = "ok"
    model = $response.model
    answer = $answer
    prompt_tokens = $response.usage.prompt_tokens
    completion_tokens = $response.usage.completion_tokens
    elapsed_sec = [math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
    predicted_tokens_per_second = $response.timings.predicted_per_second
} | ConvertTo-Json -Depth 5
