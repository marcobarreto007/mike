# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Task Mesh — Multi-step task orchestration
================================================

Inspired by OpenAI Codex's agent loop and delegate pattern.
When Mike receives a complex task that needs more than MCP_TOOL_MAX_STEPS
tool calls, the TaskMesh automatically:

1. Detects complex tasks via heuristics
2. Asks the LLM to decompose into numbered steps
3. Executes each step with its own fresh tool budget
4. Compacts context between steps (keeps plan + summaries only)
5. Streams progress updates to the user
6. Consolidates and returns the final result

Architecture (adapted from Codex):
- Codex delegate pattern: each sub-task gets its own execution context
- Codex task types: structured steps with status tracking
- Codex context compaction: keeps summaries between turns
- Auto-continuation: no user intervention needed between steps
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, List, Optional, Tuple

log = logging.getLogger("mike.mesh")

# Safe import: mike_config is a leaf module, no circular risk
try:
    from mike_config import STREAM_KEEPALIVE_SECONDS
except ImportError:
    STREAM_KEEPALIVE_SECONDS = 15.0  # fallback: 15-second keepalive

# Lazy import to avoid circular — mike_swarm uses no mike_task_mesh types
_swarm_mod = None
def _get_swarm():
    global _swarm_mod
    if _swarm_mod is None:
        try:
            import mike_swarm as sw
            _swarm_mod = sw
        except ImportError:
            _swarm_mod = False  # mark as unavailable
    return _swarm_mod if _swarm_mod else None

# Retry / resilience constants (inspired by Temporal durable execution)
_STEP_MAX_RETRIES = 3
_STEP_RETRY_BASE_SEC = 5.0       # exponential backoff base
_STEP_RETRY_MAX_SEC = 60.0
_GENERATE_MAX_RETRIES = 4
_GENERATE_RETRY_BASE_SEC = 3.0
_GENERATE_RETRY_MAX_SEC = 45.0
_CHECKPOINT_DIR = Path("mike/memory/mesh_checkpoints")


# ======================================================================
# Data structures
# ======================================================================

class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskStep:
    id: int
    description: str
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    tools_used: list = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class TaskPlan:
    goal: str
    steps: list[TaskStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def current_step_idx(self) -> int:
        for i, s in enumerate(self.steps):
            if s.status in (StepStatus.PENDING, StepStatus.RUNNING):
                return i
        return len(self.steps)

    @property
    def is_complete(self) -> bool:
        return all(
            s.status in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.FAILED)
            for s in self.steps
        )

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.DONE)

    def progress_summary(self) -> str:
        lines = [f"PLANO: {self.goal}"]
        for s in self.steps:
            icon = {
                "done": "✓", "failed": "✗", "running": "→",
                "pending": "○", "skipped": "–",
            }[s.status.value]
            lines.append(f"  {icon} Passo {s.id}: {s.description}")
            if s.result and s.status == StepStatus.DONE:
                # Compact summary — max 200 chars per completed step
                r = s.result[:200] + ("..." if len(s.result) > 200 else "")
                lines.append(f"    Resultado: {r}")
        return "\n".join(lines)

    # -- Checkpoint persistence (Temporal-inspired durable state) --

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "created_at": self.created_at,
            "steps": [
                {
                    "id": s.id,
                    "description": s.description,
                    "status": s.status.value,
                    "result": s.result[:500] if s.result else "",
                    "tools_used": s.tools_used,
                    "elapsed": s.elapsed,
                }
                for s in self.steps
            ],
        }

    def save_checkpoint(self, tag: str = "") -> None:
        """Persist current plan state to disk so progress survives crashes."""
        try:
            _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            label = f"{ts}_{tag}" if tag else ts
            path = _CHECKPOINT_DIR / f"mesh_{label}.json"
            path.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.debug("Mesh checkpoint save failed: %s", exc)


# ======================================================================
# Complexity detection
# ======================================================================

