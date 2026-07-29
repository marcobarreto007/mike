# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Project Memory — Pilar 2: Consciência de Projeto
=======================================================

Não é "lembrar o que você disse". É entender o PORQUÊ de cada decisão.

Cada decisão arquitetural vira uma restrição futura:
- "Usamos Per-project mutex no ECC" → constraint: nunca global mutex
- "SQLite WAL mode para concorrência" → constraint: sempre WAL
- "urllib sem SDK do Twilio" → constraint: sem dependências heavy

A memória se torna consciência: não apenas banco de dados,
mas um sistema de restrições vivas que evolui com o projeto.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("mike.project_memory")

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent.parent  # core/memory -> core -> mike (project root)


# ======================================================================
# Data structures
# ======================================================================

@dataclass
class Decision:
    """An architectural/technical decision with context."""
    id: int = 0
    timestamp: str = ""
    project: str = ""         # "mike", "ecc", "dashboard", etc.
    context: str = ""         # What was the situation?
    decision: str = ""        # What was decided?
    rationale: str = ""       # Why this choice?
    alternatives: str = ""    # What was rejected and why?
    constraints: list[str] = field(default_factory=list)  # What this implies for the future
    tags: list[str] = field(default_factory=list)         # Searchable tags
    status: str = "active"    # active, superseded, revoked
    superseded_by: int = 0    # ID of newer decision that replaces this

    def to_constraint_text(self) -> str:
        """Format as a constraint block for system prompt injection."""
        parts = [f"[Decisão #{self.id}] {self.decision}"]
        if self.rationale:
            parts.append(f"Motivo: {self.rationale}")
        for c in self.constraints:
            parts.append(f"⚠️ Restrição: {c}")
        return "\n".join(parts)


@dataclass
class ProjectPattern:
    """A recurring pattern or convention detected across the project."""
    id: int = 0
    pattern: str = ""         # Description of the pattern
    examples: list[str] = field(default_factory=list)
    frequency: int = 0        # How many times observed
    last_seen: str = ""
    project: str = ""
    confidence: float = 0.0   # 0.0-1.0


# ======================================================================
# SQLite Storage
# ======================================================================

