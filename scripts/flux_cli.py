#!/usr/bin/env python3
"""
FLUX CLI — Geracao de imagens multi-GPU (RTX 2070 + P106-100)

Modos de carga:
  auto  — device_map="balanced" com max_memory (recomendado para 8GB+6GB)
  v2    — carga manual shard-by-shard (componente a componente, evita crash de RAM)
  cpu   — model_cpu_offload (GPU unica ou pouca VRAM)

Uso: python scripts/flux_cli.py -p "seu prompt" -o saida.png
"""

import argparse
import gc
import json
import os
import re
import sys
import time

os.environ.setdefault("HF_HOME", "D:/huggingface")
os.environ.setdefault("HF_HUB_CACHE", "D:/huggingface/hub")

import torch
import safetensors.torch
from diffusers import FluxPipeline, AutoencoderKL, FluxTransformer2DModel
from transformers import CLIPTextModel, T5EncoderModel, CLIPTokenizer, T5TokenizerFast


def get_dtype(name: str) -> torch.dtype:
    return {"fp16": torch.float16, "bf16": torch.bfloat16, "fp32": torch.float32}[name]


def list_gpus():
    print(f"CUDA disponivel: {torch.cuda.is_available()}")
    print(f"GPUs: {torch.cuda.device_count()}")
    for i in range(torch.cuda.device_count()):
        p = torch.cuda.get_device_properties(i)
        print(f"  cuda:{i} — {p.name} | {p.total_memory // 1024**2} MB | CC {p.major}.{p.minor}")


def load_auto(model_id: str, dtype: torch.dtype, device_count: int) -> FluxPipeline:
    """Carga balanceada entre GPUs (modo padrao)."""
    print("  device_map=balanced (8GB + 6GB VRAM)")
    return FluxPipeline.from_pretrained(
        model_id,
        torch_dtype=dtype,
        device_map="balanced",
        max_memory={0: "8GB", 1: "6GB"},
    )


def load_v2(model_id: str, dtype: torch.dtype) -> FluxPipeline:
    """Carga manual componente-a-componente com posicionamento explicito de GPU."""
    print("[1/4] Carregando CLIP-L...")
    text_encoder = CLIPTextModel.from_pretrained(model_id, subfolder="text_encoder", torch_dtype=dtype)
    text_encoder.to("cuda:1")
    print(f"  CLIP-L → P106 ({sum(p.numel()*p.element_size() for p in text_encoder.parameters())/1e9:.2f} GB)")

    print("[2/4] Carregando T5 XXL...")
    text_encoder_2 = T5EncoderModel.from_pretrained(model_id, subfolder="text_encoder_2", torch_dtype=dtype)
    text_encoder_2.to("cuda:0")
    print(f"  T5 XXL → RTX 2070 ({sum(p.numel()*p.element_size() for p in text_encoder_2.parameters())/1e9:.2f} GB)")

    print("[3/4] Carregando VAE...")
    vae = AutoencoderKL.from_pretrained(model_id, subfolder="vae", torch_dtype=dtype)
    vae.to("cuda:1")
    print(f"  VAE → P106 ({sum(p.numel()*p.element_size() for p in vae.parameters())/1e9:.2f} GB)")

    print("[4/4] Carregando Transformer...")
    transformer = FluxTransformer2DModel.from_pretrained(
        model_id, subfolder="transformer", torch_dtype=dtype,
        device_map={"": "cuda:0"},
        max_memory={0: "8GB"},
    )
    print(f"  Transformer OK ({sum(p.numel()*p.element_size() for p in transformer.parameters())/1e9:.2f} GB)")

    print("Montando pipeline...")
    pipe = FluxPipeline(
        text_encoder=text_encoder,
        text_encoder_2=text_encoder_2,
        vae=vae,
        transformer=transformer,
        tokenizer=CLIPTokenizer.from_pretrained(model_id, subfolder="tokenizer"),
        tokenizer_2=T5TokenizerFast.from_pretrained(model_id, subfolder="tokenizer_2"),
    )
    return pipe


def load_cpu_offload(model_id: str, dtype: torch.dtype) -> FluxPipeline:
    """Carga com model_cpu_offload para GPUs com pouca VRAM."""
    pipe = FluxPipeline.from_pretrained(model_id, torch_dtype=dtype)
    pipe.enable_model_cpu_offload()
    print("  model_cpu_offload: ativo")
    return pipe


