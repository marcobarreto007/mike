# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Autonomy Engine
====================

Motor de autonomia proativa do Mike.
Transforma o Mike de reativo (espera mensagem) em proativo (cria agenda,
rastreia emails, executa tarefas sozinho).

Arquitetura:
- AgenticLoop: perceive → reason → act → observe (single-threaded)
- Routine: tarefas recorrentes com schedule cron-like
- TaskBoard: lousa de tarefas — Marco escreve, Mike executa
- EmailTracker: rastreamento de respostas de emails enviados
- MikeAutonomy: scheduler principal (tick a cada 60s)

Persistência:
  mike/memory/autonomy/
  ├── routines.json
  ├── task_board.json
  ├── email_tracking.json
  └── autonomy_log.jsonl
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
from mike_task_board import TaskBoard, TaskItem
from mike_email_tracker import EmailTracker, TrackedEmail, EMAIL_DEFAULT_DEADLINE_HOURS

log = logging.getLogger("mike.autonomy")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AUTONOMY_ENABLED = env_bool("MIKE_AUTONOMY_ENABLED", True)
AUTONOMY_TICK_SEC = int(os.getenv("MIKE_AUTONOMY_TICK_SEC", "60"))
AUTONOMY_MAX_LOG_ENTRIES = int(os.getenv("MIKE_AUTONOMY_MAX_LOG", "500"))
TASK_MAX_ITERATIONS = int(os.getenv("MIKE_TASK_MAX_ITERATIONS", "8"))
EMAIL_CHECK_INTERVAL_MIN = int(os.getenv("MIKE_EMAIL_CHECK_INTERVAL_MIN", "30"))


from core.shared.time_utils import utc_now, utc_now_iso


def _local_now() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class RoutineSchedule:
    """Cron-like schedule definition."""
    hour: int = 7
    minute: int = 0
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    # 0=Monday, 6=Sunday
    interval_minutes: int = 0  # If > 0, ignores hour/minute and runs every N min

    def should_run(self, now: datetime, last_run: Optional[datetime]) -> bool:
        """Check if this routine should run at the given time."""
        if self.interval_minutes > 0:
            if last_run is None:
                return True
            elapsed = (now - last_run).total_seconds() / 60
            return elapsed >= self.interval_minutes

        # Cron-like: check hour, minute, weekday
        if now.weekday() not in self.weekdays:
            return False
        if now.hour != self.hour:
            return False
        if now.minute != self.minute:
            return False
        # Don't re-run within same minute
        if last_run and last_run.hour == now.hour and last_run.minute == now.minute:
            if last_run.date() == now.date():
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "hour": self.hour,
            "minute": self.minute,
            "weekdays": self.weekdays,
            "interval_minutes": self.interval_minutes,
        }

    @staticmethod
    def from_dict(data: dict) -> "RoutineSchedule":
        return RoutineSchedule(
            hour=int(data.get("hour", 7)),
            minute=int(data.get("minute", 0)),
            weekdays=list(data.get("weekdays", [0, 1, 2, 3, 4, 5, 6])),
            interval_minutes=int(data.get("interval_minutes", 0)),
        )

    def describe(self) -> str:
        if self.interval_minutes > 0:
            return f"a cada {self.interval_minutes} min"
        days = {0: "Seg", 1: "Ter", 2: "Qua", 3: "Qui", 4: "Sex", 5: "Sab", 6: "Dom"}
        day_str = ", ".join(days.get(d, "?") for d in sorted(self.weekdays))
        return f"{self.hour:02d}:{self.minute:02d} ({day_str})"


@dataclass
class Routine:
    """Tarefa recorrente agendada por cron."""
    id: str
    name: str
    description: str  # Prompt/goal para o agentic loop
    schedule: RoutineSchedule
    enabled: bool = True
    action_type: str = "agentic_loop"  # agentic_loop | simple_check | mission_spawn
    action_config: dict = field(default_factory=dict)
    max_duration_sec: int = 300
    notify: bool = True  # Notificar via Telegram
    approval_required: bool = True  # Pedir OK antes de ações destrutivas
    created_at: str = field(default_factory=utc_now_iso)
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    last_result: str = ""
    run_count: int = 0
    fail_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "schedule": self.schedule.to_dict(), "enabled": self.enabled,
            "action_type": self.action_type, "action_config": self.action_config,
            "max_duration_sec": self.max_duration_sec, "notify": self.notify,
            "approval_required": self.approval_required,
            "created_at": self.created_at, "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at, "last_result": self.last_result,
            "run_count": self.run_count, "fail_count": self.fail_count,
        }

    @staticmethod
    def from_dict(data: dict) -> "Routine":
        return Routine(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            schedule=RoutineSchedule.from_dict(data.get("schedule", {})),
            enabled=bool(data.get("enabled", True)),
            action_type=str(data.get("action_type", "agentic_loop")),
            action_config=dict(data.get("action_config", {})),
            max_duration_sec=int(data.get("max_duration_sec", 300)),
            notify=bool(data.get("notify", True)),
            approval_required=bool(data.get("approval_required", True)),
            created_at=str(data.get("created_at", utc_now_iso())),
            last_run_at=data.get("last_run_at"),
            next_run_at=data.get("next_run_at"),
            last_result=str(data.get("last_result", "")),
            run_count=int(data.get("run_count", 0)),
            fail_count=int(data.get("fail_count", 0)),
        )

