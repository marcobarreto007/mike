# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Governance — Pilar 1: Closed-Loop Autonomy Engine
=======================================================

A IA que GOVERNA, não apenas responde.

Ciclo: Observar → Diagnosticar → Agir → Verificar

Em vez de apenas alertar "VRAM 94%", o Governance:
1. Observa a tendência (crescendo há 30 min?)
2. Diagnostica a causa (leak? modelo grande? cache?)
3. Age (limpa cache, reduz batch, reinicia se necessário)
4. Verifica se a ação resolveu

Roda como background task no FastAPI server, a cada 5 minutos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from mike_config import env_bool, set_runtime_override

# Kill-switch global (panic-stop). Import defensivo: se o modulo desaparecer,
# o governance continua a funcionar (fail-open) — o kill-switch e otim-in de seguranca.
try:
    from mike_kill_switch import is_engaged as _kill_switch_engaged
except Exception:  # pragma: no cover
    _kill_switch_engaged = None

log = logging.getLogger("mike.governance")

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent.parent  # core/autonomy -> core -> mike (project root)
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

# ======================================================================
# Configuration
# ======================================================================

GOVERNANCE_ENABLED = env_bool("MIKE_GOVERNANCE_ENABLED", True)
GOVERNANCE_INTERVAL_SEC = int(os.getenv("MIKE_GOVERNANCE_INTERVAL_SEC", "300"))  # 5 min
TREND_WINDOW = int(os.getenv("MIKE_GOVERNANCE_TREND_WINDOW", "12"))  # samples to track

# Escalation levels: AUTO → NOTIFY → ASK
# AUTO: execute silently
# NOTIFY: execute and tell Marco via Telegram
# ASK: don't execute, ask Marco first
ESCALATION_THRESHOLDS = {
    "cache_clear": "auto",       # Limpar cache de Python/temp — sempre seguro
    "log_rotate": "auto",        # Rotacionar logs — sempre seguro
    "batch_reduce": "auto",      # Reduzir batch size — sempre seguro
    "gpu_cache_clear": "notify", # Limpar GPU cache — notifica
    "process_restart": "notify", # Reiniciar llama server — notifica
    "model_unload": "ask",       # Descarregar modelo — pede permissão
}


# ======================================================================
# Data structures
# ======================================================================

@dataclass
class SystemSnapshot:
    """A point-in-time snapshot of system state."""
    timestamp: str
    gpu_temp: float = 0.0
    gpu_vram_pct: float = 0.0
    gpu_vram_used_mb: float = 0.0
    gpu_vram_total_mb: float = 0.0
    gpu_utilization: float = 0.0
    disk_free_gb: float = 999.0
    disk_total_gb: float = 0.0
    inference_tps: float = 0.0
    active_requests: int = 0
    memory_conversations: int = 0
    error_rate_pct: float = 0.0


@dataclass
class Diagnosis:
    """Result of analyzing system trends."""
    issue: str              # Short description
    severity: str           # low, medium, high, critical
    trend: str              # stable, rising, falling, spike
    evidence: list[str]     # Supporting data points
    recommended_action: str # Action key from ESCALATION_THRESHOLDS
    details: dict = field(default_factory=dict)


@dataclass
class GovernanceAction:
    """An action taken (or proposed) by the governance loop."""
    timestamp: str
    diagnosis: str
    action: str
    escalation: str  # auto, notify, ask
    executed: bool
    result: str
    reverted: bool = False


# ======================================================================
# Trend Analysis
# ======================================================================

