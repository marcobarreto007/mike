# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

from __future__ import annotations

import os
from dataclasses import is_dataclass, replace
from typing import Callable, Optional, Sequence, TypeVar
from mike_config import env_bool


T = TypeVar("T")





class MikeReranker:
    def __init__(
        self,
        log: Optional[Callable[[str], None]] = None,
        *,
        enabled: Optional[bool] = None,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        backend: Optional[str] = None,
        candidate_pool: Optional[int] = None,
        batch_size: Optional[int] = None,
        max_length: Optional[int] = None,
    ) -> None:
        self.log = log or (lambda _: None)
        self.enabled = env_bool("MIKE_RERANKER_ENABLED", False) if enabled is None else bool(enabled)
        self.model_name = (model_name or os.getenv("MIKE_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")).strip()
        self.device = (device or os.getenv("MIKE_RERANKER_DEVICE", "cpu")).strip() or "cpu"
        self.candidate_pool = max(1, int(candidate_pool or os.getenv("MIKE_RERANKER_CANDIDATE_POOL", "20")))
        self.batch_size = max(1, int(batch_size or os.getenv("MIKE_RERANKER_BATCH_SIZE", "16")))
        self.max_length = max(64, int(max_length or os.getenv("MIKE_RERANKER_MAX_LENGTH", "512")))
        self.backend = (backend or os.getenv("MIKE_RERANKER_BACKEND", "auto").strip().lower() or "auto")
        self._backend = None
        self._load_error: Optional[str] = None

    def candidate_limit(self, top_k: int) -> int:
        normalized_top_k = max(1, int(top_k or 1))
        if not self.enabled:
            return normalized_top_k
        return max(normalized_top_k, self.candidate_pool)

    def rerank(
        self,
        query: str,
        passages: Sequence[T],
        top_k: int = 5,
        *,
        text_getter: Optional[Callable[[T], str]] = None,
    ) -> list[T]:
        items = list(passages or [])
        if not items:
            return []

        normalized_top_k = max(1, min(int(top_k or len(items)), len(items)))
        if not self._ensure_backend():
            return items[:normalized_top_k]

        pairs: list[tuple[int, list[str]]] = []
        for index, item in enumerate(items):
            text = text_getter(item) if text_getter else getattr(item, "content", "")
            normalized_text = str(text or "").strip()
            if normalized_text:
                pairs.append((index, [query, normalized_text]))

        if not pairs:
            return items[:normalized_top_k]

        try:
            scores = self._backend.compute_score(
                [pair for _, pair in pairs],
                batch_size=self.batch_size,
                max_length=self.max_length,
            )
        except Exception as exc:
            self.log(f"Reranker scoring failed: {exc}")
            return items[:normalized_top_k]

        if not isinstance(scores, list):
            scores = [scores]

        indexed_scores: list[tuple[int, float]] = []
        for (index, _), score in zip(pairs, scores):
            try:
                indexed_scores.append((index, float(score)))
            except (TypeError, ValueError):
                indexed_scores.append((index, 0.0))

        indexed_scores.sort(key=lambda item: (-item[1], item[0]))

        ranked: list[T] = []
        seen_indices: set[int] = set()
        for index, score in indexed_scores[:normalized_top_k]:
            seen_indices.add(index)
            ranked.append(self._copy_with_score(items[index], score))

        if len(ranked) < normalized_top_k:
            for index, item in enumerate(items):
                if index in seen_indices:
                    continue
                ranked.append(item)
                if len(ranked) >= normalized_top_k:
                    break

        return ranked

    def _ensure_backend(self) -> bool:
        if not self.enabled:
            return False
        if self._backend is not None:
            return True
        if self._load_error is not None:
            return False

        errors: list[str] = []
        for backend in self._backend_order():
            try:
                if backend == "flagembedding":
                    self._backend = self._load_flagembedding_backend()
                elif backend == "transformers":
                    self._backend = self._load_transformers_backend()
                else:
                    continue
                if self._backend is not None:
                    return True
            except Exception as exc:
                errors.append(f"{backend}: {exc}")
                self.log(f"Reranker backend {backend} unavailable: {exc}")

        self._load_error = "; ".join(errors) or "No reranker backend available."
        self.log(f"Reranker unavailable: {self._load_error}")
        return False

    def _backend_order(self) -> list[str]:
        preferred = self.backend
        if preferred == "auto":
            return ["flagembedding", "transformers"]
        if preferred in {"flagembedding", "transformers"}:
            return [preferred, "transformers" if preferred == "flagembedding" else "flagembedding"]
        return ["flagembedding", "transformers"]

    def _load_flagembedding_backend(self):
        from FlagEmbedding import FlagReranker

        return FlagReranker(
            self.model_name,
            use_fp16=self.device.lower().startswith("cuda"),
            devices=self.device,
            batch_size=self.batch_size,
            max_length=self.max_length,
        )

    def _load_transformers_backend(self):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        model = AutoModelForSequenceClassification.from_pretrained(self.model_name)
        model.eval()
        target_device = "cuda" if self.device.lower().startswith("cuda") and torch.cuda.is_available() else "cpu"
        model.to(target_device)

        class _TransformersBackend:
            def __init__(self, tokenizer, model, device, batch_size, max_length):
                self.tokenizer = tokenizer
                self.model = model
                self.device = device
                self.batch_size = batch_size
                self.max_length = max_length

            def compute_score(self, pairs, batch_size=None, max_length=None):
                normalized_batch = max(1, int(batch_size or self.batch_size))
                normalized_length = max(64, int(max_length or self.max_length))
                scores: list[float] = []
                for start in range(0, len(pairs), normalized_batch):
                    window = pairs[start:start + normalized_batch]
                    queries = [str(pair[0] or "") for pair in window]
                    passages = [str(pair[1] or "") for pair in window]
                    encoded = self.tokenizer(
                        queries,
                        passages,
                        padding=True,
                        truncation=True,
                        max_length=normalized_length,
                        return_tensors="pt",
                    )
                    encoded = {
                        key: value.to(self.device)
                        for key, value in encoded.items()
                    }
                    with torch.no_grad():
                        logits = self.model(**encoded).logits.squeeze(-1)
                    scores.extend(float(score) for score in logits.detach().cpu().tolist())
                return scores

        return _TransformersBackend(
            tokenizer=tokenizer,
            model=model,
            device=target_device,
            batch_size=self.batch_size,
            max_length=self.max_length,
        )

    def _copy_with_score(self, item: T, score: float) -> T:
        if not is_dataclass(item):
            return item
        if not hasattr(item, "score") or not hasattr(item, "metadata"):
            return item
        metadata = getattr(item, "metadata", None)
        if not isinstance(metadata, dict):
            return item
        try:
            return replace(
                item,
                score=score,
                metadata={**metadata, "reranker_score": score},
            )
        except Exception:
            return item
