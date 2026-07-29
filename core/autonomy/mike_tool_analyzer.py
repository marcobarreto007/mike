# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Tool Failure Analyzer
===========================

Analisa falhas de ferramentas, classifica por tipo, detecta padroes,
e gera estrategias de recuperacao. Aprende com falhas repetidas.

Arquitetura:
  FALHA → CLASSIFICAR → REGISTRAR → DETECTAR PADRAO → GERAR ESTRATEGIA → RECUPERAR
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from mike_config import env_bool

log = logging.getLogger("mike.tool_analyzer")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOOL_ANALYZER_ENABLED = env_bool("MIKE_TOOL_ANALYZER_ENABLED", True)
TOOL_PATTERN_MIN_OCCURRENCES = int(os.getenv("MIKE_TOOL_PATTERN_MIN", "3"))
TOOL_RECOVERY_MAX_RETRIES = int(os.getenv("MIKE_TOOL_MAX_RETRIES", "2"))


from core.shared.time_utils import utc_now, utc_now_iso


# ---------------------------------------------------------------------------
# Error Classification
# ---------------------------------------------------------------------------

# Patterns for classifying tool failures
ERROR_CLASSIFIERS = [
    # (regex pattern, error_type, recovery_strategy)
    (r"(?i)(time\s*out|timed?\s*out|deadline\s*exceeded)", "timeout",
     "Reduzir payload/timeout e retentar com backoff exponencial"),
    (r"(?i)(unauthorized|auth\s*required|invalid\s*api\s*key|forbidden|401|403|not\s*authenticated)", "auth",
     "Verificar credenciais/tokens. Se OAuth, renovar token. Notificar Marco se necessario."),
    (r"(?i)(bad\s*request|invalid\s*(argument|param|input)|missing\s*(required|param|arg)|400|422)", "bad_args",
     "Revisar parametros enviados. Validar tipos e campos obrigatorios antes de reenviar."),
    (r"(?i)(internal\s*server|500|502|503|service\s*unavailable|upstream|backend)", "server_error",
     "Erro do servidor remoto. Aguardar 30s e retentar. Se persistir 3x, notificar."),
    (r"(?i)(rate\s*limit|too\s*many\s*requests|quota\s*exceeded|429|throttl)", "rate_limit",
     "Respeitar rate limit. Aguardar tempo do header Retry-After. Reduzir frequencia de chamadas."),
    (r"(?i)(connection\s*(refused|reset|error|failed)|network|dns|resolve|unreachable|socket|econnrefused|enotfound)", "network",
     "Verificar conectividade de rede. Ping no host. Retentar com intervalo crescente."),
    (r"(?i)(out\s*of\s*memory|memory\s*error|allocation\s*failed|oom|cuda.*out.*memory)", "out_of_memory",
     "Liberar memoria: limpar cache, reduzir batch, descarregar modelos nao usados."),
    (r"(?i)(permission\s*denied|access\s*denied|not\s*allowed|read.only|eacces)", "permission",
     "Verificar permissoes do arquivo/diretorio. Tentar caminho alternativo."),
    (r"(?i)(not\s*found|doesn'?t\s*exist|no\s*such|404|enoent)", "not_found",
     "Recurso nao encontrado. Verificar se path/ID existe. Criar se aplicavel."),
    (r"(?i)(encoding|decode|unicode|utf|charset|garbled)", "encoding",
     "Tentar diferentes encodings (utf-8, latin-1, cp1252). Usar errors='replace'."),
]