_COMPLEX_PATTERNS = [
    # "todos/todas/cada/all" + objects
    r"\b(?:todos?|todas?|cada|all|every)\b.*\b(?:emails?|arquivos?|pastas?|files?|folders?)\b",
    # Organize/clean/move + everything
    r"\b(?:organiz|limpar?|mover?|copiar?|clean|move|copy|renomear?|rename)\b.+\b(?:todos?|todas?|tudo|everything)\b",
    # Send to multiple
    r"\b(?:envi[ae]r?|send|mandar)\b.+\b(?:para\s+\d|to\s+\d|vários|múltiplos|multiple)\b",
    # N things/steps/tasks
    r"\b\d+\s+(?:coisa|tarefa|passo|etapa|item|thing|step|email|arquivo)s?\b",
    # Sequential markers (first...then...)
    r"\b(?:primeiro|segundo|terceiro|depois|em seguida|then|first|second|next)\b.*\b(?:depois|em seguida|then|next|finalmente|finally)\b",
    # Explicit step-by-step
    r"\b(?:passo\s*a\s*passo|step\s*by\s*step)\b",
    # List then perform another state-changing action. Merely summarizing the
    # result of one read tool does not justify a multi-agent TaskMesh.
    r"\b(?:lista[re]?|liste|list|veja|ver|check|olh[ae])\b.+\b(?:e\s+(?:ent[aã]o\s+)?(?:mova|copie|apague|envie|delete|remove))\b",
    # Create multiple files/directories
    r"\b(?:cri[ae]r?|create|fazer?|make)\b.+\b(?:vários|múltipl[oa]s|multiple|\d+\s+\w+)\b",
    # Batch operations
    r"\b(?:batch|lote|em massa|bulk)\b",
    # Create code/game/app/script — needs planning + write_file
    r"\b(?:cri[ae]r?|crie|criar|faz[ae]r?|fazer|fa[cç]a|build|make|develop[ae]r?|desenvolv[ae]r?|implement[ae]r?)\b.{0,60}\b(?:jogo|game|app|aplicativo|site|p[aá]gina|script|programa|sistema|dashboard|html|c[oó]digo|bot|ferramenta|tool|componente)\b",
    # Imitate/clone something (Donkey Kong, etc.)
    r"\b(?:imitando|imitante|clone|clona[re]?|parecido\s+com|similar\s+a|baseado\s+em|inspirado\s+em|tipo\s+o|como\s+o)\b",
]

_COMPLEX_KEYWORDS = [
    "e também", "e depois", "em seguida", "além disso",
    "and then", "and also", "additionally", "furthermore",
    "primeiro", "segundo", "terceiro", "quarto",
    "step 1", "step 2", "passo 1", "passo 2",
    "pra cada", "para cada", "for each",
    "um por um", "one by one",
    # Code creation signals
    "em html", "em python", "em javascript", "em js", "em css",
    "write_file", "salva o arquivo", "salva em", "cria o arquivo",
]


def looks_complex(text: str) -> bool:
    """Heuristic: does this task likely need multi-step decomposition?"""
    lower = text.lower()

    # Pattern match
    for pat in _COMPLEX_PATTERNS:
        if re.search(pat, lower):
            return True

    # Keyword count
    kw_hits = sum(1 for kw in _COMPLEX_KEYWORDS if kw in lower)
    if kw_hits >= 2:
        return True

    # Length + keyword: very long requests with any sequential keyword
    if len(text) > 300 and kw_hits >= 1:
        return True

    return False


# ======================================================================
# Plan parsing from LLM output
# ======================================================================

