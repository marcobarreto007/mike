---
name: alternative-engines-2026
description: Pesquisa completa de motores de inferencia alternativos ao llama.cpp para RTX 2070 8GB — Julho 2026. Dados de GitHub, READMEs, releases, HuggingFace.
metadata:
  type: reference
---

# Motores de Inferencia Alternativos — RTX 2070 8GB + Qwen3.6-35B-A3B IQ4_XS

Pesquisa realizada em 2026-07-24. Fontes: GitHub Releases, READMEs, HuggingFace API, Reddit r/LocalLLaMA (via [[community-benchmarks-2025-2026]]).

## Baseline: llama.cpp (b10107, 2026-07-24)

- **TG tok/s**: 22-27 (IQ4_XS, moe=99, FA=on, ctx=4096)
- **PP tok/s**: 62-78
- **VRAM**: 4.8 GB
- **Windows**: Builds nativos CUDA para Windows
- **Formato**: GGUF nativo
- **Estado**: Ativo, commits diarios, comunidade massiva
- **Verificado no MIKE**: Sim, 24.5 tok/s medido

---

## 1. mistral.rs v0.9.0 (2026-07-07)

**GitHub**: EricLBuehler/mistral.rs | **Stars**: ~15k+ | **Linguagem**: Rust

### Performance Reportada
| Modelo | Hardware | mistral.rs | llama.cpp | Delta |
|--------|----------|------------|-----------|-------|
| Gemma 4 E4B Q8 | GB10 | 44.1 tok/s | 40.5 tok/s | +8.9% |
| Gemma 4 E4B Q8 | B200 | 241.4 tok/s | 194.4 tok/s | +24.2% |
| Gemma 4 26B-A4B Q8 | GB10 | 46.8 tok/s | 46.4 tok/s | +0.9% |
| Gemma 4 E4B BF16 | GB10 | 25.1 tok/s | - | vs vLLM: +33.5% |

**Nota**: Benchmarks oficiais SAO para GB10 (DGX Spark), B200, H100 SXM — zero dados para Turing/consumer.

### Compatibilidade
- **Turing (SM 7.5)**: Sem builds prebuilt. Assets sao sm100 (Blackwell) e sm120 apenas.
- **Windows**: Suporte PowerShell installer, mas binario Windows e CPU-only. CUDA requer Linux.
- **GGUF**: Suportado. UQFF como formato nativo.
- **MoE**: Suporte via AnyMoE + modelos LFM 2.5, Hunyuan v1 MoE.
- **Flash Attention**: V2/V3 suportado.

### Recursos Notaveis
- `mistralrs tune` recomenda quant/device mapping automaticamente
- ISQ (In-Situ Quantization) — quantiza qualquer modelo HF on-the-fly
- Per-layer topology: quantizacao diferente por layer
- OpenAI + Anthropic API simultaneas
- Agentes nativos: web search, code exec, shell, MCP client

### Veredito para RTX 2070 8GB
**NAO RECOMENDADO**. Sem builds CUDA para Windows. Binario Windows e CPU-only. Precisaria compilar do source no Linux/WSL2. Benchmarks so em hardware datacenter. Muito risco de incompatibilidade com Turing.

**Custo de entrada**: Alto (compilar Rust + CUDA do source, debug de compatibilidade)
**Potencial teorico**: +5-25% vs llama.cpp em hardware suportado
**Risco**: Nao testado em Turing — pode nem compilar para SM 7.5

---

## 2. vLLM v0.25.1 (2026-07-14)

**GitHub**: vllm-project/vllm | **Stars**: ~70k+ | **Linguagem**: Python/C++/CUDA

### Performance Reportada
- Benchmarks oficiais apenas em A100/H100/B200
- FlashInfer sparse index cache: 2-4% TTFT improvement
- DeepSeek-V4: FlashInfer MoE kernels com prefill optimization 4% E2E
- Sem dados para Turing ou GPUs < 24GB