@dataclass
class LoopResult:
    """Result of an AgenticLoop execution."""
    success: bool = False
    output: str = ""
    actions_taken: list[dict] = field(default_factory=list)
    iterations: int = 0
    elapsed_sec: float = 0.0
    tools_used: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agentic Loop — perceive → reason → act → observe
# ---------------------------------------------------------------------------

class AgenticLoop:
    """Single-threaded agentic loop — perceive → reason → act → observe.

    Receives a goal, available tools, and iterates:
    1. Perceive: gather context (agenda, emails, calendar, system) + past reflections
    2. Reason: LLM decides next action (informed by past successes/failures)
    3. Act: execute tool via MCP/AgentSDK
    4. Observe: capture result, update state
    5. Reflect: store reflection for future improvement (Reflexion pattern)
    6. Repeat until done or max_iterations
    """

    def __init__(
        self,
        generate_fn: Callable,
        execute_tool_fn: Callable,
        extract_tool_call_fn: Callable,
        strip_tool_call_fn: Callable,
        render_tool_result_fn: Callable,
        compact_tool_payload_fn: Callable,
        system_prompt: str = "",
        reflection_store: Any = None,  # EpisodicReflectionStore
        tool_analyzer: Any = None,     # ToolFailureAnalyzer
    ):
        self._generate = generate_fn
        self._execute_tool = execute_tool_fn
        self._extract_tool = extract_tool_call_fn
        self._strip_tool = strip_tool_call_fn
        self._render_result = render_tool_result_fn
        self._compact_payload = compact_tool_payload_fn
        self._system_prompt = system_prompt
        self._reflection_store = reflection_store
        self._tool_analyzer = tool_analyzer

    async def run(
        self,
        goal: str,
        context: str = "",
        tool_block: str = "",
        max_iterations: int = TASK_MAX_ITERATIONS,
    ) -> LoopResult:
        """Execute the agentic loop for a goal."""
        t0 = time.time()
        result = LoopResult()

        full_system = self._system_prompt + ("\n\n" + tool_block if tool_block else "")
        messages = [{"role": "system", "content": full_system}]

        # Perceive — search past reflections for similar goals
        reflection_context = ""
        if self._reflection_store:
            try:
                similar = self._reflection_store.search_similar(goal, limit=3)
                if similar:
                    lines = ["\n[LICOES DO PASSADO — Reflexoes similares]"]
                    for i, hit in enumerate(similar, 1):
                        r = hit.reflection
                        emoji = "OK" if r.success else "FALHOU"
                        lines.append(f"{i}. [{emoji}] {r.goal[:120]}")
                        if r.reflection_text:
                            lines.append(f"   Reflexao: {r.reflection_text[:200]}")
                        if r.lessons_learned:
                            lines.append(f"   Licoes: {', '.join(r.lessons_learned[:3])}")
                        if r.what_worked:
                            lines.append(f"   Funcionou: {', '.join(r.what_worked[:3])}")
                    reflection_context = "\n".join(lines)
            except Exception as exc:
                log.debug("Reflection search failed: %s", exc)

        # Perceive — inject current context (with past reflections)
        perceive_block = (
            "[CONTEXTO AUTONOMO]\n"
            f"Voce esta executando uma tarefa AUTONOMAMENTE (sem Marco online).\n"
            f"Hora atual: {_local_now().strftime('%d/%m/%Y %H:%M')}\n"
        )
        if context:
            perceive_block += f"\n{context}\n"
        if reflection_context:
            perceive_block += f"\n{reflection_context}\n"
        perceive_block += (
            f"\nTAREFA: {goal}\n\n"
            "Execute esta tarefa usando as tools disponiveis. "
            "Seja objetivo. Quando concluir, diga 'TAREFA CONCLUIDA' e resuma o resultado."
        )
        messages.append({"role": "user", "content": perceive_block})

        for iteration in range(max_iterations):
            result.iterations = iteration + 1

            # Reason — ask LLM what to do
            try:
                response = await self._generate(messages, None)
                assistant_text = response.get("assistant_text", "")
            except Exception as exc:
                log.warning("AgenticLoop generate failed at iteration %d: %s", iteration, exc)
                result.output = f"Erro no LLM: {exc}"
                result.success = False
                break

            # Check if task complete (LLM says it's done)
            if not assistant_text.strip():
                result.output = "(sem resposta do LLM)"
                result.success = False
                break

            # Extract tool call
            tool_call = self._extract_tool(assistant_text) if iteration < max_iterations - 1 else None

            if not tool_call:
                # LLM responded with text only — task is done
                result.output = assistant_text
                result.success = True
                break

            # Act — execute the tool
            tool_name = tool_call["name"]
            tool_args = tool_call["arguments"]
            try:
                tool_result = await self._execute_tool(tool_name, tool_args)
                result.tools_used.append(tool_name)
                result.actions_taken.append({
                    "iteration": iteration + 1,
                    "tool": tool_name,
                    "success": True,
                })
            except Exception as exc:
                log.warning("AgenticLoop tool %s failed: %s", tool_name, exc)
                result.actions_taken.append({
                    "iteration": iteration + 1,
                    "tool": tool_name,
                    "success": False,
                    "error": str(exc),
                })
                # Record failure for pattern analysis
                if self._tool_analyzer:
                    try:
                        self._tool_analyzer.record_failure(
                            tool_name=tool_name,
                            error_message=str(exc),
                            context=goal[:300],
                            arguments=tool_args,
                            source="agentic_loop",
                        )
                    except Exception:
                        log.warning("Failed to record tool failure in analyzer for tool=%s", tool_name)
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append({
                    "role": "system",
                    "content": f"ERRO na tool {tool_name}: {exc}",
                })
                continue

            # Observe — feed result back into context
            compact_result = dict(tool_result)
            compact_result["text"] = self._compact_payload(
                tool_name, tool_result.get("text", ""),
            )
            clean_text = self._strip_tool(assistant_text) or "(chamei tool)"
            messages.append({"role": "assistant", "content": clean_text})
            messages.append({
                "role": "system",
                "content": self._render_result(tool_name, tool_args, compact_result),
            })

        result.elapsed_sec = round(time.time() - t0, 2)
        if not result.output and result.iterations >= max_iterations:
            result.output = "(limite de iteracoes atingido)"
            result.success = False

        # Reflect — store episodic reflection for future improvement (Reflexion pattern)
        if self._reflection_store:
            try:
                from mike_reflection import EpisodicReflection
                reflection = EpisodicReflection(
                    id="",
                    goal=goal[:500],
                    context=context[:500],
                    output=result.output[:1000],
                    success=result.success,
                    reflection_text=(
                        f"Task {'succeeded' if result.success else 'failed'} "
                        f"after {result.iterations} iterations using {', '.join(result.tools_used) if result.tools_used else 'no tools'}. "
                        f"Output: {result.output[:300]}"
                    ),
                    tool_calls=result.tools_used,
                    iterations=result.iterations,
                    elapsed_sec=result.elapsed_sec,
                )
                self._reflection_store.save(reflection)
            except Exception as exc:
                log.debug("Reflection save failed: %s", exc)

        return result


