# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Reflection — Episodic Reflection Store (Reflexion Pattern)
===============================================================

Implementa o padrao Reflexion (arxiv 2303.11366):
- Armazena reflexoes sobre cada execucao do AgenticLoop
- Busca reflexoes similares antes de novas acoes
- Permite aprendizado continuo sem fine-tuning

Paper: Reflexion: Language Agents with Verbal Reinforcement Learning
Achieved 91% pass@1 on HumanEval (vs GPT-4 80%).

Arquitetura:
  EXECUTAR → REFLETIR → ARMAZENAR → RECUPERAR → MELHORAR
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

import numpy as np

from mike_config import env_bool

log = logging.getLogger("mike.reflection")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REFLECTION_ENABLED = env_bool("MIKE_REFLECTION_ENABLED", True)
REFLECTION_MAX_RESULTS = int(os.getenv("MIKE_REFLECTION_MAX_RESULTS", "3"))
REFLECTION_SIMILARITY_THRESHOLD = float(os.getenv("MIKE_REFLECTION_SIMILARITY_THRESHOLD", "0.55"))
REFLECTION_MAX_STORED = int(os.getenv("MIKE_REFLECTION_MAX_STORED", "1000"))


from core.shared.time_utils import utc_now, utc_now_iso


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class EpisodicReflection:
    """Uma reflexao sobre uma execucao do AgenticLoop."""
    id: str
    goal: str                          # O que estava tentando fazer
    context: str = ""                  # Contexto adicional (rotina, tarefa, etc)
    output: str = ""                   # Resultado da execucao
    success: bool = False              # Deu certo?
    reflection_text: str = ""          # Texto da reflexao (gerado pelo LLM)
    lessons_learned: list[str] = field(default_factory=list)  # Licoes extraidas
    mistakes_made: list[str] = field(default_factory=list)    # Erros cometidos
    what_worked: list[str] = field(default_factory=list)      # O que funcionou
    tool_calls: list[str] = field(default_factory=list)       # Tools usadas
    iterations: int = 0                # Quantas iteracoes
    elapsed_sec: float = 0.0           # Tempo total
    source: str = "agentic_loop"       # agentic_loop | task | routine | manual
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    embedding: Optional[np.ndarray] = None  # Nao serializado no JSON

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "context": self.context,
            "output": self.output[:1000],
            "success": self.success,
            "reflection_text": self.reflection_text[:500],
            "lessons_learned": self.lessons_learned,
            "mistakes_made": self.mistakes_made,
            "what_worked": self.what_worked,
            "tool_calls": self.tool_calls,
            "iterations": self.iterations,
            "elapsed_sec": self.elapsed_sec,
            "source": self.source,
            "tags": self.tags,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "EpisodicReflection":
        return EpisodicReflection(
            id=str(data.get("id", "")),
            goal=str(data.get("goal", "")),
            context=str(data.get("context", "")),
            output=str(data.get("output", "")),
            success=bool(data.get("success", False)),
            reflection_text=str(data.get("reflection_text", "")),
            lessons_learned=list(data.get("lessons_learned", [])),
            mistakes_made=list(data.get("mistakes_made", [])),
            what_worked=list(data.get("what_worked", [])),
            tool_calls=list(data.get("tool_calls", [])),
            iterations=int(data.get("iterations", 0)),
            elapsed_sec=float(data.get("elapsed_sec", 0.0)),
            source=str(data.get("source", "agentic_loop")),
            tags=list(data.get("tags", [])),
            created_at=str(data.get("created_at", utc_now_iso())),
        )


@dataclass
class ReflectionHit:
    """Resultado de busca de reflexoes similares."""
    reflection: EpisodicReflection
    score: float


# ---------------------------------------------------------------------------
# EpisodicReflectionStore
# ---------------------------------------------------------------------------