class TrendTracker:
    """Tracks system metrics over time and detects anomalies."""

    def __init__(self, window: int = TREND_WINDOW) -> None:
        self.window = window
        self.history: deque[SystemSnapshot] = deque(maxlen=window)

    def add(self, snapshot: SystemSnapshot) -> None:
        self.history.append(snapshot)

    def _values(self, attr: str) -> list[float]:
        return [getattr(s, attr, 0.0) for s in self.history]

    def trend(self, attr: str) -> str:
        """Detect trend: stable, rising, falling, spike."""
        values = self._values(attr)
        if len(values) < 3:
            return "stable"

        recent = values[-3:]
        avg_recent = sum(recent) / len(recent)
        older = values[:-3] if len(values) > 3 else values[:1]
        avg_older = sum(older) / len(older) if older else avg_recent

        delta = avg_recent - avg_older
        pct_change = (delta / avg_older * 100) if avg_older else 0

        # Spike: last value >> average
        if values[-1] > avg_recent * 1.3 and values[-1] > avg_older * 1.3:
            return "spike"
        if pct_change > 15:
            return "rising"
        if pct_change < -15:
            return "falling"
        return "stable"

    def average(self, attr: str, last_n: int = 0) -> float:
        values = self._values(attr)
        if not values:
            return 0.0
        if last_n:
            values = values[-last_n:]
        return sum(values) / len(values)

    def latest(self, attr: str) -> float:
        if not self.history:
            return 0.0
        return getattr(self.history[-1], attr, 0.0)

    def rate_of_change(self, attr: str) -> float:
        """Average change per sample interval."""
        values = self._values(attr)
        if len(values) < 2:
            return 0.0
        deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
        return sum(deltas) / len(deltas)

    def predict(self, attr: str, steps_ahead: int = 6) -> Optional[float]:
        """Predict future value using simple linear regression.
        steps_ahead: number of sampling intervals ahead (default 6 = ~30min at 5min cycles).
        Returns None if not enough data.
        """
        values = self._values(attr)
        if len(values) < 5:
            return None
        n = len(values)
        x = list(range(n))
        mean_x = sum(x) / n
        mean_y = sum(values) / n
        num = sum((x[i] - mean_x) * (values[i] - mean_y) for i in range(n))
        den = sum((x[i] - mean_x) ** 2 for i in range(n))
        if den == 0:
            return mean_y
        slope = num / den
        intercept = mean_y - slope * mean_x
        return intercept + slope * (n + steps_ahead)

    def time_until_threshold(self, attr: str, threshold: float, higher_is_bad: bool = True) -> Optional[float]:
        """Estimate time (in sampling intervals) until metric crosses threshold.
        Returns None if trend is not approaching threshold.
        """
        values = self._values(attr)
        if len(values) < 5:
            return None
        n = len(values)
        x = list(range(n))
        mean_x = sum(x) / n
        mean_y = sum(values) / n
        num = sum((x[i] - mean_x) * (values[i] - mean_y) for i in range(n))
        den = sum((x[i] - mean_x) ** 2 for i in range(n))
        if den == 0:
            return None
        slope = num / den

        # Direction check
        current = values[-1]
        if higher_is_bad and slope <= 0:
            return None  # Not rising toward threshold
        if not higher_is_bad and slope >= 0:
            return None  # Not falling toward threshold

        steps = (threshold - current) / slope
        return max(0, steps) if steps > 0 else None


# ======================================================================
# Predictive Engine
# ======================================================================