### Compatibilidade
- **Turing (SM 7.5)**: Possivel mas nao testado. FlashInfer kernels requerem SM 8.0+ tipicamente.
- **Windows**: NAO suportado nativamente. WSL2 apenas.
- **GGUF**: Suporte limitado — foco em safetensors/HF.
- **MoE**: Excelente — DeepSeek-V4, MiniMax-M3, GraniteMoE, GLM-5 MoE.
- **Flash Attention**: FlashInfer como backend padrao desde v0.25.
- **VRAM**: Por design toma 92% da VRAM — pessimo para 8GB.

### Veredito para RTX 2070 8GB
**NAO RECOMENDADO**. Motor de servidor — assume GPUs 24GB+ com batching continuo. 92% VRAM usage default eliminaria qualquer margem para contexto. Sem Windows nativo. Complexidade de configuracao extremamente alta para uso single-user.

**Custo de entrada**: Altissimo (setup cluster-level para single GPU consumer)
**Potencial**: Teoricamente otimo para MoE (FlashInfer kernels) mas inacessivel
**Risco**: Desenvolvimento focado em datacenter — consumer GPU e afterthought

---

## 3. Sonar (ex-Aphrodite Engine) v0.22.0 (2026-07-19)

**GitHub**: dphnAI/sonar (era aphrodite-engine/aphrodite-engine) | **Fork de**: vLLM

### Compatibilidade
- **Turing (SM 7.5)**: DeepSeek Sparse Attention kernels para sm89 (Ada) mencionados. Nada para sm75.
- **Windows**: WSL2 apenas.
- **GGUF**: Listado como suportado.
- **ExLlamaV3**: Suportado como backend alternativo.
- **VRAM**: Mesmo comportamento do vLLM — 92% default.

### Diferenciais vs vLLM
- Metal (Apple Silicon) support nativo — unico motor server com isso
- Samplers avancados: DRY, XTC, Mirostat
- TurboQuant para KV cache
- Foco historico em GPUs menores, mas migrando para datacenter

### Veredito para RTX 2070 8GB
**NAO RECOMENDADO**. Mesmo que vLLM, herdou todas as limitacoes. Rebrand para "Sonar" indica pivot para escala. Melhor que vLLM para Apple Silicon, irrelevante para Turing.

---

## 4. ExLlamaV3 v1.1.0 (2026-07-18)

**GitHub**: turboderp-org/exllamav3 | **Linguagem**: Python/C++/CUDA

**Nota**: ExLlamaV2 foi ARQUIVADO. Desenvolvimento continua no V3.

### Performance Reportada (V2, benchmarks antigos)
| Modelo | Formato | GPU | tok/s |
|--------|---------|-----|-------|
| Llama 7B | EXL2 3.0bpw | 3090Ti | 217 |
| Llama 7B | EXL2 4.0bpw | 4090 | 211 |
| Llama 70B | EXL2 2.5bpw | 3090Ti | 33 |
| CodeLlama 34B | EXL2 4.0bpw | 3090Ti | 44 |

### V3 Novidades (v1.0.0, 2026-07-14)
- **MoE kernel com ticket scheduler** — otimizacao especifica para MoE
- INT8 GEMV kernel
- GEMM/GEMV melhorado em **Ampere** (SM 8.0/8.6)
- Graph path para attention/GDN modules
- Fused sampling kernels
- MTP (Multi-Token Prediction) drafting
- Suporte: GptOss, NemotronH, HYV3, Gemma4

### Compatibilidade
- **Turing (SM 7.5)**: NAO TESTADO. "Ampere" explicitamente mencionado para melhorias GEMM. V2 historicamente focava em GPUs 24GB+.
- **Windows**: Build from source com Visual Studio + CUDA Toolkit. Sem binaries prebuilt.
- **GGUF**: NAO SUPORTADO. ExLlama usa formato EXL2 proprietario.
- **MoE**: V3 adicionou kernel MoE, mas sem dados de performance em consumer.
- **Flash Attention**: Kernel proprio — removeu dependencia de flash-attention-2 e xformers.

