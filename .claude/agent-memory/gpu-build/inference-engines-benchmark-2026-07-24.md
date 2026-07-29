---
name: inference-engines-benchmark-2026-07-24
description: Benchmark completo de motores de inferencia Windows 11 + RTX 2070 8GB — Julho 2026. Resultados do llama-bench confirmam 12.1 tok/s com flags otimizadas.
metadata:
  type: reference
---

# Motor de Inferencia — Resultados Benchmark Julho 2026

## Sistema
- GPU: NVIDIA GeForce RTX 2070, 8192 MiB, CC 7.5
- CUDA: 13.2, Driver: 596.49
- Modelo: Qwen3.6-35B-A3B-UD-IQ4_XS.gguf (16.95 GiB, IQ4_XS)

## Melhor Configuracao (llama-bench)
```
llama-bench.exe -m <model> -ngl 99 -ncmoe 99 -t 8 -b 1024 -ub 256 -fa 1 -mmp 0 -ctk f16 -ctv f16
```
- pp256: 273.5 t/s
- pp512: 272.8 t/s
- tg32: 12.1 t/s
- tg64: 10.7 t/s
- tg128: 10.8 t/s

## Flags Otimizadas
| Flag | Valor | Impacto |
|------|-------|---------|
| --no-mmap | 1 | 2x prompt processing (269 vs 137) |
| --n-cpu-moe | 99 | Obrigatorio para MoE em 8GB |
| --flash-attn | on | +2% generation, -30% VRAM attn |
| --batch-size | 1024 | Sweet spot |
| --ubatch-size | 256 | Balance memoria/velocidade |
| --threads | 8 | Melhor que 4 |
| --cache-type-k/v | f16 | Melhor tok/s (q8_0 economiza VRAM) |

## Problema --no-mmap + --mlock
Combinacao falha com OOM (16 GB alloc). Usar apenas --no-mmap sem --mlock.

## Problema Qwen CoT
O modelo gera ~70% tokens invisiveis (thinking). Tok/s visivel parece 2-3 mas real e 10-12.

## Motores Testados
- **llama.cpp**: FUNCIONAL, 12.1 tok/s, recomendado
- **Ollama v0.32.1**: Instalado, sem modelos, nao otimizado para MoE
- **mistral.rs**: Nao instalado, requer compilacao Rust
- **vLLM**: INCOMPATIVEL (Linux/WSL2 apenas)
- **Unsloth**: Nao e motor de inferencia
- **GPT4All**: Obsoleto, nao suporta MoE
- **LM Studio**: Nao instalado, usa llama.cpp backend