def classify_error(error_message: str) -> dict:
    """Classify a tool error message into type and suggest recovery."""
    if not error_message:
        return {"type": "unknown", "recovery": "Sem informacao de erro. Retentar uma vez.", "confidence": 0.0}

    best_match = None
    best_score = 0.0

    for pattern, error_type, recovery in ERROR_CLASSIFIERS:
        match = re.search(pattern, error_message)
        if match:
            # Score by match length relative to pattern
            score = len(match.group(0)) / len(pattern)
            if score > best_score:
                best_score = score
                best_match = {"type": error_type, "recovery": recovery, "confidence": round(score, 2)}
                if score > 0.9:  # High confidence, stop searching
                    break

    if best_match:
        return best_match

    return {"type": "unknown", "recovery": "Verificar logs e retentar com parametros padrao.", "confidence": 0.0}


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class ToolFailure:
    """Uma falha de ferramenta registrada."""
    id: str
    tool_name: str
    error_message: str
    error_type: str = "unknown"
    recovery_suggested: str = ""
    context: str = ""                  # O que estava tentando fazer
    arguments_summary: str = ""        # Resumo dos argumentos (sem dados sensiveis)
    source: str = "agentic_loop"       # agentic_loop | chat | routine | manual
    recovered: bool = False
    recovery_strategy_used: str = ""
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "tool_name": self.tool_name,
            "error_message": self.error_message[:500],
            "error_type": self.error_type,
            "recovery_suggested": self.recovery_suggested,
            "context": self.context[:300],
            "arguments_summary": self.arguments_summary,
            "source": self.source, "recovered": self.recovered,
            "recovery_strategy_used": self.recovery_strategy_used,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(data: dict) -> "ToolFailure":
        return ToolFailure(
            id=str(data.get("id", "")),
            tool_name=str(data.get("tool_name", "")),
            error_message=str(data.get("error_message", "")),
            error_type=str(data.get("error_type", "unknown")),
            recovery_suggested=str(data.get("recovery_suggested", "")),
            context=str(data.get("context", "")),
            arguments_summary=str(data.get("arguments_summary", "")),
            source=str(data.get("source", "agentic_loop")),
            recovered=bool(data.get("recovered", False)),
            recovery_strategy_used=str(data.get("recovery_strategy_used", "")),
            timestamp=str(data.get("timestamp", utc_now_iso())),
        )


@dataclass
class FailurePattern:
    """Um padrao de falha detectado (3+ ocorrencias similares)."""
    tool_name: str
    error_type: str
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    examples: list[str] = field(default_factory=list)  # Ultimos 3 exemplos
    recovery_strategy: str = ""
    lesson_learned: str = ""
    auto_mitigation: str = ""  # Acao automatica para mitigar

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "error_type": self.error_type,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "examples": self.examples,
            "recovery_strategy": self.recovery_strategy,
            "lesson_learned": self.lesson_learned,
            "auto_mitigation": self.auto_mitigation,
        }


# ---------------------------------------------------------------------------
# ToolFailureAnalyzer
# ---------------------------------------------------------------------------