_PLAN_STEP_RE = re.compile(
    r"^\s*(?:PASSO|STEP|ETAPA)?\s*(\d+)[.:)\-]\s*(.+)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_plan_from_llm(text: str) -> list[tuple[int, str]]:
    """Extract numbered steps from LLM planning output."""
    steps = []
    for m in _PLAN_STEP_RE.finditer(text):
        num = int(m.group(1))
        desc = m.group(2).strip()
        desc = re.sub(r"\s*\[.*?\]\s*$", "", desc)
        if desc:
            steps.append((num, desc))

    # Fallback: simple numbered list
    if not steps:
        for m in re.finditer(r"^\s*(\d+)[.:)\-]\s*(.+)", text, re.MULTILINE):
            num = int(m.group(1))
            desc = m.group(2).strip()
            if desc and len(desc) > 5:
                steps.append((num, desc))

    return steps


def fallback_plan_from_goal(goal: str) -> list[tuple[int, str]]:
    """Deterministically split explicitly sequential goals when planning fails."""
    text = re.sub(
        r"^\s*(?:execute|faça|faca|realize)\s+(?:duas|três|tres|\d+)\s+"
        r"(?:ações|acoes|etapas|passos)\s*:\s*",
        "",
        goal,
        flags=re.IGNORECASE,
    )
    parts = re.split(
        r"(?:[.;]\s*)?\b(?:depois|em seguida|por fim|finalmente)\b[:,]?\s*",
        text,
        flags=re.IGNORECASE,
    )
    cleaned: list[str] = []
    for part in parts:
        part = re.sub(
            r"^\s*(?:primeiro|primeiramente)\b[:,]?\s*",
            "",
            part,
            flags=re.IGNORECASE,
        )
        part = re.split(
            r"(?<=[.!?])\s+(?=(?:responda|resuma|apresente)\b)",
            part,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" \t\r\n.;")
        if part and not re.match(r"^(?:responda|resuma|apresente)\b", part, re.IGNORECASE):
            cleaned.append(part)
    if len(cleaned) < 2:
        return []
    return [(index + 1, part) for index, part in enumerate(cleaned)]


# ======================================================================
# Prompt templates
# ======================================================================

PLAN_PROMPT = """Tarefa complexa recebida: {goal}

Decomponha em passos simples e sequenciais. Cada passo = UMA ação concreta que posso executar com UMA tool.

Formato (uma linha por passo):
1. [ação concreta com detalhes — caminhos, nomes, etc.]
2. [próxima ação]
...

Regras: máximo {max_steps} passos. Seja específico. Sem passos de "verificar",
"confirmar", "resumir" ou "consolidar": o TaskMesh consolida os resultados
automaticamente depois das ações. NÃO execute nem chame tools durante o
planejamento; devolva somente a lista numerada."""

STEP_PROMPT = """{progress}

EXECUTAR AGORA — Passo {step_id}: {step_description}
Use a tool necessária e responda com o resultado."""

CONSOLIDATION_PROMPT = """{progress}

Todos os passos foram executados. Resuma BREVEMENTE e naturalmente o que foi
feito. Não chame nenhuma tool e não emita <tool_call>: use exclusivamente os
resultados acima. Não liste passos — conte como se estivesse conversando."""


# ======================================================================
# TaskMesh orchestrator
# ======================================================================

class TaskMesh:
    """Orchestrates multi-step task execution for Mike.

    Inspired by OpenAI Codex's patterns:
    - codex_delegate.rs: sub-agent spawning with independent turn budgets
    - tasks/regular.rs: structured step execution
    - compact.rs: context compaction between turns
    """

    def __init__(
        self,
        generate_fn: Callable,
        execute_tool_fn: Callable,
        extract_tool_call_fn: Callable,
        strip_tool_call_fn: Callable,
        render_tool_result_fn: Callable,
        compact_tool_payload_fn: Callable,
        tool_manifest: Optional[List[dict]] = None,
        max_steps_per_subtask: int = 3,
        max_plan_steps: int = 8,
        sub_agent_spawner: Optional[Any] = None,
    ):
        self._generate = generate_fn  # async (messages, req) -> dict with "assistant_text"
        self._execute_tool = execute_tool_fn  # async (name, args) -> dict
        self._extract_tool = extract_tool_call_fn  # (text) -> dict|None
        self._strip_tool = strip_tool_call_fn  # (text) -> str
        self._render_result = render_tool_result_fn  # (name, args, result) -> str
        self._compact_payload = compact_tool_payload_fn  # (name, text) -> str
        self._tool_manifest = tool_manifest or []
        self._max_inner_steps = max_steps_per_subtask
        self._max_plan_steps = max_plan_steps
        self._spawner = sub_agent_spawner  # Optional SubAgentSpawner

    @staticmethod
    def _normalized_words(text: str) -> set[str]:
        normalized = unicodedata.normalize("NFKD", text.lower())
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        words = set(re.findall(r"[a-z0-9_-]{4,}", normalized))
        return words - {
            "acao", "acoes", "agora", "apropriada", "completa", "execute",
            "ferramenta", "ferramentas", "resultado", "resultados", "sistema",
            "tool", "tools", "usar", "usando",
        }

    def _tools_for_step(self, description: str) -> list[dict]:
        """Select a compact, relevant tool subset for one planned action."""
        words = self._normalized_words(description)
        expansions = {
            "banco": {"database", "sqlite"},
            "dados": {"database", "sqlite"},
            "diretorio": {"directory", "filesystem", "workspace"},
            "diretorios": {"directory", "filesystem", "workspace"},
            "pasta": {"directory", "folder", "filesystem"},
            "permitidos": {"allowed"},
            "tabela": {"table", "sqlite"},
            "tabelas": {"table", "sqlite"},
        }
        expanded = set(words)
        for word in words:
            expanded.update(expansions.get(word, set()))

        scored: list[tuple[int, dict]] = []
        for tool in self._tool_manifest:
            name = str(tool.get("name") or "").lower()
            server = str(tool.get("server_name") or "").lower()
            description_text = str(tool.get("description") or "").lower()
            searchable = unicodedata.normalize(
                "NFKD", f"{name} {server} {description_text}"
            )
            searchable = "".join(
                ch for ch in searchable if not unicodedata.combining(ch)
            )
            score = 0
            for word in expanded:
                if word in name:
                    score += 5
                elif word == server:
                    score += 4
                elif word in searchable:
                    score += 1
            if score:
                scored.append((score, tool))

        if not scored:
            return self._tool_manifest
        scored.sort(key=lambda item: (-item[0], str(item[1].get("name") or "")))
        best = scored[0][0]
        cutoff = max(2, int(best * 0.4))
        return [tool for score, tool in scored if score >= cutoff][:12]

    @staticmethod
    def _focused_tool_block(tools: list[dict]) -> str:
        lines = []
        for tool in tools:
            schema = tool.get("input_schema") or tool.get("inputSchema") or {}
            args = ", ".join((schema.get("properties") or {}).keys())
            lines.append(f"- {tool.get('name')}({args}): {tool.get('description', '')}")
        return (
            "TOOLS AUTORIZADAS NESTE PASSO:\n"
            + "\n".join(lines)
            + "\n\nChame no máximo UMA dessas tools no formato:\n"
            '<tool_call>{"name":"NOME","arguments":{"arg":"valor"}}</tool_call>\n'
            "Depois de receber o resultado, responda sem chamar outra tool."
        )

    async def create_plan(
        self,
        goal: str,
        system_prompt: str,
        tool_block: str = "",
    ) -> TaskPlan:
        """Use LLM to decompose a complex task into numbered steps."""
        # Do not pass executable tool instructions to the planning turn.
        full_system = system_prompt
        messages = [{"role": "system", "content": full_system}]
        messages.append({
            "role": "user",
            "content": PLAN_PROMPT.format(
                goal=goal, max_steps=self._max_plan_steps,
            ),
        })

        response = await self._generate(messages, None)
        text = response.get("assistant_text", "")

        raw_steps = parse_plan_from_llm(text)
        if not raw_steps:
            raw_steps = fallback_plan_from_goal(goal)
            if not raw_steps:
                # Last resort: treat the whole task as one mega-step.
                return TaskPlan(
                    goal=goal,
                    steps=[TaskStep(id=1, description=goal)],
                )

        steps = [
            TaskStep(id=i + 1, description=desc)
            for i, (_, desc) in enumerate(raw_steps[:self._max_plan_steps])
        ]
        return TaskPlan(goal=goal, steps=steps)

    async def execute_step(
        self,
        plan: TaskPlan,
        step: TaskStep,
        system_prompt: str,
        tool_block: str = "",
    ) -> TaskStep:
        """Execute a single plan step with its own FRESH context and tool budget.

        Each step gets:
        - The system prompt
        - Tool instructions
        - A compact progress summary of completed steps
        - The current step instruction
        - Its own inner tool loop (up to max_inner_steps)

        If a SubAgentSpawner is available, tries to delegate to a
        specialized sub-agent first (focused tools + persona).
        Falls back to generic execution if no agent matches.
        """
        step.status = StepStatus.RUNNING
        t0 = time.time()
        step_tools = self._tools_for_step(step.description)
        step_tool_block = self._focused_tool_block(step_tools)

        # === SUB-AGENT DELEGATION ===
        if self._spawner and step_tools:
            try:
                agent_result = await self._spawner.try_agent_for_step(
                    step.description,
                    system_prompt,
                    step_tools,
                    context=plan.progress_summary(),
                    threshold=0.75,
                )
                if agent_result is not None:
                    if agent_result.success:
                        step.result = agent_result.output
                        step.tools_used = agent_result.tools_used
                        step.status = StepStatus.DONE
                        step.elapsed = time.time() - t0
                        log.info(
                            "TaskMesh step %d delegated to sub-agent '%s' (%.1fs, tools=%s)",
                            step.id, agent_result.agent_name, step.elapsed, step.tools_used,
                        )
                        return step
                    log.warning(
                        "TaskMesh sub-agent '%s' could not complete step %d "
                        "(reason=%s); falling back to generic execution",
                        agent_result.agent_name,
                        step.id,
                        agent_result.metadata.get("reason", "agent_failed"),
                    )
            except Exception as exc:
                log.warning(
                    "TaskMesh sub-agent delegation failed for step %d, "
                    "falling back to generic: %s", step.id, exc,
                )

        # === GENERIC EXECUTION (original) ===

        # Build fresh context for this step — compact, no old baggage
        full_system = system_prompt + "\n\n" + step_tool_block
        messages = [{"role": "system", "content": full_system}]
        messages.append({
            "role": "user",
            "content": STEP_PROMPT.format(
                progress=plan.progress_summary(),
                step_description=step.description,
                step_id=step.id,
            ),
        })

        # Inner tool-execution loop (like _generate_response_with_tools)
        last_tool_text = ""
        for inner in range(self._max_inner_steps + 1):
            # --- LLM call with retry + exponential backoff ---
            response = None
            for gen_attempt in range(_GENERATE_MAX_RETRIES):
                try:
                    response = await self._generate(messages, None)
                    break
                except Exception as gen_exc:
                    delay = min(
                        _GENERATE_RETRY_BASE_SEC * (2 ** gen_attempt),
                        _GENERATE_RETRY_MAX_SEC,
                    )
                    log.warning(
                        "TaskMesh step %d generate attempt %d/%d failed: %s — retry in %.0fs",
                        step.id, gen_attempt + 1, _GENERATE_MAX_RETRIES, gen_exc, delay,
                    )
                    if gen_attempt + 1 >= _GENERATE_MAX_RETRIES:
                        step.result = f"LLM indisponivel apos {_GENERATE_MAX_RETRIES} tentativas: {gen_exc}"
                        step.status = StepStatus.FAILED
                        step.elapsed = time.time() - t0
                        return step
                    await asyncio.sleep(delay)

            assistant_text = response.get("assistant_text", "") if response else ""

            tool_call = (
                self._extract_tool(assistant_text)
                if step_tools and inner < self._max_inner_steps
                else None
            )

            if not tool_call:
                step.result = assistant_text
                step.status = StepStatus.DONE
                step.elapsed = time.time() - t0
                return step

            if step.tools_used:
                step.result = last_tool_text or self._strip_tool(assistant_text)
                step.status = StepStatus.DONE
                step.elapsed = time.time() - t0
                return step

            allowed_names = {str(tool.get("name") or "") for tool in step_tools}
            allowed_aliases = {name.rsplit(".", 1)[-1] for name in allowed_names}
            if (
                tool_call["name"] not in allowed_names
                and tool_call["name"] not in allowed_aliases
            ):
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Tool '{tool_call['name']}' não autorizada neste passo. "
                        f"Use somente uma destas: {', '.join(sorted(allowed_names))}"
                    ),
                })
                continue

            # Execute the tool with retry + exponential backoff
            tool_result = None
            tool_succeeded = False
            for tool_attempt in range(_STEP_MAX_RETRIES):
                try:
                    tool_result = await self._execute_tool(
                        tool_call["name"], tool_call["arguments"]
                    )
                    step.tools_used.append(tool_call["name"])
                    last_tool_text = str(tool_result.get("text", ""))
                    tool_succeeded = True
                    break
                except Exception as e:
                    delay = min(
                        _STEP_RETRY_BASE_SEC * (2 ** tool_attempt),
                        _STEP_RETRY_MAX_SEC,
                    )
                    log.warning(
                        "TaskMesh step %d tool %s attempt %d/%d failed: %s — retry in %.0fs",
                        step.id, tool_call["name"], tool_attempt + 1,
                        _STEP_MAX_RETRIES, e, delay,
                    )
                    if tool_attempt + 1 >= _STEP_MAX_RETRIES:
                        step.result = f"Tool {tool_call['name']} falhou apos {_STEP_MAX_RETRIES} tentativas: {e}"
                        step.status = StepStatus.FAILED
                        step.elapsed = time.time() - t0
                        return step
                    await asyncio.sleep(delay)

            if not tool_succeeded:
                step.result = f"Tool {tool_call['name']} falhou"
                step.status = StepStatus.FAILED
                step.elapsed = time.time() - t0
                return step

            # Compact tool result and append for next inner iteration
            compact_result = dict(tool_result)
            compact_result["text"] = self._compact_payload(
                tool_call["name"],
                tool_result.get("text", ""),
            )
            clean_assistant = self._strip_tool(assistant_text)
            messages.append({
                "role": "assistant",
                "content": clean_assistant or "(chamei tool)",
            })
            messages.append({
                "role": "user",
                "content": self._render_result(
                    tool_call["name"], tool_call["arguments"], compact_result
                ),
            })

        # Hit inner step limit — still mark as done with whatever we have
        step.result = assistant_text if assistant_text else "(passo concluido)"
        step.status = StepStatus.DONE
        step.elapsed = time.time() - t0
        return step

    async def consolidate(
        self,
        plan: TaskPlan,
        system_prompt: str,
    ) -> str:
        """Generate a natural summary of the completed plan."""
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": CONSOLIDATION_PROMPT.format(
                    progress=plan.progress_summary(),
                ),
            },
        ]
        response = await self._generate(messages, None)
        text = response.get("assistant_text", plan.progress_summary())
        if self._extract_tool and self._extract_tool(text):
            clean = self._strip_tool(text) if self._strip_tool else ""
            return clean.strip() or plan.progress_summary()
        return text

    # ------------------------------------------------------------------
    # Non-streaming entry point
    # ------------------------------------------------------------------
    async def run(
        self,
        goal: str,
        system_prompt: str,
        tool_block: str = "",
    ) -> Tuple[TaskPlan, str]:
        """Decompose → execute all steps → consolidate. Returns (plan, summary).

        Resilience model (inspired by Temporal durable execution):
        - Each step retried up to _STEP_MAX_RETRIES with exponential backoff
        - Plan state checkpointed after each step for crash recovery
        - Failed steps don't block subsequent independent steps
        """
        log.info("TaskMesh: planning for: %s", goal[:120])
        plan = await self.create_plan(goal, system_prompt, tool_block)
        log.info("TaskMesh: %d steps planned", len(plan.steps))
        plan.save_checkpoint("plan")

        for step in plan.steps:
            log.info("TaskMesh: step %d/%d — %s",
                      step.id, len(plan.steps), step.description[:80])

            # Step-level retry with exponential backoff
            for step_retry in range(_STEP_MAX_RETRIES):
                await self.execute_step(plan, step, system_prompt, tool_block)
                if step.status == StepStatus.DONE:
                    break
                if step.status == StepStatus.FAILED and step_retry + 1 < _STEP_MAX_RETRIES:
                    delay = min(
                        _STEP_RETRY_BASE_SEC * (2 ** step_retry),
                        _STEP_RETRY_MAX_SEC,
                    )
                    log.warning(
                        "TaskMesh: step %d failed, retrying (%d/%d) in %.0fs",
                        step.id, step_retry + 1, _STEP_MAX_RETRIES, delay,
                    )
                    step.status = StepStatus.PENDING
                    await asyncio.sleep(delay)

            log.info("TaskMesh: step %d → %s (%.1fs, tools=%s)",
                      step.id, step.status.value, step.elapsed, step.tools_used)
            plan.save_checkpoint(f"step{step.id}")

        summary = await self.consolidate(plan, system_prompt)
        log.info("TaskMesh: done — %d/%d steps succeeded", plan.done_count, len(plan.steps))
        if self._spawner:
            log.info("TaskMesh sub-agents: %s", self._spawner.summary())
        plan.save_checkpoint("done")
        return plan, summary

    # ------------------------------------------------------------------
    # Streaming entry point — yields (event_type, data) pairs
    # ------------------------------------------------------------------
    async def run_streaming(
        self,
        goal: str,
        system_prompt: str,
        tool_block: str = "",
    ) -> AsyncGenerator[Tuple[str, Any], None]:
        """Streaming variant that yields progress events."""
        log.info("TaskMesh streaming: planning for: %s", goal[:120])
        yield ("plan_start", {"goal": goal})

        plan = await self.create_plan(goal, system_prompt, tool_block)
        log.info("TaskMesh streaming: %d steps planned", len(plan.steps))
        yield ("plan_created", {
            "steps": [{"id": s.id, "description": s.description} for s in plan.steps],
        })

        for step in plan.steps:
            yield ("step_start", {
                "step_id": step.id,
                "description": step.description,
                "total": len(plan.steps),
            })

            log.info("TaskMesh streaming: step %d/%d — %s",
                      step.id, len(plan.steps), step.description[:80])

            # Step-level retry with exponential backoff (streaming)
            # Execute step in a Task so we can yield keepalives while waiting —
            # prevents SSE client timeout during long tool/LLM calls (40-60s).
            for step_retry in range(_STEP_MAX_RETRIES):
                _step_task = asyncio.ensure_future(
                    self.execute_step(plan, step, system_prompt, tool_block)
                )
                try:
                    while not _step_task.done():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(_step_task),
                                timeout=STREAM_KEEPALIVE_SECONDS,
                            )
                            break
                        except asyncio.TimeoutError:
                            yield ("keepalive", None)
                    # If task completed with exception, re-raise
                    if not _step_task.cancelled() and _step_task.exception():
                        raise _step_task.exception()
                except Exception as step_exc:
                    log.warning(
                        "TaskMesh streaming step %d exception: %s", step.id, step_exc,
                    )
                    step.status = StepStatus.FAILED
                    step.result = str(step_exc)
                if step.status == StepStatus.DONE:
                    break
                if step.status == StepStatus.FAILED and step_retry + 1 < _STEP_MAX_RETRIES:
                    delay = min(
                        _STEP_RETRY_BASE_SEC * (2 ** step_retry),
                        _STEP_RETRY_MAX_SEC,
                    )
                    log.warning(
                        "TaskMesh streaming: step %d retry %d/%d in %.0fs",
                        step.id, step_retry + 1, _STEP_MAX_RETRIES, delay,
                    )
                    step.status = StepStatus.PENDING
                    # Yield keepalives during retry backoff too
                    _retry_deadline = asyncio.get_event_loop().time() + delay
                    while asyncio.get_event_loop().time() < _retry_deadline:
                        await asyncio.sleep(min(STREAM_KEEPALIVE_SECONDS, delay))
                        yield ("keepalive", None)

            log.info("TaskMesh streaming: step %d → %s (%.1fs)",
                      step.id, step.status.value, step.elapsed)
            plan.save_checkpoint(f"step{step.id}")

            yield ("step_done", {
                "step_id": step.id,
                "status": step.status.value,
                "result_preview": step.result[:400] if step.result else "",
                "tools_used": step.tools_used,
            })

        summary = await self.consolidate(plan, system_prompt)
        log.info("TaskMesh streaming: done — %d/%d",
                  plan.done_count, len(plan.steps))
        if self._spawner:
            log.info("TaskMesh streaming sub-agents: %s", self._spawner.summary())
        yield ("complete", {"summary": summary})

    # ------------------------------------------------------------------
    # Parallel execution — groups independent steps
    # ------------------------------------------------------------------
    async def _execute_step_group(
        self,
        plan: TaskPlan,
        step_indices: list[int],
        system_prompt: str,
        tool_block: str = "",
    ) -> None:
        """Execute a group of independent steps in parallel.

        Steps in the group have no dependencies on each other,
        so they can safely run concurrently with asyncio.gather.
        """
        if len(step_indices) <= 1:
            # Single step — run normally with retries
            if step_indices:
                step = plan.steps[step_indices[0]]
                await self._execute_step_with_retry(plan, step, system_prompt, tool_block)
            return

        log.info("TaskMesh parallel: running %d steps concurrently: %s",
                 len(step_indices), step_indices)

        async def _run_one(idx: int):
            step = plan.steps[idx]
            await self._execute_step_with_retry(plan, step, system_prompt, tool_block)

        await asyncio.gather(*[_run_one(i) for i in step_indices])

    async def _execute_step_with_retry(
        self,
        plan: TaskPlan,
        step: TaskStep,
        system_prompt: str,
        tool_block: str = "",
    ) -> None:
        """Execute a single step with retry + exponential backoff."""
        for step_retry in range(_STEP_MAX_RETRIES):
            await self.execute_step(plan, step, system_prompt, tool_block)
            if step.status == StepStatus.DONE:
                break
            if step.status == StepStatus.FAILED and step_retry + 1 < _STEP_MAX_RETRIES:
                delay = min(
                    _STEP_RETRY_BASE_SEC * (2 ** step_retry),
                    _STEP_RETRY_MAX_SEC,
                )
                log.warning(
                    "TaskMesh: step %d failed, retrying (%d/%d) in %.0fs",
                    step.id, step_retry + 1, _STEP_MAX_RETRIES, delay,
                )
                step.status = StepStatus.PENDING
                await asyncio.sleep(delay)

        log.info("TaskMesh: step %d → %s (%.1fs, tools=%s)",
                 step.id, step.status.value, step.elapsed, step.tools_used)
        plan.save_checkpoint(f"step{step.id}")

    async def run_parallel(
        self,
        goal: str,
        system_prompt: str,
        tool_block: str = "",
    ) -> Tuple[TaskPlan, str]:
        """Like run(), but groups independent steps for parallel execution.

        Uses mike_swarm.detect_parallel_groups() to find steps that
        can run concurrently. Falls back to sequential run() if swarm
        is unavailable.
        """
        swarm = _get_swarm()
        if not swarm:
            log.info("TaskMesh.run_parallel: swarm unavailable, falling back to sequential")
            return await self.run(goal, system_prompt, tool_block)

        log.info("TaskMesh parallel: planning for: %s", goal[:120])
        plan = await self.create_plan(goal, system_prompt, tool_block)
        log.info("TaskMesh parallel: %d steps planned", len(plan.steps))
        plan.save_checkpoint("plan")

        # Detect which steps can run in parallel
        step_dicts = [{"description": s.description} for s in plan.steps]
        groups = swarm.detect_parallel_groups(step_dicts)
        parallel_groups = sum(1 for g in groups if len(g) > 1)
        log.info("TaskMesh parallel: %d groups (%d parallel) from %d steps",
                 len(groups), parallel_groups, len(plan.steps))

        # Execute group by group
        for group_idx, group in enumerate(groups):
            step_ids = [plan.steps[i].id for i in group]
            if len(group) > 1:
                log.info("TaskMesh parallel group %d: steps %s (concurrent)", group_idx, step_ids)
            await self._execute_step_group(plan, group, system_prompt, tool_block)

        summary = await self.consolidate(plan, system_prompt)
        log.info("TaskMesh parallel: done — %d/%d steps succeeded",
                 plan.done_count, len(plan.steps))
        if self._spawner:
            log.info("TaskMesh parallel sub-agents: %s", self._spawner.summary())
        plan.save_checkpoint("done")
        return plan, summary

    async def run_parallel_streaming(
        self,
        goal: str,
        system_prompt: str,
        tool_block: str = "",
    ) -> AsyncGenerator[Tuple[str, Any], None]:
        """Streaming parallel execution — yields progress events."""
        swarm = _get_swarm()
        if not swarm:
            async for event in self.run_streaming(goal, system_prompt, tool_block):
                yield event
            return

        yield ("plan_start", {"goal": goal})
        plan = await self.create_plan(goal, system_prompt, tool_block)
        yield ("plan_created", {
            "steps": [{"id": s.id, "description": s.description} for s in plan.steps],
        })

        step_dicts = [{"description": s.description} for s in plan.steps]
        groups = swarm.detect_parallel_groups(step_dicts)
        yield ("parallel_groups", {
            "groups": [[plan.steps[i].id for i in g] for g in groups],
        })

        for group_idx, group in enumerate(groups):
            if len(group) > 1:
                yield ("parallel_start", {
                    "group": group_idx,
                    "step_ids": [plan.steps[i].id for i in group],
                })

            for i in group:
                yield ("step_start", {
                    "step_id": plan.steps[i].id,
                    "description": plan.steps[i].description,
                    "total": len(plan.steps),
                    "parallel": len(group) > 1,
                })

            await self._execute_step_group(plan, group, system_prompt, tool_block)

            for i in group:
                step = plan.steps[i]
                plan.save_checkpoint(f"step{step.id}")
                yield ("step_done", {
                    "step_id": step.id,
                    "status": step.status.value,
                    "result_preview": step.result[:400] if step.result else "",
                    "tools_used": step.tools_used,
                })

        summary = await self.consolidate(plan, system_prompt)
        yield ("complete", {"summary": summary})