### Veredito para RTX 2070 8GB
**NAO RECOMENDADO**. Motor mais rapido do mundo para GPUs high-end (3090/4090), mas:
- EXL2 apenas — Qwen3.6-35B-A3B precisaria ser convertido (ninguem fez ainda)
- Foco em Ampere+ (SM 8.0+)
- Sem testes em Turing
- 8GB VRAM e apertado para EXL2 (formato menos compacto que GGUF IQ)

**Custo de entrada**: Altissimo (conversao de modelo + build from source)
**Potencial teorico**: Se funcionasse, provavelmente o mais rapido — mas nao funciona
**Risco**: Incompativel com Turing — otimizacoes assumem SM 8.0+

---

## 5. Ollama v0.32.3 (2026-07-23)

**GitHub**: ollama/ollama | **Stars**: ~130k+ | **Backend**: llama.cpp

### Performance
- Usa llama.cpp como backend — performance identica ao llama.cpp puro
- Overhead adicional: ~2-5% (camada de API/servidor)
- Flash attention habilitado para GPUs antigas (CC 6.x) desde v0.31.2

### Compatibilidade
- **Turing (SM 7.5)**: SIM. Flash attention em CC 6.x+.
- **Windows**: SIM. Instalador nativo. CUDA Windows ARM64 desde v0.32.3.
- **GGUF**: SIM. Formato nativo do Ollama.
- **MoE**: SIM (via llama.cpp backend).
- **Modelos**: Qwen3.6 disponivel como GGUF import.

### Diferenciais
- Zero config — `ollama run qwen3.6:35b` (se disponivel no registry)
- API OpenAI-compatible integrada
- Agent mode interativo (v0.32.0+)
- Claude Code / Codex integration (`ollama launch`)
- MLX backend para Apple Silicon

### Veredito para RTX 2070 8GB
**VIAVEL COMO ALTERNATIVA SIMPLES**. Se o llama.cpp ja funciona, Ollama funciona identicamente. Principal vantagem: UX (zero config, API automatica, updates). Principal desvantagem: menos controle sobre parametros avancados (n_cpu_moe, threads, batch size, etc.). Overhead de 2-5% vs llama.cpp puro.

**Para quem**: Usuarios que priorizam simplicidade sobre micro-otimizacao.
**Nao recomendado para**: Quem precisa de n_cpu_moe=99 e controle fino de VRAM.

---

## 6. Unsloth Studio v0.1.501-beta (2026-07-20)

**GitHub**: unslothai/unsloth | **Backend**: llama.cpp + MLX + transformers

### Performance
- Inferencia: usa llama.cpp como backend — performance identica
- **MoE offloading**: Suporte nativo na UI: "offload MoE experts"
- **GGUF hardware controls**: Escolher GPU/layers, offload MoE, multi-GPU, Tensor Parallelism

### Compatibilidade
- **Turing (SM 7.5)**: SIM, via llama.cpp backend. NVIDIA training so em RTX 30/40/50.
- **Windows**: SIM. Instalador PowerShell nativo. `irm https://unsloth.ai/install.ps1 | iex`
- **GGUF**: SIM. Search + download + run GGUF integrado.
- **MoE**: SIM. "Move MoE expert layers into system memory" — mesmo que n_cpu_moe.
- **Vulkan**: Suporte para Intel GPUs.

### Diferenciais
- Web UI completa (Studio)
- Model Arena: comparar 2 modelos lado a lado
- Auto inference settings
- Export models: GGUF, safetensors, FP8
- OpenAI/Anthropic API endpoints
- `unsloth start claude` — conecta modelos locais ao Claude Code
- Self-healing tool calling
- MCP control endpoint

### Veredito para RTX 2070 8GB
**RECOMENDADO COMO WRAPPER**. Nao e um motor mais rapido que llama.cpp (usa o mesmo backend), mas e a melhor UX para gerenciar configuracoes MoE. O MoE offloading na UI e exatamente o n_cpu_moe que o MIKE ja usa. A API OpenAI-compatible integrada e o `unsloth start claude` sao killer features.

**Vantagem sobre llama.cpp puro**: UX, gerenciamento de modelos, API integrada, auto-config
**Desvantagem**: Overhead da UI (~200-500MB RAM extra), menos controle granular
**Performance inferencia**: Identica ao llama.cpp (mesmo backend)

