# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import numpy as np
from openai import AsyncOpenAI

from mike_embeddings import MikeLocalEmbedder
from mike_extractors import extract_docx, extract_html, extract_pdf
from mike_reranker import MikeReranker
from mike_config import env_bool





def _read_ingest_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix in {".html", ".htm"}:
        return extract_html(path)
    if suffix in {".json", ".jsonl"}:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return path.read_text(encoding="utf-8-sig", errors="ignore")


def _split_naive_chunks(text: str, max_chars: int = 1000) -> list[str]:
    paragraphs = [part.strip() for part in str(text or "").splitlines() if part.strip()]
    if not paragraphs:
        return [str(text or "").strip()] if str(text or "").strip() else []

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start:start + max_chars].strip())
            start += max_chars
        current = ""
    if current:
        chunks.append(current)
    return chunks


class MikeLightRAG:
    def __init__(
        self,
        log: Optional[Callable[[str], None]] = None,
        *,
        enabled: Optional[bool] = None,
        working_dir: Optional[Path] = None,
        workspace: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        llm_model: Optional[str] = None,
        api_key: Optional[str] = None,
        query_mode: Optional[str] = None,
        response_type: Optional[str] = None,
        ingest_mode: Optional[str] = None,
        top_k: Optional[int] = None,
        chunk_top_k: Optional[int] = None,
        chunk_token_size: Optional[int] = None,
        max_tokens: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        embedding_timeout_seconds: Optional[int] = None,
        llm_max_async: Optional[int] = None,
        embed_max_async: Optional[int] = None,
        max_parallel_insert: Optional[int] = None,
        embedder: Optional[MikeLocalEmbedder] = None,
        reranker: Optional[MikeReranker] = None,
    ) -> None:
        self.log = log or (lambda _: None)
        self.enabled = env_bool("MIKE_LIGHTRAG_ENABLED", True) if enabled is None else bool(enabled)
        self.working_dir = Path(
            working_dir
            or os.getenv("MIKE_LIGHTRAG_WORKING_DIR")
            or "mike/memory/lightrag"
        )
        self.workspace = (workspace or os.getenv("MIKE_LIGHTRAG_WORKSPACE", "main")).strip() or "main"
        self.llm_base_url = (
            llm_base_url
            or os.getenv("MIKE_LIGHTRAG_LLM_BASE_URL")
            or "http://127.0.0.1:8080/v1"
        ).rstrip("/")
        self.llm_model = (llm_model or os.getenv("MIKE_LIGHTRAG_LLM_MODEL", "mike")).strip() or "mike"
        self.api_key = api_key or os.getenv("MIKE_LIGHTRAG_API_KEY") or os.getenv("MIKE_API_KEY") or "sk-local"
        self.query_mode = (query_mode or os.getenv("MIKE_LIGHTRAG_QUERY_MODE", "mix")).strip().lower() or "mix"
        self.ingest_mode = (ingest_mode or os.getenv("MIKE_LIGHTRAG_INGEST_MODE", "naive")).strip().lower() or "naive"
        self.response_type = (
            response_type
            or os.getenv("MIKE_LIGHTRAG_RESPONSE_TYPE", "Multiple Paragraphs")
        ).strip() or "Multiple Paragraphs"
        self.top_k = max(1, int(top_k or os.getenv("MIKE_LIGHTRAG_TOP_K", "20")))
        self.chunk_top_k = max(1, int(chunk_top_k or os.getenv("MIKE_LIGHTRAG_CHUNK_TOP_K", "10")))
        self.chunk_token_size = max(256, int(chunk_token_size or os.getenv("MIKE_LIGHTRAG_CHUNK_TOKEN_SIZE", "1200")))
        self.max_tokens = max(128, int(max_tokens or os.getenv("MIKE_LIGHTRAG_MAX_TOKENS", "1024")))
        self.timeout_seconds = max(10, int(timeout_seconds or os.getenv("MIKE_LIGHTRAG_TIMEOUT_SECONDS", "180")))
        self.embedding_timeout_seconds = max(
            10,
            int(
                embedding_timeout_seconds
                or os.getenv("MIKE_LIGHTRAG_EMBED_TIMEOUT_SECONDS")
                or str(max(self.timeout_seconds, 120))
            ),
        )
        self.llm_max_async = max(1, int(llm_max_async or os.getenv("MIKE_LIGHTRAG_LLM_MAX_ASYNC", "1")))
        self.embed_max_async = max(1, int(embed_max_async or os.getenv("MIKE_LIGHTRAG_EMBED_MAX_ASYNC", "2")))
        self.max_parallel_insert = max(1, int(max_parallel_insert or os.getenv("MIKE_LIGHTRAG_MAX_PARALLEL_INSERT", "1")))
        self.embedder = embedder or MikeLocalEmbedder(log=self.log)
        self.reranker = reranker or MikeReranker(log=self.log)
        self._client: Optional[AsyncOpenAI] = None
        self._rag = None
        self._query_param_cls = None
        self._loop_factory = None
        self._load_error: Optional[str] = None
        self.document_count = 0
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_loop_thread: Optional[threading.Thread] = None
        self._embedder_warmed = False

    def ensure_ready(self) -> bool:
        if self._rag is not None:
            return True
        if not self.enabled:
            return False
        if self._load_error is not None:
            return False

        try:
            from lightrag import LightRAG, QueryParam
            from lightrag.utils import always_get_an_event_loop, wrap_embedding_func_with_attrs

            @wrap_embedding_func_with_attrs(
                embedding_dim=self.embedder.dims,
                max_token_size=self.chunk_token_size,
                model_name=self.embedder.model_name,
            )
            async def _embedding_func(texts: list[str], **kwargs) -> np.ndarray:
                return await asyncio.to_thread(self._embed_batch, texts)

            self._warm_embedder()
            self.working_dir.mkdir(parents=True, exist_ok=True)
            self._rag = LightRAG(
                working_dir=str(self.working_dir),
                workspace=self.workspace,
                top_k=self.top_k,
                chunk_top_k=self.chunk_top_k,
                chunk_token_size=self.chunk_token_size,
                embedding_func=_embedding_func,
                llm_model_func=self._llm_complete,
                llm_model_name=self.llm_model,
                llm_model_kwargs={
                    "max_tokens": self.max_tokens,
                    "temperature": 0.2,
                    "top_p": 0.95,
                },
                default_embedding_timeout=self.embedding_timeout_seconds,
                default_llm_timeout=self.timeout_seconds,
                llm_model_max_async=self.llm_max_async,
                embedding_func_max_async=self.embed_max_async,
                max_parallel_insert=self.max_parallel_insert,
                rerank_model_func=self._rerank_documents if self.reranker.enabled else None,
            )
            self._query_param_cls = QueryParam
            self._loop_factory = always_get_an_event_loop
            self._run_coroutine_blocking(self._rag.initialize_storages())
            return True
        except Exception as exc:
            self._load_error = str(exc)
            self.log(f"LightRAG unavailable: {exc}")
            self._rag = None
            return False

    def __deepcopy__(self, memo: dict):
        # LightRAG calls dataclasses.asdict(self) internally, which deepcopies
        # our llm_model_func (a bound method of this class). Python's deepcopy
        # then tries to copy __self__ (this instance), which contains
        # self._client (AsyncOpenAI / httpx) that holds non-picklable asyncio
        # ContextVar objects.  Returning self avoids the error: config dicts
        # built by LightRAG only need the function references, not a true copy.
        memo[id(self)] = self
        return self

    def close(self) -> None:
        if self._rag is not None and self._loop_factory is not None:
            try:
                self._run_coroutine_blocking(self._rag.finalize_storages())
            except Exception as exc:
                self.log(f"LightRAG finalize failed: {exc}")
        if self._client is not None:
            try:
                self._run_coroutine_blocking(self._client.close())
            except Exception as exc:
                self.log(f"LightRAG client close failed: {exc}")
        self._rag = None
        self._client = None
        self._stop_async_loop()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "ready": self._rag is not None,
            "load_error": self._load_error,
            "working_dir": str(self.working_dir),
            "workspace": self.workspace,
            "document_count": self.document_count,
            "has_indexed_data": self.has_indexed_data(),
            "ingest_mode": self.ingest_mode,
            "query_mode": self.query_mode,
            "response_type": self.response_type,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "embedding_timeout_seconds": self.embedding_timeout_seconds,
            "llm_max_async": self.llm_max_async,
            "embed_max_async": self.embed_max_async,
            "max_parallel_insert": self.max_parallel_insert,
            "embed_model": self.embedder.model_name,
            "embed_dims": self.embedder.dims,
            "reranker_enabled": self.reranker.enabled,
        }

    def has_indexed_data(self) -> bool:
        workspace_dir = self._workspace_dir()
        candidate_files = (
            workspace_dir / "vdb_chunks.json",
            workspace_dir / "kv_store_text_chunks.json",
            workspace_dir / "graph_chunk_entity_relation.graphml",
        )
        return any(path.exists() and path.stat().st_size > 0 for path in candidate_files)

    def ingest(self, file_path: str | Path) -> str:
        source_path = Path(file_path)
        text = _read_ingest_file(source_path)
        return self.insert_text(text, doc_id=str(source_path.resolve()), file_path=source_path)

    def insert_text(
        self,
        text: str,
        *,
        doc_id: Optional[str] = None,
        file_path: Optional[str | Path] = None,
    ) -> str:
        if not self.ensure_ready() or self._rag is None:
            raise RuntimeError(self._load_error or "LightRAG is not available.")
        file_ref = None if file_path is None else str(Path(file_path))
        normalized_text = str(text or "").strip()
        if self.ingest_mode == "naive":
            self._seed_naive_chunks(text, doc_id=doc_id, file_path=file_path)
            track_id = f"naive-{doc_id or 'doc'}"
        else:
            track_id = self._rag.insert(
                text,
                ids=doc_id,
                file_paths=file_ref,
            )
            if normalized_text and not self.has_indexed_data():
                self.log("LightRAG graph extraction produced no queryable chunks; resetting workspace and seeding naive chunk index.")
                self.reset_storage()
                self.ensure_ready()
                self._seed_naive_chunks(text, doc_id=doc_id, file_path=file_path)
        if normalized_text:
            self.document_count += 1
        return track_id

    def _warm_embedder(self) -> None:
        if self._embedder_warmed or not getattr(self.embedder, "enabled", False):
            return
        if getattr(self.embedder, "backend", "") == "onnx":
            if not self.embedder.prepare_onnx_files():
                raise RuntimeError("LightRAG ONNX embedder preparation failed.")
            self._embedder_warmed = True
            return
        warmup_text = "Mike LightRAG warmup"
        vectors = self.embedder.embed_texts([warmup_text])
        if not vectors:
            raise RuntimeError("LightRAG embedder warmup failed.")
        self._embedder_warmed = True

    def update(self, file_path: str | Path) -> bool:
        self.ingest(file_path)
        return True

    def rebuild_from_files(self, files: Sequence[str | Path]) -> int:
        normalized_files = sorted(
            {
                Path(file_path).expanduser().resolve()
                for file_path in files
                if Path(file_path).exists()
            }
        )
        self.reset_storage()
        if not normalized_files:
            self.document_count = 0
            return 0
        if not self.ensure_ready():
            raise RuntimeError(self._load_error or "LightRAG is not available.")

        ingested = 0
        for file_path in normalized_files:
            try:
                self.ingest(file_path)
                ingested += 1
            except Exception as exc:
                self.log(f"LightRAG ingest failed for {file_path}: {exc}")
        self.document_count = ingested
        return ingested

    def reset_storage(self) -> None:
        self.close()
        self._load_error = None
        self.document_count = 0
        workspace_dir = self._workspace_dir()
        if workspace_dir.exists():
            resolved_workspace = workspace_dir.resolve()
            resolved_root = self.working_dir.resolve()
            if str(resolved_workspace).startswith(str(resolved_root)):
                shutil.rmtree(resolved_workspace, ignore_errors=False)

    def query(
        self,
        question: str,
        mode: Optional[str] = None,
        *,
        conversation_history: Optional[Sequence[dict[str, str]]] = None,
        include_references: bool = False,
        response_type: Optional[str] = None,
        only_need_context: bool = False,
    ) -> str:
        if not self.ensure_ready() or self._rag is None or self._query_param_cls is None:
            raise RuntimeError(self._load_error or "LightRAG is not available.")
        effective_mode = mode or self.query_mode or "mix"
        if mode is None and self.has_indexed_data() and not self._has_graph_data():
            effective_mode = "naive"
        params = self._query_param_cls(
            mode=effective_mode,
            top_k=self.top_k,
            chunk_top_k=self.chunk_top_k,
            only_need_context=only_need_context,
            response_type=response_type or self.response_type,
            conversation_history=list(conversation_history or []),
            enable_rerank=self.reranker.enabled,
            include_references=include_references,
        )
        result = self._rag.query(question, param=params)
        return str(result or "").strip()

    def query_data(
        self,
        question: str,
        mode: Optional[str] = None,
        *,
        conversation_history: Optional[Sequence[dict[str, str]]] = None,
    ) -> dict[str, Any]:
        if not self.ensure_ready() or self._rag is None or self._query_param_cls is None:
            raise RuntimeError(self._load_error or "LightRAG is not available.")
        effective_mode = mode or self.query_mode or "mix"
        if mode is None and self.has_indexed_data() and not self._has_graph_data():
            effective_mode = "naive"
        params = self._query_param_cls(
            mode=effective_mode,
            top_k=self.top_k,
            chunk_top_k=self.chunk_top_k,
            response_type="Bullet Points",
            conversation_history=list(conversation_history or []),
            enable_rerank=self.reranker.enabled,
        )
        return self._run_coroutine_blocking(self._rag.aquery_data(question, param=params))

    def query_context(
        self,
        question: str,
        mode: Optional[str] = None,
        *,
        conversation_history: Optional[Sequence[dict[str, str]]] = None,
    ) -> str:
        payload = self.query_data(
            question,
            mode=mode,
            conversation_history=conversation_history,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return ""

        sections: list[str] = []

        entities = data.get("entities") or []
        if isinstance(entities, list) and entities:
            entity_lines = []
            for item in entities[: min(5, len(entities))]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("entity_name") or item.get("name") or "entity").strip()
                description = str(item.get("description") or "").strip()
                if description:
                    entity_lines.append(f"- [{name}] {description[:400]}")
            if entity_lines:
                sections.append("Entidades relevantes:\n" + "\n".join(entity_lines))

        relationships = data.get("relationships") or []
        if isinstance(relationships, list) and relationships:
            relation_lines = []
            for item in relationships[: min(5, len(relationships))]:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("src_id") or "").strip()
                tgt = str(item.get("tgt_id") or "").strip()
                description = str(item.get("description") or "").strip()
                label = f"{src} -> {tgt}".strip(" ->")
                if label and description:
                    relation_lines.append(f"- [{label}] {description[:400]}")
            if relation_lines:
                sections.append("Relacoes relevantes:\n" + "\n".join(relation_lines))

        chunks = data.get("chunks") or []
        if isinstance(chunks, list) and chunks:
            chunk_lines = []
            for item in chunks[: min(6, len(chunks))]:
                if not isinstance(item, dict):
                    continue
                file_path = Path(str(item.get("file_path") or "")).name or "chunk"
                content = str(item.get("content") or "").strip()
                if content:
                    chunk_lines.append(f"- [{file_path}] {content[:500]}")
            if chunk_lines:
                sections.append("Trechos relevantes:\n" + "\n".join(chunk_lines))

        return "\n\n".join(sections).strip()

    def _embed_batch(self, texts: Sequence[str]) -> np.ndarray:
        cleaned_texts = [str(text or "").strip() for text in texts]
        if not cleaned_texts:
            return np.zeros((0, self.embedder.dims), dtype=np.float32)
        vectors = self.embedder.embed_texts(cleaned_texts)
        if len(vectors) != len(cleaned_texts):
            raise RuntimeError("Local embedding backend returned an incomplete batch.")
        return np.vstack([np.asarray(vector, dtype=np.float32) for vector in vectors])

    async def _llm_complete(
        self,
        prompt,
        system_prompt=None,
        history_messages=None,
        keyword_extraction=False,
        **kwargs,
    ):
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.llm_base_url,
            )

        history_messages = list(history_messages or [])
        messages = kwargs.pop("messages", None)
        if messages is None:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.extend(history_messages)
            messages.append({"role": "user", "content": prompt})

        kwargs.pop("hashing_kv", None)
        kwargs.pop("keyword_extraction", None)

        extra_body = dict(kwargs.pop("extra_body", {}) or {})
        extra_body.setdefault("raw_mode", True)
        extra_body.setdefault("private_mode", True)
        extra_body.setdefault(
            "chat_template_kwargs",
            {"enable_thinking": False},
        )

        payload: dict[str, Any] = {
            "model": self.llm_model,
            "messages": messages,
            "stream": bool(kwargs.pop("stream", False)),
            "max_tokens": int(kwargs.pop("max_tokens", self.max_tokens)),
            "temperature": float(kwargs.pop("temperature", 0.2)),
            "top_p": float(kwargs.pop("top_p", 0.95)),
        }
        if keyword_extraction:
            payload["temperature"] = 0.0
        stop = kwargs.pop("stop", None)
        if stop:
            payload["stop"] = stop
        response = await self._client.chat.completions.create(
            **payload,
            extra_body=extra_body,
            timeout=None,
        )
        if payload["stream"]:

            async def _iter_chunks():
                async for chunk in response:
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    content = getattr(delta, "content", None) if delta is not None else None
                    if content:
                        yield content

            return _iter_chunks()

        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        return getattr(message, "content", "") or ""

    async def _rerank_documents(
        self,
        query: str,
        documents: list[str],
        top_n: Optional[int] = None,
        extra_body: Optional[dict] = None,
    ) -> list[dict]:
        if not documents:
            return []
        if not self.reranker._ensure_backend():
            limit = min(top_n or len(documents), len(documents))
            return [
                {"index": index, "relevance_score": float(limit - index)}
                for index in range(limit)
            ]

        def _compute_scores() -> list[float]:
            scores = self.reranker._backend.compute_score(
                [[query, str(document or "")] for document in documents],
                batch_size=self.reranker.batch_size,
                max_length=self.reranker.max_length,
            )
            return scores if isinstance(scores, list) else [scores]

        scores = await asyncio.to_thread(_compute_scores)
        indexed_scores = []
        for index, score in enumerate(scores):
            try:
                indexed_scores.append((index, float(score)))
            except (TypeError, ValueError):
                indexed_scores.append((index, 0.0))
        indexed_scores.sort(key=lambda item: (-item[1], item[0]))
        limit = min(top_n or len(indexed_scores), len(indexed_scores))
        return [
            {"index": index, "relevance_score": score}
            for index, score in indexed_scores[:limit]
        ]

    def _workspace_dir(self) -> Path:
        return self.working_dir / self.workspace

    def _seed_naive_chunks(
        self,
        text: str,
        *,
        doc_id: Optional[str] = None,
        file_path: Optional[str | Path] = None,
    ) -> None:
        if not self.ensure_ready() or self._rag is None:
            return
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return
        source_id = str(doc_id or Path(file_path).name if file_path else "doc")
        chunks = _split_naive_chunks(normalized_text, max_chars=self.chunk_token_size)
        custom_kg = {
            "chunks": [
                {
                    "content": chunk,
                    "source_id": f"{source_id}:{index}",
                    "file_path": str(file_path or ""),
                    "chunk_order_index": index,
                }
                for index, chunk in enumerate(chunks)
            ],
            "entities": [],
            "relationships": [],
        }
        self._run_coroutine_blocking(
            self._rag.ainsert_custom_kg(custom_kg, full_doc_id=doc_id or source_id)
        )

    def _has_graph_data(self) -> bool:
        workspace_dir = self._workspace_dir()
        candidate_files = (
            workspace_dir / "vdb_entities.json",
            workspace_dir / "vdb_relationships.json",
            workspace_dir / "kv_store_full_entities.json",
            workspace_dir / "kv_store_full_relations.json",
        )
        return any(path.exists() and path.stat().st_size > 64 for path in candidate_files)

    def _run_coroutine_blocking(self, coro):
        loop = self._ensure_async_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    def _ensure_async_loop(self) -> asyncio.AbstractEventLoop:
        if self._async_loop is not None and not self._async_loop.is_closed():
            return self._async_loop

        ready = threading.Event()

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._async_loop = loop
            ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        thread = threading.Thread(target=_runner, daemon=True)
        self._async_loop_thread = thread
        thread.start()
        ready.wait()
        if self._async_loop is None:
            raise RuntimeError("Failed to create LightRAG async loop.")
        return self._async_loop

    def _stop_async_loop(self) -> None:
        loop = self._async_loop
        thread = self._async_loop_thread
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._async_loop = None
        self._async_loop_thread = None