def main():
    parser = argparse.ArgumentParser(description="FLUX CLI — Multi-GPU Image Generation")
    parser.add_argument("--prompt", "-p", required=True, help="Prompt de texto")
    parser.add_argument("--negative-prompt", "-n", default="", help="Prompt negativo")
    parser.add_argument("--model", "-m", default="black-forest-labs/FLUX.1-schnell",
                        help="Modelo HF (FLUX.1-dev ou FLUX.1-schnell)")
    parser.add_argument("--steps", "-s", type=int, default=None, help="Passos de inferencia")
    parser.add_argument("--seed", type=int, default=None, help="Seed (aleatorio se omitido)")
    parser.add_argument("--width", "-W", type=int, default=1024)
    parser.add_argument("--height", "-H", type=int, default=1024)
    parser.add_argument("--guidance", "-g", type=float, default=3.5, help="Guidance scale")
    parser.add_argument("--output", "-o", default=None, help="Arquivo de saida")
    parser.add_argument("--dtype", default="fp16", choices=["fp16", "bf16", "fp32"])
    parser.add_argument("--mode", default="auto", choices=["auto", "v2", "cpu"],
                        help="Estrategia de carga: auto (balanced), v2 (manual shard), cpu (offload)")
    parser.add_argument("--lora", "-l", default=None, help="Caminho para pesos LoRA (.safetensors)")
    parser.add_argument("--lora-scale", type=float, default=1.0, help="Escala do LoRA")
    parser.add_argument("--compile", action="store_true", help="Usar torch.compile (mais lento 1a vez)")
    parser.add_argument("--list-gpus", action="store_true", help="Listar GPUs e sair")
    args = parser.parse_args()

    if args.list_gpus:
        list_gpus()
        return

    if not torch.cuda.is_available():
        print("ERRO: CUDA nao disponivel. Verifique o driver NVIDIA.", file=sys.stderr)
        sys.exit(1)

    device_count = torch.cuda.device_count()
    dtype = get_dtype(args.dtype)

    print(f"=== FLUX CLI ===")
    print(f"Modelo: {args.model} | Modo: {args.mode} | GPUs: {device_count}")
    for i in range(device_count):
        p = torch.cuda.get_device_properties(i)
        print(f"  [{i}] {p.name} ({p.total_memory // 1024**2} MB)")
    print(f"Prompt: {args.prompt[:100]}{'...' if len(args.prompt) > 100 else ''}")

    steps = args.steps or (4 if "schnell" in args.model else 28)
    print(f"Passos: {steps} | Guidance: {args.guidance} | {args.width}x{args.height}")

    output = args.output or f"flux_{re.sub(r'[^a-zA-Z0-9]+', '_', args.prompt[:40]).strip('_')}_{int(time.time())}.png"
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    # ---- LOAD ----
    print(f"\nCarregando modelo... (dtype={args.dtype}, mode={args.mode})")
    t0 = time.time()

    if args.mode == "v2":
        pipe = load_v2(args.model, dtype)
    elif args.mode == "cpu":
        pipe = load_cpu_offload(args.model, dtype)
    else:
        pipe = load_auto(args.model, dtype, device_count)

    # LoRA
    if args.lora:
        print(f"  LoRA: {args.lora} (scale={args.lora_scale})")
        pipe.load_lora_weights(args.lora)
        pipe.fuse_lora(lora_scale=args.lora_scale)

    # torch.compile (opcional)
    if args.compile:
        print("  Compilando transformer (torch.compile)...")
        pipe.transformer = torch.compile(pipe.transformer, mode="reduce-overhead", fullgraph=True)

    load_time = time.time() - t0
    print(f"Modelo carregado em {load_time:.1f}s")

    # ---- GENERATE ----
    print("\nGerando...")
    torch.cuda.reset_peak_memory_stats()
    t1 = time.time()

    gen_kwargs = {
        "prompt": args.prompt,
        "num_inference_steps": steps,
        "width": args.width,
        "height": args.height,
        "guidance_scale": args.guidance,
    }
    if args.negative_prompt:
        gen_kwargs["negative_prompt"] = args.negative_prompt
    if args.seed is not None:
        gen_kwargs["generator"] = torch.Generator(device="cpu").manual_seed(args.seed)

    result = pipe(**gen_kwargs)
    image = result.images[0]

    gen_time = time.time() - t1
    print(f"Geracao: {gen_time:.1f}s ({gen_time/steps:.1f}s/passo)")

    for i in range(device_count):
        peak = torch.cuda.max_memory_allocated(i) // 1024**2
        print(f"  GPU {i} peak VRAM: {peak} MB")

    # ---- SAVE ----
    image.save(output)
    size_mb = os.path.getsize(output) / 1024 / 1024
    print(f"\nSalvo: {output} ({size_mb:.1f} MB)")
    print(f"Tempo total: {load_time + gen_time:.1f}s")


if __name__ == "__main__":
    main()
