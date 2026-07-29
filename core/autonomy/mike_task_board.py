# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
TaskBoard — Lousa de Tarefas.
Marco escreve, Mike executa.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.shared.time_utils import utc_now_iso

_log = logging.getLogger(__name__)


@dataclass
class TaskItem:
    """Item da lousa de tarefas — Marco escreve, Mike executa."""
    id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending | running | done | failed | cancelled
    priority: int = 3  # 1=urgente, 5=baixa
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: str = ""
    error: str = ""
    attempts: int = 0
    max_attempts: int = 3
    created_by: str = "marco"  # marco | mike (auto-generated)
    notify_on_complete: bool = True
    mission_id: Optional[str] = None  # Link to MissionEngine if spawned

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "status": self.status, "priority": self.priority,
            "created_at": self.created_at, "started_at": self.started_at,
            "completed_at": self.completed_at, "result": self.result,
            "error": self.error, "attempts": self.attempts,
            "max_attempts": self.max_attempts, "created_by": self.created_by,
            "notify_on_complete": self.notify_on_complete,
            "mission_id": self.mission_id,
        }

    @staticmethod
    def from_dict(data: dict) -> "TaskItem":
        return TaskItem(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=str(data.get("status", "pending")),
            priority=int(data.get("priority", 3)),
            created_at=str(data.get("created_at", utc_now_iso())),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            result=str(data.get("result", "")),
            error=str(data.get("error", "")),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            created_by=str(data.get("created_by", "marco")),
            notify_on_complete=bool(data.get("notify_on_complete", True)),
            mission_id=data.get("mission_id"),
        )


class TaskBoard:
    """Lousa de tarefas — Marco escreve, Mike executa."""

    def __init__(
        self,
        *,
        store_dir: Path,
        lock: asyncio.Lock,
        log_fn,
        notify_fn,
        event_bus=None,
    ):
        self._store_dir = Path(store_dir)
        self._lock = lock
        self._log_fn = log_fn
        self._notify_fn = notify_fn
        self._event_bus = event_bus
        self._tasks: dict[str, TaskItem] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _tasks_path(self) -> Path:
        return self._store_dir / "task_board.json"

    def _save_tasks(self) -> None:
        payload = {
            "updated_at": utc_now_iso(),
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        self._atomic_write(self._tasks_path(), payload)

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _notify(self, title: str, body: str, tag: str = "autonomy") -> None:
        if self._notify_fn:
            try:
                self._notify_fn(title, body, tag)
            except Exception as exc:
                _log.debug("TaskBoard notify failed: %s", exc)

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            # Load task board
            if self._tasks_path().exists():
                try:
                    data = json.loads(self._tasks_path().read_text(encoding="utf-8"))
                    for item in data.get("tasks", []):
                        t = TaskItem.from_dict(item)
                        if t.id:
                            self._tasks[t.id] = t
                except Exception as exc:
                    _log.warning("Task board load failed: %s", exc)
            self._loaded = True

    # ------------------------------------------------------------------
    # TaskBoard API
    # ------------------------------------------------------------------

    async def create_task(
        self,
        title: str,
        description: str = "",
        priority: int = 3,
        created_by: str = "marco",
        notify_on_complete: bool = True,
    ) -> dict:
        """Create a new task on the board."""
        await self._ensure_loaded()

        task = TaskItem(
            id=f"task_{uuid.uuid4().hex[:8]}",
            title=title.strip(),
            description=description.strip(),
            priority=max(1, min(5, priority)),
            created_by=created_by,
            notify_on_complete=notify_on_complete,
        )

        async with self._lock:
            self._tasks[task.id] = task
            self._save_tasks()

        self._log_fn("task_created", f"[{task.id}] {task.title}", {"created_by": created_by})
        _log.info("Task created: %s — %s", task.id, task.title)

        # Publish task.created event
        if self._event_bus:
            try:
                await self._event_bus.publish(
                    self._event_bus.EVENT_TASK_CREATED
                    if hasattr(self._event_bus, 'EVENT_TASK_CREATED')
                    else "task.created",
                    {"task_id": task.id, **task.to_dict()},
                )
            except Exception as exc:
                _log.debug("Event publish failed: %s", exc)

        return task.to_dict()

    async def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """List all tasks, optionally filtered by status."""
        await self._ensure_loaded()
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        tasks.sort(key=lambda t: (
            {"pending": 0, "running": 1, "done": 2, "failed": 3, "cancelled": 4}.get(t.status, 5),
            t.priority,
            t.created_at,
        ))
        return [t.to_dict() for t in tasks[:limit]]

    async def get_task(self, task_id: str) -> Optional[dict]:
        await self._ensure_loaded()
        t = self._tasks.get(task_id)
        return t.to_dict() if t else None

    async def complete_task(self, task_id: str, result: str = "", actor: str = "mike") -> Optional[dict]:
        """Mark a task as complete."""
        await self._ensure_loaded()
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.status = "done"
            task.completed_at = utc_now_iso()
            task.result = result
            self._save_tasks()

        self._log_fn("task_completed", f"[{task.id}] {task.title}", {"actor": actor})

        if task.notify_on_complete:
            self._notify(
                "✅ Tarefa Concluída",
                f"{task.title}\n\n{result[:200] if result else 'Sem detalhes'}",
                "task-done",
            )

        return task.to_dict()

    async def fail_task(self, task_id: str, error: str = "") -> Optional[dict]:
        """Mark a task as failed."""
        await self._ensure_loaded()
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.status = "failed"
            task.completed_at = utc_now_iso()
            task.error = error
            self._save_tasks()

        self._log_fn("task_failed", f"[{task.id}] {task.title}", {"error": error})
        self._notify(
            "❌ Tarefa Falhou",
            f"{task.title}\n\nErro: {error[:200] if error else '?'}",
            "task-failed",
        )
        return task.to_dict()

    async def cancel_task(self, task_id: str) -> Optional[dict]:
        """Cancel a task."""
        await self._ensure_loaded()
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            if task.status in ("done", "cancelled"):
                return task.to_dict()
            task.status = "cancelled"
            task.completed_at = utc_now_iso()
            self._save_tasks()
        return task.to_dict()

    async def delete_task(self, task_id: str) -> bool:
        """Hard delete completed/cancelled task."""
        await self._ensure_loaded()
        async with self._lock:
            if task_id in self._tasks:
                t = self._tasks[task_id]
                if t.status in ("done", "cancelled", "failed"):
                    del self._tasks[task_id]
                    self._save_tasks()
                    return True
        return False
