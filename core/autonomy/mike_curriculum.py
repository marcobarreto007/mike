# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Auto-Curriculum (Voyager Pattern)
=======================================

Implementa curriculo automatico de aprendizado inspirado no Voyager
(arxiv 2305.16291). O Mike gera seus proprios objetivos de aprendizado,
pratica no tempo ocioso, e avanca por niveis de dificuldade.

Paper: Voyager: An Open-Ended Embodied Agent with Large Language Models
Key results: 3.3x more unique items, 15.3x faster tech tree unlock

Arquitetura:
  AVALIAR CAPACIDADES → GERAR GOALS → PRATICAR → AVALIAR → DOMINAR → AVANCAR
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from mike_config import env_bool

log = logging.getLogger("mike.curriculum")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CURRICULUM_ENABLED = env_bool("MIKE_CURRICULUM_ENABLED", True)
CURRICULUM_MAX_ACTIVE_GOALS = int(os.getenv("MIKE_CURRICULUM_MAX_ACTIVE", "5"))
CURRICULUM_IDLE_THRESHOLD_SEC = int(os.getenv("MIKE_CURRICULUM_IDLE_SEC", "300"))
CURRICULUM_MASTERY_THRESHOLD = float(os.getenv("MIKE_CURRICULUM_MASTERY_THRESHOLD", "0.80"))


from core.shared.time_utils import utc_now, utc_now_iso


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class ExplorationGoal:
    """Um objetivo de aprendizado auto-gerado pelo Mike."""
    id: str
    description: str
    category: str = "general"         # file_ops | web | email | data | system | communication
    difficulty: int = 1               # 1=trivial, 5=expert
    status: str = "pending"           # pending | active | completed | failed | skipped
    prerequisites: list[str] = field(default_factory=list)  # goal IDs que precisam ser completados antes
    skill_ids: list[str] = field(default_factory=list)      # skills relacionadas
    attempts: int = 0
    max_attempts: int = 3
    score: float = 0.0               # 0.0-1.0 (qualidade da execucao)
    reflection_ids: list[str] = field(default_factory=list)  # reflexoes associadas
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_error: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "description": self.description,
            "category": self.category, "difficulty": self.difficulty,
            "status": self.status, "prerequisites": self.prerequisites,
            "skill_ids": self.skill_ids,
            "attempts": self.attempts, "max_attempts": self.max_attempts,
            "score": self.score, "reflection_ids": self.reflection_ids,
            "created_at": self.created_at, "started_at": self.started_at,
            "completed_at": self.completed_at,
            "last_error": self.last_error, "tags": self.tags,
        }

    @staticmethod
    def from_dict(data: dict) -> "ExplorationGoal":
        return ExplorationGoal(
            id=str(data.get("id", "")),
            description=str(data.get("description", "")),
            category=str(data.get("category", "general")),
            difficulty=int(data.get("difficulty", 1)),
            status=str(data.get("status", "pending")),
            prerequisites=list(data.get("prerequisites", [])),
            skill_ids=list(data.get("skill_ids", [])),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            score=float(data.get("score", 0.0)),
            reflection_ids=list(data.get("reflection_ids", [])),
            created_at=str(data.get("created_at", utc_now_iso())),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            last_error=str(data.get("last_error", "")),
            tags=list(data.get("tags", [])),
        )


# ---------------------------------------------------------------------------
# Capability Templates — what Mike can learn
# ---------------------------------------------------------------------------

