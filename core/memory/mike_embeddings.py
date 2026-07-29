# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
from mike_config import env_bool





class MikeLocalEmbedder:
    def __init__(
        self,
        log: Optional[Callable[[str], None]] = None,
        *,
        enabled: Optional[bool] = None,
        model_name: Optional[str] = None,
        dims: Optional[int] = None,
        device: Optional[str] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        self.log = log or (lambda _: None)
        self.enabled = env_bool("MIKE_VECTOR_SEARCH_ENABLED", True) if enabled is None else bool(enabled)
        self.model_name = (
            model_name
            or os.getenv("MIKE_VECTOR_SEARCH_MODEL")
            or os.getenv("MIKE_MEM0_EMBED_MODEL")
            or "BAAI/bge-m3"
        ).strip()
        self.dims = max(
            1,
            int(
                dims
                or os.getenv("MIKE_VECTOR_SEARCH_DIMS")
                or os.getenv("MIKE_MEM0_EMBED_DIMS")
                or "1024"
            ),
        )
        self.device = (
            device
            or os.getenv("MIKE_VECTOR_SEARCH_DEVICE")
            or os.getenv("MIKE_MEM0_EMBED_DEVICE")
            or "cpu"
        ).strip() or "cpu"
        self.batch_size = max(1, int(batch_size or os.getenv("MIKE_VECTOR_SEARCH_BATCH_SIZE", "8")))
        default_backend = "onnx" if self.model_name.lower() == "baai/bge-m3" else "sentence-transformers"
        self.backend = (os.getenv("MIKE_VECTOR_SEARCH_BACKEND", default_backend).strip().lower() or default_backend)
        self.onnx_repo = (
            os.getenv("MIKE_VECTOR_SEARCH_ONNX_REPO")
            or ("aapot/bge-m3-onnx" if self.model_name.lower() == "baai/bge-m3" else "")
        ).strip()
        self.onnx_dir = Path(
            os.getenv("MIKE_VECTOR_SEARCH_ONNX_DIR") or "mike/cache/vector_models/bge-m3-onnx"
        )
        self._model = None
        self._tokenizer = None
        self._onnx_session = None
        self._onnx_output_name = ""
        self._onnx_files_ready = False
        self._load_error: Optional[str] = None

    def cache_key(self, text: str, scope: str = "knowledge_chunk") -> str:
        payload = f"{scope}\n{self.model_name}\n{str(text or '').strip()}".encode("utf-8", errors="ignore")
        return hashlib.sha1(payload).hexdigest()

    def embed_query(self, text: str) -> Optional[np.ndarray]:
        vectors = self.embed_texts([text])
        return vectors[0] if vectors else None

    def embed_texts(self, texts: Sequence[str]) -> list[np.ndarray]:
        items = [str(text or "").strip() for text in texts]
        if not self.enabled or not items:
            return []
        if not self._ensure_model():
            return []

        if self._onnx_session is not None and self._tokenizer is not None:
            return self._embed_texts_onnx(items)

        try:
            embeddings = self._model.encode(
                items,
                batch_size=self.batch_size,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            self.log(f"Local embedding encode failed: {exc}")
            return []

        vectors: list[np.ndarray] = []
        for vector in embeddings:
            array = np.asarray(vector, dtype=np.float32)
            if array.ndim != 1:
                continue
            vectors.append(array)
        return vectors

    def _ensure_model(self) -> bool:
        if not self.enabled:
            return False
        if self._onnx_session is not None and self._tokenizer is not None:
            return True
        if self._model is not None:
            return True
        if self._load_error is not None:
            return False

        attempted: list[str] = []
        backend_order = self._backend_order()
        for backend in backend_order:
            self.log(f"Attempting embedding backend: {backend}")
            if backend == "onnx":
                attempted.append("onnx")
                try:
                    if self._ensure_onnx_model():
                        return True
                except Exception as exc:
                    self.log(f"Local ONNX embedder unavailable: {exc}")
            elif backend in {"sentence-transformers", "sentence_transformers", "st"}:
                attempted.append("sentence-transformers")
                try:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(self.model_name, device=self.device)
                    actual_dims = self._model.get_sentence_embedding_dimension()
                    if actual_dims:
                        self.dims = int(actual_dims)
                    return True
                except Exception as exc:
                    self.log(f"Local embedder unavailable: {exc}")
                    self._model = None

        self._load_error = f"No embedding backend available for {self.model_name} (tried: {', '.join(attempted) or 'none'})."
        return False

    def _backend_order(self) -> list[str]:
        preferred = self.backend
        if preferred == "auto":
            preferred = "onnx" if self.onnx_repo else "sentence-transformers"
        if preferred == "onnx":
            return ["onnx", "sentence-transformers"]
        if preferred in {"sentence-transformers", "sentence_transformers", "st"}:
            return ["sentence-transformers", "onnx"]
        return [preferred, "onnx", "sentence-transformers"]

    def _ensure_onnx_model(self) -> bool:
        if self._onnx_session is not None and self._tokenizer is not None:
            return True
        if not self.onnx_repo:
            return False

        import onnxruntime as ort
        from transformers import AutoTokenizer

        self.prepare_onnx_files()

        self._tokenizer = AutoTokenizer.from_pretrained(
            str(self.onnx_dir),
            local_files_only=True,
        )
        self._onnx_session = ort.InferenceSession(
            str(self.onnx_dir / "model.onnx"),
            providers=["CPUExecutionProvider"],
        )
        output_names = [output.name for output in self._onnx_session.get_outputs()]
        self._onnx_output_name = "dense_vecs" if "dense_vecs" in output_names else output_names[0]
        return True

    def prepare_onnx_files(self) -> bool:
        if self._onnx_files_ready:
            return True
        if not self.onnx_repo:
            return False

        from huggingface_hub import hf_hub_download

        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        self.onnx_dir.mkdir(parents=True, exist_ok=True)

        required_files = [
            "model.onnx",
            "model.onnx.data",
            "tokenizer.json",
            "sentencepiece.bpe.model",
            "config.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
        ]
        for filename in required_files:
            local_path = self.onnx_dir / filename
            if local_path.exists() and local_path.stat().st_size > 0:
                continue
            hf_hub_download(
                repo_id=self.onnx_repo,
                filename=filename,
                local_dir=str(self.onnx_dir),
            )
        self._onnx_files_ready = True
        return True

    def _embed_texts_onnx(self, texts: Sequence[str]) -> list[np.ndarray]:
        encoded = self._tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            return_tensors="np",
        )
        feeds = {}
        for input_meta in self._onnx_session.get_inputs():
            if input_meta.name in encoded:
                feeds[input_meta.name] = encoded[input_meta.name].astype(np.int64)

        try:
            outputs = self._onnx_session.run([self._onnx_output_name], feeds)
        except Exception as exc:
            self.log(f"Local ONNX embedding encode failed: {exc}")
            return []
        if not outputs:
            return []

        dense = np.asarray(outputs[0], dtype=np.float32)
        if dense.ndim != 2:
            return []
        norms = np.linalg.norm(dense, axis=1, keepdims=True)
        dense = dense / np.clip(norms, 1e-12, None)
        if dense.shape[1]:
            self.dims = int(dense.shape[1])
        return [row.astype(np.float32) for row in dense]
