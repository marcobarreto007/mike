"""
Mike LLM boot / vision handler helpers.

Functions for detecting the vision handler backend, building fallback
boot candidates, and creating the LLM instance with graceful fallback.

Extracted from mike_server.py — Phase 2 monolith breakup.
"""
from __future__ import annotations

import gc
import logging
import time
from pathlib import Path
from typing import List, Optional

from huggingface_hub import hf_hub_download

from mike_config import (
    CTX_SIZE,
    FLASH_ATTN,
    GPU_INFO,
    GPU_LAYERS,
    KV_TYPE_K,
    KV_TYPE_V,
    N_BATCH,
    N_CPU_MOE,
    N_THREADS,
    N_THREADS_BATCH,
    N_UBATCH,
    OFFLOAD_KQV,
    TENSOR_SPLIT,
    USE_MLOCK,
    USE_MMAP,
    VERBOSE,
)
from mike_stats import stats

log = logging.getLogger("mike")

# ---------------------------------------------------------------------------
# Native Gemma 4 batch floor constants
# ---------------------------------------------------------------------------

_VISION_NATIVE_BATCH_FLOOR = 768
_VISION_NATIVE_UBATCH_FLOOR = 768


# ---------------------------------------------------------------------------
# Vision handler backend detection
# ---------------------------------------------------------------------------

def _native_gemma4_chat_handler_class():
    try:
        from llama_cpp.llama_chat_format import Gemma4ChatHandler
    except Exception:
        return None
    return Gemma4ChatHandler


def _vision_handler_backend_label() -> str:
    try:
        return "native-gemma4" if _native_gemma4_chat_handler_class() is not None else "legacy-llava16"
    except Exception:
        return "unavailable (CUDA runtime missing)"


def _uses_native_gemma4_vision_handler() -> bool:
    try:
        return _native_gemma4_chat_handler_class() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Boot candidate helpers
# ---------------------------------------------------------------------------

def _apply_vision_batch_floor(candidate: dict) -> dict:
    if not _uses_native_gemma4_vision_handler():
        return candidate

    adjusted = dict(candidate)
    min_batch = min(adjusted["n_ctx"], _VISION_NATIVE_BATCH_FLOOR)
    min_ubatch = min(adjusted["n_ctx"], _VISION_NATIVE_UBATCH_FLOOR)
    adjusted["n_batch"] = min(adjusted["n_ctx"], max(adjusted["n_batch"], min_batch))
    adjusted["n_ubatch"] = min(adjusted["n_batch"], max(adjusted["n_ubatch"], min_ubatch))
    return adjusted