class PredictiveEngine:
    """Predictive analytics for proactive governance.

    Uses historical trend data to predict future system states
    and trigger preventive actions BEFORE problems occur.
    """

    def __init__(self, tracker: TrendTracker) -> None:
        self.tracker = tracker

    def forecast(self) -> list[Diagnosis]:
        """Run all predictive checks. Returns preventive diagnoses."""
        diagnoses = []
        for rule in [
            self._predict_vram_critical,
            self._predict_disk_full,
            self._predict_thermal_threshold,
            self._predict_inference_crash,
        ]:
            d = rule()
            if d:
                diagnoses.append(d)
        return diagnoses

    def _predict_vram_critical(self) -> Optional[Diagnosis]:
        """Predict if VRAM will exceed 90% within next 30 minutes."""
        current = self.tracker.latest("gpu_vram_pct")
        if current < 60:
            return None  # Not worth predicting yet

        predicted = self.tracker.predict("gpu_vram_pct", steps_ahead=6)
        if predicted is None:
            return None

        if predicted >= 90:
            eta = self.tracker.time_until_threshold("gpu_vram_pct", 90.0)
            eta_str = f"{eta * 5:.0f}min" if eta else "indeterminado"
            return Diagnosis(
                issue="PREVISAO: VRAM atingira nivel critico",
                severity="high",
                trend="rising",
                evidence=[
                    f"VRAM atual: {current:.1f}%",
                    f"VRAM prevista (30min): {predicted:.1f}%",
                    f"ETA para 90%: {eta_str}",
                ],
                recommended_action="gpu_cache_clear" if predicted < 96 else "model_unload",
                details={"current": current, "predicted": predicted, "eta": eta},
            )
        return None

    def _predict_disk_full(self) -> Optional[Diagnosis]:
        """Predict if disk will drop below 10% within next hour."""
        free = self.tracker.latest("disk_free_gb")
        total = self.tracker.latest("disk_total_gb")
        if total <= 0 or free / max(1, total) > 0.30:
            return None

        predicted = self.tracker.predict("disk_free_gb", steps_ahead=12)  # ~1h
        if predicted is None:
            return None

        pct = predicted / max(1, total) * 100
        if pct < 10:
            eta = self.tracker.time_until_threshold("disk_free_gb", total * 0.10, higher_is_bad=False)
            eta_str = f"{eta * 5:.0f}min" if eta else "indeterminado"
            return Diagnosis(
                issue="PREVISAO: Disco ficara cheio em breve",
                severity="high",
                trend="falling",
                evidence=[
                    f"Disco livre: {free:.1f}GB ({free / max(1, total) * 100:.1f}%)",
                    f"Previsao (1h): {predicted:.1f}GB ({pct:.1f}%)",
                    f"ETA para 10%: {eta_str}",
                ],
                recommended_action="log_rotate",
                details={"free_gb": free, "predicted_gb": predicted, "eta": eta},
            )
        return None

    def _predict_thermal_threshold(self) -> Optional[Diagnosis]:
        """Predict if GPU temp will exceed 82C within 15 minutes."""
        current = self.tracker.latest("gpu_temp")
        if current < 60:
            return None

        predicted = self.tracker.predict("gpu_temp", steps_ahead=3)  # ~15min
        if predicted is None:
            return None

        if predicted >= 82:
            return Diagnosis(
                issue="PREVISAO: GPU atingira temperatura critica",
                severity="high",
                trend="rising",
                evidence=[
                    f"Temperatura atual: {current:.0f}C",
                    f"Previsao (15min): {predicted:.0f}C",
                ],
                recommended_action="batch_reduce",
                details={"current_temp": current, "predicted_temp": predicted},
            )
        return None

    def _predict_inference_crash(self) -> Optional[Diagnosis]:
        """Predict inference degradation leading to potential crash."""
        current_tps = self.tracker.latest("inference_tps")
        if current_tps <= 0 or current_tps >= 50:
            return None

        predicted = self.tracker.predict("inference_tps", steps_ahead=4)
        if predicted is None:
            return None

        if predicted < max(1.0, current_tps * 0.3):
            return Diagnosis(
                issue="PREVISAO: Velocidade de inferencia caira drasticamente",
                severity="medium",
                trend="falling",
                evidence=[
                    f"TPS atual: {current_tps:.1f} tok/s",
                    f"TPS previsto (20min): {predicted:.1f} tok/s",
                ],
                recommended_action="cache_clear",
                details={"current_tps": current_tps, "predicted_tps": predicted},
            )
        return None


# ======================================================================
# Diagnostic Engine
# ======================================================================

