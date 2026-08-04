# Hardware do MIKE (máquina atual)

Atualizado em **3 de agosto de 2026** a partir de probes reais (`nvidia-smi`, WMI).

## Inventário

| Componente | Valor |
|---|---|
| CPU | Intel Core i7-12700K (12c / 20t) |
| RAM | 64 GB (~43 GB livres no momento da probe) |
| GPU 0 | NVIDIA GeForce RTX 5060 Ti — 16311 MiB — compute 12.0 |
| GPU 1 | NVIDIA GeForce RTX 3060 — 12288 MiB — compute 8.6 |
| Driver | 595.97 |
| CUDA Toolkit | v12.8 (`C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8`) |
| Disco C: | ~360 GB livres / ~948 GB |
| SO | Windows |

## O que isso muda em relação à máquina antiga

A máquina validada antiga era **RTX 2070 8 GB + ~40 GB RAM** (offload pesado
`n_cpu_moe=99`). Esta máquina tem **~28 GB VRAM combinada** e **64 GB RAM**.

| Item | Antiga (2070) | Atual (5060 Ti + 3060) |
|---|---|---|
| VRAM principal | 8 GB | 16 GB (GPU0) |
| Segunda GPU | P106 / nenhuma estável | 3060 12 GB |
| Perfil MoE | hybrid CPU/GPU agressivo | dual-GPU preferencial |
| Quant Qwen3.6 recomendada | UD-IQ4_XS (~18 GB) hybrid | UD-Q3_K_M / UD-IQ4_XS com tensor-split |
| Qwen3.8-Max 2.4T local | impossível | ainda impossível (datacenter) |
| Qwen3.8-27B local (quando open weights) | apertado | candidato forte em Q3/Q4 |

## Perfil de inferência recomendado (Qwen3.6-35B-A3B)

### Preferido — dual GPU

```text
CUDA_VISIBLE_DEVICES=0,1
--n-gpu-layers 999
--n-cpu-moe 0
--tensor-split 16,12
--ctx-size 16384
--flash-attn on
--cache-type-k q8_0
--cache-type-v q8_0
--threads 12
```

Quant sugerida:

1. `UD-Q3_K_M` (~16.6 GB) — melhor qualidade que ainda cabe no par 16+12;
2. `UD-IQ4_XS` (~17.7 GB) — qualidade validada no projeto, agora com bem menos CPU;
3. `UD-Q2_K_XL` (~12.3 GB) — se quiser margem de KV/contexto maior só na 5060 Ti.

### Fallback — só GPU0 (5060 Ti)

```text
CUDA_VISIBLE_DEVICES=0
--n-gpu-layers 999
--n-cpu-moe 0   # se a quant couber
# ou --n-cpu-moe 8..32 se a quant/contexto estourar
```

## Qwen 3.8 Max — o que cabe aqui

| Artefato | Status nesta máquina |
|---|---|
| `qwen3.8-max` API (cloud) | Sim — OpenAI/DashScope compatible |
| Open weights 2.4T | Não para desktop; multi-node |
| `Qwen3.8-27B` open weights | Caminho local realista (aguardar release + GGUF) |

Estratégia MIKE:

1. Manter cérebro local Qwen3.6-35B-A3B no par 5060 Ti + 3060;
2. Quando sair GGUF do 3.8-27B, benchmarkar no mesmo launcher;
3. Opcional: backend cloud `qwen3.8-max` só para tarefas hard (coding/agentic),
   sem abandonar o single-brain local no dia a dia.

## Portas e serviços observados

| Porta | Estado na probe |
|---|---|
| 8081 (Qwen MIKE) | down |
| 8083 (API MIKE) | down |
| 11434 (Ollama) | up — **não** é o cérebro oficial do MIKE |

## Próximos passos de bootstrap neste PC

1. Criar `.venv` + `pip install -r config\requirements.txt`
2. Build/obter `llama.cpp\build\bin\llama-server.exe` com CUDA SM 8.6 + 12.0
3. Baixar quant recomendada: `.\scripts\ops\download_model.ps1 -Quant UD-Q3_K_M`
4. Ajustar `config\.env.runtime` (`MIKE_MODEL_FILE`, URLs 8081/8083)
5. Subir: `.\scripts\ops\launch_mike.ps1 -SkipTunnel`