def _llama_boot_candidates() -> List[dict]:
    base = {
        "label": "configured",
        "n_gpu_layers": max(0, int(GPU_LAYERS)),
        "n_ctx": max(512, int(CTX_SIZE)),
        "n_batch": max(32, int(min(N_BATCH, CTX_SIZE))),
        "n_ubatch": max(32, int(min(N_UBATCH, N_BATCH, CTX_SIZE))),
        "flash_attn": bool(FLASH_ATTN),
        "offload_kqv": bool(OFFLOAD_KQV),
        "type_k": int(KV_TYPE_K),
        "type_v": int(KV_TYPE_V),
    }
    candidates: List[dict] = []

    def add(label: str, **overrides):
        candidate = {**base, **overrides, "label": label}
        candidate["n_gpu_layers"] = max(0, int(candidate["n_gpu_layers"]))
        candidate["n_ctx"] = max(512, int(candidate["n_ctx"]))
        candidate["n_batch"] = max(32, int(min(candidate["n_batch"], candidate["n_ctx"])))
        candidate["n_ubatch"] = max(32, int(min(candidate["n_ubatch"], candidate["n_batch"])))
        candidate = _apply_vision_batch_floor(candidate)
        candidates.append(candidate)

    add("configured")
    if base["type_k"] != 1 or base["type_v"] != 1:
        add("configured-f16-kv", type_k=1, type_v=1)
    # Fallback sem flash_attn mantendo todos os GPU layers (antes de reduzir camadas)
    if base["flash_attn"]:
        add("configured-no-flash", flash_attn=False)
    if GPU_INFO["cuda_detected"]:
        add(
            "gpu-safe",
            n_gpu_layers=min(max(24, base["n_gpu_layers"] // 2), base["n_gpu_layers"]),
            n_ctx=min(base["n_ctx"], 4096),
            n_batch=min(base["n_batch"], 512),
            n_ubatch=min(base["n_ubatch"], 256),
            flash_attn=False,
            offload_kqv=False,
            type_k=1,
            type_v=1,
        )
        add(
            "gpu-min",
            n_gpu_layers=min(16, base["n_gpu_layers"]),
            n_ctx=min(base["n_ctx"], 3072),
            n_batch=min(base["n_batch"], 256),
            n_ubatch=min(base["n_ubatch"], 128),
            flash_attn=False,
            offload_kqv=False,
            type_k=1,
            type_v=1,
        )
    add(
        "cpu-safe",
        n_gpu_layers=0,
        n_ctx=min(base["n_ctx"], 2048),
        n_batch=min(base["n_batch"], 256),
        n_ubatch=min(base["n_ubatch"], 128),
        flash_attn=False,
        offload_kqv=False,
        type_k=1,
        type_v=1,
    )

    unique: List[dict] = []
    seen: set = set()
    for candidate in candidates:
        key = (
            candidate["n_gpu_layers"],
            candidate["n_ctx"],
            candidate["n_batch"],
            candidate["n_ubatch"],
            candidate["flash_attn"],
            candidate["offload_kqv"],
            candidate["type_k"],
            candidate["type_v"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _record_llm_boot(candidate: dict, attempts: int) -> None:
    stats["gpu_layers"] = candidate["n_gpu_layers"]
    stats["ctx_size"] = candidate["n_ctx"]
    stats["n_batch"] = candidate["n_batch"]
    stats["n_ubatch"] = candidate["n_ubatch"]
    stats["flash_attn"] = candidate["flash_attn"]
    stats["offload_kqv"] = candidate["offload_kqv"]
    stats["runtime_profile_loaded"] = candidate["label"]
    stats["llm_boot_profile"] = candidate["label"]
    stats["llm_boot_attempts"] = attempts
    stats["boot_fallback_used"] = attempts > 1


# ---------------------------------------------------------------------------
# LLM creation with fallback
# ---------------------------------------------------------------------------

def _create_llm_with_fallback(Llama, model_path: str):
    candidates = _llama_boot_candidates()
    errors: List[str] = []
    for attempt_index, candidate in enumerate(candidates, start=1):
        log.info(
            "LLM boot attempt %s/%s: profile=%s gpu_layers=%s ctx=%s n_batch=%s n_ubatch=%s flash_attn=%s offload_kqv=%s kv=%s/%s",
            attempt_index,
            len(candidates),
            candidate["label"],
            candidate["n_gpu_layers"],
            candidate["n_ctx"],
            candidate["n_batch"],
            candidate["n_ubatch"],
            candidate["flash_attn"],
            candidate["offload_kqv"],
            candidate["type_k"],
            candidate["type_v"],
        )
        try:
            # Build kwargs — multi-GPU support via split_mode + main_gpu
            llama_kwargs = dict(
                model_path=model_path,
                n_gpu_layers=candidate["n_gpu_layers"],
                n_ctx=candidate["n_ctx"],
                n_batch=candidate["n_batch"],
                n_ubatch=candidate["n_ubatch"],
                n_threads=N_THREADS,
                n_threads_batch=N_THREADS_BATCH,
                flash_attn=candidate["flash_attn"],
                offload_kqv=candidate["offload_kqv"],
                type_k=candidate["type_k"],
                type_v=candidate["type_v"],
                use_mmap=USE_MMAP,
                use_mlock=USE_MLOCK,
                tensor_split=TENSOR_SPLIT,
                verbose=VERBOSE,
            )
            # Multi-GPU: set split_mode=1 (row) and main_gpu=0 (largest GPU first)
            if TENSOR_SPLIT and len(TENSOR_SPLIT) > 1:
                llama_kwargs["split_mode"] = 1   # LLAMA_SPLIT_MODE_ROW
                llama_kwargs["main_gpu"] = 0     # RTX 3060 (index 0, most VRAM)
            # MoE: offload expert layers to CPU (critical for 8GB VRAM)
            if N_CPU_MOE > 0:
                llama_kwargs["n_cpu_moe"] = N_CPU_MOE
            llm_instance = Llama(**llama_kwargs)

            _record_llm_boot(candidate, attempt_index)
            return llm_instance
        except Exception as exc:
            error_text = f"{candidate['label']}={type(exc).__name__}: {exc}"
            errors.append(error_text)
            log.warning("LLM boot attempt failed: %s", error_text)
            gc.collect()
            time.sleep(1.0)

    raise RuntimeError(
        "Nao consegui iniciar o Mike local em nenhum perfil de boot. "
        + " | ".join(errors)
    )


# ---------------------------------------------------------------------------
# Hugging Face file resolution
# ---------------------------------------------------------------------------

def _resolve_hf_file(
    *,
    repo_id: str,
    configured_file: str,
    revision: Optional[str],
    cache_dir: Path,
    label: str,
) -> str:
    """Resolve a local or Hugging Face file into a concrete local path."""
    if not configured_file:
        raise FileNotFoundError(f"{label} file is not configured.")

    configured_path = Path(configured_file).expanduser()
    if configured_path.is_absolute():
        if configured_path.exists():
            if configured_path.stat().st_size > 0:
                return str(configured_path.resolve())
            log.warning("%s file exists but is empty; removing stale file: %s", label, configured_path)
            configured_path.unlink(missing_ok=True)
        filename = configured_path.name
        local_dir = configured_path.parent
    else:
        local_candidate = (cache_dir / configured_path).resolve()
        if local_candidate.exists():
            if local_candidate.stat().st_size > 0:
                return str(local_candidate)
            log.warning("%s file exists but is empty; removing stale file: %s", label, local_candidate)
            local_candidate.unlink(missing_ok=True)
        filename = configured_file.replace("\\", "/")
        local_dir = cache_dir

    if not repo_id:
        raise FileNotFoundError(
            f"{label} file not found locally and no Hugging Face repo is configured: {configured_path}"
        )
    local_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "%s file missing locally; downloading %s from %s into %s",
        label,
        filename,
        repo_id,
        local_dir,
    )
    return str(Path(hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        revision=revision or None,
        local_dir=str(local_dir),
    )).resolve())