class ProjectMemoryDB:
    """SQLite storage for decisions and patterns."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_path = str(_PROJECT_ROOT / "runtime" / "memory" / "mike_memory.db")
        self.db_path = db_path
        self._ensure_tables()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    project TEXT DEFAULT '',
                    context TEXT DEFAULT '',
                    decision TEXT NOT NULL,
                    rationale TEXT DEFAULT '',
                    alternatives TEXT DEFAULT '',
                    constraints_json TEXT DEFAULT '[]',
                    tags_json TEXT DEFAULT '[]',
                    status TEXT DEFAULT 'active',
                    superseded_by INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern TEXT NOT NULL,
                    examples_json TEXT DEFAULT '[]',
                    frequency INTEGER DEFAULT 1,
                    last_seen TEXT NOT NULL,
                    project TEXT DEFAULT '',
                    confidence REAL DEFAULT 0.5
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_decisions_project
                    ON decisions(project, status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_decisions_tags
                    ON decisions(tags_json)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_patterns_project
                    ON project_patterns(project)
            """)

    # -- Decisions --

    def record_decision(self, decision: Decision) -> Decision:
        """Store a new decision."""
        if not decision.timestamp:
            decision.timestamp = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO decisions
                   (timestamp, project, context, decision, rationale,
                    alternatives, constraints_json, tags_json, status, superseded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.timestamp, decision.project, decision.context,
                    decision.decision, decision.rationale, decision.alternatives,
                    json.dumps(decision.constraints, ensure_ascii=False),
                    json.dumps(decision.tags, ensure_ascii=False),
                    decision.status, decision.superseded_by,
                ),
            )
            decision.id = cur.lastrowid
        return decision

    def get_decision(self, decision_id: int) -> Optional[Decision]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if not row:
            return None
        return self._row_to_decision(row)

    def get_active_decisions(self, project: str = "") -> list[Decision]:
        """Get all active decisions, optionally filtered by project."""
        with self._conn() as conn:
            if project:
                rows = conn.execute(
                    "SELECT * FROM decisions WHERE status = 'active' AND project = ? ORDER BY id DESC",
                    (project,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM decisions WHERE status = 'active' ORDER BY id DESC"
                ).fetchall()
        return [self._row_to_decision(r) for r in rows]

    def search_decisions(self, query: str, limit: int = 10) -> list[Decision]:
        """Search decisions by text match (decision, rationale, constraints, tags)."""
        query_lower = query.lower()
        words = query_lower.split()

        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE status = 'active' ORDER BY id DESC"
            ).fetchall()

        scored = []
        for row in rows:
            d = self._row_to_decision(row)
            text = f"{d.decision} {d.rationale} {d.context} {' '.join(d.constraints)} {' '.join(d.tags)}".lower()
            score = sum(1 for w in words if w in text)
            if score > 0:
                scored.append((score, d))

        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:limit]]

    def supersede_decision(self, old_id: int, new_decision: Decision) -> Decision:
        """Mark old decision as superseded and record the new one."""
        new_decision = self.record_decision(new_decision)
        with self._conn() as conn:
            conn.execute(
                "UPDATE decisions SET status = 'superseded', superseded_by = ? WHERE id = ?",
                (new_decision.id, old_id),
            )
        return new_decision

    def get_constraints_for_context(self, context_keywords: list[str], project: str = "") -> list[str]:
        """Get all active constraints relevant to a set of keywords."""
        decisions = self.get_active_decisions(project)
        relevant_constraints = []

        for d in decisions:
            if not d.constraints:
                continue
            # Check if any keyword matches decision text or tags
            d_text = f"{d.decision} {d.rationale} {' '.join(d.tags)}".lower()
            if any(kw.lower() in d_text for kw in context_keywords):
                for c in d.constraints:
                    relevant_constraints.append(f"[D#{d.id}] {c}")

        return relevant_constraints

    def _row_to_decision(self, row: sqlite3.Row) -> Decision:
        return Decision(
            id=row["id"],
            timestamp=row["timestamp"],
            project=row["project"],
            context=row["context"],
            decision=row["decision"],
            rationale=row["rationale"],
            alternatives=row["alternatives"],
            constraints=json.loads(row["constraints_json"]),
            tags=json.loads(row["tags_json"]),
            status=row["status"],
            superseded_by=row["superseded_by"],
        )

    # -- Patterns --

    def record_pattern(self, pattern: ProjectPattern) -> ProjectPattern:
        """Record or update a project pattern."""
        if not pattern.last_seen:
            pattern.last_seen = datetime.now(timezone.utc).isoformat()

        # Check if similar pattern exists
        existing = self._find_similar_pattern(pattern.pattern, pattern.project)
        if existing:
            # Update frequency and examples
            with self._conn() as conn:
                examples = json.loads(conn.execute(
                    "SELECT examples_json FROM project_patterns WHERE id = ?",
                    (existing,),
                ).fetchone()["examples_json"])
                for ex in pattern.examples:
                    if ex not in examples:
                        examples.append(ex)
                examples = examples[-10:]  # Keep max 10 examples

                conn.execute(
                    """UPDATE project_patterns SET
                       frequency = frequency + 1,
                       last_seen = ?,
                       examples_json = ?,
                       confidence = MIN(1.0, confidence + 0.1)
                       WHERE id = ?""",
                    (pattern.last_seen, json.dumps(examples, ensure_ascii=False), existing),
                )
            pattern.id = existing
        else:
            with self._conn() as conn:
                cur = conn.execute(
                    """INSERT INTO project_patterns
                       (pattern, examples_json, frequency, last_seen, project, confidence)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        pattern.pattern,
                        json.dumps(pattern.examples, ensure_ascii=False),
                        pattern.frequency or 1,
                        pattern.last_seen,
                        pattern.project,
                        pattern.confidence,
                    ),
                )
                pattern.id = cur.lastrowid
        return pattern

    def get_patterns(self, project: str = "", min_confidence: float = 0.3) -> list[ProjectPattern]:
        """Get known patterns for a project."""
        with self._conn() as conn:
            if project:
                rows = conn.execute(
                    "SELECT * FROM project_patterns WHERE project = ? AND confidence >= ? ORDER BY frequency DESC",
                    (project, min_confidence),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM project_patterns WHERE confidence >= ? ORDER BY frequency DESC",
                    (min_confidence,),
                ).fetchall()
        return [self._row_to_pattern(r) for r in rows]

    def _find_similar_pattern(self, pattern_text: str, project: str) -> Optional[int]:
        """Find an existing pattern that's similar enough to merge."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, pattern FROM project_patterns WHERE project = ?",
                (project,),
            ).fetchall()

        pattern_words = set(pattern_text.lower().split())
        for row in rows:
            existing_words = set(row["pattern"].lower().split())
            overlap = len(pattern_words & existing_words)
            total = len(pattern_words | existing_words)
            if total and overlap / total > 0.6:
                return row["id"]
        return None

    def _row_to_pattern(self, row: sqlite3.Row) -> ProjectPattern:
        return ProjectPattern(
            id=row["id"],
            pattern=row["pattern"],
            examples=json.loads(row["examples_json"]),
            frequency=row["frequency"],
            last_seen=row["last_seen"],
            project=row["project"],
            confidence=row["confidence"],
        )


# ======================================================================
# Project Consciousness — The Bridge
# ======================================================================

