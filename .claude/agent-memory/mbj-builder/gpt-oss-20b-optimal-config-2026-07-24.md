---
name: gpt-oss-20b-optimal-config-2026-07-24
description: Configuracao otima do llama-server para GPT-OSS-20B IQ4_NL (12GB) na RTX 2070 8GB + Xeon E5-1630 v4 (4c/8t) + 40GB RAM + Windows 11. Foco em maxima velocidade de geracao de tokens.
metadata:
  type: reference
---

# GPT-OSS-20B Optimal llama-server Configuration

Determinado em 2026-07-24 por MBJ-Builder (Maker stage).

## Modelo
- Arquitetura: GPT-OSS-20B, MoE, 24 camadas
- Experts: 32 por camada, 4 ativos por token
- Embedding dim: 2880, GQA 64/8 cabecas
- Quant: IQ4_NL (~4.5 bpw, 12 GB ficheiro, ~10.6 GB pesos)
- Contexto nativo: 4096, suporta ate 131072 via YaRN

## Hardware
- GPU: RTX 2070 8GB (8191 MiB, 7154 MiB livres)
- CPU: Xeon E5-1630 v4 (4 nucleos / 8 threads, Broadwell, AVX2)
- RAM: 40 GB (23.5 GB livres)
- OS: Windows 11 Pro

## Estrategia de Offloading (MoE)
O modelo tem 24 camadas, todas MoE com 32 experts cada. Os pesos dos experts ocupam ~9-10 GB
(~85% do modelo). As partes densas (atencao, normas, embeddings) ocupam ~1.5-2 GB.

Estrategia: **n_cpu_moe=99 (TODOS os experts em RAM/CPU), todas as camadas densas na GPU.**
Isto e a abordagem validada pela comunidade: manter experts na CPU com computacao em CPU,
enquanto a GPU processa atencao e camadas densas.

VRAM usado: ~2.5 GB (modelo denso) + ~200 MB (KV cache q8_0, ctx=8192) + ~300 MB (overhead CUDA)
= ~3.0 GB total. Sobram ~4 GB para outros usos.

## Comando Otimo
```
C:\Users\Admin\Desktop\mike\llama.cpp\build\bin\llama-server.exe \
  --model "C:\Users\Admin\Desktop\mike\llm_cache\gpt-oss-20b-abliterated\OpenAI-20B-NEO-Uncensored2-IQ4_NL.gguf" \
  --host 127.0.0.1 \
  --port 8081 \
  --n-gpu-layers 999 \
  --n-cpu-moe 99 \
  --ctx-size 8192 \
  --threads 6 \
  --threads-batch 6 \
  --batch-size 1024 \
  --ubatch-size 512 \
  --flash-attn on \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --parallel 1 \
  --cont-batching \
  --no-warmup \
  --mlock \
  --no-mmap \
  --poll 100 \
  --prio 2
```

## Performance Estimada
- TG (geracao): 25-35 tok/s (ctx=4096), 22-30 tok/s (ctx=8192), 18-25 tok/s (ctx=16384)
- PP (processamento prompt): 70-90 tok/s
- TTFT: <1s (ctx curto), <3s (ctx=8192)
- VRAM: ~3.0 GB usados, ~4 GB livres
- RAM: ~14-16 GB usados (modelo 12 GB + overhead)
- Temp GPU: 65-72C (estimado)
