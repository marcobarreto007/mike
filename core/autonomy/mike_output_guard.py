# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Output Guard — Anti-Simulation Sentinel
=============================================

Intercepta respostas do LLM e detecta quando o modelo SIMULA acoes
em vez de realmente executa-las via <tool_call>.

Regras de deteccao:
  - Texto descreve acao concluida (salvei, criei, enviei, etc.)
  - Mas NAO contem <tool_call> nem tool_calls no response
  - Padroes de "falso sucesso": "Prontinho!", "Feito!", "Ja salvei!"

Arquitetura:
  RESPOSTA_LLM → ANALISAR → FLAG_SIMULACAO → LOG_AUDIT → CORRIGIR
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from mike_config import env_bool

log = logging.getLogger("mike.output_guard")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GUARD_ENABLED = env_bool("MIKE_OUTPUT_GUARD_ENABLED", True)
GUARD_AUTO_CORRECT = env_bool("MIKE_GUARD_AUTO_CORRECT", True)
GUARD_LOG_DIR = os.getenv("MIKE_GUARD_LOG_DIR", "runtime/memory/guard")


from core.shared.time_utils import utc_now, utc_now_iso


# ---------------------------------------------------------------------------
# Simulation Detection Patterns
# ---------------------------------------------------------------------------

# Frases que indicam que o modelo esta SIMULANDO ter feito algo
SIMULATION_PATTERNS = [
    # Portugues — acoes concluidas falsamente
    (r"(?i)\b(prontinho|pronto|feito|j[aá]\s*est[aá]|t[aá]\s*feito|conclu[íi]do)\b", "falso_sucesso"),
    (r"(?i)\b(j[aá]\s*salvei|salvei\s*o\s*arquivo|gravei|j[aá]\s*criei|criei\s*o)\b", "falso_salvamento"),
    (r"(?i)\b(j[aá]\s*enviei|enviei\s*o\s*email|mandei\s*o|disparei)\b", "falso_envio"),
    (r"(?i)\b(j[aá]\s*executei|executei\s*o\s*script|rodei\s*o|processei)\b", "falsa_execucao"),
    (r"(?i)\b(j[aá]\s*baixei|baixei\s*o|fiz\s*o\s*download|download\s*conclu)\b", "falso_download"),
    (r"(?i)\b(j[aá]\s*configurei|configurei\s*o|setei|defini\s*o)\b", "falsa_configuracao"),
    (r"(?i)\b(segue\s*o\s*resultado|resultado\s*abaixo|aqui\s*est[aá]\s*o)\b", "falso_resultado"),
    # Ingles — mesmo padrao
    (r"(?i)\b(done|all\s*set|ready|completed|finished|created|saved|sent)\b", "falso_sucesso_en"),
    # Navegacao / arquivo
    (r"(?i)\b(est[aá]\s*em\s*.*\/|caminho:.*\/|salvo\s*(em:?|com\s*sucesso\s*em)\s*.*\.html)\b", "falso_caminho"),
    (r"(?i)\b(arquivo\s*(foi\s*)?(salvo|criado|gerado)\s*(com\s*sucesso\s*)?(em\s*)?\/)", "falso_caminho"),
]

# Palavras que sozinhas nao indicam simulacao (falsos positivos comuns)
SAFE_PATTERNS = [
    r"(?i)\b(posso\s*(te\s*)?ajudar|como\s*posso|o\s*que\s*(voce|vc)\s*(precisa|quer|deseja))",
    r"(?i)\b(preciso\s*de|preciso\s*usar|vou\s*precisar|necess[aá]rio)",
    r"(?i)\b(se\s*quiser|se\s*voc[eê]\s*quiser|quando\s*quiser)",
    r"(?i)\b(n[aã]o\s*tenho\s*acesso|n[aã]o\s*consigo|infelizmente)",
    r"(?i)\b(perguntar|pergunta|d[uú]vida|explicar)",
]


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class GuardVerdict:
    """Resultado da analise de uma resposta."""
    timestamp: str = field(default_factory=utc_now_iso)
    session_id: str = "main"
    is_simulation: bool = False
    confidence: float = 0.0
    matched_patterns: list[str] = field(default_factory=list)
    response_snippet: str = ""
    contains_tool_call: bool = False
    tool_calls_count: int = 0
    action_taken: str = ""  # "allow" | "flag" | "block" | "correct"
    correction_note: str = ""


@dataclass
class GuardStats:
    total_checked: int = 0
    simulations_detected: int = 0
    auto_corrected: int = 0
    blocked: int = 0
    false_positives: int = 0


# ---------------------------------------------------------------------------
# OutputGuard
# ---------------------------------------------------------------------------

