# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from mike_config import env_bool
from mike_embeddings import MikeLocalEmbedder

_log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    source: str
    title: str
    content: str
    score: float
    metadata: dict


def _normalize_snippet(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


class _Mem0OnnxEmbedderAdapter:
    """Adapts MikeLocalEmbedder (ONNX) to mem0's EmbeddingBase interface."""

    def __init__(self, embedder: MikeLocalEmbedder) -> None:
        self._embedder = embedder
        self.config = type("_Cfg", (), {
            "model": embedder.model_name,
            "embedding_dims": embedder.dims,
        })()

    def embed(self, text, memory_action=None):
        vec = self._embedder.embed_query(str(text or ""))
        if vec is None:
            return [0.0] * self._embedder.dims
        return vec.tolist()


class OptionalMem0Client:
    def __init__(
        self,
        user_id: str,
        agent_id: str,
        storage_root: Path,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        self.storage_root = Path(storage_root)
        self.log = log or (lambda _: None)
        self.enabled = False
        self.client = None
        self.mode = "disabled"
        self.policy = "off"
        self.backend = "sqlite"
        self.provider = None
        self.supports_infer = False
        self.embed_provider = ""
        self.embed_model = ""
        self.embed_dims = 0
        self.bootstrap_required = False
        self.infer = env_bool("MIKE_MEM0_INFER", False)

    def initialize(self) -> None:
        requested_mode = (os.getenv("MIKE_MEM0_MODE", "auto") or "auto").strip().lower()
        if requested_mode in {"off", "disabled", "false", "0"}:
            return

        if requested_mode in {"cloud", "api"} or (requested_mode == "auto" and os.getenv("MEM0_API_KEY")):
            self._initialize_cloud()
            return

        if requested_mode in {"auto", "local", "oss", "oss_local"}:
            self._initialize_oss_local()

    def _initialize_cloud(self) -> None:
        if "MEM0_TELEMETRY" not in os.environ:
            os.environ["MEM0_TELEMETRY"] = os.getenv("MIKE_MEM0_TELEMETRY", "false")
        try:
            from mem0 import MemoryClient  # type: ignore
        except Exception as exc:
            self.log(f"Mem0 cloud unavailable: {exc}")
            return

        client_kwargs = {}
        if os.getenv("MEM0_API_KEY"):
            client_kwargs["api_key"] = os.getenv("MEM0_API_KEY")
        if os.getenv("MEM0_ORG_ID"):
            client_kwargs["org_id"] = os.getenv("MEM0_ORG_ID")
        if os.getenv("MEM0_PROJECT_ID"):
            client_kwargs["project_id"] = os.getenv("MEM0_PROJECT_ID")

        try:
            self.client = MemoryClient(**client_kwargs)
            self.enabled = True
            self.mode = "cloud"
            self.policy = "raw_user_memory"
            self.backend = "mem0-cloud+sqlite"
            self.provider = "mem0_cloud"
            self.supports_infer = True
        except Exception as exc:
            self.log(f"Mem0 cloud init failed: {exc}")
            self.client = None
            self.enabled = False

    def _initialize_oss_local(self) -> None:
        if "MEM0_TELEMETRY" not in os.environ:
            os.environ["MEM0_TELEMETRY"] = os.getenv("MIKE_MEM0_TELEMETRY", "false")
        try:
            from mem0 import Memory  # type: ignore
        except Exception as exc:
            self.log(f"Mem0 OSS unavailable: {exc}")
            return

        embed_provider, embed_model, embed_dims, embed_config = self._resolve_embedder_config()
        collection_name = os.getenv("MIKE_MEM0_COLLECTION", "mike_mem0")
        qdrant_dir_name = os.getenv("MIKE_MEM0_QDRANT_DIR", "qdrant_store")

        self.storage_root.mkdir(parents=True, exist_ok=True)
        qdrant_path = self.storage_root / qdrant_dir_name
        history_db_path = self.storage_root / "history.db"
        self.embed_provider = embed_provider
        self.embed_model = embed_model
        self.embed_dims = embed_dims
        self.bootstrap_required = not (qdrant_path / "meta.json").exists()
        self._archive_incompatible_store(
            qdrant_path=qdrant_path,
            history_db_path=history_db_path,
            collection_name=collection_name,
            expected_dims=embed_dims,
        )

        config = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": collection_name,
                    "path": str(qdrant_path),
                    "embedding_model_dims": embed_dims,
                    "on_disk": True,
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": os.getenv("MIKE_MODEL_ALIAS", "mike"),
                    "api_key": os.getenv("MIKE_MEM0_API_KEY", "mike-local"),
                    "openai_base_url": os.getenv("MIKE_MEM0_OPENAI_BASE_URL", "http://127.0.0.1:8080/v1"),
                    "temperature": 0.0,
                    "max_tokens": 128,
                },
            },
            "embedder": {
                "provider": embed_provider,
                "config": embed_config,
            },
            "history_db_path": str(history_db_path),
            "version": "v1.1",
        }

        try:
            self.client = Memory.from_config(config)
            self._swap_to_onnx_embedder()
            self.enabled = True
            self.mode = "oss_local"
            self.policy = "raw_user_memory"
            self.backend = "mem0-oss+sqlite"
            self.provider = "mem0_oss"
            self.supports_infer = True
        except Exception as exc:
            lock_file = qdrant_path / ".lock"
            if lock_file.exists() and "already accessed" in str(exc).lower():
                import time as _time
                for _attempt in range(3):
                    try:
                        lock_file.unlink(missing_ok=True)
                        self.client = Memory.from_config(config)
                        self._swap_to_onnx_embedder()
                        self.enabled = True
                        self.mode = "oss_local"
                        self.policy = "raw_user_memory"
                        self.backend = "mem0-oss+sqlite"
                        self.provider = "mem0_oss"
                        self.supports_infer = True
                        self.log("Mem0 OSS recovered from stale Qdrant lock.")
                        return
                    except Exception as retry_exc:
                        if _attempt < 2:
                            _time.sleep(1)
                        else:
                            self.log(f"Mem0 OSS retry failed: {retry_exc}")
            self.log(f"Mem0 OSS init failed: {exc}")
            self.client = None
            self.enabled = False

    def _resolve_embedder_config(self) -> tuple[str, str, int, dict]:
        embed_model = os.getenv("MIKE_MEM0_EMBED_MODEL", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
        embed_dims = int(os.getenv("MIKE_MEM0_EMBED_DIMS", "1024"))
        configured_provider = os.getenv("MIKE_MEM0_EMBED_PROVIDER", "").strip().lower()
        if configured_provider:
            embed_provider = configured_provider
        elif embed_model.lower() == "baai/bge-m3":
            embed_provider = "huggingface"
        else:
            embed_provider = "fastembed"

        config: dict = {
            "model": embed_model,
            "embedding_dims": embed_dims,
        }
        device = os.getenv("MIKE_MEM0_EMBED_DEVICE", "").strip()
        if device:
            config["model_kwargs"] = {"device": device}
        return embed_provider, embed_model, embed_dims, config

    def _read_existing_vector_size(self, qdrant_path: Path, collection_name: str) -> Optional[int]:
        meta_path = qdrant_path / "meta.json"
        if not meta_path.exists():
            return None
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        collections = payload.get("collections")
        if not isinstance(collections, dict):
            return None

        candidates = []
        if collection_name in collections:
            candidates.append(collections.get(collection_name))
        candidates.extend(collections.values())
        for collection in candidates:
            if not isinstance(collection, dict):
                continue
            vectors = collection.get("vectors")
            if not isinstance(vectors, dict):
                continue
            size = vectors.get("size")
            if size is None:
                continue
            try:
                return int(size)
            except (TypeError, ValueError):
                return None
        return None

    def _archive_incompatible_store(
        self,
        qdrant_path: Path,
        history_db_path: Path,
        collection_name: str,
        expected_dims: int,
    ) -> None:
        existing_dims = self._read_existing_vector_size(qdrant_path, collection_name)
        if existing_dims is None or existing_dims == expected_dims:
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = self.storage_root / "backups" / f"mem0_reset_{timestamp}_{existing_dims}d_to_{expected_dims}d"
        backup_root.mkdir(parents=True, exist_ok=True)

        if qdrant_path.exists():
            shutil.move(str(qdrant_path), str(backup_root / qdrant_path.name))
        if history_db_path.exists():
            shutil.move(str(history_db_path), str(backup_root / history_db_path.name))

        self.bootstrap_required = True
        self.log(
            "Mem0 OSS archive created for incompatible embedding dimensions "
            f"({existing_dims} -> {expected_dims}) at {backup_root}."
        )

    def _swap_to_onnx_embedder(self) -> None:
        if self.client is None:
            return
        configured = os.getenv("MIKE_MEM0_EMBED_PROVIDER", "").strip().lower()
        if configured:
            return
        try:
            onnx_embedder = MikeLocalEmbedder(log=self.log)
            if not onnx_embedder._ensure_model():
                self.log("Mem0 ONNX adapter: embedder failed to load, keeping original.")
                return
            adapter = _Mem0OnnxEmbedderAdapter(onnx_embedder)
            old_type = type(getattr(self.client, 'embedding_model', None)).__name__
            self.client.embedding_model = adapter
            self.embed_dims = onnx_embedder.dims
            self.log(f"Mem0 embedding swapped: {old_type} -> ONNX ({onnx_embedder.model_name}, {onnx_embedder.dims}d)")
        except Exception as exc:
            self.log(f"Mem0 ONNX adapter swap failed (keeping original): {exc}")

    def close(self) -> None:
        if not self.client:
            return
        db = getattr(self.client, "db", None)
        db_close = getattr(db, "close", None)
        if callable(db_close):
            try:
                db_close()
            except Exception:
                pass
        vector_store = getattr(self.client, "vector_store", None)
        client = getattr(vector_store, "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        self.client = None

    def add(self, user_text: str, assistant_text: str, session_id: str = "main") -> None:
        if not self.enabled or self.client is None:
            return

        snippets = self._build_snippets(user_text)
        if not snippets:
            return

        for snippet in snippets:
            if self._snippet_exists(snippet):
                continue
            payload = [{"role": "user", "content": snippet}]
            add_kwargs = {
                "user_id": self.user_id,
                "agent_id": self.agent_id,
                "metadata": {
                    "session_id": session_id,
                    "source": "mike_dialogue",
                    "role": "user",
                },
            }
            if self.mode == "oss_local" or not self.infer:
                add_kwargs["infer"] = False
            try:
                self.client.add(payload, **add_kwargs)
            except Exception as exc:
                self.log(f"Mem0 add failed: {exc}")

    def _build_snippets(self, user_text: str) -> List[str]:
        normalized = re.sub(r"\s+", " ", user_text or "").strip()
        if not normalized:
            return []
        parts = [part.strip(" -") for part in re.split(r"(?<=[.!?])\s+|\n+", normalized) if part.strip()]
        command_prefixes = (
            "responda",
            "diga",
            "me diga",
            "fale",
            "explique",
            "liste",
            "pesquise",
            "procure",
            "traduza",
            "repita",
        )
        snippets = []
        for part in parts[:3]:
            if len(part) < 24:
                continue
            lowered = part.lower()
            if lowered.startswith(command_prefixes):
                continue
            snippets.append(part)
        if not snippets and len(normalized) >= 24:
            snippets.append(normalized)
        return snippets[:3]

    def _snippet_exists(self, snippet: str) -> bool:
        target = _normalize_snippet(snippet)
        for hit in self.search(snippet, limit=5):
            if _normalize_snippet(hit.content) == target:
                return True
        return False

    def search(self, query: str, limit: int = 3, session_id: Optional[str] = None) -> List[SearchHit]:
        if not self.enabled or self.client is None:
            return []

        # mem0 >= 1.0 requires entity constraints inside ``filters`` for
        # search(), while add() still accepts top-level user_id/agent_id.
        filters = {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
        }
        search_kwargs = {"filters": filters, "limit": limit}
        scope = os.getenv("MIKE_MEM0_SCOPE", "global").strip().lower()
        if session_id and scope == "session":
            filters["session_id"] = session_id
        if self.mode == "oss_local":
            search_kwargs["rerank"] = False

        try:
            results = self.client.search(query, **search_kwargs)
        except Exception as exc:
            self.log(f"Mem0 search failed: {exc}")
            return []

        hits: List[SearchHit] = []
        raw_results = results.get("results", []) if isinstance(results, dict) else list(results or [])
        for item in list(raw_results or [])[:limit]:
            memory_text = item.get("memory") or item.get("data", {}).get("memory") or item.get("text") or ""
            if not memory_text:
                continue
            metadata = dict(item.get("metadata") or {})
            for field in ("session_id", "agent_id", "role", "user_id"):
                value = item.get(field)
                if value is not None and field not in metadata:
                    metadata[field] = value
            hits.append(
                SearchHit(
                    source="mem0",
                    title=item.get("id", "mem0"),
                    content=memory_text,
                    score=float(item.get("score") or 0.0),
                    metadata={"id": item.get("id"), **metadata},
                )
            )
        return hits