class DiagnosticEngine:
    """Analyzes trends and produces diagnoses."""

    def __init__(self, tracker: TrendTracker) -> None:
        self.tracker = tracker

    def diagnose(self) -> list[Diagnosis]:
        """Run all diagnostic rules and return issues found."""
        diagnoses = []
        for rule in [
            self._check_vram_pressure,
            self._check_gpu_thermal,
            self._check_disk_pressure,
            self._check_inference_degradation,
            self._check_error_rate,
        ]:
            d = rule()
            if d:
                diagnoses.append(d)
        return diagnoses

    def _check_vram_pressure(self) -> Optional[Diagnosis]:
        vram = self.tracker.latest("gpu_vram_pct")
        trend = self.tracker.trend("gpu_vram_pct")

        if vram >= 95:
            return Diagnosis(
                issue="VRAM crítica",
                severity="critical",
                trend=trend,
                evidence=[
                    f"VRAM atual: {vram:.1f}%",
                    f"Tendência: {trend}",
                    f"Taxa de mudança: {self.tracker.rate_of_change('gpu_vram_pct'):.1f}%/ciclo",
                ],
                recommended_action="gpu_cache_clear" if trend == "rising" else "batch_reduce",
                details={"vram_pct": vram, "trend": trend},
            )
        elif vram >= 88 and trend == "rising":
            return Diagnosis(
                issue="VRAM subindo — risco de OOM",
                severity="high",
                trend=trend,
                evidence=[
                    f"VRAM: {vram:.1f}% e subindo",
                    f"Média últimos 3: {self.tracker.average('gpu_vram_pct', 3):.1f}%",
                ],
                recommended_action="cache_clear",
                details={"vram_pct": vram},
            )
        return None

    def _check_gpu_thermal(self) -> Optional[Diagnosis]:
        temp = self.tracker.latest("gpu_temp")
        trend = self.tracker.trend("gpu_temp")

        if temp >= 90:
            return Diagnosis(
                issue="GPU superaquecendo",
                severity="critical",
                trend=trend,
                evidence=[
                    f"Temperatura: {temp:.0f}°C",
                    f"Tendência: {trend}",
                ],
                recommended_action="model_unload",
                details={"temp": temp},
            )
        elif temp >= 82 and trend == "rising":
            return Diagnosis(
                issue="GPU esquentando",
                severity="high",
                trend=trend,
                evidence=[f"Temp: {temp:.0f}°C, subindo"],
                recommended_action="batch_reduce",
                details={"temp": temp},
            )
        return None

    def _check_disk_pressure(self) -> Optional[Diagnosis]:
        free = self.tracker.latest("disk_free_gb")
        trend = self.tracker.trend("disk_free_gb")
        rate = self.tracker.rate_of_change("disk_free_gb")

        if free <= 5:
            return Diagnosis(
                issue="Disco quase cheio",
                severity="critical",
                trend=trend,
                evidence=[f"Livre: {free:.1f}GB, rate: {rate:.2f}GB/ciclo"],
                recommended_action="log_rotate",
                details={"free_gb": free, "rate": rate},
            )
        elif free <= 15 and trend == "falling":
            return Diagnosis(
                issue="Disco diminuindo",
                severity="medium",
                trend=trend,
                evidence=[f"Livre: {free:.1f}GB, perdendo {abs(rate):.2f}GB/ciclo"],
                recommended_action="log_rotate",
                details={"free_gb": free},
            )
        return None

    def _check_inference_degradation(self) -> Optional[Diagnosis]:
        tps = self.tracker.latest("inference_tps")
        trend = self.tracker.trend("inference_tps")
        avg = self.tracker.average("inference_tps")

        if tps > 0 and tps < 2.0:
            return Diagnosis(
                issue="Inferência extremamente lenta",
                severity="high",
                trend=trend,
                evidence=[
                    f"Velocidade: {tps:.1f} tok/s (média: {avg:.1f})",
                    f"Tendência: {trend}",
                ],
                recommended_action="process_restart",
                details={"tps": tps, "avg_tps": avg},
            )
        elif tps > 0 and tps < avg * 0.5 and trend == "falling":
            return Diagnosis(
                issue="Inferência degradando",
                severity="medium",
                trend=trend,
                evidence=[f"Atual: {tps:.1f} tok/s vs média {avg:.1f}"],
                recommended_action="cache_clear",
                details={"tps": tps},
            )
        return None

    def _check_error_rate(self) -> Optional[Diagnosis]:
        error_rate = self.tracker.latest("error_rate_pct")
        trend = self.tracker.trend("error_rate_pct")

        if error_rate > 20:
            return Diagnosis(
                issue="Taxa de erro alta",
                severity="high",
                trend=trend,
                evidence=[f"Erros: {error_rate:.1f}%"],
                recommended_action="process_restart",
                details={"error_rate": error_rate},
            )
        return None


# ======================================================================
# Action Executor
# ======================================================================

