# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Self-Monitor — Phase 6.1
==============================

Continuous self-monitoring of system health, model performance,
memory growth, and automatic recovery actions.

Features:
  • GPU temp / VRAM / utilization tracking
  • Disk space monitoring with growth-rate estimation
  • Model inference speed (tokens/sec) tracking
  • Memory fact counts (SQLite, Qdrant, Graph)
  • Uptime and request-rate tracking
  • Auto-recovery: restart llama server, clear GPU cache, log rotation
  • Telegram alerts on thresholds

Usage:
  from mike_monitor import MikeMonitor
  monitor = MikeMonitor()
  snapshot = monitor.snapshot()
  alerts   = monitor.check_alerts()
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from mike_config import nvidia_smi_path, env_int

log = logging.getLogger("mike.monitor")

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent.parent  # core/autonomy -> core -> mike (project root)


# ======================================================================
# Thresholds (env-overridable)
# ======================================================================

def _env_float(key: str, default: float) -> float:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


GPU_TEMP_WARN = env_int("MIKE_GPU_TEMP_WARN", 82)
GPU_TEMP_CRITICAL = env_int("MIKE_GPU_TEMP_CRITICAL", 90)
VRAM_USED_WARN_PCT = _env_float("MIKE_VRAM_WARN_PCT", 90.0)
DISK_FREE_WARN_GB = _env_float("MIKE_DISK_FREE_WARN_GB", 20.0)
DISK_FREE_CRITICAL_GB = _env_float("MIKE_DISK_FREE_CRITICAL_GB", 10.0)
LOG_MAX_SIZE_MB = env_int("MIKE_LOG_MAX_SIZE_MB", 200)
INFERENCE_SLOW_TPS = _env_float("MIKE_INFERENCE_SLOW_TPS", 3.0)


