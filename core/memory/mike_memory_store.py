# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""LocalMikeStore — local vector+SQLite memory store for MIKE.

Provides conversation history, knowledge indexing, memory mesh,
session checkpoints, session summaries, and vector search.
"""

# Extracted from mike_memory.py — Phase 3 refactor

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

from mike_embeddings import MikeLocalEmbedder
from mike_mem0 import SearchHit

# ── Utility helpers from mike_memory_utils ──
from mike_memory_utils import (
    TEXT_EXTENSIONS,
    _DIRECT_CHAT_PATTERNS,
    _read_text_file,
    _sanitize_query,
    chunk_text,
)

log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# Resilience helpers — retry with exponential backoff, input validation
# ════════════════════════════════════════════════════════════════════════════

_SQLITE_LOCKED_CODES: Tuple[int, ...] = (5, 6)  # SQLITE_BUSY, SQLITE_LOCKED
_RETRY_BASE_DELAY_MS = max(10, int(os.getenv("MIKE_MEMORY_RETRY_BASE_MS", "50")))
_RETRY_MAX_ATTEMPTS = max(1, int(os.getenv("MIKE_MEMORY_RETRY_MAX_ATTEMPTS", "5")))
_RETRY_MAX_DELAY_MS = max(100, int(os.getenv("MIKE_MEMORY_RETRY_MAX_DELAY_MS", "3000")))


def _is_sqlite_locked(error: Exception) -> bool:
    """Return True if the error is a transient SQLite lock/busy error."""
    if isinstance(error, sqlite3.OperationalError):
        msg = str(error).lower()
        return "locked" in msg or "busy" in msg
    return False


def retry_on_lock(func):
    """Decorator: retry on SQLite locked/busy errors with exponential backoff."""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        last_exc: Optional[Exception] = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                return func(self, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_locked(exc):
                    raise
                last_exc = exc
                delay_ms = min(
                    _RETRY_BASE_DELAY_MS * (2 ** attempt),
                    _RETRY_MAX_DELAY_MS,
                )
                self.log(
                    f"[memory:retry] {func.__name__} attempt {attempt + 1}/{_RETRY_MAX_ATTEMPTS} "
                    f"failed: {exc}. Retrying in {delay_ms}ms..."
                )
                time.sleep(delay_ms / 1000.0)
            except Exception:
                raise
        raise last_exc  # type: ignore[misc]

    return wrapper


def _validate_non_empty(value: Optional[str], label: str) -> Optional[str]:
    """Return stripped value if non-empty, else raise ValueError."""
    if value is None:
        return None
    stripped = str(value).strip()
    if not stripped:
        raise ValueError(f"{label} must not be empty")
    return stripped


def _validate_positive(value: int, label: str) -> int:
    """Return value if > 0, else raise ValueError."""
    if value <= 0:
        raise ValueError(f"{label} must be positive, got {value}")
    return value


# ════════════════════════════════════════════════════════════════════════════

class LocalMikeStore:
    # In-memory LRU cache shared across all instances to avoid re-embedding
    # the same query text when multiple search paths (conversations, knowledge,
    # LightRAG) all embed the same user query within a single build_context call.
    _QUERY_EMBED_CACHE: dict[str, np.ndarray] = {}
    _QUERY_EMBED_CACHE_MAX = 256
    _QUERY_EMBED_CACHE_HITS = 0

    def __init__(
        self,
        db_path: Path,
        knowledge_paths: Iterable[Path],
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.knowledge_paths = [Path(path) for path in knowledge_paths]
        self.log = log or (lambda _: None)
        self.embedder = MikeLocalEmbedder(log=self.log)
        self._init_lock = threading.Lock()
        # ── Memory budget config (opt-in via env vars) ──
        self._max_conversations: int = max(0, int(os.getenv("MIKE_MEMORY_MAX_CONVERSATIONS", "0")))
        self._max_db_size_mb: float = max(0.0, float(os.getenv("MIKE_MEMORY_MAX_DB_SIZE_MB", "0")))
        self._prune_batch: int = max(10, int(os.getenv("MIKE_MEMORY_PRUNE_BATCH", "100")))
        self._backup_enabled: bool = os.getenv("MIKE_MEMORY_BACKUP_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
        self._vector_fallback: bool = os.getenv("MIKE_VECTOR_FALLBACK_TO_FTS", "1").strip().lower() in {"1", "true", "yes", "on"}
        # Resolve backup dir
        _backup_env = os.getenv("MIKE_MEMORY_BACKUP_DIR", "").strip()
        self._backup_dir: Path = (
            Path(_backup_env).expanduser().resolve()
            if _backup_env
            else self.db_path.parent / "backups"
        )

    # ═══════════════════════════════════════════════════════════════════
    # Resilience: integrity check, backup, budget enforcement
    # ═══════════════════════════════════════════════════════════════════

    def run_integrity_check(self) -> Dict[str, Any]:
        """Run PRAGMA integrity_check and return {ok, errors, quick_check_ok}.

        This is the public entry point for integrity verification. It is also
        called by :meth:`initialize` when MIKE_MEMORY_INTEGRITY_CHECK=1.
        """
        result: Dict[str, Any] = {"ok": True, "errors": [], "quick_check_ok": True}
        try:
            with self._connect() as conn:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
                for row in rows:
                    msg = str(row[0]) if row else ""
                    if msg and msg != "ok":
                        result["ok"] = False
                        result["errors"].append(msg)
                        self.log(f"[memory:integrity] FAIL: {msg}")
                # Quick check (faster, binary)
                quick = conn.execute("PRAGMA quick_check").fetchone()
                if quick:
                    qc_val = str(quick[0])
                    if qc_val != "ok":
                        result["quick_check_ok"] = False
                        result["errors"].append(f"quick_check: {qc_val}")
        except Exception as exc:
            result["ok"] = False
            result["errors"].append(f"integrity check raised: {exc}")
            self.log(f"[memory:integrity] exception: {exc}")
        if result["ok"]:
            self.log("[memory:integrity] check passed")
        return result

    def backup_database(self, label: str = "") -> Optional[Path]:
        """Create a timestamped backup of the SQLite database file.

        Returns the backup path, or None if the db file does not exist.
        The *label* is appended to the filename for identification.
        """
        if not self._backup_enabled:
            return None
        src = self.db_path
        if not src.exists():
            self.log("[memory:backup] DB file not found, nothing to back up")
            return None
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{label}" if label else ""
        dst = self._backup_dir / f"{src.stem}-{ts}{suffix}{src.suffix}"
        try:
            shutil.copy2(src, dst)
            self.log(f"[memory:backup] created {dst.name}")
            # Prune old backups, keep last 20
            existing = sorted(self._backup_dir.glob(f"{src.stem}-*.db"), key=lambda p: p.stat().st_mtime)
            for old in existing[:-20]:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass
            return dst
        except Exception as exc:
            self.log(f"[memory:backup] FAILED: {exc}")
            return None

    def _backup_before_migration(self, label: str = "pre-migration") -> Optional[Path]:
        """Convenience: backup if enabled. Always called before schema changes."""
        return self.backup_database(label=label)

    @retry_on_lock
    def _enforce_memory_budget(self, conn: sqlite3.Connection) -> int:
        """Automatically prune oldest conversation entries if budget exceeded.

        Returns the number of pruned rows (0 if budgets are disabled or not exceeded).
        """
        pruned = 0

        # ── Check DB file size ──
        if self._max_db_size_mb > 0 and self.db_path.exists():
            size_mb = self.db_path.stat().st_size / (1024.0 * 1024.0)
            if size_mb > self._max_db_size_mb:
                excess_mb = size_mb - self._max_db_size_mb
                self.log(
                    f"[memory:budget] DB size {size_mb:.1f}MB exceeds limit "
                    f"{self._max_db_size_mb:.1f}MB by {excess_mb:.1f}MB, pruning..."
                )
                # Prune oldest conversations until under budget
                for _ in range(10):  # safety: max 10 rounds
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM conversations"
                    ).fetchone()
                    if row and row["cnt"] == 0:
                        break
                    conn.execute(
                        """
                        DELETE FROM conversations
                        WHERE id IN (
                            SELECT id FROM conversations
                            ORDER BY id ASC LIMIT ?
                        )
                        """,
                        (self._prune_batch,),
                    )
                    conn.execute("DELETE FROM conversations_fts WHERE rowid NOT IN (SELECT id FROM conversations)")
                    conn.execute("DELETE FROM conversation_embeddings WHERE conversation_id NOT IN (SELECT id FROM conversations)")
                    pruned += self._prune_batch
                    self.db_path.stat().st_size  # force refresh
                    new_size_mb = self.db_path.stat().st_size / (1024.0 * 1024.0)
                    if new_size_mb <= self._max_db_size_mb:
                        break

        # ── Check row count ──
        if self._max_conversations > 0:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM conversations").fetchone()
            current = int(row["cnt"]) if row else 0
            if current > self._max_conversations:
                excess = current - self._max_conversations
                to_remove = min(excess + self._prune_batch, current)
                self.log(
                    f"[memory:budget] {current} conversations exceeds limit "
                    f"{self._max_conversations}, pruning {to_remove} oldest..."
                )
                conn.execute(
                    """
                    DELETE FROM conversations
                    WHERE id IN (
                        SELECT id FROM conversations
                        ORDER BY id ASC LIMIT ?
                    )
                    """,
                    (to_remove,),
                )
                conn.execute("DELETE FROM conversations_fts WHERE rowid NOT IN (SELECT id FROM conversations)")
                conn.execute("DELETE FROM conversation_embeddings WHERE conversation_id NOT IN (SELECT id FROM conversations)")
                pruned += to_remove

        if pruned:
            self.log(f"[memory:budget] pruned {pruned} entries total")
        return pruned

    def initialize(self) -> None:
        # ── Integrity check (opt-in via env var, defaults to on) ──
        if os.getenv("MIKE_MEMORY_INTEGRITY_CHECK", "1").strip().lower() in {"1", "true", "yes", "on"}:
            self.log("[memory] Running integrity check...")
            result = self.run_integrity_check()
            if not result["ok"]:
                self.log(f"[memory] WARNING: integrity check found issues: {result['errors']}")
                if result["errors"]:
                    raise RuntimeError(
                        f"SQLite integrity check failed: {result['errors']}"
                    )

        self.log("[memory] Ensuring storage directory...")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log("[memory] Creating tables...")
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_hash TEXT UNIQUE,
                    timestamp TEXT,
                    session_id TEXT DEFAULT 'main',
                    user_text TEXT,
                    assistant_text TEXT,
                    importance REAL DEFAULT 0.5,
                    importance_source TEXT DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts
                USING fts5(user_text, assistant_text, combined)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    title TEXT,
                    kind TEXT,
                    chunk_index INTEGER,
                    checksum TEXT,
                    content TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
                USING fts5(source, title, content)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_embeddings (
                    chunk_id INTEGER PRIMARY KEY,
                    cache_key TEXT,
                    model TEXT,
                    dims INTEGER,
                    vector BLOB
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    cache_key TEXT PRIMARY KEY,
                    scope TEXT,
                    model TEXT,
                    dims INTEGER,
                    vector BLOB,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_embeddings (
                    conversation_id INTEGER PRIMARY KEY,
                    cache_key TEXT,
                    model TEXT,
                    dims INTEGER,
                    vector BLOB
                )
                """
            )
            self._ensure_schema(conn)
            conn.execute("PRAGMA optimize")
            conn.commit()

        self.log("[memory] Reindexing knowledge...")
        self.reindex_knowledge()
        self.log("[memory] Knowledge reindexed.")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")        # 8 MiB page cache
        conn.execute("PRAGMA mmap_size=268435456")      # 256 MiB memory-mapped I/O
        conn.execute("PRAGMA temp_store=MEMORY")        # Temp tables in memory
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self, conn) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(conversations)").fetchall()
        }

        # ── Detect if migrations are needed and backup first ──
        needs_migration = (
            "session_id" not in columns
            or "importance" not in columns
        )
        knowledge_embed_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(knowledge_embeddings)").fetchall()
        }
        needs_migration = needs_migration or ("created_at" not in knowledge_embed_cols)

        if needs_migration:
            self._backup_before_migration(label="pre-schema-migration")

        if "session_id" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN session_id TEXT DEFAULT 'main'")
            conn.execute(
                "UPDATE conversations SET session_id = 'main' WHERE session_id IS NULL OR session_id = ''"
            )
        if "importance" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN importance REAL DEFAULT 0.5")
            conn.execute("ALTER TABLE conversations ADD COLUMN importance_source TEXT DEFAULT ''")
            conn.execute(
                "UPDATE conversations SET importance = 0.5 WHERE importance IS NULL"
            )

        # ── knowledge_embeddings.created_at migration ──
        knowledge_embed_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(knowledge_embeddings)").fetchall()
        }
        if "created_at" not in knowledge_embed_cols:
            conn.execute(
                "ALTER TABLE knowledge_embeddings ADD COLUMN created_at TEXT"
            )
            conn.execute(
                "UPDATE knowledge_embeddings SET created_at = ? WHERE created_at IS NULL",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),),
            )

        # ── Memory Mesh: links between related memories ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_mesh (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                relation TEXT DEFAULT 'similar',
                strength REAL DEFAULT 0.0,
                created_at TEXT,
                UNIQUE(source_id, target_id, relation)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_mesh_source ON memory_mesh(source_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_mesh_target ON memory_mesh(target_id)
        """)

        # ── Session Checkpoints: save/restore session state ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checkpoint_id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                profile TEXT,
                label TEXT,
                created_at TEXT NOT NULL,
                turn_count INTEGER DEFAULT 0,
                summary TEXT,
                metadata_json TEXT
            )
        """)

        # ── Session Summaries: persistent cross-session memory ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                profile TEXT,
                turn_count INTEGER DEFAULT 0,
                topics_json TEXT,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)

        # ── Performance indexes for frequent query patterns ──
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_timestamp ON conversations(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_importance ON conversations(importance)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ce_model ON conversation_embeddings(model)"
        )

    def _conversation_hash(
        self,
        timestamp: str,
        user_text: str,
        assistant_text: str,
        session_id: str = "main",
    ) -> str:
        normalized_session = (session_id or "main").strip() or "main"
        if normalized_session == "main":
            payload = f"{timestamp}\n{user_text}\n{assistant_text}".encode("utf-8", errors="ignore")
        else:
            payload = (
                f"{timestamp}\n{normalized_session}\n{user_text}\n{assistant_text}".encode(
                    "utf-8",
                    errors="ignore",
                )
            )
        return hashlib.sha1(payload).hexdigest()

    @staticmethod
    def _estimate_importance(user_text: str, assistant_text: str) -> tuple[float, str]:
        """Heuristic importance scoring based on content signals.
        Returns (score 0.0-1.0, source_label).
        Enhanced version can use LLM for more accurate scoring.
        """
        combined = f"{user_text}\n{assistant_text}".lower()
        score = 0.5  # baseline
        signals: list[str] = []

        # High importance signals
        high_patterns = [
            (r"\b(?:senha|password|credencial|token|api.key|secret)\b", 0.95, "credentials"),
            (r"\b(?:urgente|emergencia|socorro|grave|critic[oa])\b", 0.90, "urgency"),
            (r"\b(?:decis[aã]o|decid[ie]|escolh[ae]|defin[ae])\b", 0.80, "decision"),
            (r"\b(?:fam[íi]lia|filh[oa]|espos[ao]|m[aã]e|pai|irm[ãa]o)\b", 0.85, "family"),
            (r"\b(?:trabalho|emprego|entrevista|promo[cç][aã]o|sal[áa]rio)\b", 0.80, "work"),
            (r"\b(?:doen[çc]a|m[ée]dico|hospital|sa[úu]de|exame)\b", 0.85, "health"),
            (r"\b(?:endere[çc]o|telefone|whatsapp|email|contato)\b", 0.75, "contact"),
            (r"\b(?:obrigado|valeu|obrigada|thanks)\b.*\b(?:ajud|resolveu|funcionou|perfeito)\b", 0.80, "gratitude"),
            (r"\b(?:n[aã]o funcionou|quebrou|bug|erro|falhou|problema)\b", 0.75, "problem"),
            (r"\b(?:aprend[aei]|descobri|entendi|percebi|notei)\b", 0.70, "learning"),
            (r"\b(?:c[óo]digo|script|programa|desenvolve|implementa)\b", 0.75, "development"),
        ]
        for pattern, weight, source in high_patterns:
            import re as _re
            if _re.search(pattern, combined):
                score = max(score, weight)
                signals.append(source)

        # Low importance signals
        low_patterns = [
            r"\b(?:oi|ol[aá]|tudo bem|tudo bom|blz|beleza|show)\b",
            r"\b(?:rs|kkk|haha|lol|hehe)\b",
        ]
        is_trivial = False
        for pattern in low_patterns:
            import re as _re
            if _re.search(pattern, combined) and len(combined) < 100:
                is_trivial = True
                break
        if is_trivial and not signals:
            score = 0.15
            signals.append("trivial")

        source_label = ", ".join(signals[:3]) if signals else "default"
        return round(min(1.0, max(0.1, score)), 2), source_label

    @retry_on_lock
    def add_conversation(
        self,
        timestamp: str,
        user_text: str,
        assistant_text: str,
        session_id: str = "main",
        importance: Optional[float] = None,
    ) -> bool:
        # ── Input validation ──
        try:
            _validate_non_empty(timestamp, "timestamp")
            _validate_non_empty(user_text, "user_text")
            _validate_non_empty(assistant_text, "assistant_text")
        except ValueError as exc:
            self.log(f"[memory:validation] add_conversation rejected: {exc}")
            return False

        # Validate embedding vectors are not nonsense (all zeros, NaN, inf)
        if importance is not None:
            importance = max(0.0, min(1.0, float(importance)))

        normalized_session = (session_id or "main").strip() or "main"
        entry_hash = self._conversation_hash(timestamp, user_text, assistant_text, session_id=normalized_session)
        combined = f"{user_text}\n{assistant_text}"

        # Auto-estimate importance if not provided
        if importance is None:
            importance, importance_source = self._estimate_importance(user_text, assistant_text)
        else:
            importance_source = "explicit"

        with self._connect() as conn:
            # ── Memory budget enforcement ──
            self._enforce_memory_budget(conn)

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO conversations (entry_hash, timestamp, session_id, user_text, assistant_text, importance, importance_source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_hash, timestamp, normalized_session, user_text, assistant_text, importance, importance_source),
            )
            if cursor.rowcount:
                rowid = cursor.lastrowid
                conn.execute(
                    """
                    INSERT INTO conversations_fts (rowid, user_text, assistant_text, combined)
                    VALUES (?, ?, ?, ?)
                    """,
                    (rowid, user_text, assistant_text, combined),
                )
                # Embed the conversation for vector search (infinite RAG memory)
                self._embed_conversation(conn, rowid, combined)
            conn.commit()

        # Auto-link via memory mesh (async-safe, non-blocking)
        if cursor.rowcount:
            try:
                self.mesh_auto_link(cursor.lastrowid, top_k=3, threshold=0.65)
            except Exception as exc:
                self.log(f"mesh_auto_link non-critical: {exc}")

        return bool(cursor.rowcount)

    @retry_on_lock
    def set_importance(self, conversation_id: int, importance: float, source: str = "manual") -> bool:
        """Update importance score for a conversation."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET importance = ?, importance_source = ? WHERE id = ?",
                (max(0.0, min(1.0, importance)), source, conversation_id),
            )
            conn.commit()
        return True

    @classmethod
    def _embed_query_cached(cls, query: str) -> Optional[np.ndarray]:
        """Return a cached query embedding, computing it only on first access."""
        if query in cls._QUERY_EMBED_CACHE:
            cls._QUERY_EMBED_CACHE_HITS += 1
            return cls._QUERY_EMBED_CACHE[query]
        # Compute fresh — caller must have a local embedder reference.
        # This cache is populated by the instance methods that call embed_query.
        return None

    @classmethod
    def _cache_query_vector(cls, query: str, vector: np.ndarray) -> None:
        """Store a query embedding in the shared LRU cache."""
        if len(cls._QUERY_EMBED_CACHE) >= cls._QUERY_EMBED_CACHE_MAX:
            # Evict oldest entry (dicts preserve insertion order in Python 3.7+)
            cls._QUERY_EMBED_CACHE.pop(next(iter(cls._QUERY_EMBED_CACHE)))
        cls._QUERY_EMBED_CACHE[query] = vector

    @classmethod
    def query_embed_cache_stats(cls) -> dict:
        """Return hit/miss statistics for the in-memory query embedding cache."""
        return {
            "size": len(cls._QUERY_EMBED_CACHE),
            "max": cls._QUERY_EMBED_CACHE_MAX,
            "hits": cls._QUERY_EMBED_CACHE_HITS,
        }

    @staticmethod
    def _truncate_for_embedding(text: str, max_chars: int = 2000) -> str:
        """Truncate text for embedding to avoid ONNX OOM on long conversations."""
        if len(text) <= max_chars:
            return text
        return text[:max_chars]

    def _embed_conversation(self, conn, conversation_id: int, text: str) -> None:
        """Embed a single conversation turn and store in conversation_embeddings."""
        if not self.embedder.enabled or not text.strip():
            return
        text = self._truncate_for_embedding(text)
        cache_key = self.embedder.cache_key(text, scope="conversation")
        cached = self._load_cached_embeddings(conn, [cache_key])
        if cache_key in cached:
            vector = cached[cache_key]
        else:
            vectors = self.embedder.embed_texts([text])
            if not vectors:
                return
            vector = vectors[0]
            conn.execute(
                """
                INSERT OR REPLACE INTO embedding_cache (cache_key, scope, model, dims, vector, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    "conversation",
                    self.embedder.model_name,
                    int(vector.shape[0]),
                    sqlite3.Binary(vector.astype(np.float32).tobytes()),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO conversation_embeddings (conversation_id, cache_key, model, dims, vector)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                cache_key,
                self.embedder.model_name,
                int(vector.shape[0]),
                sqlite3.Binary(vector.astype(np.float32).tobytes()),
            ),
        )

    def search_conversations_vector(self, query: str, limit: int = 5) -> List[SearchHit]:
        """Semantic vector search over all conversation embeddings.
        Score = similarity_weight * cosine_similarity + importance_weight * importance
        Default weights: similarity=0.60, importance=0.25, recency=0.15

        If vector search fails or is disabled, gracefully falls back to FTS
        when MIKE_VECTOR_FALLBACK_TO_FTS=1 (default). Returns empty list otherwise.
        """
        if limit <= 0 or not self.embedder.enabled:
            return []
        if not isinstance(query, str) or not query.strip():
            return []

        try:
            query_vector = self._embed_query_cached(query)
            if query_vector is None:
                query_vector = self.embedder.embed_query(query)
                if query_vector is not None:
                    self._cache_query_vector(query, query_vector)
            if query_vector is None:
                return []

            # ── Validate query vector is not nonsense ──
            norm = float(np.linalg.norm(query_vector))
            if norm < 1e-9 or not np.isfinite(norm):
                self.log("[memory:vector] query vector is degenerate, falling back to FTS")
                if self._vector_fallback:
                    return self.search_conversations(query, limit=limit)
                return []

            sim_weight = float(os.getenv("MIKE_MEMORY_SIMILARITY_WEIGHT", "0.60"))
            imp_weight = float(os.getenv("MIKE_MEMORY_IMPORTANCE_WEIGHT", "0.25"))
            recency_weight = float(os.getenv("MIKE_MEMORY_RECENCY_WEIGHT", "0.15"))
            now_ts = datetime.now(timezone.utc).timestamp()

            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ce.conversation_id, ce.vector, ce.dims,
                           c.timestamp, c.session_id, c.user_text, c.assistant_text,
                           COALESCE(c.importance, 0.5) AS importance
                    FROM conversation_embeddings ce
                    JOIN conversations c ON c.id = ce.conversation_id
                    WHERE ce.model = ?
                    """,
                    (self.embedder.model_name,),
                ).fetchall()

            scored: List[SearchHit] = []
            for row in rows:
                vector = self._deserialize_vector(row["vector"], int(row["dims"] or 0))
                if vector is None or vector.shape != query_vector.shape:
                    continue
                cosine_sim = float(np.dot(query_vector, vector))
                importance = float(row["importance"] or 0.5)

                # Recency bonus: newer = higher (capped at 30 days)
                recency_score = 0.5
                try:
                    ts = datetime.fromisoformat(str(row["timestamp"] or "").replace("Z", "+00:00"))
                    age_days = max(0, (now_ts - ts.timestamp()) / 86400.0)
                    recency_score = max(0.0, 1.0 - age_days / 30.0)
                except Exception:
                    log.warning("Failed to compute recency score for memory row, using default")

                # Combined score: similarity + importance + recency
                score = (
                    sim_weight * cosine_sim +
                    imp_weight * importance +
                    recency_weight * recency_score
                )

                sid = row["session_id"] or "main"
                profile_name = sid.split("-")[0].capitalize() if "-" in sid else sid.capitalize()
                content = f"{profile_name}: {row['user_text']}\nMike: {row['assistant_text']}"
                scored.append(
                    SearchHit(
                        source="conversation_memory",
                        title=row["timestamp"] or "conversation",
                        content=content.strip(),
                        score=score,
                        metadata={
                            "conversation_id": int(row["conversation_id"]),
                            "timestamp": row["timestamp"],
                            "session_id": sid,
                            "profile": profile_name,
                            "cosine_similarity": round(cosine_sim, 4),
                            "importance": round(importance, 2),
                            "recency_score": round(recency_score, 4),
                        },
                    )
                )
            scored.sort(key=lambda h: h.score, reverse=True)
            return scored[:limit]

        except Exception as exc:
            self.log(f"[memory:vector] vector search failed: {exc}, falling back to FTS")
            if self._vector_fallback:
                try:
                    return self.search_conversations(query, limit=limit)
                except Exception as fallback_exc:
                    self.log(f"[memory:vector] FTS fallback also failed: {fallback_exc}")
                    return []
            return []

    def backfill_conversation_embeddings(self) -> int:
        """One-time migration: embed all existing conversations that lack embeddings."""
        if not self.embedder.enabled:
            return 0
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.user_text, c.assistant_text
                FROM conversations c
                LEFT JOIN conversation_embeddings ce ON ce.conversation_id = c.id
                WHERE ce.conversation_id IS NULL
                ORDER BY c.id ASC
                """
            ).fetchall()
            if not rows:
                return 0
            count = 0
            # Small batches — ONNX bge-m3 OOMs on large batches with long text
            batch_size = 4
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                texts = [self._truncate_for_embedding(f"{r['user_text']}\n{r['assistant_text']}") for r in batch]
                cache_keys = [self.embedder.cache_key(t, scope="conversation") for t in texts]
                cached = self._load_cached_embeddings(conn, cache_keys)
                missing_idx = [j for j, ck in enumerate(cache_keys) if ck not in cached]
                if missing_idx:
                    missing_texts = [texts[j] for j in missing_idx]
                    fresh = self.embedder.embed_texts(missing_texts)
                    for j, vec in zip(missing_idx, fresh):
                        ck = cache_keys[j]
                        cached[ck] = vec
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO embedding_cache (cache_key, scope, model, dims, vector, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                ck, "conversation", self.embedder.model_name,
                                int(vec.shape[0]),
                                sqlite3.Binary(vec.astype(np.float32).tobytes()),
                                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            ),
                        )
                for row, ck in zip(batch, cache_keys):
                    vec = cached.get(ck)
                    if vec is None:
                        continue
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO conversation_embeddings (conversation_id, cache_key, model, dims, vector)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            int(row["id"]), ck, self.embedder.model_name,
                            int(vec.shape[0]),
                            sqlite3.Binary(vec.astype(np.float32).tobytes()),
                        ),
                    )
                    count += 1
                conn.commit()
        return count

    def reindex_knowledge(self) -> int:
        files = self._collect_knowledge_files()
        with self._connect() as conn:
            conn.execute("DELETE FROM knowledge_embeddings")
            conn.execute("DELETE FROM knowledge_chunks")
            conn.execute("DELETE FROM knowledge_fts")
            self._index_knowledge_files(conn, files)
            conn.commit()
        return len(files)

    def upsert_knowledge_file(self, file_path: Path) -> bool:
        target = file_path.expanduser().resolve()
        source = str(target)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM knowledge_embeddings WHERE chunk_id IN (SELECT id FROM knowledge_chunks WHERE source = ?)",
                (source,),
            )
            conn.execute(
                "DELETE FROM knowledge_fts WHERE rowid IN (SELECT id FROM knowledge_chunks WHERE source = ?)",
                (source,),
            )
            conn.execute("DELETE FROM knowledge_chunks WHERE source = ?", (source,))
            if target.exists() and target.suffix.lower() in TEXT_EXTENSIONS:
                self._index_knowledge_files(conn, [target])
                conn.commit()
                return True
            conn.commit()
            return False

    def _index_knowledge_files(self, conn, files: Iterable[Path]) -> None:
        pending_embeddings: List[tuple[int, str]] = []
        for file_path in files:
            try:
                text = _read_text_file(file_path)
            except Exception as exc:
                self.log(f"Knowledge extractor failed for {file_path}: {exc}")
                continue
            if not text.strip():
                continue
            checksum = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
            title = file_path.stem.replace("_", " ").replace("-", " ").strip() or file_path.name
            kind = file_path.suffix.lower().lstrip(".") or "text"
            for chunk_index, (chunk_title, chunk_content) in enumerate(chunk_text(title, text), start=1):
                cursor = conn.execute(
                    """
                    INSERT INTO knowledge_chunks (source, title, kind, chunk_index, checksum, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (str(file_path), chunk_title, kind, chunk_index, checksum, chunk_content),
                )
                conn.execute(
                    """
                    INSERT INTO knowledge_fts (rowid, source, title, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cursor.lastrowid, str(file_path), chunk_title, chunk_content),
                )
                pending_embeddings.append((int(cursor.lastrowid), chunk_content))
        self._upsert_knowledge_embeddings(conn, pending_embeddings)

    def _upsert_knowledge_embeddings(self, conn, items: List[tuple[int, str]]) -> None:
        if not items or not self.embedder.enabled:
            return

        texts = [content for _, content in items]
        cache_keys = [self.embedder.cache_key(text, scope="knowledge_chunk") for text in texts]
        embeddings = self._load_cached_embeddings(conn, cache_keys)

        missing_indices = [index for index, cache_key in enumerate(cache_keys) if cache_key not in embeddings]
        if missing_indices:
            batch_size = max(1, int(getattr(self.embedder, "batch_size", 8) or 8))
            for start in range(0, len(missing_indices), batch_size):
                batch_indices = missing_indices[start:start + batch_size]
                missing_texts = [texts[index] for index in batch_indices]
                fresh_vectors = self.embedder.embed_texts(missing_texts)
                for index, vector in zip(batch_indices, fresh_vectors):
                    cache_key = cache_keys[index]
                    embeddings[cache_key] = vector
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO embedding_cache (cache_key, scope, model, dims, vector, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cache_key,
                            "knowledge_chunk",
                            self.embedder.model_name,
                            int(vector.shape[0]),
                            sqlite3.Binary(vector.astype(np.float32).tobytes()),
                            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        ),
                    )

        for (chunk_id, _), cache_key in zip(items, cache_keys):
            vector = embeddings.get(cache_key)
            if vector is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO knowledge_embeddings (chunk_id, cache_key, model, dims, vector, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    cache_key,
                    self.embedder.model_name,
                    int(vector.shape[0]),
                    sqlite3.Binary(vector.astype(np.float32).tobytes()),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )

    def _load_cached_embeddings(self, conn, cache_keys: List[str]) -> dict[str, np.ndarray]:
        if not cache_keys:
            return {}

        embeddings: dict[str, np.ndarray] = {}
        max_cache_keys_per_query = 900
        for start in range(0, len(cache_keys), max_cache_keys_per_query):
            batch = cache_keys[start:start + max_cache_keys_per_query]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT cache_key, vector, dims
                FROM embedding_cache
                WHERE cache_key IN ({placeholders})
                  AND model = ?
                """,
                (*batch, self.embedder.model_name),
            ).fetchall()
            for row in rows:
                vector = self._deserialize_vector(row["vector"], int(row["dims"] or 0))
                if vector is None:
                    continue
                embeddings[str(row["cache_key"])] = vector
        return embeddings

    def _purge_expired_embeddings(self, max_age_days: int = 30) -> int:
        """Delete embedding cache entries older than max_age_days.
        Returns total number of deleted rows across both tables.
        """
        if max_age_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        total_deleted = 0
        with self._connect() as conn:
            # Purge from embedding_cache (global cross-scope cache)
            cursor = conn.execute(
                "DELETE FROM embedding_cache WHERE created_at IS NOT NULL AND created_at < ?",
                (cutoff_str,),
            )
            total_deleted += cursor.rowcount
            # Purge from knowledge_embeddings
            cursor = conn.execute(
                "DELETE FROM knowledge_embeddings WHERE created_at IS NOT NULL AND created_at < ?",
                (cutoff_str,),
            )
            total_deleted += cursor.rowcount
            conn.commit()
        if total_deleted:
            self.log(f"[memory] Purged {total_deleted} expired embedding cache entries (max_age={max_age_days}d)")
        return total_deleted

    def _deserialize_vector(self, blob: bytes, dims: int) -> Optional[np.ndarray]:
        if not blob or dims <= 0:
            return None
        vector = np.frombuffer(blob, dtype=np.float32)
        if vector.size != dims:
            return None
        return vector

    def _collect_knowledge_files(self) -> List[Path]:
        files: List[Path] = []
        for raw_path in self.knowledge_paths:
            path = raw_path.expanduser().resolve()
            if path.is_dir():
                files.extend(
                    candidate
                    for candidate in path.rglob("*")
                    if candidate.is_file() and candidate.suffix.lower() in TEXT_EXTENSIONS
                )
            elif path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS:
                files.append(path)
        deduped = {file.resolve() for file in files}
        return sorted(deduped)

    @retry_on_lock
    def search_conversations(
        self,
        query: str,
        limit: int = 3,
        session_id: Optional[str] = None,
    ) -> List[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            return []
        sanitized = _sanitize_query(query)
        if not sanitized:
            return []
        with self._connect() as conn:
            if session_id:
                rows = conn.execute(
                    """
                    SELECT
                        c.id,
                        c.entry_hash,
                        c.timestamp,
                        c.session_id,
                        c.user_text,
                        c.assistant_text,
                        bm25(conversations_fts) AS rank,
                        CASE WHEN c.session_id = ? THEN 0 ELSE 1 END AS session_rank
                    FROM conversations_fts
                    JOIN conversations c ON c.id = conversations_fts.rowid
                    WHERE conversations_fts MATCH ?
                    ORDER BY session_rank, rank
                    LIMIT ?
                    """,
                    (session_id, sanitized, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        c.id,
                        c.entry_hash,
                        c.timestamp,
                        c.session_id,
                        c.user_text,
                        c.assistant_text,
                        bm25(conversations_fts) AS rank
                    FROM conversations_fts
                    JOIN conversations c ON c.id = conversations_fts.rowid
                    WHERE conversations_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (sanitized, limit),
                ).fetchall()
        hits: List[SearchHit] = []
        for row in rows:
            sid = row["session_id"] or "main"
            # Extrai o nome do perfil do session_id (formato: "marco-1234567890")
            profile_name = sid.split("-")[0].capitalize() if "-" in sid else sid.capitalize()
            content = f"{profile_name}: {row['user_text']}\nMike: {row['assistant_text']}"
            hits.append(
                SearchHit(
                    source="conversation_memory",
                    title=row["timestamp"] or "conversation",
                    content=content.strip(),
                    score=float(row["rank"] or 0.0),
                    metadata={
                        "conversation_id": int(row["id"]),
                        "entry_hash": row["entry_hash"],
                        "timestamp": row["timestamp"],
                        "session_id": sid,
                        "profile": profile_name,
                    },
                )
            )
        return hits

    def recent_messages(self, session_id: str = "main", limit: int = 5) -> List[dict]:
        normalized_session = (session_id or "main").strip() or "main"
        with self._connect() as conn:
            current_rows = conn.execute(
                """
                SELECT user_text, assistant_text
                FROM conversations
                WHERE session_id = ?
                  AND user_text NOT LIKE '[SYSTEM]%'
                  AND user_text NOT LIKE '%Given the above conversation%'
                ORDER BY id DESC
                LIMIT ?
                """,
                (normalized_session, limit),
            ).fetchall()

            # Cross-session continuity: if current session has fewer messages
            # than requested, backfill from the same profile's recent sessions.
            backfill_rows: list = []
            _MAX_BACKFILL = 8  # cross-session backfill pairs (was 4, increased for better continuity)
            if len(current_rows) < limit and "-" in normalized_session:
                profile_prefix = normalized_session.split("-")[0] + "-%"
                remaining = min(limit - len(current_rows), _MAX_BACKFILL)
                seen_ids = {(r["user_text"], r["assistant_text"]) for r in current_rows}
                candidates = conn.execute(
                    """
                    SELECT user_text, assistant_text
                    FROM conversations
                    WHERE session_id LIKE ?
                      AND session_id != ?
                      AND user_text NOT LIKE '[SYSTEM]%'
                      AND user_text NOT LIKE '%Given the above conversation, extract%'
                      AND user_text NOT LIKE '%PREVIOUS MESSAGES%'
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (profile_prefix, normalized_session, remaining + 10),
                ).fetchall()
                for br in candidates:
                    if len(backfill_rows) >= remaining:
                        break
                    key = (br["user_text"], br["assistant_text"])
                    if key not in seen_ids:
                        backfill_rows.append(br)
                        seen_ids.add(key)

        messages: List[dict] = []
        # Backfilled messages first (older, from previous sessions)
        if backfill_rows:
            for row in reversed(backfill_rows):
                messages.append({"role": "user", "content": row["user_text"]})
                messages.append({"role": "assistant", "content": row["assistant_text"]})
            messages.append({
                "role": "user",
                "content": "[As mensagens acima são de conversas anteriores. A conversa atual começa abaixo.]",
            })
        # Current session messages
        for row in reversed(current_rows):
            messages.append({"role": "user", "content": row["user_text"]})
            messages.append({"role": "assistant", "content": row["assistant_text"]})
        return messages

    def last_session_summary(self, session_id: str, max_turns: int = 12) -> Optional[str]:
        """Build a compact text summary of the most recent *previous* session
        for the same profile.  Returns ``None`` if there is no previous session
        or it has no meaningful content.

        This does NOT call the LLM — it simply condenses the raw user/assistant
        turns into a bullet-point recap so the model has context about what was
        discussed before.
        """
        normalized = (session_id or "").strip()
        if "-" not in normalized:
            return None
        profile = normalized.split("-")[0]
        profile_prefix = f"{profile}-%"
        with self._connect() as conn:
            prev_session = conn.execute(
                """
                SELECT session_id, COUNT(*) as cnt
                FROM conversations
                WHERE session_id LIKE ?
                  AND session_id != ?
                GROUP BY session_id
                ORDER BY MAX(id) DESC
                LIMIT 1
                """,
                (profile_prefix, normalized),
            ).fetchone()
            if not prev_session or prev_session["cnt"] == 0:
                return None
            prev_sid = prev_session["session_id"]
            rows = conn.execute(
                """
                SELECT user_text, assistant_text
                FROM conversations
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (prev_sid, max_turns),
            ).fetchall()
        if not rows:
            return None
        # Build compact bullet points (most recent first → reverse to chronological)
        lines: list[str] = []
        for row in reversed(rows):
            user_short = (row["user_text"] or "").strip()[:120].replace("\n", " ")
            asst_short = (row["assistant_text"] or "").strip()[:120].replace("\n", " ")
            if not user_short:
                continue
            # Skip trivial greetings
            if user_short.lower() in _DIRECT_CHAT_PATTERNS:
                continue
            lines.append(f"- Você pediu: {user_short}")
            if asst_short:
                lines.append(f"  Mike respondeu: {asst_short}")
        if not lines:
            return None
        header = f"[Resumo da sessão anterior ({prev_sid})]"
        return header + "\n" + "\n".join(lines)

    def list_sessions(
        self,
        profile_key: Optional[str] = None,
        limit: int = 30,
    ) -> List[dict]:
        normalized_profile = (profile_key or "").strip().lower()
        normalized_limit = max(1, min(int(limit or 30), 100))
        prefix = f"{normalized_profile}-%" if normalized_profile else "%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    c_first.session_id,
                    c_last.timestamp  AS updated_at,
                    c_first.user_text AS title_raw,
                    c_last.user_text AS preview_raw,
                    summary.turn_count
                FROM (
                    SELECT
                        session_id,
                        MIN(id) AS first_id,
                        MAX(id) AS last_id,
                        COUNT(*) AS turn_count
                    FROM conversations
                    WHERE (? = '' OR session_id = ? OR session_id LIKE ?)
                      AND user_text NOT LIKE '[SYSTEM]%'
                      AND user_text NOT LIKE '%Given the above conversation%'
                    GROUP BY session_id
                ) summary
                JOIN conversations c_first ON c_first.id = summary.first_id
                JOIN conversations c_last  ON c_last.id  = summary.last_id
                ORDER BY summary.last_id DESC
                LIMIT ?
                """,
                (normalized_profile, normalized_profile, prefix, normalized_limit),
            ).fetchall()
        sessions = []
        for row in rows:
            raw = re.sub(r"\s+", " ", str(row["title_raw"] or "")).strip()
            title = raw[:60] + ("…" if len(raw) > 60 else "")
            preview_raw = re.sub(r"\s+", " ", str(row["preview_raw"] or raw or "")).strip()
            preview = preview_raw[:120] + ("â€¦" if len(preview_raw) > 120 else "")
            sessions.append({
                "session_id": row["session_id"],
                "updated_at": row["updated_at"],
                "title": title or "Nova conversa",
                "preview": preview or title or "Nova conversa",
                "turn_count": int(row["turn_count"] or 0),
                "message_count": int(row["turn_count"] or 0) * 2,
            })
        return sessions

    def conversation_history(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[dict]:
        normalized_session = (session_id or "main").strip() or "main"
        with self._connect() as conn:
            if limit is None:
                rows = conn.execute(
                    """
                    SELECT timestamp, user_text, assistant_text
                    FROM conversations
                    WHERE session_id = ?
                    ORDER BY id ASC
                    """,
                    (normalized_session,),
                ).fetchall()
            else:
                normalized_limit = max(1, min(int(limit), 200))
                rows = conn.execute(
                    """
                    SELECT timestamp, user_text, assistant_text
                    FROM conversations
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (normalized_session, normalized_limit),
                ).fetchall()
                rows = list(reversed(rows))
        messages: List[dict] = []
        for row in rows:
            timestamp = row["timestamp"]
            messages.append({
                "role": "user",
                "text": row["user_text"],
                "timestamp": timestamp,
            })
            messages.append({
                "role": "assistant",
                "text": row["assistant_text"],
                "timestamp": timestamp,
            })
        return messages

    # ==================================================================
    # Memory Mesh — links between related memories
    # ==================================================================
    @retry_on_lock
    def mesh_link(
        self,
        source_id: int,
        target_id: int,
        relation: str = "similar",
        strength: float = 0.8,
    ) -> bool:
        """Create a link between two conversation memories."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO memory_mesh
                       (source_id, target_id, relation, strength, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (source_id, target_id, relation, strength, now),
                )
                conn.commit()
                return True
            except Exception as exc:
                self.log(f"mesh_link failed: {exc}")
                return False

    def mesh_neighbors(
        self,
        conversation_id: int,
        relation: Optional[str] = None,
        limit: int = 10,
    ) -> List[dict]:
        """Get memories linked to a given conversation."""
        with self._connect() as conn:
            if relation:
                rows = conn.execute(
                    """SELECT m.target_id, m.relation, m.strength,
                              c.user_text, c.assistant_text, c.session_id, c.timestamp
                       FROM memory_mesh m
                       JOIN conversations c ON c.id = m.target_id
                       WHERE m.source_id = ? AND m.relation = ?
                       ORDER BY m.strength DESC LIMIT ?""",
                    (conversation_id, relation, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT m.target_id, m.relation, m.strength,
                              c.user_text, c.assistant_text, c.session_id, c.timestamp
                       FROM memory_mesh m
                       JOIN conversations c ON c.id = m.target_id
                       WHERE m.source_id = ?
                       ORDER BY m.strength DESC LIMIT ?""",
                    (conversation_id, limit),
                ).fetchall()
        return [
            {
                "id": row["target_id"],
                "relation": row["relation"],
                "strength": float(row["strength"]),
                "user_text": row["user_text"],
                "assistant_text": row["assistant_text"],
                "session_id": row["session_id"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    def mesh_auto_link(self, conversation_id: int, top_k: int = 3, threshold: float = 0.65) -> int:
        """Auto-link a conversation to similar ones via embedding cosine similarity."""
        with self._connect() as conn:
            source = conn.execute(
                "SELECT vector FROM conversation_embeddings WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if not source or not source["vector"]:
                return 0

            src_vec = np.frombuffer(source["vector"], dtype=np.float32)
            if np.linalg.norm(src_vec) < 1e-9:
                return 0

            # Get all other embeddings
            others = conn.execute(
                """SELECT ce.conversation_id, ce.vector
                   FROM conversation_embeddings ce
                   WHERE ce.conversation_id != ?""",
                (conversation_id,),
            ).fetchall()

        linked = 0
        scores = []
        for other in others:
            if not other["vector"]:
                continue
            other_vec = np.frombuffer(other["vector"], dtype=np.float32)
            if np.linalg.norm(other_vec) < 1e-9:
                continue
            sim = float(np.dot(src_vec, other_vec))
            if sim >= threshold:
                scores.append((other["conversation_id"], sim))

        scores.sort(key=lambda x: -x[1])
        for target_id, sim in scores[:top_k]:
            if self.mesh_link(conversation_id, target_id, "similar", sim):
                linked += 1
            # Bidirectional
            self.mesh_link(target_id, conversation_id, "similar", sim)
        return linked

    def mesh_context(self, conversation_id: int, depth: int = 1, limit: int = 5) -> str:
        """Build a context block from the memory mesh around a conversation."""
        visited: set[int] = {conversation_id}
        context_parts: list[str] = []
        frontier = [conversation_id]

        for _ in range(depth):
            next_frontier = []
            for node_id in frontier:
                neighbors = self.mesh_neighbors(node_id, limit=limit)
                for n in neighbors:
                    nid = n["id"]
                    if nid in visited:
                        continue
                    visited.add(nid)
                    next_frontier.append(nid)
                    user_short = (n["user_text"] or "")[:150].replace("\n", " ")
                    asst_short = (n["assistant_text"] or "")[:150].replace("\n", " ")
                    context_parts.append(
                        f"- [{n['relation']}, {n['strength']:.2f}] "
                        f"Você: {user_short} → Mike: {asst_short}"
                    )
            frontier = next_frontier
            if not frontier:
                break

        if not context_parts:
            return ""
        return "Memórias conectadas (mesh):\n" + "\n".join(context_parts[:limit])

    def mesh_stats(self) -> dict:
        """Return mesh statistics."""
        with self._connect() as conn:
            total_links = conn.execute("SELECT COUNT(*) FROM memory_mesh").fetchone()[0]
            unique_nodes = conn.execute(
                "SELECT COUNT(DISTINCT source_id) + COUNT(DISTINCT target_id) FROM memory_mesh"
            ).fetchone()[0]
            avg_strength = conn.execute(
                "SELECT AVG(strength) FROM memory_mesh"
            ).fetchone()[0]
        return {
            "total_links": total_links,
            "unique_nodes": unique_nodes,
            "avg_strength": round(avg_strength or 0, 3),
        }

    # ==================================================================
    # Session Checkpoints — save/restore session state
    # ==================================================================
    @retry_on_lock
    def checkpoint_save(
        self,
        session_id: str,
        label: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """Save a checkpoint of the current session state. Returns checkpoint_id."""
        import uuid
        normalized = (session_id or "main").strip() or "main"
        profile = normalized.split("-")[0] if "-" in normalized else ""
        checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._connect() as conn:
            # Count turns in session
            turn_count = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE session_id = ?",
                (normalized,),
            ).fetchone()[0]

            # Build summary of the session at this point
            rows = conn.execute(
                """SELECT user_text, assistant_text FROM conversations
                   WHERE session_id = ? ORDER BY id DESC LIMIT 20""",
                (normalized,),
            ).fetchall()
            summary_lines = []
            for row in reversed(rows):
                user_short = (row["user_text"] or "").strip()[:100].replace("\n", " ")
                if user_short and user_short.lower() not in ("oi", "olá", "e aí", "tudo bem"):
                    summary_lines.append(f"- {user_short}")
            summary = "\n".join(summary_lines[-10:]) or "(sessão vazia)"

            conn.execute(
                """INSERT OR REPLACE INTO session_checkpoints
                   (checkpoint_id, session_id, profile, label, created_at,
                    turn_count, summary, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id, normalized, profile,
                    label or f"Checkpoint at turn {turn_count}",
                    now, turn_count, summary,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            conn.commit()

        self.log(f"Checkpoint saved: {checkpoint_id} (session={normalized}, turns={turn_count})")
        return checkpoint_id

    def checkpoint_list(
        self,
        session_id: Optional[str] = None,
        profile: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """List checkpoints, optionally filtered by session or profile."""
        with self._connect() as conn:
            if session_id:
                rows = conn.execute(
                    """SELECT * FROM session_checkpoints
                       WHERE session_id = ? ORDER BY created_at DESC LIMIT ?""",
                    (session_id.strip(), limit),
                ).fetchall()
            elif profile:
                rows = conn.execute(
                    """SELECT * FROM session_checkpoints
                       WHERE profile = ? ORDER BY created_at DESC LIMIT ?""",
                    (profile.strip().lower(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM session_checkpoints ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "checkpoint_id": row["checkpoint_id"],
                "session_id": row["session_id"],
                "profile": row["profile"],
                "label": row["label"],
                "created_at": row["created_at"],
                "turn_count": row["turn_count"],
                "summary": row["summary"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in rows
        ]

    def checkpoint_restore(self, checkpoint_id: str) -> Optional[dict]:
        """Restore session context from a checkpoint.
        Returns the checkpoint data with conversation history up to that point."""
        with self._connect() as conn:
            ckpt = conn.execute(
                "SELECT * FROM session_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
            if not ckpt:
                return None

            # Fetch conversations up to the turn count at checkpoint time
            rows = conn.execute(
                """SELECT user_text, assistant_text, timestamp
                   FROM conversations
                   WHERE session_id = ?
                   ORDER BY id ASC
                   LIMIT ?""",
                (ckpt["session_id"], ckpt["turn_count"]),
            ).fetchall()

        messages = []
        for row in rows:
            messages.append({"role": "user", "content": row["user_text"]})
            messages.append({"role": "assistant", "content": row["assistant_text"]})

        return {
            "checkpoint_id": ckpt["checkpoint_id"],
            "session_id": ckpt["session_id"],
            "profile": ckpt["profile"],
            "label": ckpt["label"],
            "created_at": ckpt["created_at"],
            "turn_count": ckpt["turn_count"],
            "summary": ckpt["summary"],
            "messages": messages,
            "metadata": json.loads(ckpt["metadata_json"] or "{}"),
        }

    # ==================================================================
    # Persistent Session Summaries — cross-session knowledge
    # ==================================================================
    @retry_on_lock
    def session_summary_save(self, session_id: str, summary: str, topics: Optional[list] = None) -> bool:
        """Save/update a persistent summary for a session."""
        normalized = (session_id or "").strip()
        if not normalized:
            return False
        profile = normalized.split("-")[0] if "-" in normalized else ""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._connect() as conn:
            turn_count = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE session_id = ?",
                (normalized,),
            ).fetchone()[0]

            conn.execute(
                """INSERT INTO session_summaries
                   (session_id, profile, turn_count, topics_json, summary, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       summary = excluded.summary,
                       topics_json = excluded.topics_json,
                       turn_count = excluded.turn_count,
                       updated_at = excluded.updated_at""",
                (
                    normalized, profile, turn_count,
                    json.dumps(topics or [], ensure_ascii=False),
                    summary, now, now,
                ),
            )
            conn.commit()
        return True

    def session_summaries_recent(self, profile: str, limit: int = 5) -> List[dict]:
        """Get recent session summaries for a profile."""
        prefix = f"{profile.strip().lower()}-%"
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM session_summaries
                   WHERE session_id LIKE ?
                   ORDER BY created_at DESC LIMIT ?""",
                (prefix, limit),
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "profile": row["profile"],
                "turn_count": row["turn_count"],
                "topics": json.loads(row["topics_json"] or "[]"),
                "summary": row["summary"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def cross_session_context(self, session_id: str, max_summaries: int = 3) -> str:
        """Build cross-session context from persistent summaries."""
        normalized = (session_id or "").strip()
        if "-" not in normalized:
            return ""
        profile = normalized.split("-")[0]
        summaries = self.session_summaries_recent(profile, limit=max_summaries + 1)
        # Exclude current session
        summaries = [s for s in summaries if s["session_id"] != normalized][:max_summaries]
        if not summaries:
            return ""
        parts = []
        for s in summaries:
            topics = ", ".join(s["topics"][:5]) if s["topics"] else "variados"
            parts.append(f"[{s['session_id']}] Tópicos: {topics}\n{s['summary']}")
        return "Resumo de sessões anteriores:\n" + "\n\n".join(parts)

    def iter_conversations(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, session_id, user_text, assistant_text
                FROM conversations
                ORDER BY id ASC
                """
            ).fetchall()
        return [
            {
                "timestamp": row["timestamp"],
                "session_id": row["session_id"] or "main",
                "user_text": row["user_text"] or "",
                "assistant_text": row["assistant_text"] or "",
            }
            for row in rows
        ]

    def search_knowledge(self, query: str, limit: int = 4) -> List[SearchHit]:
        if not isinstance(query, str) or not query.strip():
            return []
        sanitized = _sanitize_query(query)
        if not sanitized:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT kc.id, kc.source, kc.title, kc.kind, kc.content, bm25(knowledge_fts) AS rank
                FROM knowledge_fts
                JOIN knowledge_chunks kc ON kc.id = knowledge_fts.rowid
                WHERE knowledge_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (sanitized, limit),
            ).fetchall()
        hits: List[SearchHit] = []
        for row in rows:
            hits.append(
                SearchHit(
                    source=row["source"],
                    title=row["title"],
                    content=row["content"],
                    score=float(row["rank"] or 0.0),
                    metadata={"kind": row["kind"], "chunk_id": int(row["id"])},
                )
            )
        return hits

    def search_knowledge_vector(self, query: str, limit: int = 4) -> List[SearchHit]:
        """Vector search over knowledge embeddings.

        If vector search fails or the embedding is degenerate, gracefully falls
        back to FTS when MIKE_VECTOR_FALLBACK_TO_FTS=1 (default).
        """
        if limit <= 0 or not self.embedder.enabled:
            return []
        if not isinstance(query, str) or not query.strip():
            return []

        try:
            query_vector = self._embed_query_cached(query)
            if query_vector is None:
                query_vector = self.embedder.embed_query(query)
                if query_vector is not None:
                    self._cache_query_vector(query, query_vector)
            if query_vector is None:
                return []

            # ── Validate query vector ──
            norm = float(np.linalg.norm(query_vector))
            if norm < 1e-9 or not np.isfinite(norm):
                self.log("[memory:vector] knowledge query vector is degenerate, falling back to FTS")
                if self._vector_fallback:
                    return self.search_knowledge(query, limit=limit)
                return []

            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ke.chunk_id, ke.vector, ke.dims, kc.source, kc.title, kc.kind, kc.content
                    FROM knowledge_embeddings ke
                    JOIN knowledge_chunks kc ON kc.id = ke.chunk_id
                    WHERE ke.model = ?
                    ORDER BY ke.chunk_id ASC
                    """,
                    (self.embedder.model_name,),
                ).fetchall()

            scored_hits: List[SearchHit] = []
            for row in rows:
                vector = self._deserialize_vector(row["vector"], int(row["dims"] or 0))
                if vector is None or vector.shape != query_vector.shape:
                    continue
                score = float(np.dot(query_vector, vector))
                scored_hits.append(
                    SearchHit(
                        source=row["source"],
                        title=row["title"],
                        content=row["content"],
                        score=score,
                        metadata={
                            "kind": row["kind"],
                            "chunk_id": int(row["chunk_id"]),
                            "vector_score": score,
                        },
                    )
                )

            scored_hits.sort(key=lambda hit: hit.score, reverse=True)
            return scored_hits[:limit]

        except Exception as exc:
            self.log(f"[memory:vector] knowledge vector search failed: {exc}, falling back to FTS")
            if self._vector_fallback:
                try:
                    return self.search_knowledge(query, limit=limit)
                except Exception as fallback_exc:
                    self.log(f"[memory:vector] knowledge FTS fallback also failed: {fallback_exc}")
                    return []
            return []

    def conversation_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM conversations").fetchone()
        return int(row["total"])

    def knowledge_stats(self) -> dict:
        with self._connect() as conn:
            docs = conn.execute("SELECT COUNT(DISTINCT source) AS total FROM knowledge_chunks").fetchone()
            chunks = conn.execute("SELECT COUNT(*) AS total FROM knowledge_chunks").fetchone()
            web_cache = conn.execute(
                """
                SELECT COUNT(DISTINCT source) AS total
                FROM knowledge_chunks
                WHERE source LIKE ?
                """,
                (f"%{os.sep}web_cache{os.sep}%",),
            ).fetchone()
        return {
            "knowledge_documents": int(docs["total"]),
            "knowledge_chunks": int(chunks["total"]),
            "web_cache_documents": int(web_cache["total"]),
            "conversation_records": self.conversation_count(),
        }