class OutputGuard:
    """Sentinel that monitors LLM outputs for simulation/hallucination."""

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        log_fn: Optional[Callable] = None,
    ):
        self._log_dir = Path(log_dir) if log_dir else Path(GUARD_LOG_DIR)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._audit_file = self._log_dir / "guard_audit.jsonl"
        self._verdicts_file = self._log_dir / "guard_verdicts.jsonl"
        self._log = log_fn or log.info
        self._stats = GuardStats()
        self._compiled_patterns = [(re.compile(p), label) for p, label in SIMULATION_PATTERNS]
        self._compiled_safe = [re.compile(p) for p in SAFE_PATTERNS]

    # ------------------------------------------------------------------
    # Main Check
    # ------------------------------------------------------------------

    def check(
        self,
        assistant_text: str,
        tool_calls: Optional[list] = None,
        session_id: str = "main",
        original_request: str = "",
    ) -> GuardVerdict:
        """Analyze an assistant response for simulation patterns.

        Returns a GuardVerdict with detection results and recommended action.
        """
        verdict = GuardVerdict(
            session_id=session_id,
            response_snippet=assistant_text[:300],
            contains_tool_call=bool(tool_calls),
            tool_calls_count=len(tool_calls) if tool_calls else 0,
        )

        # Fast path: if tool calls were made, it's not simulation
        if tool_calls:
            verdict.action_taken = "allow"
            self._stats.total_checked += 1
            return verdict

        # Check for simulation patterns
        text = assistant_text or ""
        matched = []

        for pattern, label in self._compiled_patterns:
            if pattern.search(text):
                # Check safe patterns — don't flag if safe pattern also matches
                is_safe = any(safe.search(text) for safe in self._compiled_safe)
                if not is_safe:
                    matched.append(label)

        verdict.matched_patterns = matched
        verdict.is_simulation = len(matched) > 0
        verdict.confidence = min(0.5 + len(matched) * 0.15, 1.0) if matched else 0.0

        # Decide action
        if verdict.is_simulation:
            self._stats.simulations_detected += 1

            if GUARD_AUTO_CORRECT and verdict.confidence >= 0.7:
                verdict.action_taken = "correct"
                verdict.correction_note = self._build_correction(matched, original_request)
                self._stats.auto_corrected += 1
            elif verdict.confidence >= 0.85:
                verdict.action_taken = "block"
                self._stats.blocked += 1
            else:
                verdict.action_taken = "flag"
        else:
            verdict.action_taken = "allow"

        self._stats.total_checked += 1

        # Log to audit trail
        self._write_audit(verdict)

        return verdict

    def _build_correction(self, matched_patterns: list[str], original_request: str) -> str:
        """Build a correction note to inject into the conversation."""
        patterns_str = ", ".join(matched_patterns)
        return (
            f"\n\n[AVISO DO GUARDA INTERNO: Detectei que minha resposta anterior continha "
            f"padroes de simulacao ({patterns_str}). Eu NAO executei nenhuma ferramenta real. "
            f"Vou corrigir isso agora e executar a acao de verdade.]"
        )

    # ------------------------------------------------------------------
    # Audit Trail
    # ------------------------------------------------------------------

    def _write_audit(self, verdict: GuardVerdict) -> None:
        """Write verdict to persistent audit log."""
        entry = {
            "timestamp": verdict.timestamp,
            "session_id": verdict.session_id,
            "is_simulation": verdict.is_simulation,
            "confidence": round(verdict.confidence, 2),
            "matched_patterns": verdict.matched_patterns,
            "action_taken": verdict.action_taken,
            "snippet": verdict.response_snippet[:200],
        }
        try:
            with open(self._audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            log.exception("Failed to write guard audit entry")

    def write_verdict(self, verdict: GuardVerdict) -> None:
        """Write a detailed verdict record."""
        entry = {
            "timestamp": verdict.timestamp,
            "session_id": verdict.session_id,
            "is_simulation": verdict.is_simulation,
            "confidence": verdict.confidence,
            "matched": verdict.matched_patterns,
            "tool_calls": verdict.tool_calls_count,
            "action": verdict.action_taken,
            "correction": verdict.correction_note[:200],
            "snippet": verdict.response_snippet[:300],
        }
        try:
            with open(self._verdicts_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            log.exception("Failed to write guard verdict entry")

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def recent_violations(self, limit: int = 20) -> list[dict]:
        """Get recent simulation violations from audit log."""
        if not self._audit_file.exists():
            return []
        entries = []
        try:
            lines = self._audit_file.read_text(encoding="utf-8").strip().split("\n")
            for line in lines[-limit:]:
                if line.strip():
                    entry = json.loads(line)
                    if entry.get("is_simulation"):
                        entries.append(entry)
        except Exception:
            pass
        entries.reverse()
        return entries

    def stats(self) -> dict:
        """Get guard statistics."""
        return {
            "enabled": GUARD_ENABLED,
            "auto_correct": GUARD_AUTO_CORRECT,
            "total_checked": self._stats.total_checked,
            "simulations_detected": self._stats.simulations_detected,
            "auto_corrected": self._stats.auto_corrected,
            "blocked": self._stats.blocked,
            "false_positives": self._stats.false_positives,
            "detection_rate": round(
                self._stats.simulations_detected / max(1, self._stats.total_checked) * 100, 1
            ),
        }

    def report_false_positive(self) -> None:
        """Call this when a flag/block was incorrect (user confirms action was real)."""
        self._stats.false_positives += 1


# ---------------------------------------------------------------------------
# Quick Check (stateless, for inline use)
# ---------------------------------------------------------------------------

def detect_simulation(text: str, has_tool_calls: bool = False) -> tuple[bool, float, list[str]]:
    """Quick stateless check: does this text look like simulation?"""
    if has_tool_calls:
        return False, 0.0, []

    if not text or not text.strip():
        return False, 0.0, []

    matched = []
    for pattern, label in [(re.compile(p), label) for p, label in SIMULATION_PATTERNS]:
        if pattern.search(text):
            is_safe = any(re.compile(s).search(text) for s in SAFE_PATTERNS)
            if not is_safe:
                matched.append(label)

    is_sim = len(matched) > 0
    confidence = min(0.5 + len(matched) * 0.15, 1.0) if matched else 0.0
    return is_sim, confidence, matched