# ======================================================================
# GPU helpers (nvidia-smi)
# ======================================================================
def _nvidia_smi_query(*fields: str) -> Optional[dict[str, str]]:
    """Run nvidia-smi --query-gpu and return dict of values."""
    command = nvidia_smi_path()
    if not command:
        return None
    try:
        out = subprocess.check_output(
            [
                command,
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            timeout=5,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
        best = None
        for line in out.splitlines():
            values = [v.strip() for v in line.split(",")]
            if len(values) == len(fields):
                data = dict(zip(fields, values))
                if "memory.total" not in data:
                    return data
                if best is None or _safe_float(data.get("memory.total")) > _safe_float(best.get("memory.total")):
                    best = data
        return best
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Expected: nvidia-smi not installed, no GPU present, or driver timeout
        return None
    except Exception:
        log.exception("Unexpected error querying nvidia-smi")
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("W", "").strip())
    except (TypeError, ValueError):
        return default


def gpu_snapshot() -> dict[str, Any]:
    """Return current GPU metrics or empty dict."""
    data = _nvidia_smi_query(
        "temperature.gpu",
        "utilization.gpu",
        "memory.used",
        "memory.total",
        "power.draw",
        "fan.speed",
    )
    if not data:
        return {}
    mem_used = _safe_float(data.get("memory.used"))
    mem_total = _safe_float(data.get("memory.total"))
    mem_pct = round(mem_used / mem_total * 100, 1) if mem_total > 0 else 0
    return {
        "temp_c": int(_safe_float(data.get("temperature.gpu"))),
        "util_pct": int(_safe_float(data.get("utilization.gpu"))),
        "vram_used_mb": int(mem_used),
        "vram_total_mb": int(mem_total),
        "vram_pct": mem_pct,
        "power_w": _safe_float(data.get("power.draw")),
        "fan_pct": int(_safe_float(data.get("fan.speed"))),
    }


def disk_snapshot(path: Optional[Path] = None) -> dict[str, Any]:
    """Disk usage/free for the given path (defaults to project root)."""
    target = path or _PROJECT_ROOT
    try:
        usage = shutil.disk_usage(str(target))
        free_gb = round(usage.free / (1024**3), 2)
        total_gb = round(usage.total / (1024**3), 2)
        used_pct = round(usage.used / usage.total * 100, 1) if usage.total else 0
        return {
            "free_gb": free_gb,
            "total_gb": total_gb,
            "used_pct": used_pct,
        }
    except OSError:
        # Expected: path not accessible, permission denied, or drive not mounted
        return {}
    except Exception:
        log.exception("Unexpected error in disk snapshot for path=%s", target)
        return {}


# ======================================================================
# MikeMonitor
# ======================================================================
class MikeMonitor:
    """Self-monitoring and auto-recovery for Mike."""

    def __init__(
        self,
        log_fn: Optional[Callable[..., Any]] = None,
        telegram: Any = None,
    ) -> None:
        self._log = log_fn or log.info
        self._telegram = telegram  # MikeTelegram instance (optional)

        # History for trend analysis
        self._history_file = _PROJECT_ROOT / "logs" / "monitor_history.jsonl"
        self._history_file.parent.mkdir(parents=True, exist_ok=True)

        # Inference tracking
        self._inference_samples: list[float] = []  # tokens/sec samples
        self._request_count = 0
        self._start_time = time.time()

    # ------------------------------------------------------------------
    # Record inference speed
    # ------------------------------------------------------------------
    def record_inference(self, tokens: int, elapsed_sec: float) -> None:
        """Record a generation speed sample (tokens/sec)."""
        if elapsed_sec > 0 and tokens > 0:
            tps = tokens / elapsed_sec
            self._inference_samples.append(tps)
            # Keep last 100 samples
            if len(self._inference_samples) > 100:
                self._inference_samples = self._inference_samples[-100:]

    def record_request(self) -> None:
        """Increment request counter."""
        self._request_count += 1

    # ------------------------------------------------------------------
    # Snapshot — full system state
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Gather a complete system snapshot."""
        now = datetime.now(timezone.utc)
        uptime = time.time() - self._start_time

        gpu = gpu_snapshot()
        disk = disk_snapshot()

        # Memory counts
        memory_counts = self._memory_counts()

        # Inference stats
        inf = {}
        if self._inference_samples:
            samples = self._inference_samples
            inf = {
                "avg_tps": round(sum(samples) / len(samples), 1),
                "min_tps": round(min(samples), 1),
                "max_tps": round(max(samples), 1),
                "samples": len(samples),
            }

        # Log sizes
        log_size_mb = self._log_dir_size_mb()

        snap = {
            "timestamp": now.isoformat(),
            "uptime_hours": round(uptime / 3600, 2),
            "requests_total": self._request_count,
            "requests_per_hour": round(self._request_count / max(uptime / 3600, 0.01), 1),
            "gpu": gpu,
            "disk": disk,
            "memory_counts": memory_counts,
            "inference": inf,
            "log_size_mb": log_size_mb,
        }

        # Persist snapshot to history
        self._persist(snap)
        return snap

    # ------------------------------------------------------------------
    # Alerts — check thresholds
    # ------------------------------------------------------------------
    def check_alerts(self) -> list[dict[str, str]]:
        """Return list of {level, type, message} alert dicts."""
        alerts: list[dict[str, str]] = []
        gpu = gpu_snapshot()
        disk = disk_snapshot()

        # GPU temp
        if gpu:
            temp = gpu.get("temp_c", 0)
            if temp >= GPU_TEMP_CRITICAL:
                alerts.append({
                    "level": "critical",
                    "type": "gpu_temp",
                    "message": f"GPU {temp}°C — temperatura crítica!",
                })
            elif temp >= GPU_TEMP_WARN:
                alerts.append({
                    "level": "warning",
                    "type": "gpu_temp",
                    "message": f"GPU {temp}°C — temperatura alta",
                })

            # VRAM
            vram_pct = gpu.get("vram_pct", 0)
            if vram_pct >= VRAM_USED_WARN_PCT:
                alerts.append({
                    "level": "warning",
                    "type": "vram",
                    "message": f"VRAM {vram_pct}% usada ({gpu.get('vram_used_mb', 0)}MB / {gpu.get('vram_total_mb', 0)}MB)",
                })

        # Disk
        if disk:
            free_gb = disk.get("free_gb", 999)
            if free_gb <= DISK_FREE_CRITICAL_GB:
                alerts.append({
                    "level": "critical",
                    "type": "disk",
                    "message": f"Disco: apenas {free_gb}GB livres!",
                })
            elif free_gb <= DISK_FREE_WARN_GB:
                alerts.append({
                    "level": "warning",
                    "type": "disk",
                    "message": f"Disco: {free_gb}GB livres",
                })

        # Inference speed
        if self._inference_samples:
            avg_tps = sum(self._inference_samples) / len(self._inference_samples)
            if avg_tps < INFERENCE_SLOW_TPS:
                alerts.append({
                    "level": "warning",
                    "type": "inference",
                    "message": f"Inferência lenta: {avg_tps:.1f} tok/s (limite: {INFERENCE_SLOW_TPS})",
                })

        # Log rotation needed
        log_mb = self._log_dir_size_mb()
        if log_mb > LOG_MAX_SIZE_MB:
            alerts.append({
                "level": "warning",
                "type": "logs",
                "message": f"Logs ocupando {log_mb:.0f}MB (limite: {LOG_MAX_SIZE_MB}MB)",
            })

        return alerts

    # ------------------------------------------------------------------
    # Auto-recovery actions
    # ------------------------------------------------------------------
    def auto_recover(self) -> list[str]:
        """
        Run automatic recovery actions based on current state.
        Returns list of actions taken.
        """
        actions: list[str] = []
        alerts = self.check_alerts()

        for alert in alerts:
            atype = alert.get("type", "")

            # Log rotation
            if atype == "logs" and alert["level"] in ("warning", "critical"):
                rotated = self._rotate_logs()
                if rotated:
                    actions.append(f"Rotacionou {rotated} arquivo(s) de log")

            # GPU overheat → suggest model unload (don't force-kill)
            if atype == "gpu_temp" and alert["level"] == "critical":
                self._log(f"[!] GPU critica -- considere descarregar modelo")
                actions.append("Alerta GPU crítica registrado")

        # Send alerts via Telegram
        if alerts and self._telegram:
            critical = [a for a in alerts if a["level"] == "critical"]
            if critical:
                msg = "🚨 *ALERTA CRÍTICO — Mike Monitor*\n\n"
                for a in critical:
                    msg += f"• {a['message']}\n"
                try:
                    self._telegram.send_marco(msg)
                    actions.append(f"Enviou {len(critical)} alerta(s) Telegram")
                except Exception as exc:
                    self._log(f"Telegram alert failed: {exc}")

        return actions

    # ------------------------------------------------------------------
    # Status summary (for /v1/monitor route)
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Compact status for API responses."""
        snap = self.snapshot()
        alerts = self.check_alerts()
        return {
            "healthy": len([a for a in alerts if a["level"] == "critical"]) == 0,
            "alerts": alerts,
            "gpu": snap.get("gpu", {}),
            "disk": snap.get("disk", {}),
            "inference": snap.get("inference", {}),
            "uptime_hours": snap.get("uptime_hours", 0),
            "requests_total": snap.get("requests_total", 0),
            "memory_counts": snap.get("memory_counts", {}),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _memory_counts(self) -> dict[str, int]:
        """Count stored facts/conversations in SQLite DB."""
        counts: dict[str, int] = {}
        db_path = _PROJECT_ROOT / "data" / "mike_memory.db"
        if db_path.exists():
            try:
                with sqlite3.connect(str(db_path)) as conn:
                    for table in ("conversations", "knowledge", "tool_results"):
                        try:
                            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                            counts[table] = row[0] if row else 0
                        except sqlite3.OperationalError:
                            pass
            except Exception:
                log.exception("Failed to query memory counts from database %s", db_path)
        return counts

    def _log_dir_size_mb(self) -> float:
        """Total size of logs/ directory in MB."""
        log_dir = _PROJECT_ROOT / "logs"
        if not log_dir.exists():
            return 0
        total = sum(f.stat().st_size for f in log_dir.rglob("*") if f.is_file())
        return round(total / (1024 * 1024), 1)

    def _rotate_logs(self) -> int:
        """Rotate log files bigger than 50 MB. Returns count of rotated files."""
        log_dir = _PROJECT_ROOT / "logs"
        if not log_dir.exists():
            return 0
        rotated = 0
        for f in log_dir.glob("*.log"):
            try:
                if f.stat().st_size > 50 * 1024 * 1024:  # 50 MB
                    archive = f.with_suffix(f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.log.old")
                    f.rename(archive)
                    f.touch()
                    rotated += 1
            except Exception:
                pass
        return rotated

    def _persist(self, snap: dict) -> None:
        """Append snapshot to JSONL history file."""
        try:
            with open(self._history_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Trend analysis (use history)
    # ------------------------------------------------------------------
    def disk_growth_rate(self, hours: int = 24) -> Optional[float]:
        """
        Estimate disk usage growth in GB/day from recent history.
        Returns None if insufficient data.
        """
        if not self._history_file.exists():
            return None

        entries: list[tuple[float, float]] = []
        cutoff = time.time() - hours * 3600

        try:
            for line in self._history_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                ts = rec.get("timestamp", "")
                free = rec.get("disk", {}).get("free_gb")
                if free is None:
                    continue
                # Parse ISO timestamp to epoch
                try:
                    dt = datetime.fromisoformat(ts)
                    epoch = dt.timestamp()
                    if epoch >= cutoff:
                        entries.append((epoch, free))
                except (ValueError, OSError):
                    continue
        except Exception:
            return None

        if len(entries) < 2:
            return None

        # Linear regression: free_gb declining = positive growth
        first_time, first_free = entries[0]
        last_time, last_free = entries[-1]
        dt_hours = (last_time - first_time) / 3600
        if dt_hours < 0.1:
            return None
        delta_gb = first_free - last_free  # positive = space being consumed
        gb_per_day = delta_gb / dt_hours * 24
        return round(gb_per_day, 2)
