# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Learner — Phase 6.2: Learning Loops
==========================================

Post-conversation evaluation and continuous learning.
Extracts knowledge from conversations, detects patterns and errors,
creates anticipations and future reminders.

Learning Loop:
  CONVERSATION → EXTRACTION → GRAPH/MEMORY → IMPROVE NEXT CONVERSATION

Features:
  • Extract facts, preferences, and entities from conversations
  • Detect repeated questions / errors → register to avoid next time
  • Score conversations (informativeness, satisfaction signals)
  • Generate self-improvement notes saved to knowledge base
  • Anticipation engine: predict future needs based on patterns
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("mike.learner")

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent.parent  # core/autonomy -> core -> mike (project root)


class MikeLearner:
    """Post-conversation analysis and learning loop engine."""

    def __init__(
        self,
        log_fn: Optional[Callable[..., Any]] = None,
        memory: Any = None,   # MikeMemoryService
        graph: Any = None,     # MikeGraph
    ) -> None:
        self._log = log_fn or log.info
        self._memory = memory
        self._graph = graph

        # Persistent learning notes
        self._notes_dir = _PROJECT_ROOT / "runtime" / "knowledge" / "learnings"
        self._notes_dir.mkdir(parents=True, exist_ok=True)

        # Pattern tracking (in-memory, persists to file)
        self._patterns_file = _PROJECT_ROOT / "runtime" / "memory" / "learner_patterns.json"
        self._patterns = self._load_patterns()
        # Concurrency: conversas concorrentes (toda a familia) mutam _patterns e gravam
        # o JSON em paralelo. RLock protege mutacao + persistencia (sem lost updates).
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Post-conversation analysis
    # ------------------------------------------------------------------
    def analyze_conversation(
        self,
        user_text: str,
        assistant_text: str,
        session_id: str = "main",
    ) -> dict[str, Any]:
        """
        Analyze a single exchange and extract learnings.
        Returns a dict with extracted facts, score, and any actions taken.
        """
        result: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "facts_extracted": [],
            "patterns_detected": [],
            "score": 0,
            "actions": [],
        }

        # 1. Extract factual statements
        facts = self._extract_facts(user_text, assistant_text)
        result["facts_extracted"] = facts

        # 2. Score the exchange
        score = self._score_exchange(user_text, assistant_text)
        result["score"] = score

        # 3-5. Mutacao de _patterns + persistencia: atomicas por conversa (thread-safe).
        # Varias conversas concorrentes partilham self._patterns; sem lock havia lost
        # updates (similar[key] += 1) e gravacoes inconsistentes do JSON.
        with self._lock:
            # 3. Detect patterns (repeated questions, error corrections)
            patterns = self._detect_patterns(user_text, assistant_text)
            result["patterns_detected"] = patterns

            # 4. Track for future anticipation
            self._track_topic(user_text, session_id)

            # 5. Save notable learnings
            if facts or patterns:
                self._save_learning(result)

        return result

    # ------------------------------------------------------------------
    # Fact extraction (rule-based, no LLM needed)
    # ------------------------------------------------------------------
    def _extract_facts(self, user_text: str, assistant_text: str) -> list[dict]:
        """Extract explicit facts from the conversation."""
        facts: list[dict] = []
        combined = f"{user_text} {assistant_text}"

        # Preference patterns: "eu gosto de X", "prefiro X", "minha X é Y"
        pref_patterns = [
            (r"(?:eu\s+)?(?:gosto|adoro|amo|prefiro|curto)\s+(?:de\s+)?(.{3,50})", "preference"),
            (r"meu\s+(?:favorit[oa]|preferid[oa])\s+(?:é|eh)\s+(.{3,50})", "preference"),
            (r"minh[ao]\s+(\w+)\s+(?:é|eh)\s+(.{3,50})", "attribute"),
        ]
        for pattern, fact_type in pref_patterns:
            for match in re.finditer(pattern, user_text, re.IGNORECASE):
                value = match.group(1).strip().rstrip(".!?,;")
                if len(value) > 2:
                    facts.append({"type": fact_type, "value": value, "source": "user"})

        # Name/entity mentions: "meu amigo X", "a X trabalha em Y"
        name_patterns = [
            (r"meu\s+(?:amigo|colega|vizinho|chefe)\s+(\w+)", "person"),
            (r"(?:a|o)\s+(\w+)\s+(?:trabalha|mora|estuda)\s+(?:em|na|no)\s+(.{3,30})", "person_detail"),
        ]
        for pattern, fact_type in name_patterns:
            for match in re.finditer(pattern, user_text, re.IGNORECASE):
                facts.append({"type": fact_type, "value": match.group(0).strip(), "source": "user"})

        # Date/event patterns: "amanhã eu tenho", "na sexta", "dia 15"
        date_patterns = [
            (r"(?:amanhã|amanh[ãa])\s+(?:eu\s+)?(?:tenho|vou|preciso)\s+(.{3,60})", "upcoming_event"),
            (r"dia\s+(\d{1,2})\s+(?:eu\s+)?(?:tenho|vou|preciso)\s+(.{3,60})", "upcoming_event"),
        ]
        for pattern, fact_type in date_patterns:
            for match in re.finditer(pattern, user_text, re.IGNORECASE):
                facts.append({"type": fact_type, "value": match.group(0).strip(), "source": "user"})

        # Error correction: "não, na verdade é X", "correto é X"
        correction_patterns = [
            (r"(?:não|nao),?\s+(?:na verdade|o correto|o certo)\s+(?:é|eh)\s+(.{3,60})", "correction"),
            (r"(?:errad[oa]|incorret[oa]),?\s+(.{3,60})", "correction"),
        ]
        for pattern, fact_type in correction_patterns:
            for match in re.finditer(pattern, user_text, re.IGNORECASE):
                facts.append({
                    "type": fact_type,
                    "value": match.group(1).strip(),
                    "source": "user",
                    "priority": "high",
                })

        return facts

    # ------------------------------------------------------------------
    # Conversation scoring
    # ------------------------------------------------------------------
    def _score_exchange(self, user_text: str, assistant_text: str) -> int:
        """
        Score 0-10 based on signals of quality/satisfaction.
        Higher = more informative / better interaction.
        """
        score = 5  # baseline

        # Positive signals
        positive_words = re.findall(
            r"\b(?:obrigado|valeu|perfeito|excelente|ótimo|legal|ajudou|massa|show|top|bom)\b",
            user_text.lower(),
        )
        score += min(len(positive_words), 3)

        # Negative signals
        negative_words = re.findall(
            r"\b(?:errado|incorreto|não era|não entendeu|ruim|péssimo|inútil)\b",
            user_text.lower(),
        )
        score -= min(len(negative_words) * 2, 4)

        # Length bonus (longer answers usually = more helpful)
        if len(assistant_text) > 500:
            score += 1
        if len(assistant_text) < 50:
            score -= 1

        # Correction penalty
        if re.search(r"(?:não|nao),?\s+(?:na verdade|o correto)", user_text, re.IGNORECASE):
            score -= 2

        return max(0, min(10, score))

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------
    def _detect_patterns(self, user_text: str, assistant_text: str) -> list[dict]:
        """Detect repeated questions, error patterns, topic clusters."""
        patterns: list[dict] = []

        # Normalize user query for comparison
        key = self._normalize(user_text)
        similar = self._patterns.get("queries", {})

        # Check for repeated similar queries
        for prev_key, count in similar.items():
            if self._similarity(key, prev_key) > 0.7 and count >= 2:
                patterns.append({
                    "type": "repeated_question",
                    "query": user_text[:80],
                    "count": count + 1,
                    "suggestion": "Considere salvar esta informação no knowledge base",
                })
                break

        # Update query counts
        if key not in similar:
            similar[key] = 0
        similar[key] += 1
        self._patterns["queries"] = similar

        # Error pattern: user corrected Mike
        if re.search(r"(?:errad|incorret|não era|nao era)", user_text, re.IGNORECASE):
            errors = self._patterns.get("errors", [])
            errors.append({
                "user_said": user_text[:200],
                "mike_said": assistant_text[:200],
                "when": datetime.now(timezone.utc).isoformat(),
            })
            # Keep last 50 errors
            self._patterns["errors"] = errors[-50:]
            patterns.append({
                "type": "error_correction",
                "total_errors": len(self._patterns["errors"]),
            })

        self._save_patterns()
        return patterns

    # ------------------------------------------------------------------
    # Anticipation engine
    # ------------------------------------------------------------------
    def _track_topic(self, user_text: str, session_id: str) -> None:
        """Track topic frequency for anticipation."""
        topics = self._patterns.get("topics", {})
        # Simple keyword extraction
        words = set(re.findall(r"\b[a-záàâãéèêíïóôõúüç]{4,}\b", user_text.lower()))
        stop_words = {
            "para", "como", "pode", "isso", "esse", "esta", "esta",
            "qual", "quais", "onde", "quando", "porque", "porquê",
            "fazer", "falar", "dizer", "aqui", "algo", "sobre",
            "muito", "mais", "menos", "ainda", "sendo", "estou",
            "voce", "você", "mike", "preciso", "quero", "gostaria",
        }
        keywords = words - stop_words

        for kw in keywords:
            if kw not in topics:
                topics[kw] = {"count": 0, "last_seen": ""}
            topics[kw]["count"] += 1
            topics[kw]["last_seen"] = datetime.now(timezone.utc).isoformat()

        self._patterns["topics"] = topics
        self._save_patterns()

    def get_hot_topics(self, top_n: int = 10) -> list[dict]:
        """Return most frequently discussed topics."""
        topics = self._patterns.get("topics", {})
        sorted_topics = sorted(topics.items(), key=lambda x: x[1]["count"], reverse=True)
        return [
            {"topic": k, "count": v["count"], "last_seen": v["last_seen"]}
            for k, v in sorted_topics[:top_n]
        ]

    def get_error_history(self, limit: int = 10) -> list[dict]:
        """Return recent errors/corrections."""
        errors = self._patterns.get("errors", [])
        return errors[-limit:]

    def get_learning_summary(self) -> dict[str, Any]:
        """Return summary of all learnings."""
        patterns = self._patterns
        total_queries = sum(patterns.get("queries", {}).values())
        unique_queries = len(patterns.get("queries", {}))
        total_errors = len(patterns.get("errors", []))
        total_topics = len(patterns.get("topics", {}))
        hot = self.get_hot_topics(5)

        # Count learning notes
        note_count = len(list(self._notes_dir.glob("*.json")))

        return {
            "total_analyzed": total_queries,
            "unique_queries": unique_queries,
            "total_errors": total_errors,
            "total_topics": total_topics,
            "hot_topics": hot,
            "learning_notes": note_count,
        }

    # ------------------------------------------------------------------
    # Internal: persistence
    # ------------------------------------------------------------------
    def _load_patterns(self) -> dict:
        if self._patterns_file.exists():
            try:
                return json.loads(self._patterns_file.read_text(encoding="utf-8"))
            except Exception:
                log.exception("Failed to load learner patterns from %s", self._patterns_file)
        return {"queries": {}, "errors": [], "topics": {}}

    def _save_patterns(self) -> None:
        with self._lock:
            try:
                self._patterns_file.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = str(self._patterns_file) + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self._patterns, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(self._patterns_file))
            except Exception:
                log.exception("Failed to save learner patterns to %s", self._patterns_file)

    def _save_learning(self, result: dict) -> None:
        """Save notable learning to daily file."""
        today = datetime.now().strftime("%Y-%m-%d")
        path = self._notes_dir / f"learnings_{today}.json"
        entries: list[dict] = []
        if path.exists():
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                entries = []
        entries.append(result)
        try:
            path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: text utils
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize text for comparison."""
        t = text.lower().strip()
        t = re.sub(r"[^\w\s]", "", t)
        t = re.sub(r"\s+", " ", t)
        return t[:200]  # cap length

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Simple word-overlap Jaccard similarity."""
        wa = set(a.split())
        wb = set(b.split())
        if not wa or not wb:
            return 0.0
        intersection = wa & wb
        union = wa | wb
        return len(intersection) / len(union) if union else 0.0