class EpisodicReflectionStore:
    """Armazena e recupera reflexoes episodicas com busca por similaridade.

    Usa SQLite + FTS5 para texto e embeddings para similaridade semantica.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        embedder: Any = None,  # MikeLocalEmbedder
        log_fn: Optional[Callable] = None,
    ):
        self._db_path = Path(db_path) if db_path else Path("runtime/memory/reflections.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        self._log = log_fn or log.info

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create tables if they don't exist."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reflections (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    context TEXT DEFAULT '',
                    output TEXT DEFAULT '',
                    success INTEGER DEFAULT 0,
                    reflection_text TEXT DEFAULT '',
                    lessons_learned TEXT DEFAULT '[]',
                    mistakes_made TEXT DEFAULT '[]',
                    what_worked TEXT DEFAULT '[]',
                    tool_calls TEXT DEFAULT '[]',
                    iterations INTEGER DEFAULT 0,
                    elapsed_sec REAL DEFAULT 0.0,
                    source TEXT DEFAULT 'agentic_loop',
                    tags TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS reflections_fts
                USING fts5(goal, reflection_text, output, content)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reflection_embeddings (
                    reflection_id TEXT PRIMARY KEY,
                    cache_key TEXT,
                    model TEXT,
                    dims INTEGER,
                    vector BLOB
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reflection_stats (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    total_reflections INTEGER DEFAULT 0,
                    total_successes INTEGER DEFAULT 0,
                    total_failures INTEGER DEFAULT 0,
                    avg_iterations REAL DEFAULT 0.0,
                    updated_at TEXT
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO reflection_stats (id, updated_at)
                VALUES (1, ?)
            """, (utc_now_iso(),))
            conn.commit()
        self._log("[reflection] Reflection store initialized")

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, reflection: EpisodicReflection) -> bool:
        """Save a reflection to the store."""
        if not reflection.id:
            reflection.id = f"refl_{uuid.uuid4().hex[:8]}"

        with self._connect() as conn:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO reflections (
                        id, goal, context, output, success, reflection_text,
                        lessons_learned, mistakes_made, what_worked, tool_calls,
                        iterations, elapsed_sec, source, tags, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    reflection.id,
                    reflection.goal,
                    reflection.context,
                    reflection.output[:2000],
                    1 if reflection.success else 0,
                    reflection.reflection_text[:2000],
                    json.dumps(reflection.lessons_learned, ensure_ascii=False),
                    json.dumps(reflection.mistakes_made, ensure_ascii=False),
                    json.dumps(reflection.what_worked, ensure_ascii=False),
                    json.dumps(reflection.tool_calls, ensure_ascii=False),
                    reflection.iterations,
                    reflection.elapsed_sec,
                    reflection.source,
                    json.dumps(reflection.tags, ensure_ascii=False),
                    reflection.created_at,
                ))

                # FTS index
                content = f"{reflection.goal}\n{reflection.reflection_text}\n{reflection.output}"
                conn.execute("""
                    INSERT OR REPLACE INTO reflections_fts (rowid, goal, reflection_text, output, content)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    conn.execute("SELECT rowid FROM reflections WHERE id = ?", (reflection.id,)).fetchone()["rowid"],
                    reflection.goal,
                    reflection.reflection_text,
                    reflection.output,
                    content,
                ))

                # Embedding
                if self._embedder and self._embedder.enabled:
                    self._embed_reflection(conn, reflection)

                # Prune old reflections if over limit
                count = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
                if count > REFLECTION_MAX_STORED:
                    excess = count - REFLECTION_MAX_STORED
                    conn.execute("""
                        DELETE FROM reflections WHERE id IN (
                            SELECT id FROM reflections
                            ORDER BY created_at ASC LIMIT ?
                        )
                    """, (excess,))

                # Update stats
                conn.execute("""
                    UPDATE reflection_stats SET
                        total_reflections = (SELECT COUNT(*) FROM reflections),
                        total_successes = (SELECT COUNT(*) FROM reflections WHERE success = 1),
                        total_failures = (SELECT COUNT(*) FROM reflections WHERE success = 0),
                        avg_iterations = (SELECT AVG(iterations) FROM reflections),
                        updated_at = ?
                    WHERE id = 1
                """, (utc_now_iso(),))

                conn.commit()
                return True
            except Exception as exc:
                self._log(f"[reflection] Save failed: {exc}")
                return False

    def _embed_reflection(self, conn, reflection: EpisodicReflection) -> None:
        """Embed and store reflection for semantic search."""
        if not self._embedder or not self._embedder.enabled:
            return
        text = f"{reflection.goal}\n{reflection.reflection_text}"[:2000]
        if not text.strip():
            return
        try:
            cache_key = self._embedder.cache_key(text, scope="reflection")
            vectors = self._embedder.embed_texts([text])
            if not vectors:
                return
            vector = vectors[0]
            conn.execute("""
                INSERT OR REPLACE INTO reflection_embeddings (reflection_id, cache_key, model, dims, vector)
                VALUES (?, ?, ?, ?, ?)
            """, (
                reflection.id,
                cache_key,
                self._embedder.model_name,
                int(vector.shape[0]),
                sqlite3.Binary(vector.astype(np.float32).tobytes()),
            ))
        except Exception as exc:
            self._log(f"[reflection] Embed failed: {exc}")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_similar(
        self,
        goal: str,
        limit: int = REFLECTION_MAX_RESULTS,
        threshold: float = REFLECTION_SIMILARITY_THRESHOLD,
    ) -> List[ReflectionHit]:
        """Find similar past reflections using text + embedding search."""
        hits: List[ReflectionHit] = []
        seen_ids: set[str] = set()

        # 1. FTS5 text search
        with self._connect() as conn:
            # Sanitize for FTS5
            terms = " OR ".join(
                term for term in goal.split()
                if len(term) >= 3 and term.lower() not in ("que", "para", "com", "como", "uma", "dos", "das")
            )
            if terms:
                try:
                    rows = conn.execute("""
                        SELECT r.*, bm25(reflections_fts) AS rank
                        FROM reflections_fts
                        JOIN reflections r ON r.id = (
                            SELECT id FROM reflections WHERE id = reflections_fts.rowid
                        )
                        WHERE reflections_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                    """, (terms, limit * 2)).fetchall()
                    for row in rows:
                        rid = row["id"]
                        if rid not in seen_ids:
                            seen_ids.add(rid)
                            hits.append(ReflectionHit(
                                reflection=EpisodicReflection.from_dict(dict(row)),
                                score=1.0 / (1.0 + float(row["rank"] or 0.0)),
                            ))
                except Exception:
                    pass  # FTS query failed (e.g., no matching terms)

        # 2. Semantic embedding search
        if self._embedder and self._embedder.enabled:
            try:
                query_vec = self._embedder.embed_query(goal)
                if query_vec is not None:
                    with self._connect() as conn:
                        emb_rows = conn.execute("""
                            SELECT re.reflection_id, re.vector, re.dims
                            FROM reflection_embeddings re
                            WHERE re.model = ?
                        """, (self._embedder.model_name,)).fetchall()

                        scored: list[tuple[float, str]] = []
                        for row in emb_rows:
                            rid = row["reflection_id"]
                            if rid in seen_ids:
                                continue
                            blob = row["vector"]
                            dims = int(row["dims"] or 0)
                            if not blob or dims <= 0:
                                continue
                            vec = np.frombuffer(blob, dtype=np.float32)
                            if vec.shape[0] != dims or vec.shape != query_vec.shape:
                                continue
                            sim = float(np.dot(query_vec, vec))
                            if sim >= threshold:
                                scored.append((sim, rid))

                        scored.sort(key=lambda x: -x[0])
                        for sim, rid in scored[:limit]:
                            row = conn.execute(
                                "SELECT * FROM reflections WHERE id = ?", (rid,)
                            ).fetchone()
                            if row:
                                hits.append(ReflectionHit(
                                    reflection=EpisodicReflection.from_dict(dict(row)),
                                    score=sim,
                                ))
            except Exception as exc:
                self._log(f"[reflection] Semantic search failed: {exc}")

        # Sort by score descending
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    # ------------------------------------------------------------------
    # Reflection generation
    # ------------------------------------------------------------------

    def build_reflection_prompt(
        self,
        goal: str,
        output: str,
        success: bool,
        tool_calls: list[str],
        similar_reflections: List[ReflectionHit],
    ) -> str:
        """Build a prompt for the LLM to generate a reflection."""
        parts = [
            "REFLITA sobre a execucao abaixo. Extraia licoes para melhorar no futuro.",
            "",
            f"OBJETIVO: {goal}",
            f"RESULTADO: {'SUCESSO' if success else 'FALHA'}",
            f"TOOLS USADAS: {', '.join(tool_calls) if tool_calls else 'nenhuma'}",
            f"SAIDA: {output[:500]}",
        ]

        if similar_reflections:
            parts.append("\nREFLEXOES SIMILARES DO PASSADO:")
            for i, hit in enumerate(similar_reflections[:2], 1):
                r = hit.reflection
                emoji = "✅" if r.success else "❌"
                parts.append(f"{i}. {emoji} [{r.goal[:100]}] → {r.reflection_text[:200]}")

        parts.extend([
            "",
            "Responda SOMENTE com JSON puro, sem markdown:",
            "{",
            '  "reflection": "sua reflexao em portugues (max 200 chars)",',
            '  "lessons_learned": ["licao 1", "licao 2"],',
            '  "mistakes_made": ["erro 1"],',
            '  "what_worked": ["acerto 1"],',
            '  "should_retry": true/false,',
            '  "suggested_approach": "como faria diferente (max 150 chars)"',
            "}",
        ])
        return "\n".join(parts)

    def parse_reflection_response(self, response_text: str) -> dict:
        """Parse the LLM reflection response into structured data."""
        try:
            # Try direct JSON parse
            return json.loads(response_text.strip())
        except json.JSONDecodeError:
            pass
        # Try to extract JSON block
        import re
        match = re.search(r'\{[^}]+\}', response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {
            "reflection": response_text[:200],
            "lessons_learned": [],
            "mistakes_made": [],
            "what_worked": [],
            "should_retry": False,
            "suggested_approach": "",
        }

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_recent(self, limit: int = 20, source: Optional[str] = None) -> List[EpisodicReflection]:
        """Get most recent reflections."""
        with self._connect() as conn:
            if source:
                rows = conn.execute(
                    "SELECT * FROM reflections WHERE source = ? ORDER BY created_at DESC LIMIT ?",
                    (source, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [EpisodicReflection.from_dict(dict(row)) for row in rows]

    def get_by_id(self, reflection_id: str) -> Optional[EpisodicReflection]:
        """Get a single reflection by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM reflections WHERE id = ?", (reflection_id,)
            ).fetchone()
        return EpisodicReflection.from_dict(dict(row)) if row else None

    def stats(self) -> dict:
        """Get reflection store statistics."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reflection_stats WHERE id = 1").fetchone()
            if row:
                return {
                    "total_reflections": int(row["total_reflections"] or 0),
                    "total_successes": int(row["total_successes"] or 0),
                    "total_failures": int(row["total_failures"] or 0),
                    "success_rate": round(
                        int(row["total_successes"] or 0) / max(1, int(row["total_reflections"] or 1)) * 100, 1
                    ),
                    "avg_iterations": round(float(row["avg_iterations"] or 0), 1),
                    "updated_at": row["updated_at"],
                    "enabled": REFLECTION_ENABLED,
                }
        return {"total_reflections": 0, "enabled": REFLECTION_ENABLED}

    def delete(self, reflection_id: str) -> bool:
        """Delete a reflection."""
        with self._connect() as conn:
            conn.execute("DELETE FROM reflections WHERE id = ?", (reflection_id,))
            conn.execute("DELETE FROM reflection_embeddings WHERE reflection_id = ?", (reflection_id,))
            conn.commit()
        return True

    def clear(self) -> int:
        """Clear all reflections. Returns count deleted."""
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
            conn.execute("DELETE FROM reflections")
            conn.execute("DELETE FROM reflection_embeddings")
            conn.execute("DELETE FROM reflections_fts")
            # Reset stats
            conn.execute("""
                UPDATE reflection_stats SET
                    total_reflections = 0,
                    total_successes = 0,
                    total_failures = 0,
                    avg_iterations = 0.0,
                    updated_at = ?
                WHERE id = 1
            """, (utc_now_iso(),))
            conn.commit()
        return count