class ToolFailureAnalyzer:
    """Analisa, classifica e aprende com falhas de ferramentas."""

    def __init__(
        self,
        store_dir: Optional[Path] = None,
        reflection_store: Any = None,
        log_fn: Optional[Callable] = None,
    ):
        self._store_dir = Path(store_dir) if store_dir else Path("runtime/memory/tool_analyzer")
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._failures_file = self._store_dir / "tool_failures.json"
        self._patterns_file = self._store_dir / "failure_patterns.json"
        self._reflection_store = reflection_store
        self._log = log_fn or log.info
        self._failures: list[ToolFailure] = []
        self._patterns: dict[str, FailurePattern] = {}  # key: tool_name:error_type
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._failures_file.exists():
            try:
                data = json.loads(self._failures_file.read_text(encoding="utf-8"))
                self._failures = [ToolFailure.from_dict(f) for f in data.get("failures", [])]
            except Exception as exc:
                self._log(f"[tool_analyzer] Load failures failed: {exc}")
        if self._patterns_file.exists():
            try:
                data = json.loads(self._patterns_file.read_text(encoding="utf-8"))
                for key, pdata in data.get("patterns", {}).items():
                    self._patterns[key] = FailurePattern(
                        tool_name=pdata["tool_name"],
                        error_type=pdata["error_type"],
                        count=pdata.get("count", 0),
                        first_seen=pdata.get("first_seen", ""),
                        last_seen=pdata.get("last_seen", ""),
                        examples=pdata.get("examples", []),
                        recovery_strategy=pdata.get("recovery_strategy", ""),
                        lesson_learned=pdata.get("lesson_learned", ""),
                        auto_mitigation=pdata.get("auto_mitigation", ""),
                    )
            except Exception as exc:
                self._log(f"[tool_analyzer] Load patterns failed: {exc}")
        self._loaded = True
        self._log(f"[tool_analyzer] Loaded {len(self._failures)} failures, {len(self._patterns)} patterns")

    def _save_failures(self) -> None:
        payload = {
            "updated_at": utc_now_iso(),
            "total": len(self._failures),
            "failures": [f.to_dict() for f in self._failures[-500:]],  # Keep last 500
        }
        tmp = self._failures_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._failures_file)

    def _save_patterns(self) -> None:
        payload = {
            "updated_at": utc_now_iso(),
            "total_patterns": len(self._patterns),
            "patterns": {k: v.to_dict() for k, v in self._patterns.items()},
        }
        tmp = self._patterns_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._patterns_file)

    # ------------------------------------------------------------------
    # Record & Classify
    # ------------------------------------------------------------------

    def record_failure(
        self,
        tool_name: str,
        error_message: str,
        context: str = "",
        arguments: Optional[dict] = None,
        source: str = "agentic_loop",
    ) -> ToolFailure:
        """Record a tool failure, classify it, and update patterns."""
        self._ensure_loaded()

        # Classify
        classification = classify_error(error_message)

        # Summarize arguments (strip sensitive data)
        args_summary = ""
        if arguments:
            safe_args = {
                k: (v if not _looks_sensitive(k) else "***")
                for k, v in arguments.items()
            }
            args_summary = json.dumps(safe_args, ensure_ascii=False)[:200]

        failure = ToolFailure(
            id=f"fail_{uuid.uuid4().hex[:8]}",
            tool_name=tool_name,
            error_message=error_message[:1000],
            error_type=classification["type"],
            recovery_suggested=classification["recovery"],
            context=context[:300],
            arguments_summary=args_summary,
            source=source,
        )

        self._failures.append(failure)
        self._save_failures()

        # Update pattern
        self._update_pattern(failure)

        # Log to reflection store for cross-learning
        if self._reflection_store:
            try:
                from mike_reflection import EpisodicReflection
                reflection = EpisodicReflection(
                    id="",
                    goal=f"Tool {tool_name} failed",
                    output=error_message[:500],
                    success=False,
                    reflection_text=classification["recovery"],
                    tool_calls=[tool_name],
                    tags=["tool_failure", classification["type"]],
                )
                self._reflection_store.save(reflection)
            except Exception:
                pass

        return failure

    def record_recovery(self, failure_id: str, strategy_used: str) -> None:
        """Mark a failure as recovered."""
        self._ensure_loaded()
        for f in self._failures:
            if f.id == failure_id:
                f.recovered = True
                f.recovery_strategy_used = strategy_used[:300]
                self._save_failures()
                return

    # ------------------------------------------------------------------
    # Pattern Detection
    # ------------------------------------------------------------------

    def _update_pattern(self, failure: ToolFailure) -> None:
        """Update or create a failure pattern."""
        key = f"{failure.tool_name}:{failure.error_type}"

        if key in self._patterns:
            p = self._patterns[key]
            p.count += 1
            p.last_seen = failure.timestamp
            p.examples.append(failure.error_message[:200])
            if len(p.examples) > 3:
                p.examples = p.examples[-3:]
        else:
            p = FailurePattern(
                tool_name=failure.tool_name,
                error_type=failure.error_type,
                count=1,
                first_seen=failure.timestamp,
                last_seen=failure.timestamp,
                examples=[failure.error_message[:200]],
                recovery_strategy=failure.recovery_suggested,
            )
            self._patterns[key] = p

        # If pattern is confirmed (>= min occurrences), generate lesson
        if p.count >= TOOL_PATTERN_MIN_OCCURRENCES and not p.lesson_learned:
            p.lesson_learned = self._generate_lesson(p)
            p.auto_mitigation = self._generate_mitigation(p)
            self._log(f"[tool_analyzer] Pattern confirmed: {key} ({p.count}x) → {p.lesson_learned[:100]}")

        self._save_patterns()

    def _generate_lesson(self, pattern: FailurePattern) -> str:
        """Generate a human-readable lesson from a confirmed failure pattern."""
        tool = pattern.tool_name
        etype = pattern.error_type
        common = _most_common_substring(pattern.examples)[:80] if pattern.examples else ""

        lessons = {
            "timeout": f"A tool '{tool}' frequentemente timeout. Sempre reduzir payload e usar timeout maior ao chamar esta tool.",
            "auth": f"'{tool}' tem problemas de autenticacao recorrentes. Verificar tokens/credenciais ANTES de cada chamada.",
            "bad_args": f"'{tool}' recebe argumentos invalidos com frequencia. Validar tipos e ranges antes de chamar: {common}",
            "server_error": f"'{tool}' encontra erros 5xx do servidor. Implementar retry com backoff e circuito de falha.",
            "rate_limit": f"'{tool}' bate rate limit. Implementar throttling e respeitar Retry-After.",
            "network": f"'{tool}' sofre com conectividade. Verificar rede antes e implementar retry.",
            "out_of_memory": f"'{tool}' causa OOM. Reduzir batch/tamanho de payload antes de chamar.",
            "permission": f"'{tool}' encontra erros de permissao. Verificar paths/perms antes.",
            "not_found": f"'{tool}' frequentemente nao encontra recursos. Validar existencia antes de chamar.",
            "encoding": f"'{tool}' tem problemas de encoding. Usar utf-8 com fallback para latin-1.",
        }
        return lessons.get(etype, f"A tool '{tool}' falha repetidamente com {etype}. Revisar estrategia de uso.")

    def _generate_mitigation(self, pattern: FailurePattern) -> str:
        """Generate automatic mitigation strategy."""
        mitigations = {
            "timeout": "timeout_3x → reduzir_payload_50% → timeout_10x",
            "auth": "refresh_token → retry_1x → notify_marco",
            "bad_args": "validar_schema → usar_defaults → retry_1x",
            "server_error": "wait_30s → retry_1x → wait_60s → retry_1x → notify",
            "rate_limit": "sleep_retry_after → reduzir_frequencia → queue",
            "network": "ping_check → retry_3x_backoff → notify",
            "out_of_memory": "clear_cache → reduzir_batch → retry",
            "permission": "check_perms → try_alt_path → notify",
            "not_found": "check_exists → try_create → notify",
            "encoding": "try_utf8 → try_latin1 → use_replace",
        }
        return mitigations.get(pattern.error_type, "retry_1x → notify")

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_recovery_strategy(self, tool_name: str, error_message: str) -> dict:
        """Get the best recovery strategy for a failure, considering past patterns."""
        self._ensure_loaded()

        # Check if there's a known pattern
        classification = classify_error(error_message)
        etype = classification["type"]
        key = f"{tool_name}:{etype}"
        pattern = self._patterns.get(key)

        if pattern and pattern.count >= TOOL_PATTERN_MIN_OCCURRENCES:
            return {
                "type": etype,
                "recovery": pattern.recovery_strategy,
                "auto_mitigation": pattern.auto_mitigation,
                "is_known_pattern": True,
                "occurrences": pattern.count,
                "lesson": pattern.lesson_learned,
                "confidence": 0.9,
            }

        return {
            "type": etype,
            "recovery": classification["recovery"],
            "auto_mitigation": "",
            "is_known_pattern": False,
            "confidence": classification["confidence"],
        }

    def get_patterns(self, min_count: int = 1) -> list[dict]:
        """Get all detected failure patterns."""
        self._ensure_loaded()
        return [
            p.to_dict() for p in self._patterns.values()
            if p.count >= min_count
        ]

    def get_failures(
        self,
        tool_name: Optional[str] = None,
        error_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get recent failures, optionally filtered."""
        self._ensure_loaded()
        failures = list(self._failures)
        if tool_name:
            failures = [f for f in failures if f.tool_name == tool_name]
        if error_type:
            failures = [f for f in failures if f.error_type == error_type]
        failures.sort(key=lambda f: f.timestamp, reverse=True)
        return [f.to_dict() for f in failures[:limit]]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        self._ensure_loaded()
        by_type: dict[str, int] = {}
        by_tool: dict[str, int] = {}
        recovered = 0
        for f in self._failures:
            by_type[f.error_type] = by_type.get(f.error_type, 0) + 1
            by_tool[f.tool_name] = by_tool.get(f.tool_name, 0) + 1
            if f.recovered:
                recovered += 1

        confirmed_patterns = sum(
            1 for p in self._patterns.values()
            if p.count >= TOOL_PATTERN_MIN_OCCURRENCES
        )

        return {
            "enabled": TOOL_ANALYZER_ENABLED,
            "total_failures": len(self._failures),
            "recovered": recovered,
            "recovery_rate": round(recovered / max(1, len(self._failures)) * 100, 1),
            "by_type": by_type,
            "top_failing_tools": dict(sorted(by_tool.items(), key=lambda x: -x[1])[:10]),
            "patterns_total": len(self._patterns),
            "patterns_confirmed": confirmed_patterns,
            "min_occurrences_for_pattern": TOOL_PATTERN_MIN_OCCURRENCES,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_sensitive(key: str) -> bool:
    """Check if a key name looks like it contains sensitive data."""
    sensitive = {"password", "pass", "token", "secret", "key", "auth",
                  "credential", "api_key", "apikey", "pwd", "pin"}
    key_lower = key.lower().replace("_", "").replace("-", "")
    return any(s in key_lower for s in sensitive)


def _most_common_substring(strings: list[str], min_len: int = 20) -> str:
    """Find the most common substring across error messages."""
    if not strings:
        return ""
    # Simple approach: find words that appear in all strings
    all_words = []
    for s in strings[:3]:
        words = re.findall(r'\w{4,}', s.lower())
        all_words.append(set(words))
    if not all_words:
        return ""
    common = all_words[0]
    for wset in all_words[1:]:
        common = common & wset
    return " ".join(sorted(common)[:10])
