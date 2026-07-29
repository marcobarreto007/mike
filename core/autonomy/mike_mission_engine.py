# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Mission Engine
===================

Proactive mission runner for end-to-end execution:
- Mission persistence (atomic JSON store)
- Scheduled and immediate missions
- Ordered steps with dependencies
- Checklist synchronization
- Agent SDK dispatch per step
- Retries with failure tracking
- Optional calendar event creation
- Optional proactive notifications

This module is intentionally framework-agnostic. FastAPI wiring lives in
mike_server.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from mike_config import env_bool

log = logging.getLogger("mike.missions")

MISSION_ENABLED = env_bool("MIKE_MISSIONS_ENABLED", True)
MISSION_INTERVAL_SEC = int(os.getenv("MIKE_MISSIONS_INTERVAL_SEC", "45"))
MISSION_MAX_STEP_ATTEMPTS = int(os.getenv("MIKE_MISSIONS_MAX_STEP_ATTEMPTS", "5"))
MISSION_MAX_EVENTS = int(os.getenv("MIKE_MISSIONS_MAX_EVENTS", "200"))
_MISSION_RETRY_BASE_SEC = 8.0      # exponential backoff base for step retries
_MISSION_RETRY_MAX_SEC = 120.0
_ORPHAN_RUNNING_TIMEOUT_SEC = 300  # 5 minutes: steps stuck 'running' longer are orphaned


from core.shared.time_utils import utc_now, utc_now_iso


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    txt = str(value).strip()
    if not txt:
        return None
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class MissionStep:
    id: str
    description: str
    agent_hint: str = ""
    depends_on: list[str] = field(default_factory=list)
    checklist_label: str = ""
    status: str = "pending"  # pending|running|done|failed|skipped
    attempts: int = 0
    max_attempts: int = MISSION_MAX_STEP_ATTEMPTS
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    output: str = ""
    tools_used: list[str] = field(default_factory=list)
    last_error: str = ""
    retry_after: Optional[str] = None  # ISO timestamp: don't retry before this time

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "agent_hint": self.agent_hint,
            "depends_on": list(self.depends_on),
            "checklist_label": self.checklist_label,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output": self.output,
            "tools_used": list(self.tools_used),
            "last_error": self.last_error,
            "retry_after": self.retry_after,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MissionStep":
        return MissionStep(
            id=str(data.get("id", "")),
            description=str(data.get("description", "")).strip(),
            agent_hint=str(data.get("agent_hint", "")).strip(),
            depends_on=[str(x) for x in data.get("depends_on", []) if str(x).strip()],
            checklist_label=str(data.get("checklist_label", "")).strip(),
            status=str(data.get("status", "pending")),
            attempts=int(data.get("attempts", 0)),
            max_attempts=max(1, int(data.get("max_attempts", MISSION_MAX_STEP_ATTEMPTS))),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            output=str(data.get("output", "")),
            tools_used=[str(x) for x in data.get("tools_used", [])],
            last_error=str(data.get("last_error", "")),
            retry_after=data.get("retry_after"),
        )


