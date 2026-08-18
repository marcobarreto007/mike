# Mike Orchestrator — Memória

Índice de notas para roteamento de subagentes Cursor no projeto MIKE.

## Preferências do utilizador (conversa ago/2026)

- Interesse em **custo-benefício** de modelos Cursor (Composer 2.5 vs Grok 4.6 vs frontier).
- Para **delegação**: tarefas complexas → subagentes `high`/`xhigh`; routing/status → orquestrador só.
- Stack MIKE: Qwen local `:8081`, MIKE `:8083`, RTX 2070 8GB, IQ4_XS, 100% local.

## Defaults de roteamento

| Cenário | Primeiro agente |
|---------|-----------------|
| Runtime MIKE | mike-* |
| Código app genérico multi-ficheiro | swat-lead |
| GPU/build/bench | gpu-* ou mike-offload/mike-cuda |
| Alta confiança / prova | mbj-builder → verifier → arbiter |

## Notas

- (adicionar decisões de roteamento bem-sucedidas aqui)