class ActionExecutor:
    """Executes governance actions with escalation control."""

    def __init__(self, telegram_fn: Optional[Callable] = None) -> None:
        self.telegram_fn = telegram_fn
        self.action_log: list[GovernanceAction] = []
        self._cooldowns: dict[str, float] = {}  # action → last_executed_time

    def execute(self, diagnosis: Diagnosis) -> GovernanceAction:
        """Execute or propose an action based on diagnosis and escalation level."""
        action_key = diagnosis.recommended_action
        escalation = ESCALATION_THRESHOLDS.get(action_key, "ask")

        # Cooldown: don't repeat the same action within 10 minutes
        cooldown_key = f"{action_key}_{diagnosis.issue}"
        last_run = self._cooldowns.get(cooldown_key, 0)
        if time.time() - last_run < 600:
            return GovernanceAction(
                timestamp=datetime.now(timezone.utc).isoformat(),
                diagnosis=diagnosis.issue,
                action=action_key,
                escalation="cooldown",
                executed=False,
                result="Em cooldown (10 min)",
            )

        result = ""
        executed = False

        if escalation == "auto":
            result = self._run_action(action_key, diagnosis)
            executed = True
        elif escalation == "notify":
            result = self._run_action(action_key, diagnosis)
            executed = True
            self._notify(f"🔧 *Governance Auto-Ação*\n\n"
                         f"Diagnóstico: {diagnosis.issue}\n"
                         f"Severidade: {diagnosis.severity}\n"
                         f"Ação: {action_key}\n"
                         f"Resultado: {result}")
        elif escalation == "ask":
            self._notify(f"⚠️ *Governance — Permissão Necessária*\n\n"
                         f"Diagnóstico: {diagnosis.issue}\n"
                         f"Severidade: {diagnosis.severity}\n"
                         f"Evidência: {'; '.join(diagnosis.evidence)}\n"
                         f"Ação recomendada: {action_key}\n\n"
                         f"Responda 'sim' para autorizar.")
            result = "Aguardando autorização"

        if executed:
            self._cooldowns[cooldown_key] = time.time()

        action = GovernanceAction(
            timestamp=datetime.now(timezone.utc).isoformat(),
            diagnosis=diagnosis.issue,
            action=action_key,
            escalation=escalation,
            executed=executed,
            result=result,
        )
        self.action_log.append(action)
        return action

    def _run_action(self, action_key: str, diagnosis: Diagnosis) -> str:
        """Execute a specific recovery action."""
        try:
            handler = {
                "cache_clear": self._action_cache_clear,
                "log_rotate": self._action_log_rotate,
                "batch_reduce": self._action_batch_reduce,
                "gpu_cache_clear": self._action_gpu_cache_clear,
                "process_restart": self._action_process_restart,
            }.get(action_key)

            if handler:
                return handler(diagnosis)
            return f"Ação desconhecida: {action_key}"
        except Exception as exc:
            log.error(f"Action {action_key} failed: {exc}")
            return f"Falha: {exc}"

    def _action_cache_clear(self, _diagnosis: Diagnosis) -> str:
        """Clear Python and temp caches."""
        import gc
        gc.collect()

        # Clear mike cache dir
        cache_dir = _PROJECT_ROOT / "runtime" / "cache"
        cleared = 0
        if cache_dir.exists():
            for f in cache_dir.rglob("*.tmp"):
                try:
                    f.unlink()
                    cleared += 1
                except OSError:
                    pass

        # Clear web search cache
        web_cache = _PROJECT_ROOT / "llm_cache"
        if web_cache.exists():
            # Only clear files older than 24h
            cutoff = time.time() - 86400
            for f in web_cache.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    try:
                        f.unlink()
                        cleared += 1
                    except OSError:
                        pass

        log.info(f"Cache clear: {cleared} files removed, gc.collect() done")
        return f"Removidos {cleared} arquivos de cache + gc.collect()"

    def _action_log_rotate(self, _diagnosis: Diagnosis) -> str:
        """Rotate large log files."""
        log_dir = _PROJECT_ROOT / "logs"
        rotated = 0
        for f in log_dir.glob("*.log"):
            try:
                size_mb = f.stat().st_size / (1024 * 1024)
                if size_mb > 50:
                    # Rename to .log.old (overwrite previous .old)
                    old = f.with_suffix(".log.old")
                    if old.exists():
                        old.unlink()
                    f.rename(old)
                    rotated += 1
            except OSError:
                pass

        # Truncate JSONL history files if too large
        for f in log_dir.glob("*.jsonl"):
            try:
                size_mb = f.stat().st_size / (1024 * 1024)
                if size_mb > 100:
                    lines = f.read_text(encoding="utf-8").splitlines()
                    # Keep last 1000 lines
                    f.write_text("\n".join(lines[-1000:]) + "\n", encoding="utf-8")
                    rotated += 1
            except OSError:
                pass

        log.info(f"Log rotate: {rotated} files processed")
        return f"Rotacionados {rotated} arquivos de log"

    def _action_batch_reduce(self, diagnosis: Diagnosis) -> str:
        """Set a runtime override for N_BATCH — safe, no filesystem race."""
        try:
            set_runtime_override("MIKE_N_BATCH", "256")
            log.info("Batch size reduced to 256 via runtime override")
            return "N_BATCH reduzido para 256 (override em memoria)"
        except Exception as exc:
            return f"Falha ao reduzir batch: {exc}"

    def _action_gpu_cache_clear(self, _diagnosis: Diagnosis) -> str:
        """Clear CUDA cache if possible."""
        try:
            # Try torch first
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                return "torch.cuda.empty_cache() executado"
        except ImportError:
            pass

        # Fallback: gc + signal to llama.cpp to free unused memory
        import gc
        gc.collect()
        return "gc.collect() executado (torch não disponível)"

    def _action_process_restart(self, _diagnosis: Diagnosis) -> str:
        """Signal for llama.cpp server restart via flag file."""
        restart_flag = _PROJECT_ROOT / "runtime" / "cache" / ".restart_requested"
        restart_flag.parent.mkdir(parents=True, exist_ok=True)
        restart_flag.write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
        log.info("Restart flag written — server should pick up on next cycle")
        return "Flag de restart criada — servidor reiniciará no próximo ciclo"

    def _notify(self, message: str) -> None:
        """Send notification via Telegram."""
        if self.telegram_fn:
            try:
                self.telegram_fn(message)
            except Exception as exc:
                log.warning(f"Governance notification failed: {exc}")
        log.info(f"Governance notify: {message[:100]}...")