---

## 7. CTranslate2 v4.8.1 (2026-07-03)

**GitHub**: OpenNMT/CTranslate2 | **Linguagem**: C++/Python | **Foco**: CPU

### Performance
- Motor CPU-first — otimizado para Intel MKL, ARM, AMD Zen (zentorch)
- Suporte CUDA limitado a GEMM basico
- Sem kernels MoE
- Performance em GPU inferior ao llama.cpp

### Compatibilidade
- **Turing (SM 7.5)**: CUDA suportado mas nao otimizado
- **Windows**: SIM. CI/CD com compilacao Windows.
- **GGUF**: Suporte parcial (modelos CT2 usam formato proprio convertido)
- **MoE**: NAO. Sem suporte a modelos MoE (Gemma4 dense apenas).

### Veredito para RTX 2070 8GB
**NAO RECOMENDADO**. Motor CPU-first. Performance GPU inferior ao llama.cpp. Sem MoE. Foco em tradução/sequence-to-sequence, nao em LLMs generativos.

---

## 8. PowerInfer

**GitHub**: SJTU-IPADS/PowerInfer | **Estado**: ABANDONADO

- Sem releases (API retorna array vazio)
- Projeto de pesquisa academico de 2023-2024
- Foco em consumer GPU + CPU hybrid inference com sparsity
- Nao mantido — incompativel com modelos 2025-2026

### Veredito para RTX 2070 8GB
**MORTO**. Projeto abandonado. Incompativel com Qwen3.6.

---

## 9. KTransformers v0.6.4 (2026-07-23)

**GitHub**: kvcache-ai/ktransformers | **Foco**: CPU-GPU heterogeneous + Fine-tuning

### Performance Reportada
- Qwen3-30B-A3B Full-FT: ~400 tok/s (2x EPYC 9355 + 2x RTX 5090)
- Qwen3-30B-A3B LoRA: ~700 tok/s (1x RTX 4090 + AMX CPU)
- DeepSeek V4: BF16 attention fallback para Ampere GPUs
- Intel iGPU: SYCL GPTQ INT4 MoE (experimental)

### Compatibilidade
- **Turing (SM 7.5)**: NAO. DeepSeek V4 menciona "Ampere GPUs without native FP8".
- **Windows**: NAO. Linux apenas.
- **GGUF**: Parcial — foco em safetensors/HF.
- **MoE**: EXCELENTE — proposito principal do KT. CPU-GPU MoE offloading.

### Veredito para RTX 2070 8GB
**NAO RECOMENDADO**. Excelente para MoE, mas:
- Foco em treino (SFT), nao inferencia
- Requer hardware high-end (RTX 4090/5090, EPYC)
- Linux apenas
- Complexidade alta
- Nao testado em Turing

---

## 10. AirLLM v3.0.1 (2026-06-30)

**GitHub**: lyogavin/airllm | **Foco**: Rodar modelos enormes em GPUs minusculas

### Performance
- Abordagem: layer streaming — carrega uma layer por vez
- DeepSeek-V3 671B em ~12GB VRAM
- Qwen3 30B-A3B MoE em 8GB (teorico)
- Performance: **MUITO LENTA**. Layer streaming sacrifica velocidade por VRAM.

### Compatibilidade
- **Turing (SM 7.5)**: SIM — funciona em qualquer GPU com PyTorch
- **Windows**: SIM — `pip install airllm`
- **GGUF**: NAO. Usa safetensors/HF nativos.
- **MoE**: SIM — Qwen3 MoE, DeepSeek-V3, Mixtral.
- **FP8**: Suporte nativo.

### Veredito para RTX 2070 8GB
**APENAS PARA TESTE**. Motor mais acessivel (pip install, qualquer GPU, Windows), mas performance extremamente baixa para chat interativo. Estilo FlexGen — otimo para "rodar modelo enorme devagar" mas pessimo para "chat rapido com modelo medio".