class ProjectConsciousness:
    """
    High-level interface for project-aware memory.

    Bridges the gap between "database of chat" and "understanding of
    architectural decisions and their implications".
    """

    def __init__(self, db: Optional[ProjectMemoryDB] = None) -> None:
        self.db = db or ProjectMemoryDB()

    def record(
        self,
        decision: str,
        rationale: str = "",
        constraints: Optional[list[str]] = None,
        project: str = "mike",
        context: str = "",
        alternatives: str = "",
        tags: Optional[list[str]] = None,
    ) -> Decision:
        """Record a new architectural/technical decision."""
        d = Decision(
            project=project,
            context=context,
            decision=decision,
            rationale=rationale,
            alternatives=alternatives,
            constraints=constraints or [],
            tags=tags or [],
        )
        return self.db.record_decision(d)

    def get_constraints(self, topic: str, project: str = "") -> list[str]:
        """Get all active constraints relevant to a topic."""
        keywords = topic.lower().split()
        # Also extract technical terms
        tech_terms = re.findall(r'\b[A-Z][a-z]+[A-Z]\w+\b|\b\w+_\w+\b', topic)
        keywords.extend(t.lower() for t in tech_terms)
        return self.db.get_constraints_for_context(keywords, project)

    def build_constraint_block(self, topic: str, project: str = "", max_constraints: int = 5) -> str:
        """Build a constraint block for system prompt injection."""
        constraints = self.get_constraints(topic, project)
        if not constraints:
            return ""

        constraints = constraints[:max_constraints]
        lines = ["\n[RESTRIÇÕES DE PROJETO ATIVAS]"]
        lines.append("As seguintes decisões arquiteturais DEVEM ser respeitadas:")
        for c in constraints:
            lines.append(f"  • {c}")
        lines.append("NUNCA sugira algo que viole estas restrições sem explicar por quê.")
        return "\n".join(lines)

    def extract_decisions_from_conversation(
        self,
        user_text: str,
        assistant_text: str,
        project: str = "mike",
    ) -> list[Decision]:
        """Analyze a conversation turn and extract implicit decisions."""
        decisions = []

        # Pattern: "vamos usar X porque Y" / "decidimos por X" / "optamos por X"
        decision_patterns = [
            r"(?:vamos usar|usaremos|decidimos? por|optamos? por|escolhemos?|a escolha [eé])\s+(.+?)(?:\s+(?:porque|pois|já que|uma vez que)\s+(.+?))?[.\n]",
            r"(?:implementar|criar|construir)\s+(?:com|usando|via)\s+(\S+.+?)(?:\s+(?:porque|para|por)\s+(.+?))?[.\n]",
            r"(?:melhor|preferível|ideal)\s+(?:usar|optar por|escolher)\s+(.+?)(?:\s+(?:em vez de|ao invés de|contra)\s+(.+?))?[.\n]",
        ]

        combined = f"{user_text}\n{assistant_text}"
        for pattern in decision_patterns:
            for match in re.finditer(pattern, combined, re.IGNORECASE):
                decision_text = match.group(1).strip()
                rationale = match.group(2).strip() if match.lastindex >= 2 and match.group(2) else ""

                if len(decision_text) < 10 or len(decision_text) > 500:
                    continue

                d = self.record(
                    decision=decision_text,
                    rationale=rationale,
                    project=project,
                    context=user_text[:200],
                    tags=self._extract_tags(combined),
                )
                decisions.append(d)

        # Pattern: convention/pattern reinforcement
        convention_patterns = [
            r"(?:padrão|convenção|regra):\s*(.+?)(?:\.|$)",
            r"sempre\s+(?:usar?|fazer?|manter?)\s+(.+?)(?:\.|$)",
            r"nunca\s+(?:usar?|fazer?|permitir?)\s+(.+?)(?:\.|$)",
        ]

        for pattern in convention_patterns:
            for match in re.finditer(pattern, combined, re.IGNORECASE):
                convention = match.group(1).strip()
                if 5 < len(convention) < 200:
                    self.db.record_pattern(ProjectPattern(
                        pattern=convention,
                        examples=[combined[:100]],
                        project=project,
                        confidence=0.5,
                    ))

        return decisions

    def _extract_tags(self, text: str) -> list[str]:
        """Extract relevant tags from text."""
        tag_keywords = {
            "sqlite", "fastapi", "gguf", "llama", "mcp", "twilio", "pipedrive",
            "elevenlabs", "telegram", "rag", "embeddings", "lightrag",
            "mutex", "wal", "cache", "gpu", "vram", "cuda", "onnx",
            "api", "rest", "webhook", "twiml", "tts", "sms",
            "ecc", "mike", "dashboard", "mobile",
        }
        text_lower = text.lower()
        return [t for t in tag_keywords if t in text_lower]

    def summary(self, project: str = "") -> dict:
        """Get a summary of project consciousness state."""
        decisions = self.db.get_active_decisions(project)
        patterns = self.db.get_patterns(project)
        constraints = []
        for d in decisions:
            constraints.extend(d.constraints)

        return {
            "decisions_count": len(decisions),
            "patterns_count": len(patterns),
            "constraints_count": len(constraints),
            "top_decisions": [
                {"id": d.id, "decision": d.decision[:80], "constraints": len(d.constraints)}
                for d in decisions[:5]
            ],
            "top_patterns": [
                {"pattern": p.pattern[:80], "frequency": p.frequency, "confidence": p.confidence}
                for p in patterns[:5]
            ],
        }
