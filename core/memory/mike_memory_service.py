# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.
#
# Module: mike_memory_service
# Extracted from mike_memory.py — Phase 3 refactor
#
# This module contains the MikeMemoryService class which orchestrates
# multiple memory backends: local SQLite (LocalMikeStore), Mem0,
# LightRAG, and the knowledge graph.

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional

log = logging.getLogger(__name__)

from mike_config import env_bool

from mike_embeddings import MikeLocalEmbedder
from mike_graph import MikeGraph
from mike_lightrag import MikeLightRAG
from mike_memory_utils import (
    _contains_any_phrase,
    _KNOWLEDGE_CONTEXT_KEYWORDS,
    _prefers_direct_answer,
)
from mike_memory_store import LocalMikeStore
from mike_mem0 import OptionalMem0Client, SearchHit, _normalize_snippet
from mike_reranker import MikeReranker


class MikeMemoryService:
    def __init__(
        self,
        db_path: Path,
        knowledge_paths: Iterable[Path],
        user_id: str,
        agent_id: str,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.log = log or (lambda _: None)
        self.local_store = LocalMikeStore(db_path, knowledge_paths, log=self.log)
        self.reranker = MikeReranker(log=self.log)
        self.lightrag_embedder = MikeLocalEmbedder(log=self.log)
        self.lightrag = MikeLightRAG(
            log=self.log,
            embedder=self.lightrag_embedder,
            reranker=self.reranker,
        )
        self.hybrid_candidate_pool = max(1, int(os.getenv("MIKE_HYBRID_CANDIDATE_POOL", "40")))
        self.rrf_k = max(1, int(os.getenv("MIKE_RRF_K", "60")))
        self.mem0 = OptionalMem0Client(
            user_id=user_id,
            agent_id=agent_id,
            storage_root=Path(db_path).parent / "mem0",
            log=self.log,
        )
        self.graph = MikeGraph(log_fn=self.log)

    def initialize(self) -> None:
        self.log("[memory] Initializing local store...")
        self.local_store.initialize()
        self.log("[memory] Local store initialized.")

        # ── Run integrity check via service (opt-in, defaults to on) ──
        if os.getenv("MIKE_MEMORY_INTEGRITY_CHECK", "1").strip().lower() in {"1", "true", "yes", "on"}:
            from datetime import datetime as _dt, timezone as _tz
            try:
                result = self.local_store.run_integrity_check()
                if not result["ok"]:
                    self.log(f"[memory] WARNING: integrity issues found: {result['errors']}")
                else:
                    self.log("[memory] Integrity check passed")
            except Exception as exc:
                self.log(f"[memory] Integrity check failed: {exc}")

        # Purge expired embedding cache entries on startup
        try:
            max_days = int(os.getenv("MIKE_EMBEDDING_CACHE_MAX_DAYS", "30"))
            if max_days > 0:
                self.local_store._purge_expired_embeddings(max_age_days=max_days)
        except Exception as exc:
            self.log(f"[memory] embedding cache purge skipped: {exc}")

        skip_backfill = env_bool("MIKE_SKIP_CONVERSATION_BACKFILL", False)
        if skip_backfill:
            self.log("[memory] Skipping conversation embedding backfill (MIKE_SKIP_CONVERSATION_BACKFILL=true)")
        else:
            # Backfill conversation embeddings for infinite RAG memory
            try:
                self.log("[memory] Backfilling conversation embeddings...")
                count = self.local_store.backfill_conversation_embeddings()
                if count:
                    self.log(f"[memory] backfilled {count} conversation embeddings")
            except Exception as exc:
                self.log(f"[memory] conversation embedding backfill failed: {exc}")

        if self.lightrag.enabled:
            try:
                self.log("[memory] Initializing LightRAG...")
                self.lightrag.ensure_ready()
            except Exception as exc:
                self.log(f"LightRAG init failed: {exc}")

        # Initialize Mem0 (deferred from __init__ to avoid import hangs)
        try:
            self.log("[memory] Initializing Mem0...")
            self.mem0.initialize()
        except Exception as exc:
            self.log(f"Mem0 initialization failed: {exc}")

        self.log("[memory] Bootstrapping Mem0 from local store...")
        self._bootstrap_mem0_from_local_store()
        self.log("[memory] Initialization complete.")



    def close(self) -> None:
        self.mem0.close()
        self.lightrag.close()
        self.graph.close()

    def add_conversation(
        self,
        timestamp: str,
        user_text: str,
        assistant_text: str,
        session_id: str = "main",
        promote_long_term: bool = True,
        importance: Optional[float] = None,
    ) -> None:
        # ── Input validation ──
        if not timestamp or not isinstance(timestamp, str) or not timestamp.strip():
            raise ValueError("timestamp must be a non-empty string")
        if not user_text or not isinstance(user_text, str) or not user_text.strip():
            raise ValueError("user_text must be a non-empty string")
        if not assistant_text or not isinstance(assistant_text, str) or not assistant_text.strip():
            raise ValueError("assistant_text must be a non-empty string")

        self.local_store.add_conversation(timestamp, user_text, assistant_text, session_id=session_id, importance=importance)
        if promote_long_term and self._should_promote_to_mem0(user_text, assistant_text):
            self.mem0.add(user_text, assistant_text, session_id=session_id)
        if self.graph.enabled:
            self.graph.add_conversation(
                user_text, assistant_text,
                timestamp=timestamp, session_id=session_id,
            )

    def search_memories(
        self,
        query: str,
        limit: int = 3,
        session_id: Optional[str] = None,
        session_only: bool = False,
    ) -> List[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            return []

        candidate_limit = max(limit, self.hybrid_candidate_pool)
        local_hits = self.local_store.search_conversations(query, limit=candidate_limit, session_id=session_id)
        if session_only and session_id:
            return local_hits[:limit]

        # ── Vector search with graceful degradation ──
        vector_hits: List[SearchHit] = []
        try:
            vector_hits = self.local_store.search_conversations_vector(query, limit=candidate_limit)
        except Exception as exc:
            self.log(f"[memory] vector search failed in search_memories: {exc}, using FTS only")

        mem0_hits = self.mem0.search(query, limit=candidate_limit, session_id=session_id) if self.mem0.enabled else []
        return self._rrf_fuse(local_hits, vector_hits, mem0_hits, limit=limit)

    def search_knowledge(self, query: str, limit: int = 4) -> List[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            return []
        candidate_limit = max(limit, self.hybrid_candidate_pool, self.reranker.candidate_limit(limit))
        bm25_hits = self.local_store.search_knowledge(query, limit=candidate_limit)

        # ── Vector search with graceful degradation ──
        vector_hits: List[SearchHit] = []
        try:
            vector_hits = self.local_store.search_knowledge_vector(query, limit=candidate_limit)
        except Exception as exc:
            self.log(f"[memory] vector search failed in search_knowledge: {exc}, using FTS only")

        fused_hits = self._rrf_fuse(bm25_hits, vector_hits, limit=candidate_limit)

        # ── Rerank with graceful degradation ──
        try:
            return self.reranker.rerank(query, fused_hits, top_k=limit)
        except Exception as exc:
            self.log(f"[memory] reranker failed: {exc}, returning fused results")
            return fused_hits[:limit]

    def reindex_knowledge(self, rebuild_lightrag: bool = True) -> int:
        indexed = self.local_store.reindex_knowledge()
        if rebuild_lightrag and self.lightrag.enabled:
            try:
                self.lightrag.rebuild_from_files(self.local_store._collect_knowledge_files())
            except Exception as exc:
                self.log(f"LightRAG reindex failed: {exc}")
        return indexed

    def upsert_knowledge_file(self, file_path: str | Path) -> bool:
        target = Path(file_path).expanduser().resolve()
        updated = self.local_store.upsert_knowledge_file(target)
        if not updated:
            return False

        if self.lightrag.enabled:
            try:
                if target.exists():
                    self.lightrag.update(target)
                else:
                    self.lightrag.rebuild_from_files(self.local_store._collect_knowledge_files())
            except Exception as exc:
                self.log(f"LightRAG incremental ingest failed for {target}: {exc}")
        return True

    def recent_messages(self, session_id: str = "main", limit: int = 5) -> List[dict]:
        return self.local_store.recent_messages(session_id=session_id, limit=limit)

    def last_session_summary(self, session_id: str, max_turns: int = 12) -> Optional[str]:
        return self.local_store.last_session_summary(session_id=session_id, max_turns=max_turns)

    def list_sessions(self, profile_key: Optional[str] = None, limit: int = 10) -> List[dict]:
        return self.local_store.list_sessions(profile_key=profile_key, limit=limit)

    def conversation_history(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[dict]:
        return self.local_store.conversation_history(session_id=session_id, limit=limit)

    # ── Memory Mesh facade ──
    @property
    def local(self) -> "LocalMikeStore":
        return self.local_store

    def mesh_auto_link(self, conversation_id: int, top_k: int = 3, threshold: float = 0.65) -> int:
        return self.local_store.mesh_auto_link(conversation_id, top_k=top_k, threshold=threshold)

    def mesh_neighbors(self, conversation_id: int, limit: int = 10) -> List[dict]:
        return self.local_store.mesh_neighbors(conversation_id, limit=limit)

    def mesh_stats(self) -> dict:
        return self.local_store.mesh_stats()

    # ── Checkpoint facade ──
    def checkpoint_save(self, session_id: str, label: Optional[str] = None, metadata: Optional[dict] = None) -> str:
        return self.local_store.checkpoint_save(session_id, label=label, metadata=metadata)

    def checkpoint_list(self, session_id: Optional[str] = None, profile: Optional[str] = None, limit: int = 20) -> List[dict]:
        return self.local_store.checkpoint_list(session_id=session_id, profile=profile, limit=limit)

    def checkpoint_restore(self, checkpoint_id: str) -> Optional[dict]:
        return self.local_store.checkpoint_restore(checkpoint_id)

    # ── Session Summary facade ──
    def session_summary_save(self, session_id: str, summary: str, topics: Optional[list] = None) -> bool:
        return self.local_store.session_summary_save(session_id, summary, topics=topics)

    def session_summaries_recent(self, profile: str, limit: int = 5) -> List[dict]:
        return self.local_store.session_summaries_recent(profile, limit=limit)

    def _should_include_memory_context(self, query: str) -> bool:
        # Always include memory context — infinite memory on disk means we
        # always try to recall relevant past conversations via RAG.
        normalized = _normalize_snippet(query)
        if not normalized:
            return False
        return True

    def _should_include_knowledge_context(self, query: str) -> bool:
        normalized = _normalize_snippet(query)
        if not normalized:
            return False
        if _contains_any_phrase(normalized, _KNOWLEDGE_CONTEXT_KEYWORDS):
            return True
        return not _prefers_direct_answer(normalized)

    def stats(self) -> dict:
        stats = self.local_store.knowledge_stats()
        stats["memory_backend"] = self.mem0.backend if self.mem0.enabled else "sqlite"
        stats["mem0_enabled"] = self.mem0.enabled
        stats["mem0_mode"] = self.mem0.mode
        stats["mem0_policy"] = self.mem0.policy
        stats["mem0_embed_provider"] = self.mem0.embed_provider
        stats["mem0_embed_model"] = self.mem0.embed_model
        stats["mem0_embed_dims"] = self.mem0.embed_dims
        stats["mem0_scope"] = os.getenv("MIKE_MEM0_SCOPE", "global").strip().lower()
        stats["mem0_save_all"] = env_bool("MIKE_MEM0_SAVE_ALL", False)
        stats["mem0_storage_root"] = str(self.mem0.storage_root)
        stats["reranker_enabled"] = self.reranker.enabled
        stats["reranker_model"] = self.reranker.model_name
        stats["reranker_device"] = self.reranker.device
        stats["vector_search_enabled"] = self.local_store.embedder.enabled
        stats["vector_search_model"] = self.local_store.embedder.model_name
        stats["vector_search_dims"] = self.local_store.embedder.dims
        stats["hybrid_candidate_pool"] = self.hybrid_candidate_pool
        stats["rrf_k"] = self.rrf_k
        stats["lightrag"] = self.lightrag.status()
        stats["graph"] = self.graph.status()
        stats["query_embed_cache"] = LocalMikeStore.query_embed_cache_stats()
        return stats

    def migrate_to_graph(self, batch_size: int = 10) -> int:
        """Migrate all existing conversations to the knowledge graph."""
        if not self.graph.enabled:
            return 0
        conversations = self.local_store.iter_conversations()
        return self.graph.migrate_conversations(conversations, batch_size=batch_size)

    def _rrf_fuse(self, *hit_lists: List[SearchHit], limit: Optional[int] = None) -> List[SearchHit]:
        scores: dict[str, float] = {}
        exemplars: dict[str, SearchHit] = {}

        for hit_list in hit_lists:
            for rank, hit in enumerate(hit_list):
                key = self._fusion_key(hit)
                scores[key] = scores.get(key, 0.0) + 1.0 / (self.rrf_k + rank + 1)
                exemplars.setdefault(key, hit)

        ranked_keys = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        fused: List[SearchHit] = []
        for key, score in ranked_keys[: limit or len(ranked_keys)]:
            hit = exemplars[key]
            metadata = dict(hit.metadata or {})
            metadata["rrf_score"] = score
            fused.append(
                SearchHit(
                    source=hit.source,
                    title=hit.title,
                    content=hit.content,
                    score=score,
                    metadata=metadata,
                )
            )
        return fused

    def _fusion_key(self, hit: SearchHit) -> str:
        metadata = hit.metadata or {}
        if isinstance(metadata, dict):
            for field in ("chunk_id", "conversation_id", "entry_hash", "id"):
                value = metadata.get(field)
                if value not in {None, ""}:
                    return f"{field}:{value}"
        return f"{hit.source}|{hit.title}|{_normalize_snippet(hit.content)[:200]}"

    def _bootstrap_mem0_from_local_store(self) -> None:
        if not self.mem0.enabled or not self.mem0.bootstrap_required:
            return

        restored = 0
        for row in self.local_store.iter_conversations():
            user_text = row["user_text"]
            assistant_text = row["assistant_text"]
            if not self._should_promote_to_mem0(user_text, assistant_text):
                continue
            self.mem0.add(user_text, assistant_text, session_id=row["session_id"])
            restored += 1

        self.mem0.bootstrap_required = False
        if restored:
            self.log(f"Mem0 bootstrap restored {restored} conversations from local SQLite.")

    def cache_web_results(
        self,
        query: str,
        results: List[dict],
        cache_dir: Path,
        provider: str = "",
    ) -> Optional[Path]:
        if not results:
            return None

        cache_dir.mkdir(parents=True, exist_ok=True)
        slug = hashlib.sha1(query.encode("utf-8", errors="ignore")).hexdigest()[:12]
        filename = f"web_{slug}.md"
        target = cache_dir / filename

        lines = [
            "# Web Search Cache",
            "",
            f"Query: {query}",
            f"Query Hash: {slug}",
            f"Captured at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"Provider: {provider or 'unknown'}",
            "",
        ]
        for index, item in enumerate(results, start=1):
            lines.extend(
                [
                    f"## Result {index}: {item.get('title', 'Untitled')}",
                    "",
                    f"URL: {item.get('url', '')}",
                    f"Age: {item.get('age', '')}",
                    "",
                    item.get("description", "").strip(),
                    "",
                ]
            )

        target.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        self.local_store.upsert_knowledge_file(target)
        return target

    def cleanup_web_cache(self, cache_dir: Path, max_age_hours: int = 24) -> int:
        """Remove expired web cache files older than max_age_hours.
        Also deduplicates files with the same query hash, keeping only the newest.
        """
        if not cache_dir.exists():
            return 0

        removed = 0
        now_s = time.time()
        expire_before_s = now_s - (max_age_hours * 3600.0) if max_age_hours > 0 else 0.0

        grouped: dict[str, List[Path]] = {}
        for file_path in cache_dir.glob("*.md"):
            # TTL-based expiration via filesystem mtime
            if max_age_hours > 0 and expire_before_s > 0:
                try:
                    if file_path.stat().st_mtime < expire_before_s:
                        file_path.unlink(missing_ok=True)
                        removed += 1
                        continue
                except OSError:
                    continue

            match = re.search(r"([0-9a-f]{12})", file_path.stem)
            group_key = match.group(1) if match else file_path.stem
            grouped.setdefault(group_key, []).append(file_path)

        # Deduplicate remaining files by query hash
        for group_key, files in grouped.items():
            files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            keeper = files[0]
            canonical = keeper.with_name(f"web_{group_key}.md")
            if keeper != canonical:
                if canonical.exists():
                    canonical.unlink()
                    removed += 1
                keeper.replace(canonical)
                keeper = canonical
            for stale in files[1:]:
                if stale == keeper or stale == canonical:
                    continue
                stale.unlink(missing_ok=True)
                removed += 1

        if removed:
            self.log(f"[memory] Web cache cleanup: {removed} files removed (max_age={max_age_hours}h)")
        return removed

    def _should_promote_to_mem0(self, user_text: str, assistant_text: str) -> bool:
        joined = f"{user_text}\n{assistant_text}".strip()
        normalized = joined.lower()
        if self._is_noisy_exchange(normalized):
            return False

        user_clean = _normalize_snippet(user_text).lower()
        assistant_clean = _normalize_snippet(assistant_text).lower()
        if not user_clean or not assistant_clean:
            return False

        if env_bool("MIKE_MEM0_SAVE_ALL", False):
            trivial_turns = {
                "ok",
                "beleza",
                "blz",
                "valeu",
                "obrigado",
                "obrigada",
                "show",
                "sim",
                "nao",
                "não",
            }
            return not (user_clean in trivial_turns and assistant_clean in trivial_turns)

        minimum = int(os.getenv("MIKE_MEM0_MIN_TOTAL_CHARS", "48"))
        if len(joined) < minimum:
            important_markers = (
                "meu nome",
                "eu gosto",
                "gosto de",
                "prefiro",
                "trabalho",
                "moro",
                "sou ",
                "tenho ",
                "quero ",
                "preciso ",
            )
            if not any(marker in normalized for marker in important_markers):
                return False
        return True

    def _is_noisy_exchange(self, normalized: str) -> bool:
        noisy_patterns = (
            "teste-nivel",
            "repete exatamente",
            "sem adicionar nada",
            "ancora",
        )
        return any(pattern in normalized for pattern in noisy_patterns)

    def build_context(
        self,
        query: str,
        memory_limit: int = 3,
        knowledge_limit: int = 4,
        session_id: Optional[str] = None,
        *,
        include_memories: bool = True,
        include_knowledge: bool = True,
    ) -> tuple[str, dict]:
        use_memory_context = include_memories and self._should_include_memory_context(query)
        use_knowledge_context = include_knowledge and self._should_include_knowledge_context(query)

        memory_hits = (
            self.search_memories(query, limit=memory_limit, session_id=session_id)[:memory_limit]
            if use_memory_context
            else []
        )
        knowledge_hits = []
        lightrag_context = ""

        if use_knowledge_context:
            if self.lightrag.enabled and self.lightrag.has_indexed_data() and self.lightrag.ensure_ready():
                try:
                    lightrag_context = self.lightrag.query_context(query)
                except Exception as exc:
                    import traceback as _tb
                    self.log(f"LightRAG query failed: {exc}\n{_tb.format_exc()}")

            if not lightrag_context:
                knowledge_hits = self.search_knowledge(query, limit=knowledge_limit)

        # Cross-reference: extrai nomes próprios dos hits primários e busca memórias relacionadas
        if memory_hits and use_memory_context:
            import re as _re
            seen_contents = {h.content for h in memory_hits}
            all_text = " ".join(h.content for h in memory_hits)
            extra_names = list(dict.fromkeys(
                n for n in _re.findall(r'\b[A-ZÁÉÍÓÚ][a-záéíóúã]{2,}\b', all_text)
                if n.lower() not in query.lower()
            ))
            for name in extra_names[:3]:
                try:
                    extra = self.search_memories(name, limit=1, session_id=session_id)
                    if extra and extra[0].content not in seen_contents:
                        memory_hits.append(extra[0])
                        seen_contents.add(extra[0].content)
                        break
                except Exception:
                    log.warning("Failed to search extra names for query enrichment")

        # ── Memory Mesh: enrich with connected memories ──
        mesh_context = ""
        if memory_hits and use_memory_context:
            for hit in memory_hits[:2]:
                conv_id = hit.metadata.get("conversation_id")
                if conv_id:
                    try:
                        mc = self.local.mesh_context(conv_id, depth=1, limit=3)
                        if mc:
                            mesh_context = mc
                            break
                    except Exception:
                        log.warning("Failed to enrich memory mesh context")

        # ── Cross-session summaries ──
        cross_session = ""
        if session_id and use_memory_context:
            try:
                cross_session = self.local.cross_session_context(session_id, max_summaries=2)
            except Exception:
                log.warning("Failed to load cross-session summaries for %s", session_id)

        sections: List[str] = []

        # Cross-session context goes first (broader context)
        if cross_session:
            sections.append(cross_session)

        if memory_hits:
            items = []
            for hit in memory_hits:
                label = hit.title if hit.source == "conversation_memory" else hit.source
                items.append(f"- [{label}] {hit.content[:500].strip()}")
            sections.append("Memorias relevantes:\n" + "\n".join(items))

        # Mesh context after direct memories
        if mesh_context:
            sections.append(mesh_context)

        if lightrag_context:
            sections.append("Base de conhecimento local (Grafo):\n" + lightrag_context.strip())
        elif knowledge_hits:
            items = []
            for hit in knowledge_hits:
                source_name = Path(hit.source).name
                items.append(f"- [{source_name} :: {hit.title}] {hit.content[:500].strip()}")
            sections.append("Base de conhecimento local:\n" + "\n".join(items))

        return (
            "\n\n".join(sections).strip(),
            {
                "memory_hits": len(memory_hits),
                "knowledge_hits": 1 if lightrag_context else len(knowledge_hits),
                "lightrag_used": bool(lightrag_context),
                "mesh_enriched": bool(mesh_context),
                "cross_session": bool(cross_session),
            },
        )