# ======================================================================
# Governance Loop — The Brain
# ======================================================================

class GovernanceLoop:
    """
    Main governance loop: Observe → Diagnose → Act → Verify.

    Runs as a background asyncio task, integrated into the FastAPI server.
    """

    def __init__(
        self,
        monitor=None,
        telegram_fn: Optional[Callable] = None,
        stats: Optional[dict] = None,
    ) -> None:
        self.monitor = monitor
        self.tracker = TrendTracker()
        self.diagnostics = DiagnosticEngine(self.tracker)
        self.predictive = PredictiveEngine(self.tracker)
        self.executor = ActionExecutor(telegram_fn=telegram_fn)
        self.stats = stats or {}
        self._running = False
        self._cycle_count = 0
        self._last_cycle: Optional[dict] = None
        self._journal_file = _LOG_DIR / "governance_journal.jsonl"
        self._cycle_file = _LOG_DIR / "governance_cycle_count.txt"
        self._cycle_count = self._load_cycle_count()

    # ------------------------------------------------------------------
    # Observe: Collect system state
    # ------------------------------------------------------------------

    def observe(self) -> SystemSnapshot:
        """Collect a fresh system snapshot."""
        snap = SystemSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # GPU data from monitor
        if self.monitor:
            try:
                from mike_monitor import gpu_snapshot, disk_snapshot
                gpu = gpu_snapshot()
                disk = disk_snapshot()

                if gpu:
                    snap.gpu_temp = gpu.get("temp_c", 0)
                    snap.gpu_vram_pct = gpu.get("vram_pct", 0)
                    snap.gpu_vram_used_mb = gpu.get("vram_used_mb", 0)
                    snap.gpu_vram_total_mb = gpu.get("vram_total_mb", 0)
                    snap.gpu_utilization = gpu.get("utilization_pct", 0)

                if disk:
                    snap.disk_free_gb = disk.get("free_gb", 999)
                    snap.disk_total_gb = disk.get("total_gb", 0)
            except Exception as exc:
                log.debug(f"Monitor data collection failed: {exc}")

        # Inference speed from server stats
        if self.stats:
            snap.inference_tps = self.stats.get("last_speed_tps", 0)
            snap.active_requests = self.stats.get("total_requests", 0)

        self.tracker.add(snap)
        return snap

    # ------------------------------------------------------------------
    # Diagnose: Analyze trends
    # ------------------------------------------------------------------

    def diagnose(self) -> list[Diagnosis]:
        """Run diagnostic engine on current trends + predictive forecasts."""
        reactive = self.diagnostics.diagnose()
        predictive = self.predictive.forecast()
        # Merge: predictive diagnoses only if not already covered by reactive
        reactive_issues = {d.issue for d in reactive}
        combined = list(reactive)
        for p in predictive:
            if p.issue not in reactive_issues:
                combined.append(p)
        return combined

    def forecast(self) -> list[Diagnosis]:
        """Run predictive checks only (preventive)."""
        return self.predictive.forecast()

    # ------------------------------------------------------------------
    # Act: Execute corrective actions
    # ------------------------------------------------------------------

    def act(self, diagnoses: list[Diagnosis]) -> list[GovernanceAction]:
        """Execute actions for each diagnosis (with escalation)."""
        actions = []
        for d in sorted(diagnoses, key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x.severity, 4)):
            action = self.executor.execute(d)
            actions.append(action)
        return actions

    # ------------------------------------------------------------------
    # Verify: Check if actions resolved the issue
    # ------------------------------------------------------------------

    def verify(self, pre_snapshot: SystemSnapshot, actions: list[GovernanceAction]) -> list[dict]:
        """After acting, observe again and verify improvement."""
        if not actions or not any(a.executed for a in actions):
            return []

        post_snapshot = self.observe()
        verifications = []

        for action in actions:
            if not action.executed:
                continue

            v = {
                "action": action.action,
                "diagnosis": action.diagnosis,
                "resolved": False,
                "detail": "",
            }

            # Check specific metrics based on action type
            if "vram" in action.diagnosis.lower() or "cache" in action.action:
                delta = post_snapshot.gpu_vram_pct - pre_snapshot.gpu_vram_pct
                v["resolved"] = delta < 0
                v["detail"] = f"VRAM: {pre_snapshot.gpu_vram_pct:.1f}% → {post_snapshot.gpu_vram_pct:.1f}%"

            elif "disco" in action.diagnosis.lower() or "log" in action.action:
                delta = post_snapshot.disk_free_gb - pre_snapshot.disk_free_gb
                v["resolved"] = delta > 0
                v["detail"] = f"Disco livre: {pre_snapshot.disk_free_gb:.1f}GB → {post_snapshot.disk_free_gb:.1f}GB"

            elif "inferência" in action.diagnosis.lower() or "restart" in action.action:
                v["resolved"] = post_snapshot.inference_tps > pre_snapshot.inference_tps
                v["detail"] = f"TPS: {pre_snapshot.inference_tps:.1f} → {post_snapshot.inference_tps:.1f}"

            verifications.append(v)

        return verifications

    # ------------------------------------------------------------------
    # Full cycle
    # ------------------------------------------------------------------

    def run_cycle(self) -> dict:
        """Execute one complete governance cycle."""
        # Kill-switch global: se engaged, congela TODAS as acoes autonomas do governance.
        if _kill_switch_engaged is not None and _kill_switch_engaged():
            log.warning(
                "KILL_SWITCH ENGAGED — governance cycle #%d skipped (autonomia congelada)",
                self._cycle_count + 1,
            )
            frozen = {
                "cycle": self._cycle_count + 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_sec": 0.0,
                "frozen": True,
                "healthy": True,
                "snapshot": {},
                "diagnoses": [],
                "actions": [],
                "verifications": [],
            }
            self._last_cycle = frozen
            return frozen

        self._cycle_count += 1
        cycle_start = time.time()

        # 1. Observe
        snapshot = self.observe()

        # 2. Diagnose
        diagnoses = self.diagnose()

        # 3. Act (if issues found)
        actions = []
        verifications = []
        if diagnoses:
            actions = self.act(diagnoses)
            # 4. Verify (after a brief pause for actions to take effect)
            verifications = self.verify(snapshot, actions)

        cycle_result = {
            "cycle": self._cycle_count,
            "timestamp": snapshot.timestamp,
            "elapsed_sec": round(time.time() - cycle_start, 2),
            "snapshot": {
                "gpu_temp": snapshot.gpu_temp,
                "gpu_vram_pct": snapshot.gpu_vram_pct,
                "disk_free_gb": snapshot.disk_free_gb,
                "inference_tps": snapshot.inference_tps,
            },
            "diagnoses": [
                {"issue": d.issue, "severity": d.severity, "trend": d.trend, "action": d.recommended_action}
                for d in diagnoses
            ],
            "actions": [
                {"action": a.action, "executed": a.executed, "result": a.result, "escalation": a.escalation}
                for a in actions
            ],
            "verifications": verifications,
            "healthy": len(diagnoses) == 0,
        }

        self._last_cycle = cycle_result
        self._journal(cycle_result)
        self._save_cycle_count()

        if diagnoses:
            log.info(
                f"Governance cycle #{self._cycle_count}: {len(diagnoses)} issues, "
                f"{sum(1 for a in actions if a.executed)} actions taken"
            )
        else:
            log.debug(f"Governance cycle #{self._cycle_count}: healthy")

        return cycle_result

    # ------------------------------------------------------------------
    # Async background loop (for FastAPI integration)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the governance loop as a background task."""
        if not GOVERNANCE_ENABLED:
            log.info("Governance loop disabled")
            return
        if self._running:
            return

        self._running = True
        log.info(f"Governance loop started (interval: {GOVERNANCE_INTERVAL_SEC}s)")

        while self._running:
            try:
                # Run cycle in thread pool to avoid blocking event loop
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.run_cycle)
            except Exception as exc:
                log.error(f"Governance cycle error: {exc}")

            await asyncio.sleep(GOVERNANCE_INTERVAL_SEC)

    def stop(self) -> None:
        """Signal the loop to stop."""
        self._running = False
        log.info("Governance loop stopped")

    # ------------------------------------------------------------------
    # Status (for API exposure)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return current governance state for API/dashboard."""
        # Predictive metrics
        pred_vram = self.tracker.predict("gpu_vram_pct", steps_ahead=6)
        pred_disk = self.tracker.predict("disk_free_gb", steps_ahead=12)
        pred_temp = self.tracker.predict("gpu_temp", steps_ahead=3)
        eta_vram = self.tracker.time_until_threshold("gpu_vram_pct", 90.0)
        eta_disk = self.tracker.time_until_threshold("disk_free_gb", 5.0, higher_is_bad=False)

        return {
            "enabled": GOVERNANCE_ENABLED,
            "running": self._running,
            "cycle_count": self._cycle_count,
            "last_cycle": self._last_cycle,
            "trend_samples": len(self.tracker.history),
            "actions_taken": len(self.executor.action_log),
            "recent_actions": [
                {"time": a.timestamp, "action": a.action, "result": a.result}
                for a in self.executor.action_log[-5:]
            ],
            "predictive": {
                "vram_30min": round(pred_vram, 1) if pred_vram is not None else None,
                "disk_1h_gb": round(pred_disk, 1) if pred_disk is not None else None,
                "temp_15min_c": round(pred_temp, 1) if pred_temp is not None else None,
                "eta_vram_90pct_min": round(eta_vram * 5, 0) if eta_vram else None,
                "eta_disk_5gb_min": round(eta_disk * 5, 0) if eta_disk else None,
            },
        }

    # ------------------------------------------------------------------
    # Cycle count persistence
    # ------------------------------------------------------------------

    def _load_cycle_count(self) -> int:
        """Load persisted cycle count from disk."""
        if self._cycle_file.exists():
            try:
                return int(self._cycle_file.read_text(encoding="utf-8").strip())
            except Exception:
                return 0
        return 0

    def _save_cycle_count(self) -> None:
        """Persist cycle count to disk."""
        try:
            self._cycle_file.write_text(str(self._cycle_count), encoding="utf-8")
        except Exception as exc:
            log.warning(f"Failed to persist cycle count: {exc}")

    # ------------------------------------------------------------------
    # Journal (persistent log of all governance activity)
    # ------------------------------------------------------------------

    def _journal(self, cycle_result: dict) -> None:
        """Append cycle result to governance journal."""
        try:
            with open(self._journal_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(cycle_result, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning(f"Governance journal write failed: {exc}")