CAPABILITY_TEMPLATES = {
    "file_ops": [
        {"description": "Listar arquivos de um diretorio e filtrar por extensao", "difficulty": 1},
        {"description": "Ler arquivo CSV e extrair colunas especificas", "difficulty": 2},
        {"description": "Ler arquivo Excel (.xlsx) com multiplas abas", "difficulty": 2},
        {"description": "Buscar texto em todos os arquivos de uma pasta (grep local)", "difficulty": 2},
        {"description": "Compactar arquivos em .zip e extrair", "difficulty": 2},
        {"description": "Monitorar mudancas em um diretorio (watchdog)", "difficulty": 3},
        {"description": "Processar arquivo grande (>100MB) em streaming/chunks", "difficulty": 4},
        {"description": "Sincronizar dois diretorios (rsync local)", "difficulty": 4},
        {"description": "Extrair metadados de fotos e organizar por data", "difficulty": 3},
        {"description": "Criar e restaurar backup incremental de diretorio", "difficulty": 5},
    ],
    "web": [
        {"description": "Fazer busca web simples e extrair resultados", "difficulty": 1},
        {"description": "Acessar API REST e parsear JSON", "difficulty": 2},
        {"description": "Extrair dados de pagina web com scraping basico", "difficulty": 2},
        {"description": "Fazer download de arquivo e verificar integridade", "difficulty": 2},
        {"description": "Autenticar em API OAuth2 e renovar token", "difficulty": 3},
        {"description": "Criar webhook receiver para eventos externos", "difficulty": 4},
        {"description": "Implementar retry com backoff exponencial para API", "difficulty": 3},
        {"description": "Fazer scraping de site com paginacao infinita", "difficulty": 4},
    ],
    "email": [
        {"description": "Enviar email simples (texto)", "difficulty": 1},
        {"description": "Enviar email com anexo", "difficulty": 2},
        {"description": "Listar emails nao lidos e classificar por prioridade", "difficulty": 2},
        {"description": "Buscar emails por remetente/assunto/data", "difficulty": 2},
        {"description": "Enviar email com template HTML", "difficulty": 3},
        {"description": "Criar regra de auto-responder para emails especificos", "difficulty": 3},
        {"description": "Extrair anexos de multiplos emails e organizar", "difficulty": 3},
        {"description": "Analisar sentimento de emails recebidos (urgente/neutro/spam)", "difficulty": 4},
    ],
    "data": [
        {"description": "Criar tabela SQLite e inserir dados", "difficulty": 1},
        {"description": "Fazer query SQL com JOIN e GROUP BY", "difficulty": 2},
        {"description": "Exportar dados para CSV/JSON", "difficulty": 1},
        {"description": "Gerar grafico simples (barras/linhas) com matplotlib", "difficulty": 2},
        {"description": "Calcular estatisticas basicas (media, mediana, desvio)", "difficulty": 2},
        {"description": "Criar dashboard HTML com dados de uma fonte", "difficulty": 3},
        {"description": "Fazer ETL: extrair de API → transformar → carregar em DB", "difficulty": 4},
        {"description": "Detectar anomalias em serie temporal simples", "difficulty": 4},
        {"description": "Implementar caching com TTL para queries frequentes", "difficulty": 3},
        {"description": "Criar relatorio automatico PDF com dados e graficos", "difficulty": 5},
    ],
    "system": [
        {"description": "Verificar espaco em disco e alertar se <10%", "difficulty": 1},
        {"description": "Listar processos rodando e consumo de recursos", "difficulty": 2},
        {"description": "Fazer backup de arquivo com timestamp", "difficulty": 1},
        {"description": "Monitorar uso de RAM/CPU e logar metricas", "difficulty": 2},
        {"description": "Rotacionar logs antigos (compress + delete)", "difficulty": 2},
        {"description": "Detectar e matar processo zumbi", "difficulty": 3},
        {"description": "Criar sistema de alertas baseado em thresholds", "difficulty": 3},
        {"description": "Implementar healthcheck periodico com notificacao", "difficulty": 3},
        {"description": "Auto-recuperacao: reiniciar servico se cair", "difficulty": 4},
        {"description": "Otimizar uso de disco com deduplicacao de arquivos", "difficulty": 5},
    ],
}


# ---------------------------------------------------------------------------
# AutoCurriculum
# ---------------------------------------------------------------------------