# ---------------------------------------------------------------------------
# MikeAutonomy — The Proactive Scheduler
# ---------------------------------------------------------------------------

class MikeAutonomy:
    """Motor de autonomia proativa do Mike.

    Responsabilidades:
    - Avalia rotinas pendentes (cron matching)
    - Gerencia lousa de tarefas (TaskBoard)
    - Rastreia respostas de email
    - Executa ações via AgenticLoop ou MissionEngine
    - Persiste tudo em disco atomicamente
    """

    DEFAULT_ROUTINES = [
        {
            "id": "morning_agenda",
            "name": "📋 Agenda Matinal",
            "description": (
                "Verifique emails nao lidos no Gmail, eventos do calendario para hoje, "
                "e emails rastreados sem resposta. Gere um resumo organizado do dia para o Marco. "
                "Envie via Telegram."
            ),
            "schedule": {"hour": 7, "minute": 0, "weekdays": [0, 1, 2, 3, 4]},
            "action_type": "agentic_loop",
            "notify": True,
            "approval_required": False,
            "max_duration_sec": 120,
        },
        {
            "id": "inbox_check",
            "name": "📧 Check Inbox",
            "description": (
                "Verifique a caixa de entrada do Gmail por emails novos nao lidos. "
                "Se houver emails urgentes ou de contatos importantes, notifique via Telegram."
            ),
            "schedule": {"interval_minutes": 30},
            "action_type": "simple_check",
            "action_config": {"check_type": "inbox"},
            "notify": True,
            "approval_required": False,
            "max_duration_sec": 60,
        },
        {
            "id": "email_response_tracker",
            "name": "📨 Verificar Respostas",
            "description": (
                "Verifique se os emails rastreados receberam resposta. "
                "Para cada email sem resposta alem do prazo, notifique o Marco."
            ),
            "schedule": {"hour": 12, "minute": 0, "weekdays": [0, 1, 2, 3, 4]},
            "action_type": "simple_check",
            "action_config": {"check_type": "email_responses"},
            "notify": True,
            "approval_required": False,
            "max_duration_sec": 60,
        },
        {
            "id": "afternoon_followup",
            "name": "🔄 Follow-up Tarde",
            "description": (
                "Verifique emails sem resposta que passaram do prazo. "
                "Notifique o Marco via Telegram com opcao de reenviar."
            ),
            "schedule": {"hour": 15, "minute": 0, "weekdays": [0, 1, 2, 3, 4]},
            "action_type": "simple_check",
            "action_config": {"check_type": "email_responses"},
            "notify": True,
            "approval_required": False,
            "max_duration_sec": 60,
        },
        {
            "id": "auto_reply_family",
            "name": "💌 Auto-Responder Familiar",
            "description": (
                "Verifique emails nao lidos. Para cada email de um familiar (Ana Paula, Raphael, "
                "Alice, Matheus, Marilene — emails em config/identity.json), leia o conteudo completo, "
                "gere uma resposta personalizada e carinhosa como o Mike (yorkshire digital da familia), "
                "e envie a resposta. Nao responda emails que ja foram respondidos. "
                "Nao responda spam, newsletters ou remetentes desconhecidos. "
                "Use o tom adequado para cada pessoa: carinhoso e protetor para Marilene (76 anos, mae do Marco), "
                "respeitoso e inteligente para Raphael (filho estudante de Direito), "
                "doce e apropriado para Alice (filha menor de idade), "
                "calmo e paciente para Matheus (irmao da Ana Paula, autista nivel 2), "
                "respeitoso e objetivo para Ana Paula (esposa do Marco)."
            ),
            "schedule": {"interval_minutes": 15},
            "action_type": "agentic_loop",
            "notify": True,
            "approval_required": False,
            "max_duration_sec": 120,
        },
        {
            "id": "daily_summary",
            "name": "📊 Resumo do Dia",
            "description": (
                "Gere um resumo do que aconteceu hoje: tarefas concluidas, emails respondidos, "
                "pendencias para amanha. Envie via Telegram."
            ),
            "schedule": {"hour": 17, "minute": 30, "weekdays": [0, 1, 2, 3, 4]},
            "action_type": "agentic_loop",
            "notify": True,
            "approval_required": False,
            "max_duration_sec": 120,
        },
    ]

    def __init__(
        self,
        *,
        generate_fn: Optional[Callable] = None,
        execute_tool_fn: Optional[Callable] = None,
        extract_tool_call_fn: Optional[Callable] = None,
        strip_tool_call_fn: Optional[Callable] = None,
        render_tool_result_fn: Optional[Callable] = None,
        compact_tool_payload_fn: Optional[Callable] = None,
        tool_block_fn: Optional[Callable] = None,
        system_prompt: str = "",
        notify_fn: Optional[Callable] = None,
        mission_create_fn: Optional[Callable] = None,
        email_search_fn: Optional[Callable] = None,
        reflection_store: Any = None,
        curriculum: Any = None,
        tool_analyzer: Any = None,
        event_bus: Any = None,
        auto_reply_fn: Optional[Callable] = None,
        store_dir: Optional[Path] = None,
    ):
        self._generate_fn = generate_fn
        self._execute_tool_fn = execute_tool_fn
        self._extract_tool_fn = extract_tool_call_fn
        self._strip_tool_fn = strip_tool_call_fn
        self._render_result_fn = render_tool_result_fn
        self._compact_payload_fn = compact_tool_payload_fn
        self._tool_block_fn = tool_block_fn
        self._system_prompt = system_prompt
        self._notify_fn = notify_fn
        self._mission_create_fn = mission_create_fn
        self._email_search_fn = email_search_fn
        self._reflection_store = reflection_store
        self._curriculum = curriculum
        self._tool_analyzer = tool_analyzer
        self._event_bus = event_bus
        self._auto_reply_fn = auto_reply_fn
        self._idle_check_count = 0

        self._store_dir = store_dir or Path("mike/memory/autonomy")
        self._store_dir.mkdir(parents=True, exist_ok=True)

        self._routines: dict[str, Routine] = {}
        self._lock = asyncio.Lock()
        self.task_board = TaskBoard(
            store_dir=self._store_dir,
            lock=self._lock,
            log_fn=self._log_entry,
            notify_fn=self._notify,
            event_bus=self._event_bus,
        )
        self.email_tracker = EmailTracker(
            store_dir=self._store_dir,
            lock=self._lock,
            log_fn=self._log_entry,
            email_search_fn=self._email_search_fn,
            notify_fn=self._notify,
        )
        self._loaded = False
        self._running = False
        self._last_tick: Optional[datetime] = None

        # Subscribe to event bus events if available
        if self._event_bus:
            self._event_bus.subscribe("email.family", self._on_family_email)
            self._event_bus.subscribe("task.created", self._on_task_created)
            log.info("Autonomy subscribed to event bus: email.family, task.created")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _routines_path(self) -> Path:
        return self._store_dir / "routines.json"

    def _log_path(self) -> Path:
        return self._store_dir / "autonomy_log.jsonl"

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        # Load sub-components first (they handle their own locking)
        if not self.task_board._loaded:
            await self.task_board._ensure_loaded()
        if not self.email_tracker._loaded:
            await self.email_tracker._ensure_loaded()

        async with self._lock:
            if self._loaded:
                return

            # Load routines
            if self._routines_path().exists():
                try:
                    data = json.loads(self._routines_path().read_text(encoding="utf-8"))
                    for item in data.get("routines", []):
                        r = Routine.from_dict(item)
                        if r.id:
                            self._routines[r.id] = r
                except Exception as exc:
                    log.warning("Routines load failed: %s", exc)

            # Ensure default routines exist
            for routine_spec in self.DEFAULT_ROUTINES:
                rid = routine_spec["id"]
                if rid not in self._routines:
                    self._routines[rid] = Routine.from_dict(routine_spec)
                    log.info("Default routine created: %s", rid)

            self._loaded = True
            log.info("Autonomy loaded: %d routines, %d tasks, %d tracked emails",
                     len(self._routines), len(self.task_board._tasks), len(self.email_tracker._tracked_emails))

    def _save_routines(self) -> None:
        payload = {
            "updated_at": utc_now_iso(),
            "routines": [r.to_dict() for r in self._routines.values()],
        }
        self._atomic_write(self._routines_path(), payload)

    def _atomic_write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _log_entry(self, event_type: str, message: str, data: Optional[dict] = None) -> None:
        entry = {
            "timestamp": utc_now_iso(),
            "type": event_type,
            "message": message,
        }
        if data:
            entry["data"] = data
        try:
            with open(self._log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            log.exception("Failed to write autonomy log entry")

    def _notify(self, title: str, body: str, tag: str = "autonomy") -> None:
        if self._notify_fn:
            try:
                self._notify_fn(title, body, tag)
            except Exception as exc:
                log.debug("Autonomy notify failed: %s", exc)

    # ------------------------------------------------------------------
    # TaskBoard API — Lousa de Tarefas
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
        return await self.task_board.create_task(
            title=title, description=description, priority=priority,
            created_by=created_by, notify_on_complete=notify_on_complete,
        )

    async def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """List all tasks, optionally filtered by status."""
        await self._ensure_loaded()
        return await self.task_board.list_tasks(status=status, limit=limit)

    async def get_task(self, task_id: str) -> Optional[dict]:
        await self._ensure_loaded()
        return await self.task_board.get_task(task_id)

    async def complete_task(self, task_id: str, result: str = "", actor: str = "mike") -> Optional[dict]:
        """Mark a task as complete."""
        await self._ensure_loaded()
        return await self.task_board.complete_task(task_id, result=result, actor=actor)

    async def fail_task(self, task_id: str, error: str = "") -> Optional[dict]:
        """Mark a task as failed."""
        await self._ensure_loaded()
        return await self.task_board.fail_task(task_id, error=error)

    async def cancel_task(self, task_id: str) -> Optional[dict]:
        """Cancel a task."""
        await self._ensure_loaded()
        return await self.task_board.cancel_task(task_id)

    async def delete_task(self, task_id: str) -> bool:
        """Hard delete completed/cancelled task."""
        await self._ensure_loaded()
        return await self.task_board.delete_task(task_id)

    # ------------------------------------------------------------------
    # Email Tracking API
    # ------------------------------------------------------------------

    async def track_email(
        self,
        gmail_message_id: str = "",
        sent_to: str = "",
        subject: str = "",
        deadline_hours: int = EMAIL_DEFAULT_DEADLINE_HOURS,
        auto_followup: bool = False,
    ) -> dict:
        """Track a sent email for response monitoring."""
        await self._ensure_loaded()
        return await self.email_tracker.track_email(
            gmail_message_id=gmail_message_id, sent_to=sent_to,
            subject=subject, deadline_hours=deadline_hours,
            auto_followup=auto_followup,
        )

    async def list_tracked_emails(self, status: Optional[str] = None) -> list[dict]:
        await self._ensure_loaded()
        return await self.email_tracker.list_tracked_emails(status=status)

    async def dismiss_tracked_email(self, tracking_id: str) -> Optional[dict]:
        await self._ensure_loaded()
        return await self.email_tracker.dismiss_tracked_email(tracking_id)

    async def check_email_responses(self) -> list[dict]:
        """Check Gmail for replies to tracked emails."""
        await self._ensure_loaded()
        return await self.email_tracker.check_email_responses()

    # ------------------------------------------------------------------
    # Routines API
    # ------------------------------------------------------------------

    async def list_routines(self) -> list[dict]:
        await self._ensure_loaded()
        return [r.to_dict() for r in self._routines.values()]

    async def toggle_routine(self, routine_id: str, enabled: Optional[bool] = None) -> Optional[dict]:
        await self._ensure_loaded()
        async with self._lock:
            r = self._routines.get(routine_id)
            if not r:
                return None
            r.enabled = enabled if enabled is not None else (not r.enabled)
            self._save_routines()
        return r.to_dict()

    async def create_routine(self, data: dict) -> dict:
        """Create a custom routine."""
        await self._ensure_loaded()
        routine = Routine.from_dict({
            "id": data.get("id") or f"custom_{uuid.uuid4().hex[:6]}",
            **data,
        })
        async with self._lock:
            self._routines[routine.id] = routine
            self._save_routines()
        self._log_entry("routine_created", f"Rotina criada: {routine.name}")
        return routine.to_dict()

    async def run_routine_now(self, routine_id: str) -> Optional[dict]:
        """Force-execute a routine immediately."""
        await self._ensure_loaded()
        routine = self._routines.get(routine_id)
        if not routine:
            return None
        return await self._execute_routine(routine)

    # ------------------------------------------------------------------
    # Main Tick Loop
    # ------------------------------------------------------------------

    async def tick(self) -> dict:
        """Called every ~60s by the background loop. Evaluates what needs to run."""
        await self._ensure_loaded()

        now = _local_now()
        executed_routines = 0
        executed_tasks = 0
        email_alerts = 0

        # 1. Check routines
        for routine in list(self._routines.values()):
            if not routine.enabled:
                continue
            last_run = None
            if routine.last_run_at:
                try:
                    last_run = datetime.fromisoformat(routine.last_run_at)
                    if last_run.tzinfo:
                        last_run = last_run.astimezone().replace(tzinfo=None)
                except Exception:
                    last_run = None

            if routine.schedule.should_run(now, last_run):
                try:
                    await self._execute_routine(routine)
                    executed_routines += 1
                except Exception as exc:
                    log.warning("Routine %s execution failed: %s", routine.id, exc)
                    routine.fail_count += 1
                    routine.last_result = f"Erro: {exc}"

        # 2. Execute pending tasks (one at a time, highest priority first)
        pending = [
            t for t in self.task_board._tasks.values()
            if t.status == "pending" and t.attempts < t.max_attempts
        ]
        pending.sort(key=lambda t: (t.priority, t.created_at))
        for task in pending[:1]:  # Only execute one task per tick to be safe
            try:
                await self._execute_task(task)
                executed_tasks += 1
            except Exception as exc:
                log.warning("Task %s execution failed: %s", task.id, exc)
                task.error = str(exc)
                task.status = "failed" if task.attempts >= task.max_attempts else "pending"
                async with self._lock:
                    self.task_board._save_tasks()

        # 3. Curriculum idle check (every 10 ticks ~= 10 min)
        self._idle_check_count += 1
        curriculum_action = None
        if self._curriculum and self._idle_check_count % 10 == 0:
            try:
                curriculum_action = self._curriculum.idle_action()
                if curriculum_action and curriculum_action.get("action") == "practice_goal":
                    # Execute the curriculum goal as a mini-task
                    task_id = await self.create_task(
                        title=f"[Curriculo] {curriculum_action['description'][:100]}",
                        description=curriculum_action.get("description", ""),
                        priority=4,
                        created_by="mike",
                        notify_on_complete=False,
                    )
                    log.info("[AUTONOMY] Curriculum goal spawned: %s → task %s",
                             curriculum_action.get("goal_id", "?"), task_id)
            except Exception as exc:
                log.debug("Curriculum idle check failed: %s", exc)

        self._last_tick = now
        return {
            "tick_at": now.isoformat(),
            "routines_executed": executed_routines,
            "tasks_executed": executed_tasks,
            "email_alerts": email_alerts,
            "curriculum_action": curriculum_action,
        }

    async def _execute_routine(self, routine: Routine) -> dict:
        """Execute a single routine."""
        t0 = time.time()
        log.info("[AUTONOMY] Executing routine: %s (%s)", routine.id, routine.name)
        self._log_entry("routine_start", f"Executando: {routine.name}")

        result_text = ""
        success = False

        try:
            if routine.id == "auto_reply_family" and self._auto_reply_fn:
                # This workflow already has a deterministic, family-scoped
                # implementation. Do not spend the single Qwen inference slot
                # asking an agent to rediscover the same email procedure.
                auto_result = await asyncio.to_thread(self._auto_reply_fn)
                replied = int(auto_result.get("replied", 0))
                skipped = int(auto_result.get("skipped", 0))
                errors = int(auto_result.get("errors", 0))
                result_text = (
                    f"Auto-reply: {replied} respondido(s), "
                    f"{skipped} ignorado(s), {errors} erro(s)"
                )
                if auto_result.get("note"):
                    result_text += f" — {auto_result['note']}"
                success = errors == 0
                if routine.notify and replied:
                    self._notify(
                        "Mike — Auto-Reply",
                        f"Respondi {replied} email(s) de familiares",
                        "auto-reply",
                    )

            elif routine.action_type == "simple_check":
                check_type = routine.action_config.get("check_type", "")
                if check_type == "email_responses":
                    alerts = await self.check_email_responses()
                    result_text = f"{len(alerts)} alerta(s) de email"
                    for alert in alerts:
                        if routine.notify:
                            self._notify(
                                "Mike — Email",
                                alert.get("message", ""),
                                "email-tracking",
                            )
                    success = True
                elif check_type == "inbox":
                    if self._email_search_fn:
                        try:
                            emails = await self._email_search_fn(query="is:unread", max_results=5)
                            unread = len(emails) if isinstance(emails, list) else 0
                            result_text = f"Inbox: {unread} nao lido(s)"
                            success = True
                        except Exception as exc:
                            result_text = f"Inbox check falhou: {exc}"
                            success = False
                    else:
                        result_text = "Inbox check: email search function not configured"
                        success = True
                        log.warning("Inbox check skipped — email_search_fn not configured")
                else:
                    result_text = f"check_type desconhecido: {check_type}"

            elif routine.action_type == "agentic_loop":
                if self._generate_fn and self._execute_tool_fn:
                    tool_block = ""
                    if self._tool_block_fn:
                        try:
                            tool_block = await self._tool_block_fn()
                        except Exception:
                            tool_block = ""
                    loop = AgenticLoop(
                        generate_fn=self._generate_fn,
                        execute_tool_fn=self._execute_tool_fn,
                        extract_tool_call_fn=self._extract_tool_fn,
                        strip_tool_call_fn=self._strip_tool_fn,
                        render_tool_result_fn=self._render_result_fn,
                        compact_tool_payload_fn=self._compact_payload_fn,
                        system_prompt=self._system_prompt,
                        reflection_store=self._reflection_store,
                        tool_analyzer=self._tool_analyzer,
                    )
                    loop_result = await asyncio.wait_for(
                        loop.run(routine.description, tool_block=tool_block),
                        timeout=routine.max_duration_sec,
                    )
                    result_text = loop_result.output[:500]
                    success = loop_result.success
                    if routine.notify and result_text:
                        self._notify(
                            f"Mike — {routine.name}",
                            result_text[:300],
                            f"routine-{routine.id}",
                        )
                else:
                    result_text = "Generate/execute functions not configured"

            elif routine.action_type == "mission_spawn":
                if self._mission_create_fn:
                    mission = await self._mission_create_fn(
                        title=routine.name,
                        goal=routine.description,
                    )
                    result_text = f"Missao criada: {mission.get('id', '?')}"
                    success = True
                else:
                    result_text = "Mission engine not available"

        except asyncio.TimeoutError:
            result_text = f"Timeout ({routine.max_duration_sec}s)"
            success = False
        except Exception as exc:
            result_text = f"Erro: {exc}"
            success = False

        elapsed = round(time.time() - t0, 2)
        routine.last_run_at = _local_now().isoformat()
        routine.last_result = result_text[:500]
        routine.run_count += 1
        if not success:
            routine.fail_count += 1

        async with self._lock:
            self._save_routines()

        self._log_entry(
            "routine_done" if success else "routine_failed",
            f"{routine.name}: {result_text[:200]}",
            {"elapsed_sec": elapsed, "success": success},
        )
        log.info("[AUTONOMY] Routine %s %s (%.1fs): %s",
                 routine.id, "OK" if success else "FAILED", elapsed, result_text[:100])

        return {"routine_id": routine.id, "success": success, "result": result_text, "elapsed": elapsed}

    async def _execute_task(self, task: TaskItem) -> dict:
        """Execute a single task from the board via AgenticLoop."""
        log.info("[AUTONOMY] Executing task: %s — %s", task.id, task.title)
        self._log_entry("task_start", f"[{task.id}] {task.title}")

        async with self._lock:
            task.status = "running"
            task.started_at = utc_now_iso()
            task.attempts += 1
            self.task_board._save_tasks()

        # Broadcast task status change for dashboard
        self._notify(
            "🔄 Executando Tarefa",
            task.title,
            "task-running",
        )

        try:
            if self._generate_fn and self._execute_tool_fn:
                tool_block = ""
                if self._tool_block_fn:
                    try:
                        tool_block = await self._tool_block_fn()
                    except Exception:
                        tool_block = ""

                goal = task.title
                if task.description:
                    goal += f"\n\nDetalhes: {task.description}"

                loop = AgenticLoop(
                    generate_fn=self._generate_fn,
                    execute_tool_fn=self._execute_tool_fn,
                    extract_tool_call_fn=self._extract_tool_fn,
                    strip_tool_call_fn=self._strip_tool_fn,
                    render_tool_result_fn=self._render_result_fn,
                    compact_tool_payload_fn=self._compact_payload_fn,
                    system_prompt=self._system_prompt,
                    reflection_store=self._reflection_store,
                )
                result = await asyncio.wait_for(
                    loop.run(goal, tool_block=tool_block),
                    timeout=300,
                )

                if result.success:
                    await self.complete_task(task.id, result=result.output)
                else:
                    if task.attempts >= task.max_attempts:
                        await self.fail_task(task.id, error=result.output or "Falha apos max tentativas")
                    else:
                        async with self._lock:
                            task.status = "pending"
                            task.error = result.output
                            self.task_board._save_tasks()

                return {"task_id": task.id, "success": result.success, "output": result.output[:300]}
            else:
                # No LLM available — can't execute, try mission engine
                if self._mission_create_fn:
                    mission = await self._mission_create_fn(
                        title=task.title,
                        goal=task.description or task.title,
                    )
                    async with self._lock:
                        task.mission_id = mission.get("id")
                        task.status = "running"
                        self.task_board._save_tasks()
                    return {"task_id": task.id, "success": True, "mission_id": task.mission_id}
                else:
                    await self.fail_task(task.id, error="LLM e Mission Engine indisponíveis")
                    return {"task_id": task.id, "success": False, "error": "No execution engine"}

        except asyncio.TimeoutError:
            await self.fail_task(task.id, error="Timeout (300s)")
            return {"task_id": task.id, "success": False, "error": "timeout"}
        except Exception as exc:
            error_msg = str(exc)
            if task.attempts >= task.max_attempts:
                await self.fail_task(task.id, error=error_msg)
            else:
                async with self._lock:
                    task.status = "pending"
                    task.error = error_msg
                    self.task_board._save_tasks()
            return {"task_id": task.id, "success": False, "error": error_msg}

    # ------------------------------------------------------------------
    # Event Handlers (Event Bus subscribers)
    # ------------------------------------------------------------------

    async def _on_family_email(self, payload: dict) -> None:
        """Handle email.family event — trigger auto-reply immediately."""
        log.info("[AUTONOMY] email.family event received — triggering auto_reply_family")
        self._log_entry("event", "email.family — disparando auto_reply_family", payload)

        if self._auto_reply_fn:
            try:
                result = await asyncio.to_thread(self._auto_reply_fn)
                log.info("[AUTONOMY] auto_reply_family result: %s", result)
                self._log_entry("auto_reply", str(result)[:200])
                if self._notify_fn and result.get("replied", 0) > 0:
                    self._notify(
                        "Mike — Auto-Reply",
                        f"Respondi {result['replied']} email(s) de familiares",
                        "auto-reply",
                    )
            except Exception as exc:
                log.warning("[AUTONOMY] auto_reply_family failed: %s", exc)
        else:
            # Fallback: run the auto_reply_family routine immediately
            routine = self._routines.get("auto_reply_family")
            if routine and routine.enabled:
                try:
                    await self._execute_routine(routine)
                except Exception as exc:
                    log.warning("[AUTONOMY] auto_reply_family routine failed: %s", exc)

    async def _on_task_created(self, payload: dict) -> None:
        """Handle task.created event — wake up for immediate processing."""
        log.info("[AUTONOMY] task.created event received — waking up for immediate processing")
        self._log_entry("event", "task.created — processamento imediato", payload)

        # Execute the task immediately (if it's the one just created)
        task_id = payload.get("task_id") or payload.get("id", "")
        if task_id and task_id in self.task_board._tasks:
            task = self.task_board._tasks[task_id]
            if task.status == "pending":
                try:
                    await self._execute_task(task)
                except Exception as exc:
                    log.warning("[AUTONOMY] Immediate task execution failed: %s", exc)

    # ------------------------------------------------------------------
    # Status / Log
    # ------------------------------------------------------------------

    def status(self) -> dict:
        routines = list(self._routines.values())
        tasks = list(self.task_board._tasks.values())
        tracked = list(self.email_tracker._tracked_emails.values())
        return {
            "enabled": AUTONOMY_ENABLED,
            "running": self._running,
            "last_tick": self._last_tick.isoformat() if self._last_tick else None,
            "routines": {
                "total": len(routines),
                "enabled": sum(1 for r in routines if r.enabled),
            },
            "tasks": {
                "total": len(tasks),
                "pending": sum(1 for t in tasks if t.status == "pending"),
                "running": sum(1 for t in tasks if t.status == "running"),
                "done": sum(1 for t in tasks if t.status == "done"),
                "failed": sum(1 for t in tasks if t.status == "failed"),
            },
            "email_tracking": {
                "total": len(tracked),
                "waiting": sum(1 for e in tracked if e.status == "waiting"),
                "overdue": sum(1 for e in tracked if e.status == "overdue"),
                "replied": sum(1 for e in tracked if e.status == "replied"),
            },
        }

    async def get_log(self, limit: int = 50) -> list[dict]:
        """Get recent autonomy log entries."""
        if not self._log_path().exists():
            return []
        entries = []
        try:
            lines = self._log_path().read_text(encoding="utf-8").strip().split("\n")
            for line in lines[-limit:]:
                if line.strip():
                    entries.append(json.loads(line))
        except Exception:
            pass
        entries.reverse()
        return entries

    # ------------------------------------------------------------------
    # Router-facing bridge methods (usados por routers/autonomy.py)
    # ------------------------------------------------------------------

    @property
    def _data(self) -> dict:
        return {
            "tasks": {tid: t.to_dict() for tid, t in self.task_board._tasks.items()},
            "routines": [r.to_dict() for r in self._routines.values()],
            "email_tracking": {tid: e.to_dict() for tid, e in self.email_tracker._tracked_emails.items()},
        }

    async def add_task(self, description: str, priority: str = "normal") -> str:
        """Convenience wrapper para o router — compatível com interface antiga."""
        priority_map = {"urgent": 1, "high": 2, "normal": 3, "low": 4, "baixa": 5}
        prio = priority_map.get(str(priority).strip().lower(), 3)
        task = await self.create_task(title=description, description="", priority=prio)
        return task["id"]

    async def update_email_tracking(self, body: dict) -> None:
        """Atualiza tracking de email a partir de payload do router."""
        await self._ensure_loaded()
        tracking_id = str(body.get("id") or body.get("tracking_id") or "").strip()
        if not tracking_id:
            return
        async with self._lock:
            tracked = self.email_tracker._tracked_emails.get(tracking_id)
            if tracked is None:
                return
            for field in ("status", "notes", "auto_followup", "expected_reply_by"):
                val = body.get(field)
                if val is not None:
                    if field == "auto_followup":
                        setattr(tracked, field, bool(val))
                    else:
                        setattr(tracked, field, str(val) if isinstance(val, str) else val)
            self.email_tracker._save_email_tracking()

    def record_heartbeat(self, body: dict) -> None:
        """Registra heartbeat externo (ex: de app mobile ou monitor remoto)."""
        self._log_entry("heartbeat", "Heartbeat recebido", body)

    def generate_briefing(self) -> dict:
        """Gera briefing rápido baseado no estado atual da autonomia.
        Seguro para chamar antes de _ensure_loaded() — retorna vazio."""
        if not self._loaded:
            return {"timestamp": utc_now_iso(), "status": self.status(), "pending_tasks": [], "overdue_emails": [], "running": False}
        st = self.status()
        pending_tasks = [t.to_dict() for t in self.task_board._tasks.values() if t.status == "pending"]
        overdue_emails = [e.to_dict() for e in self.email_tracker._tracked_emails.values() if e.is_overdue()]
        return {
            "timestamp": utc_now_iso(),
            "status": st,
            "pending_tasks": pending_tasks[:5],
            "overdue_emails": overdue_emails,
            "running": self._running,
        }

    async def send_briefing(self) -> None:
        """Envia briefing via Telegram."""
        briefing = self.generate_briefing()
        pending = briefing.get("pending_tasks", [])
        overdue = briefing.get("overdue_emails", [])
        msg = "📋 *Briefing Mike*\n\n"
        if pending:
            msg += f"⚡ {len(pending)} tarefa(s) pendente(s):\n"
            for t in pending[:3]:
                msg += f"  - {t['title']}\n"
        if overdue:
            msg += f"📧 {len(overdue)} email(s) sem resposta:\n"
            for e in overdue[:3]:
                msg += f"  - {e['subject']} ({e['sent_to']})\n"
        if not pending and not overdue:
            msg += "✅ Nada pendente. Tudo em dia!\n"
        self._notify("Mike Briefing", msg, "briefing")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the autonomy engine background loop."""
        if not AUTONOMY_ENABLED:
            log.info("Autonomy engine disabled")
            return
        if self._running:
            return
        await self._ensure_loaded()
        self._running = True
        log.info("Autonomy engine started (tick=%ds)", AUTONOMY_TICK_SEC)

        while self._running:
            try:
                await self.tick()
            except Exception as exc:
                log.warning("Autonomy tick failed: %s", exc)
            try:
                await asyncio.sleep(AUTONOMY_TICK_SEC)
            except asyncio.CancelledError:
                break

    def stop(self) -> None:
        self._running = False