@dataclass
class Mission:
    id: str
    title: str
    goal: str
    profile_key: str
    status: str = "queued"  # queued|scheduled|running|completed|failed|cancelled
    trigger_at: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    steps: list[MissionStep] = field(default_factory=list)
    checklist: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "profile_key": self.profile_key,
            "status": self.status,
            "trigger_at": self.trigger_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "steps": [s.to_dict() for s in self.steps],
            "checklist": self.checklist,
            "metadata": self.metadata,
            "events": self.events,
            "progress": self.progress(),
        }

    def progress(self) -> dict[str, Any]:
        total = len(self.steps)
        done = sum(1 for s in self.steps if s.status == "done")
        failed = sum(1 for s in self.steps if s.status == "failed")
        running = sum(1 for s in self.steps if s.status == "running")
        pending = sum(1 for s in self.steps if s.status == "pending")
        percent = round((done / total) * 100, 1) if total else 100.0
        return {
            "total": total,
            "done": done,
            "failed": failed,
            "running": running,
            "pending": pending,
            "percent": percent,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Mission":
        mission = Mission(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")).strip(),
            goal=str(data.get("goal", "")).strip(),
            profile_key=str(data.get("profile_key", "main")),
            status=str(data.get("status", "queued")),
            trigger_at=data.get("trigger_at"),
            created_at=str(data.get("created_at", utc_now_iso())),
            updated_at=str(data.get("updated_at", utc_now_iso())),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            steps=[MissionStep.from_dict(s) for s in data.get("steps", [])],
            checklist=list(data.get("checklist", [])),
            metadata=dict(data.get("metadata", {})),
            events=list(data.get("events", [])),
        )
        mission._ensure_checklist()
        return mission

    def _ensure_checklist(self) -> None:
        if self.checklist:
            return
        generated = []
        for step in self.steps:
            generated.append({
                "id": step.id,
                "step_id": step.id,
                "label": step.checklist_label or step.description,
                "done": step.status == "done",
                "done_at": step.finished_at if step.status == "done" else None,
            })
        self.checklist = generated


class MissionEngine:
    """Persistent mission scheduler and runner."""

    def __init__(
        self,
        *,
        planner_fn: Optional[Callable[[str, str, int], Awaitable[list[dict[str, Any]]]]] = None,
        registry_factory: Callable[[str], Any],
        tool_manifest_fn: Callable[[str], Awaitable[list[dict[str, Any]]]],
        system_prompt_fn: Callable[[str], str],
        notify_fn: Optional[Callable[[str, str, str], None]] = None,
        calendar_event_fn: Optional[Callable[[str, str, str, str], Awaitable[dict[str, Any]]]] = None,
        store_path: Optional[Path] = None,
        interval_sec: int = MISSION_INTERVAL_SEC,
    ) -> None:
        self.planner_fn = planner_fn
        self.registry_factory = registry_factory
        self.tool_manifest_fn = tool_manifest_fn
        self.system_prompt_fn = system_prompt_fn
        self.notify_fn = notify_fn
        self.calendar_event_fn = calendar_event_fn
        self.store_path = store_path or Path("mike/memory/missions/missions.json")
        self.interval_sec = max(10, int(interval_sec))

        self._missions: dict[str, Mission] = {}
        self._lock = asyncio.Lock()
        self._loaded = False
        self._running = False

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            if not self.store_path.exists():
                self._loaded = True
                return
            try:
                raw = json.loads(self.store_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Mission store load failed: %s", exc)
                raw = {"missions": []}
            for item in raw.get("missions", []):
                mission = Mission.from_dict(item)
                if mission.id:
                    self._missions[mission.id] = mission
            self._loaded = True
            log.info("Mission engine loaded %d mission(s)", len(self._missions))

    def _save_locked(self) -> None:
        payload = {
            "updated_at": utc_now_iso(),
            "missions": [m.to_dict() for m in self._missions.values()],
        }
        temp = self.store_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.store_path)

    def _emit(self, title: str, body: str, tag: str = "mission") -> None:
        if not self.notify_fn:
            return
        try:
            self.notify_fn(title, body, tag)
        except Exception as exc:
            log.debug("Mission notify failed: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_mission(
        self,
        *,
        title: str,
        goal: str,
        profile_key: str,
        trigger_at: Optional[str] = None,
        steps: Optional[list[dict[str, Any]]] = None,
        auto_plan: bool = True,
        max_plan_steps: int = 6,
        metadata: Optional[dict[str, Any]] = None,
        add_to_calendar: bool = False,
    ) -> dict[str, Any]:
        await self._ensure_loaded()

        clean_title = str(title or goal).strip()
        clean_goal = str(goal or title).strip()
        if not clean_title:
            clean_title = "Mission"
        if not clean_goal:
            clean_goal = clean_title

        step_specs = list(steps or [])
        if not step_specs and auto_plan:
            step_specs = await self._plan_steps(clean_goal, profile_key, max_plan_steps)

        if not step_specs:
            step_specs = [{"description": clean_goal}]

        mission_id = f"msn_{uuid.uuid4().hex[:10]}"
        mission_steps = self._normalize_steps(step_specs)
        now = utc_now()
        scheduled_for = _parse_iso(trigger_at)
        status = "scheduled" if scheduled_for and scheduled_for > now else "queued"

        mission = Mission(
            id=mission_id,
            title=clean_title,
            goal=clean_goal,
            profile_key=str(profile_key or "main"),
            status=status,
            trigger_at=_iso_or_none(scheduled_for),
            steps=mission_steps,
            metadata=dict(metadata or {}),
        )
        mission._ensure_checklist()
        self._add_event(mission, "created", f"Mission criada com {len(mission.steps)} etapa(s)")

        if add_to_calendar and mission.trigger_at and self.calendar_event_fn:
            try:
                calendar = await self.calendar_event_fn(
                    mission.title,
                    mission.trigger_at,
                    mission.goal,
                    mission.profile_key,
                )
                mission.metadata["calendar_event"] = calendar
                self._add_event(mission, "calendar", "Evento de calendario criado")
            except Exception as exc:
                self._add_event(mission, "calendar_error", str(exc))

        async with self._lock:
            mission.updated_at = utc_now_iso()
            self._missions[mission.id] = mission
            self._save_locked()

        if mission.status == "scheduled":
            self._emit(
                "Mike Mission",
                f"Missao agendada: {mission.title} ({mission.trigger_at})",
                "mission-scheduled",
            )
        else:
            self._emit(
                "Mike Mission",
                f"Missao criada: {mission.title}",
                "mission-created",
            )

        return mission.to_dict()

    async def list_missions(
        self,
        *,
        status: Optional[str] = None,
        profile_key: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        await self._ensure_loaded()
        lim = max(1, min(int(limit), 500))

        async with self._lock:
            missions = list(self._missions.values())

        if profile_key:
            missions = [m for m in missions if m.profile_key == profile_key]
        if status:
            missions = [m for m in missions if m.status == status]

        missions.sort(key=lambda m: m.updated_at, reverse=True)
        return [m.to_dict() for m in missions[:lim]]

    async def get_mission(self, mission_id: str) -> Optional[dict[str, Any]]:
        await self._ensure_loaded()
        async with self._lock:
            mission = self._missions.get(mission_id)
            return mission.to_dict() if mission else None

    async def complete_step(
        self,
        mission_id: str,
        step_id: str,
        *,
        note: str = "",
        actor: str = "manual",
    ) -> Optional[dict[str, Any]]:
        await self._ensure_loaded()

        async with self._lock:
            mission = self._missions.get(mission_id)
            if not mission:
                return None
            step = next((s for s in mission.steps if s.id == step_id), None)
            if not step:
                return None

            step.status = "done"
            step.finished_at = utc_now_iso()
            if note:
                step.output = (step.output + "\n\n" + note).strip() if step.output else note
            self._sync_checklist_for_step(mission, step)
            self._add_event(mission, "step_done", f"Etapa {step.id} concluida por {actor}")
            self._refresh_mission_status(mission)
            mission.updated_at = utc_now_iso()
            self._save_locked()
            return mission.to_dict()

    async def cancel_mission(self, mission_id: str, reason: str = "") -> Optional[dict[str, Any]]:
        await self._ensure_loaded()

        async with self._lock:
            mission = self._missions.get(mission_id)
            if not mission:
                return None
            if mission.status in ("completed", "cancelled"):
                return mission.to_dict()
            mission.status = "cancelled"
            mission.completed_at = utc_now_iso()
            self._add_event(mission, "cancelled", reason or "Missao cancelada")
            mission.updated_at = utc_now_iso()
            self._save_locked()
            return mission.to_dict()

    async def run_once(self) -> dict[str, Any]:
        await self._ensure_loaded()
        return await self._tick()

    async def start(self) -> None:
        if not MISSION_ENABLED:
            log.info("Mission engine disabled")
            return
        if self._running:
            return
        await self._ensure_loaded()

        # Recover orphaned steps that were 'running' when process crashed
        await self._recover_orphaned_steps()

        self._running = True
        log.info("Mission engine started (interval=%ss)", self.interval_sec)

        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.warning("Mission engine tick failed: %s", exc)
            try:
                await asyncio.sleep(self.interval_sec)
            except asyncio.CancelledError:
                break

    async def start_supervised(self) -> None:
        """Start engine with auto-restart supervisor. Use this from server startup."""
        max_restarts = 10
        for restart in range(max_restarts):
            try:
                await self.start()
                break  # normal stop
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error(
                    "Mission engine crashed (restart %d/%d): %s",
                    restart + 1, max_restarts, exc,
                )
                self._running = False
                if restart + 1 < max_restarts:
                    await asyncio.sleep(min(10 * (2 ** restart), 120))

    def stop(self) -> None:
        self._running = False

    def status(self) -> dict[str, Any]:
        total = len(self._missions)
        by_status: dict[str, int] = {}
        for m in self._missions.values():
            by_status[m.status] = by_status.get(m.status, 0) + 1
        return {
            "enabled": MISSION_ENABLED,
            "running": self._running,
            "interval_sec": self.interval_sec,
            "total": total,
            "by_status": by_status,
        }

    # ------------------------------------------------------------------
    # Orphan recovery (Temporal-inspired crash recovery)
    # ------------------------------------------------------------------

    async def _recover_orphaned_steps(self) -> int:
        """Reset steps stuck in 'running' state back to 'pending' for retry.

        This handles the scenario where the process crashed while a step was
        executing. Without recovery, the step stays 'running' forever and
        _next_runnable_step() never picks it up again.
        """
        recovered = 0
        now = utc_now()
        async with self._lock:
            for mission in self._missions.values():
                if mission.status not in ("running", "queued"):
                    continue
                for step in mission.steps:
                    if step.status != "running":
                        continue
                    # Step was 'running' but process restarted => orphaned
                    started = _parse_iso(step.started_at) if step.started_at else None
                    if started and (now - started).total_seconds() > _ORPHAN_RUNNING_TIMEOUT_SEC:
                        step.status = "pending"
                        step.last_error = "Recuperado: processo reiniciou durante execucao"
                        self._add_event(
                            mission, "step_recovered",
                            f"Etapa {step.id} estava 'running' ha muito tempo, resetada para 'pending'",
                        )
                        recovered += 1
                    elif not started:
                        # started_at missing => definitely orphaned
                        step.status = "pending"
                        step.last_error = "Recuperado: sem timestamp de inicio"
                        recovered += 1
                if recovered:
                    mission.updated_at = utc_now_iso()
            if recovered:
                self._save_locked()
                log.info("Mission engine recovered %d orphaned step(s)", recovered)
        return recovered

    # ------------------------------------------------------------------
    # Internal scheduling/execution
    # ------------------------------------------------------------------

    async def _tick(self) -> dict[str, Any]:
        activated = 0
        executed = 0
        completed = 0
        failed = 0

        now = utc_now()

        async with self._lock:
            missions = list(self._missions.values())
            for mission in missions:
                if mission.status == "scheduled" and mission.trigger_at:
                    dt = _parse_iso(mission.trigger_at)
                    if dt and dt <= now:
                        mission.status = "queued"
                        mission.updated_at = utc_now_iso()
                        self._add_event(mission, "triggered", "Janela de execucao aberta")
                        activated += 1
            if activated:
                self._save_locked()

        for mission in missions:
            if mission.status not in ("queued", "running"):
                continue
            changed = await self._run_mission_step(mission.id)
            if not changed:
                continue
            executed += 1
            status = changed.get("status")
            if status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1

        return {
            "activated": activated,
            "executed": executed,
            "completed": completed,
            "failed": failed,
        }

    async def _run_mission_step(self, mission_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            mission = self._missions.get(mission_id)
            if not mission:
                return None

            next_step = self._next_runnable_step(mission)
            if next_step is None:
                self._refresh_mission_status(mission)
                mission.updated_at = utc_now_iso()
                self._save_locked()
                return mission.to_dict()

            mission.status = "running"
            if not mission.started_at:
                mission.started_at = utc_now_iso()
            next_step.status = "running"
            next_step.started_at = utc_now_iso()
            next_step.attempts += 1
            mission.updated_at = utc_now_iso()
            self._add_event(mission, "step_start", f"Etapa {next_step.id} iniciada")
            self._save_locked()

            mission_snapshot = mission.to_dict()
            step_snapshot = next_step.to_dict()

        result = await self._execute_step(mission_snapshot, step_snapshot)

        async with self._lock:
            mission = self._missions.get(mission_id)
            if not mission:
                return None
            step = next((s for s in mission.steps if s.id == step_snapshot["id"]), None)
            if not step:
                return mission.to_dict()

            if result.get("success"):
                step.status = "done"
                step.finished_at = utc_now_iso()
                step.output = str(result.get("output", "")).strip()
                step.tools_used = [str(t) for t in result.get("tools_used", [])]
                step.last_error = ""
                self._sync_checklist_for_step(mission, step)
                self._add_event(mission, "step_done", f"Etapa {step.id} concluida")
            else:
                step.last_error = str(result.get("error", "Falha na etapa"))
                if step.attempts < step.max_attempts:
                    step.status = "pending"
                    # Exponential backoff: set a real retry_after timestamp
                    backoff_delay = min(
                        _MISSION_RETRY_BASE_SEC * (2 ** (step.attempts - 1)),
                        _MISSION_RETRY_MAX_SEC,
                    )
                    retry_at = utc_now() + timedelta(seconds=backoff_delay)
                    step.retry_after = retry_at.isoformat()
                    self._add_event(
                        mission,
                        "step_retry",
                        f"Etapa {step.id} falhou, nova tentativa ({step.attempts}/{step.max_attempts}) apos {backoff_delay:.0f}s",
                    )
                else:
                    step.status = "failed"
                    step.finished_at = utc_now_iso()
                    self._add_event(mission, "step_failed", f"Etapa {step.id} falhou apos {step.max_attempts} tentativas")

            self._refresh_mission_status(mission)
            mission.updated_at = utc_now_iso()
            self._save_locked()
            mission_dict = mission.to_dict()

        if result.get("success"):
            self._emit(
                "Mike Mission",
                f"{mission_dict['title']}: etapa {step_snapshot['id']} concluida",
                "mission-step-done",
            )
        else:
            self._emit(
                "Mike Mission",
                f"{mission_dict['title']}: etapa {step_snapshot['id']} falhou",
                "mission-step-failed",
            )

        if mission_dict["status"] == "completed":
            self._emit("Mike Mission", f"Missao concluida: {mission_dict['title']}", "mission-done")
        elif mission_dict["status"] == "failed":
            self._emit("Mike Mission", f"Missao falhou: {mission_dict['title']}", "mission-failed")

        return mission_dict

    async def _execute_step(self, mission: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        profile_key = str(mission.get("profile_key") or "main")

        try:
            registry = self.registry_factory(profile_key)
            tool_manifest = await self.tool_manifest_fn(profile_key)
            system_prompt = self.system_prompt_fn(str(mission.get("goal", "")))
            context = self._build_context(mission)

            result = None
            agent_hint = str(step.get("agent_hint", "")).strip()
            if agent_hint:
                agent = registry.get(agent_hint)
                if agent:
                    result = await agent.run(
                        str(step.get("description", "")),
                        system_prompt,
                        tool_manifest,
                        context,
                    )

            if result is None:
                result = await registry.dispatch(
                    str(step.get("description", "")),
                    system_prompt,
                    tool_manifest,
                    context=context,
                    threshold=0.4,
                )

            # Fallback: if no agent matched, use LLM directly
            if (
                result
                and not getattr(result, "success", False)
                and getattr(result, "metadata", {}).get("reason") == "no_match"
            ):
                gen_fn = registry._fns.get("generate_fn")
                if gen_fn:
                    desc = str(step.get("description", ""))
                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"{context}\n\n{desc}" if context else desc},
                    ]
                    gen_result = await gen_fn(messages, None)
                    text = str(gen_result.get("assistant_text", ""))
                    if text.strip():
                        from mike_agent_sdk import AgentExecution
                        result = AgentExecution(
                            agent_name="llm_fallback", task=desc,
                            output=text, success=True,
                            tools_used=[], metadata={"fallback": True},
                        )

            success = bool(getattr(result, "success", False))
            output = str(getattr(result, "output", ""))
            tools_used = list(getattr(result, "tools_used", []))

            if not success:
                reason = str(getattr(result, "metadata", {}).get("reason", "execution_failed"))
                return {
                    "success": False,
                    "error": reason,
                    "elapsed_sec": round(time.time() - t0, 2),
                    "output": output,
                    "tools_used": tools_used,
                }

            return {
                "success": True,
                "output": output,
                "tools_used": tools_used,
                "elapsed_sec": round(time.time() - t0, 2),
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "elapsed_sec": round(time.time() - t0, 2),
                "output": "",
                "tools_used": [],
            }

    async def _plan_steps(self, goal: str, profile_key: str, max_steps: int) -> list[dict[str, Any]]:
        if not self.planner_fn:
            return []
        try:
            items = await self.planner_fn(goal, profile_key, max(1, min(max_steps, 12)))
            normalized = []
            for item in items:
                if isinstance(item, str):
                    desc = item.strip()
                    if desc:
                        normalized.append({"description": desc})
                    continue
                if not isinstance(item, dict):
                    continue
                desc = str(item.get("description", "")).strip()
                if not desc:
                    continue
                normalized.append({
                    "description": desc,
                    "agent_hint": str(item.get("agent_hint", "")).strip(),
                    "depends_on": [str(x) for x in item.get("depends_on", []) if str(x).strip()],
                    "checklist_label": str(item.get("checklist_label", "")).strip(),
                })
            return normalized
        except Exception as exc:
            log.warning("Mission planner failed: %s", exc)
            return []

    def _normalize_steps(self, specs: list[dict[str, Any]]) -> list[MissionStep]:
        steps: list[MissionStep] = []
        for idx, raw in enumerate(specs, start=1):
            if isinstance(raw, str):
                raw = {"description": raw}
            if not isinstance(raw, dict):
                continue
            desc = str(raw.get("description", "")).strip()
            if not desc:
                continue
            sid = str(raw.get("id") or idx)
            steps.append(
                MissionStep(
                    id=sid,
                    description=desc,
                    agent_hint=str(raw.get("agent_hint", "")).strip(),
                    depends_on=[str(x) for x in raw.get("depends_on", []) if str(x).strip()],
                    checklist_label=str(raw.get("checklist_label", "")).strip(),
                    max_attempts=max(1, int(raw.get("max_attempts", MISSION_MAX_STEP_ATTEMPTS))),
                )
            )

        valid_ids = {s.id for s in steps}
        for step in steps:
            step.depends_on = [dep for dep in step.depends_on if dep in valid_ids and dep != step.id]

        return steps

    def _next_runnable_step(self, mission: Mission) -> Optional[MissionStep]:
        done_ids = {s.id for s in mission.steps if s.status == "done"}
        now = utc_now()
        for step in mission.steps:
            # Normal path: pick 'pending' steps whose dependencies are done
            if step.status == "pending":
                if any(dep not in done_ids for dep in step.depends_on):
                    continue
                # Respect exponential backoff: don't retry before retry_after
                if step.retry_after:
                    retry_at = _parse_iso(step.retry_after)
                    if retry_at and retry_at > now:
                        continue  # not yet time to retry
                    step.retry_after = None  # clear once past the delay
                return step
            # Recovery path: steps stuck 'running' longer than timeout are retried
            if step.status == "running" and step.started_at:
                started = _parse_iso(step.started_at)
                if started and (now - started).total_seconds() > _ORPHAN_RUNNING_TIMEOUT_SEC:
                    log.warning(
                        "Mission %s step %s stuck 'running' for >%ds, resetting to pending",
                        mission.id, step.id, _ORPHAN_RUNNING_TIMEOUT_SEC,
                    )
                    step.status = "pending"
                    step.last_error = "Timeout: etapa presa em 'running'"
                    if any(dep not in done_ids for dep in step.depends_on):
                        continue
                    return step
        return None

    def _refresh_mission_status(self, mission: Mission) -> None:
        statuses = [s.status for s in mission.steps]
        if mission.status == "cancelled":
            return
        if statuses and all(s == "done" for s in statuses):
            mission.status = "completed"
            if not mission.completed_at:
                mission.completed_at = utc_now_iso()
            return
        if any(s == "failed" for s in statuses):
            if not any(s == "pending" for s in statuses):
                mission.status = "failed"
                if not mission.completed_at:
                    mission.completed_at = utc_now_iso()
                return
        if any(s == "running" for s in statuses):
            mission.status = "running"
            return
        if mission.trigger_at and _parse_iso(mission.trigger_at):
            trigger = _parse_iso(mission.trigger_at)
            if trigger and trigger > utc_now():
                mission.status = "scheduled"
                return
        mission.status = "queued"

    def _sync_checklist_for_step(self, mission: Mission, step: MissionStep) -> None:
        matched = False
        for item in mission.checklist:
            if str(item.get("step_id")) != step.id and str(item.get("id")) != step.id:
                continue
            item["done"] = step.status == "done"
            item["done_at"] = step.finished_at if step.status == "done" else None
            matched = True
        if not matched:
            mission.checklist.append({
                "id": step.id,
                "step_id": step.id,
                "label": step.checklist_label or step.description,
                "done": step.status == "done",
                "done_at": step.finished_at if step.status == "done" else None,
            })

    def _build_context(self, mission: dict[str, Any]) -> str:
        lines = [
            f"Mission: {mission.get('title', '')}",
            f"Goal: {mission.get('goal', '')}",
            "Progress:",
        ]
        for step in mission.get("steps", []):
            status = str(step.get("status", "pending"))
            desc = str(step.get("description", "")).strip()
            out = str(step.get("output", "")).strip()
            if out:
                out = out[:240]
                lines.append(f"- [{status}] {desc} -> {out}")
            else:
                lines.append(f"- [{status}] {desc}")
        text = "\n".join(lines)
        return text[:4000]

    def _add_event(self, mission: Mission, event_type: str, message: str) -> None:
        mission.events.append({
            "timestamp": utc_now_iso(),
            "type": event_type,
            "message": message,
        })
        if len(mission.events) > MISSION_MAX_EVENTS:
            mission.events = mission.events[-MISSION_MAX_EVENTS:]