class AutoCurriculum:
    """Motor de curriculo automatico do Mike.

    Gera, prioriza e avalia objetivos de aprendizado baseado em:
    - Templates de capacidade pre-definidos
    - Gaps detectados (falhas frequentes, skills nao dominadas)
    - Progressao de dificuldade (sobe quando domina nivel atual)
    """

    def __init__(
        self,
        store_dir: Optional[Path] = None,
        reflection_store: Any = None,   # EpisodicReflectionStore
        skill_library: Any = None,       # SkillLibrary
        log_fn: Optional[Callable] = None,
    ):
        self._store_dir = Path(store_dir) if store_dir else Path("runtime/memory/curriculum")
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._goals_file = self._store_dir / "curriculum_goals.json"
        self._reflection_store = reflection_store
        self._skill_library = skill_library
        self._log = log_fn or log.info
        self._goals: dict[str, ExplorationGoal] = {}
        self._loaded = False
        self._last_idle_check: Optional[str] = None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._goals_file.exists():
            try:
                data = json.loads(self._goals_file.read_text(encoding="utf-8"))
                for item in data.get("goals", []):
                    g = ExplorationGoal.from_dict(item)
                    if g.id:
                        self._goals[g.id] = g
                self._log(f"[curriculum] Loaded {len(self._goals)} goals")
            except Exception as exc:
                self._log(f"[curriculum] Load failed: {exc}")
        self._loaded = True

    def _save(self) -> None:
        payload = {
            "updated_at": utc_now_iso(),
            "total_goals": len(self._goals),
            "goals": [g.to_dict() for g in self._goals.values()],
        }
        tmp = self._goals_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._goals_file)

    # ------------------------------------------------------------------
    # Goal Generation
    # ------------------------------------------------------------------

    def generate_goals(self, count: int = 3) -> list[ExplorationGoal]:
        """Generate new learning goals based on templates and gaps."""
        self._ensure_loaded()

        # Don't generate if already have enough active goals
        active = [g for g in self._goals.values() if g.status in ("pending", "active")]
        if len(active) >= CURRICULUM_MAX_ACTIVE_GOALS:
            return []

        new_goals = []
        templates = self._get_applicable_templates()

        for tmpl in templates[:count]:
            goal_id = f"goal_{uuid.uuid4().hex[:8]}"

            # Check if already exists
            exists = any(
                g.description.lower() == tmpl["description"].lower()
                for g in self._goals.values()
            )
            if exists:
                continue

            goal = ExplorationGoal(
                id=goal_id,
                description=tmpl["description"],
                category=tmpl.get("category", "general"),
                difficulty=tmpl.get("difficulty", 1),
                tags=tmpl.get("tags", []),
            )
            self._goals[goal.id] = goal
            new_goals.append(goal)

        if new_goals:
            self._save()
            self._log(f"[curriculum] Generated {len(new_goals)} new goals")

        return new_goals

    def generate_from_gaps(self) -> list[ExplorationGoal]:
        """Generate goals based on detected capability gaps."""
        self._ensure_loaded()
        gaps = self.detect_gaps()
        new_goals = []

        for gap in gaps[:3]:
            goal_id = f"goal_{uuid.uuid4().hex[:8]}"
            goal = ExplorationGoal(
                id=goal_id,
                description=gap["description"],
                category=gap.get("category", "general"),
                difficulty=gap.get("difficulty", 2),
                tags=["gap_detected"] + gap.get("tags", []),
            )
            self._goals[goal.id] = goal
            new_goals.append(goal)

        if new_goals:
            self._save()
        return new_goals

    def _get_applicable_templates(self) -> list[dict]:
        """Get templates that are at appropriate difficulty for current level."""
        # Determine current mastery level per category
        category_levels: dict[str, float] = {}
        for goal in self._goals.values():
            if goal.status == "completed":
                cat = goal.category
                current = category_levels.get(cat, 1.0)
                category_levels[cat] = max(current, goal.difficulty)

        # Collect templates at or slightly above current level
        candidates = []
        for category, tmpls in CAPABILITY_TEMPLATES.items():
            current_lvl = category_levels.get(category, 1.0)
            for tmpl in tmpls:
                if tmpl["difficulty"] <= current_lvl + 1:
                    candidates.append({**tmpl, "category": category})

        # Prioritize: easier first, then mix
        candidates.sort(key=lambda t: (t["difficulty"], t["description"]))
        return candidates

    # ------------------------------------------------------------------
    # Gap Detection
    # ------------------------------------------------------------------

    def detect_gaps(self) -> list[dict]:
        """Detect capability gaps from past reflections and failures."""
        gaps = []

        # Check reflection store for patterns of failure
        if self._reflection_store:
            try:
                recent = self._reflection_store.get_recent(limit=50)
                fail_by_category: dict[str, int] = {}
                for r in recent:
                    if not r.success:
                        # Infer category from tags or goal text
                        for tag in r.tags:
                            if tag in CAPABILITY_TEMPLATES:
                                fail_by_category[tag] = fail_by_category.get(tag, 0) + 1
                        # Fallback: check keywords
                        goal_lower = r.goal.lower()
                        for cat in CAPABILITY_TEMPLATES:
                            if cat.replace("_", " ") in goal_lower or cat in goal_lower:
                                fail_by_category[cat] = fail_by_category.get(cat, 0) + 1

                # Generate gaps for categories with repeated failures
                for cat, count in fail_by_category.items():
                    if count >= 2:
                        tmpls = CAPABILITY_TEMPLATES.get(cat, [])
                        for tmpl in tmpls[:1]:
                            gaps.append({
                                "description": f"Melhorar em {cat}: {tmpl['description']}",
                                "category": cat,
                                "difficulty": tmpl["difficulty"],
                                "reason": f"{count} falhas recentes em {cat}",
                                "tags": ["gap", cat],
                            })
            except Exception as exc:
                self._log(f"[curriculum] Gap detection failed: {exc}")

        # Check skill library for low-success-rate skills
        if self._skill_library:
            try:
                skills = self._skill_library.list_skills()
                for skill in skills:
                    if skill.success_rate() < 0.5 and skill.fail_count >= 2:
                        gaps.append({
                            "description": f"Praticar e melhorar skill: {skill.name}",
                            "category": skill.category,
                            "difficulty": 2,
                            "reason": f"Baixa taxa de sucesso ({skill.success_rate():.0%})",
                            "tags": ["skill_practice", skill.category],
                        })
            except Exception:
                pass

        return gaps[:5]

    # ------------------------------------------------------------------
    # Goal Lifecycle
    # ------------------------------------------------------------------

    def start_goal(self, goal_id: str) -> Optional[ExplorationGoal]:
        """Mark a goal as active and start tracking."""
        self._ensure_loaded()
        g = self._goals.get(goal_id)
        if not g or g.status != "pending":
            return None
        # Check prerequisites
        for prereq_id in g.prerequisites:
            prereq = self._goals.get(prereq_id)
            if not prereq or prereq.status != "completed":
                self._log(f"[curriculum] Cannot start {goal_id}: prerequisite {prereq_id} not met")
                return None
        g.status = "active"
        g.started_at = utc_now_iso()
        g.attempts += 1
        self._save()
        return g

    def complete_goal(self, goal_id: str, score: float = 1.0, reflection_id: str = "") -> Optional[ExplorationGoal]:
        """Mark a goal as completed."""
        self._ensure_loaded()
        g = self._goals.get(goal_id)
        if not g or g.status != "active":
            return None
        g.status = "completed"
        g.completed_at = utc_now_iso()
        g.score = min(1.0, max(0.0, score))
        if reflection_id:
            g.reflection_ids.append(reflection_id)
        self._save()
        self._log(f"[curriculum] Goal completed: {g.description[:80]} (score={g.score:.2f})")

        # Auto-generate next level goals
        if g.score >= CURRICULUM_MASTERY_THRESHOLD:
            self.generate_goals(count=1)
        return g

    def fail_goal(self, goal_id: str, error: str = "") -> Optional[ExplorationGoal]:
        """Mark a goal as failed (may retry)."""
        self._ensure_loaded()
        g = self._goals.get(goal_id)
        if not g or g.status != "active":
            return None
        g.last_error = error[:500]
        if g.attempts >= g.max_attempts:
            g.status = "failed"
            g.completed_at = utc_now_iso()

            # Generate a lower-difficulty prerequisite
            easier = self._find_easier_alternative(g)
            if easier:
                self._goals[easier.id] = easier
                self._log(f"[curriculum] Generated easier prerequisite: {easier.description[:80]}")
        else:
            g.status = "pending"  # Allow retry
        self._save()
        return g

    def skip_goal(self, goal_id: str) -> Optional[ExplorationGoal]:
        """Skip a goal (not relevant right now)."""
        self._ensure_loaded()
        g = self._goals.get(goal_id)
        if not g or g.status in ("completed", "failed"):
            return None
        g.status = "skipped"
        g.completed_at = utc_now_iso()
        self._save()
        return g

    def _find_easier_alternative(self, goal: ExplorationGoal) -> Optional[ExplorationGoal]:
        """Find or create an easier version of a failed goal."""
        cat = goal.category
        lower_diff = max(1, goal.difficulty - 1)
        tmpls = CAPABILITY_TEMPLATES.get(cat, [])
        candidates = [t for t in tmpls if t["difficulty"] == lower_diff]
        if not candidates:
            return None
        tmpl = candidates[0]
        return ExplorationGoal(
            id=f"goal_{uuid.uuid4().hex[:8]}",
            description=tmpl["description"],
            category=cat,
            difficulty=lower_diff,
            tags=["easier_alternative"],
        )

    # ------------------------------------------------------------------
    # Next Best Goal
    # ------------------------------------------------------------------

    def next_goal(self) -> Optional[ExplorationGoal]:
        """Get the next best goal to work on, considering prerequisites and difficulty."""
        self._ensure_loaded()

        # First: active goals
        active = [g for g in self._goals.values() if g.status == "active"]
        if active:
            return active[0]

        # Then: pending goals with met prerequisites, sorted by difficulty
        pending = [g for g in self._goals.values() if g.status == "pending"]
        ready = []
        for g in pending:
            prereqs_met = all(
                self._goals.get(pid) and self._goals[pid].status == "completed"
                for pid in g.prerequisites
            )
            if prereqs_met:
                ready.append(g)

        ready.sort(key=lambda g: (g.difficulty, -g.attempts))
        if ready:
            return ready[0]

        # Generate new goals if nothing pending
        if not pending:
            self.generate_goals(count=3)
            return self.next_goal()

        return None

    def idle_action(self) -> Optional[dict]:
        """Called during idle time. Returns a goal to work on or None."""
        if not CURRICULUM_ENABLED:
            return None

        # Check if enough idle time has passed
        now = utc_now()
        if self._last_idle_check:
            try:
                last = datetime.fromisoformat(self._last_idle_check.replace("Z", "+00:00"))
                if (now - last).total_seconds() < CURRICULUM_IDLE_THRESHOLD_SEC:
                    return None
            except Exception:
                pass

        self._last_idle_check = now.isoformat()

        # Try to find something to do
        goal = self.next_goal()
        if not goal:
            # Generate from gaps
            new_goals = self.generate_from_gaps()
            if new_goals:
                goal = new_goals[0]

        if goal and goal.status == "pending":
            self.start_goal(goal.id)
            return {
                "action": "practice_goal",
                "goal_id": goal.id,
                "description": goal.description,
                "category": goal.category,
                "difficulty": goal.difficulty,
            }

        return None

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_goals(self, status: Optional[str] = None, category: Optional[str] = None) -> list[dict]:
        self._ensure_loaded()
        goals = list(self._goals.values())
        if status:
            goals = [g for g in goals if g.status == status]
        if category:
            goals = [g for g in goals if g.category == category]
        goals.sort(key=lambda g: (g.difficulty, g.created_at))
        return [g.to_dict() for g in goals]

    def get_goal(self, goal_id: str) -> Optional[dict]:
        self._ensure_loaded()
        g = self._goals.get(goal_id)
        return g.to_dict() if g else None

    def progress(self) -> dict:
        """Overall curriculum progress."""
        self._ensure_loaded()
        goals = list(self._goals.values())
        completed = sum(1 for g in goals if g.status == "completed")
        total = max(1, len(goals))
        by_category: dict[str, dict] = {}
        for cat in set(g.category for g in goals):
            cat_goals = [g for g in goals if g.category == cat]
            cat_completed = sum(1 for g in cat_goals if g.status == "completed")
            by_category[cat] = {
                "total": len(cat_goals),
                "completed": cat_completed,
                "mastery_level": round(
                    sum(g.difficulty for g in cat_goals if g.status == "completed") / max(1, cat_completed), 1
                ),
            }
        return {
            "enabled": CURRICULUM_ENABLED,
            "total_goals": len(goals),
            "completed": completed,
            "active": sum(1 for g in goals if g.status == "active"),
            "pending": sum(1 for g in goals if g.status == "pending"),
            "failed": sum(1 for g in goals if g.status == "failed"),
            "completion_rate": round(completed / total * 100, 1),
            "avg_difficulty": round(sum(g.difficulty for g in goals if g.status == "completed") / max(1, completed), 1),
            "by_category": by_category,
        }

    def stats(self) -> dict:
        return self.progress()