**tok/s estimado**: 1-4 tok/s (modelo 35B em 8GB) — inutilizavel para chat
**Caso de uso**: Testar se um modelo funciona antes de baixar GGUF

---

## 11. FlexGen

**GitHub**: FMINer/FlexGen (Stanford) | **Estado**: ABANDONADO

- Projeto de pesquisa de 2023
- CPU/GPU/disk hybrid offloading
- Incompativel com arquiteturas modernas
- Substituido por AirLLM e KTransformers

---

## TABELA COMPARATIVA FINAL

| Motor | TG tok/s Est. | Windows | Turing | GGUF | MoE | Setup | Veredito |
|-------|--------------|---------|--------|------|-----|-------|----------|
| **llama.cpp** | **22-27** | SIM | SIM | SIM | SIM | Medio | **BASELINE** |
| mistral.rs | ?? (15-28?) | CPU-only | NAO | SIM | SIM | Dificil | INCOMPATIVEL |
| vLLM | ?? | NAO | NAO | Parcial | SIM | Muito Dificil | INCOMPATIVEL |
| Sonar/Aphrodite | ?? | WSL2 | NAO | SIM | SIM | Muito Dificil | INCOMPATIVEL |
| ExLlamaV3 | ?? | Fonte | NAO | NAO | SIM* | Muito Dificil | INCOMPATIVEL |
| **Ollama** | **21-26** | **SIM** | **SIM** | **SIM** | **SIM** | **Facil** | **ALTERNATIVA** |
| **Unsloth** | **22-27** | **SIM** | **SIM** | **SIM** | **SIM** | **Facil** | **RECOMENDADO** |
| CTranslate2 | 5-10? | SIM | SIM | Parcial | NAO | Medio | IRRELEVANTE |
| PowerInfer | N/A | ?? | ?? | NAO | NAO | N/A | MORTO |
| KTransformers | ?? | NAO | NAO | Parcial | SIM | Muito Dificil | INCOMPATIVEL |
| AirLLM | 1-4 | SIM | SIM | NAO | SIM | Facil | CURIOSIDADE |
| FlexGen | N/A | ?? | ?? | NAO | NAO | N/A | MORTO |

---

## CONCLUSOES

### 1. Nenhum motor alternativo entrega MAIS tok/s que llama.cpp para RTX 2070 8GB

Dos 11 motores pesquisados, **zero** oferecem performance superior ao llama.cpp para esta combinacao especifica (RTX 2070 + Qwen3.6-35B-A3B IQ4_XS).

### 2. Motores "mais rapidos" sao para GPUs maiores

- **ExLlamaV3**: 2x mais rapido que llama.cpp... em RTX 4090. Nao funciona em Turing.
- **mistral.rs**: +25% em B200/H100. Windows CPU-only.
- **vLLM**: FlashInfer MoE kernels de ponta... para A100 80GB.

### 3. Unsloth e a melhor alternativa (mas mesmo backend)

Unsloth usa llama.cpp como backend — mesma performance. Mas a UX e muito superior:
- MoE offloading na UI (n_cpu_moe visual)
- API OpenAI/Anthropic integrada
- `unsloth start claude` para integrar com Claude Code
- Auto-config de parametros

### 4. Ollama e alternativa zero-config valida

Mesma performance que llama.cpp. Menos controle. Melhor UX. Overhead 2-5%.

### 5. O setup atual do MIKE e o estado da arte para 8GB Turing

llama.cpp + IQ4_XS + n_cpu_moe=99 + FlashAttn=on + KV q8_0 = 22-27 tok/s.
**NAO HA MOTOR ALTERNATIVO QUE SUPERE ISSO EM JULHO 2026.**

### 6. O que poderia melhorar (futuro)

- **llama.cpp com MoE expert offloading assincrono** (prefetch de experts em background)
- **ExLlamaV3 com suporte a Turing** (improvável — otimizacoes Ampere-specific)
- **Novo motor Rust-like otimizado para Turing** (ninguem esta construindo)
- **Vulkan backend do llama.cpp** — performance inferior ao CUDA em NVIDIA, mas multiplataforma
