# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Agent SDK — Sub-Agents with Dynamic Skills
=================================================

Sistema de agentes com skills dinâmicas carregadas de YAML.
Cada SkillAgent pode ser definido por classes Python (legado) ou
composto dinamicamente a partir de skills YAML via SkillRegistry.

DynamicSkillAgent:
  - Auto-configura tool_patterns, keywords, persona a partir de skills
  - Composição: um agente pode combinar N skills
  - Hot-reload: skills atualizadas sem restart

Legacy agents (EmailAgent, GitHubAgent, etc.) continuam funcionando.
AgentRegistry prioriza DynamicSkillAgent se disponível.

Usage:
  from mike_agent_sdk import AgentRegistry, SkillAgent
  from mike_skills import SkillRegistry

  skill_reg = SkillRegistry("skills/")
  skill_reg.load_all()

  registry = AgentRegistry(skill_registry=skill_reg)
  agent = registry.match_agent("busque meus emails do Raphael", tool_manifest)
  result = await agent.run(task, system_prompt, tool_block)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from mike_skills import ComposedSkill, Skill, SkillPack, SkillRegistry

log = logging.getLogger("mike.agent_sdk")


# ======================================================================
# Data structures
# ======================================================================

class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class AgentExecution:
    """Result of a SkillAgent execution."""
    agent_name: str
    task: str
    output: str
    tools_used: list[str] = field(default_factory=list)
    sub_agents_used: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0
    success: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


# ======================================================================
# SkillAgent — base class with MCP tool access
# ======================================================================

class SkillAgent:
    """
    Agent with specific MCP tool access.

    Unlike legacy agents which only called LLM,
    SkillAgent can execute MCP tools directly via the tool loop.
    Each agent filters the tool_manifest to only its allowed tools.
    """

    name: str = "base"
    description: str = ""
    # Tool name prefixes/patterns this agent can use
    tool_patterns: list[str] = []
    # Keywords for routing (task matching)
    keywords: list[str] = []
    # Specialized system prompt addition
    persona: str = ""
    # Max tool steps per execution
    max_tool_steps: int = 3

    def __init__(
        self,
        generate_fn: Optional[Callable] = None,
        execute_tool_fn: Optional[Callable] = None,
        extract_tool_call_fn: Optional[Callable] = None,
        strip_tool_call_fn: Optional[Callable] = None,
        render_tool_result_fn: Optional[Callable] = None,
        compact_tool_payload_fn: Optional[Callable] = None,
    ) -> None:
        self._generate = generate_fn
        self._execute_tool = execute_tool_fn
        self._extract_tool = extract_tool_call_fn
        self._strip_tool = strip_tool_call_fn
        self._render_result = render_tool_result_fn
        self._compact_payload = compact_tool_payload_fn
        self._status = AgentStatus.IDLE

    # ------------------------------------------------------------------
    # Tool filtering
    # ------------------------------------------------------------------
    def filter_tools(self, tool_manifest: list[dict]) -> list[dict]:
        """Return only the tools this agent is allowed to use."""
        if not self.tool_patterns:
            return tool_manifest  # no filter = access to all
        filtered = []
        for tool in tool_manifest:
            tool_name = tool.get("name", "").lower()
            for pattern in self.tool_patterns:
                if pattern.lower() in tool_name or re.search(pattern, tool_name, re.IGNORECASE):
                    filtered.append(tool)
                    break
        return filtered

    # ------------------------------------------------------------------
    # Routing confidence
    # ------------------------------------------------------------------
    def can_handle(self, task: str) -> float:
        """Return 0.0-1.0 confidence that this agent should handle the task."""
        lower = task.lower()
        matches = sum(1 for kw in self.keywords if kw in lower)
        if matches >= 3:
            return 0.95
        if matches >= 2:
            return 0.85
        if matches >= 1:
            return 0.6
        return 0.05

    def match_step(self, step_description: str, available_tools: list[dict]) -> float:
        """Score how well this agent matches a TaskMesh step.

        Considers keyword match, tool availability, AND whether the step
        description explicitly references tools this agent owns.
        """
        kw_score = self.can_handle(step_description)

        # Boost if agent has tools that match the step
        my_tools = self.filter_tools(available_tools)
        tool_boost = min(0.2, len(my_tools) * 0.05) if my_tools else -0.2

        # Extra boost: step description mentions one of our tool names
        step_lower = step_description.lower()
        for pattern in self.tool_patterns:
            if re.search(pattern, step_lower, re.IGNORECASE):
                tool_boost += 0.3
                break

        return min(1.0, max(0.0, kw_score + tool_boost))

    # ------------------------------------------------------------------
    # Build focused tool instruction block
    # ------------------------------------------------------------------
    def build_tool_block(self, tool_manifest: list[dict], format_fn: Optional[Callable] = None) -> str:
        """Build a focused tool instruction block with only this agent's tools."""
        my_tools = self.filter_tools(tool_manifest)
        if not my_tools:
            return ""

        if format_fn:
            tool_list = format_fn(my_tools)
        else:
            tool_list = "\n".join(
                f"- {t['name']}({', '.join((t.get('input_schema') or t.get('inputSchema') or {}).get('properties', {}).keys())})"
                for t in my_tools
            )

        return (
            f"TOOLS DISPONIVEIS PARA {self.name.upper()} AGENT:\n"
            f"{tool_list}\n\n"
            "FORMATO OBRIGATORIO para chamar tool:\n"
            '<tool_call>{"name":"NOME","arguments":{"arg":"valor"}}</tool_call>\n\n'
            f"{self.persona}\n\n"
            "REGRAS:\n"
            "1. Use SOMENTE as tools listadas acima.\n"
            "2. Uma tool por vez. Espere resultado antes de chamar outra.\n"
            "3. NUNCA invente resultado. Se falhar, diga que falhou.\n"
            "4. Se o usuario deu path absoluto, nome de arquivo, label ou valor concreto, use EXATAMENTE esse valor nos argumentos da tool.\n"
            "5. NUNCA emita tool_call com arguments vazio para tools que exigem path, content, query, command ou checkpoint_id.\n"
            "6. Se o usuario autorizou criar/editar arquivos, NAO peca confirmacao. Execute a acao real agora.\n"
            "7. Se a tarefa for continuar um projeto em pasta vazia, crie os arquivos-base imediatamente antes de escrever.\n"
            "8. Execute a tarefa e responda naturalmente ao final."
        )

    # ------------------------------------------------------------------
    # Core execution — async tool loop
    # ------------------------------------------------------------------
    async def run(
        self,
        task: str,
        system_prompt: str,
        tool_manifest: list[dict],
        context: str = "",
    ) -> AgentExecution:
        """Execute a task with the agent's focused tool set."""
        self._status = AgentStatus.RUNNING
        t0 = time.time()
        tools_used: list[str] = []
        last_tool_text = ""

        tool_block = self.build_tool_block(tool_manifest)
        my_tools = self.filter_tools(tool_manifest)

        full_system = system_prompt + ("\n\n" + tool_block if tool_block else "")
        messages = [
            {"role": "system", "content": full_system},
        ]
        if context:
            messages.append({"role": "user", "content": f"CONTEXTO:\n{context}"})
        messages.append({"role": "user", "content": task})

        assistant_text = ""
        for step in range(self.max_tool_steps + 1):
            try:
                response = await self._generate(messages, None)
                assistant_text = response.get("assistant_text", "")
            except Exception as exc:
                log.error("Agent %s LLM call failed: %s", self.name, exc)
                self._status = AgentStatus.FAILED
                return AgentExecution(
                    agent_name=self.name, task=task,
                    output=f"Erro no LLM: {exc}",
                    tools_used=tools_used,
                    elapsed_sec=round(time.time() - t0, 2),
                    success=False,
                )

            # Extract tool call
            tool_call = None
            if my_tools and step < self.max_tool_steps and self._extract_tool:
                tool_call = self._extract_tool(assistant_text)

            if not tool_call:
                break

            # TaskMesh plans one concrete action per delegated step. If the
            # model asks for another tool after a successful call, preserve the
            # verified result instead of broadening the step or failing over.
            if tools_used:
                self._status = AgentStatus.DONE
                return AgentExecution(
                    agent_name=self.name,
                    task=task,
                    output=last_tool_text or assistant_text,
                    tools_used=tools_used,
                    elapsed_sec=round(time.time() - t0, 2),
                    success=True,
                    metadata={"reason": "one_tool_step_complete"},
                )

            # Validate tool is in our allowed set
            tool_name = tool_call["name"]
            allowed_names = {t["name"] for t in my_tools}
            allowed_aliases = {
                name.rsplit(".", 1)[-1]
                for name in allowed_names
            }
            if tool_name not in allowed_names and tool_name not in allowed_aliases:
                log.warning(
                    "Agent %s tried to call unauthorized tool %s",
                    self.name, tool_name,
                )
                self._status = AgentStatus.FAILED
                return AgentExecution(
                    agent_name=self.name,
                    task=task,
                    output=assistant_text,
                    tools_used=tools_used,
                    elapsed_sec=round(time.time() - t0, 2),
                    success=False,
                    metadata={
                        "reason": "unauthorized_tool",
                        "tool_name": tool_name,
                    },
                )

            # Execute tool
            try:
                tool_result = await self._execute_tool(
                    tool_name, tool_call["arguments"]
                )
                tools_used.append(tool_name)
                last_tool_text = str(tool_result.get("text", ""))
            except Exception as exc:
                log.warning("Agent %s tool %s failed: %s", self.name, tool_name, exc)
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append({
                    "role": "system",
                    "content": f"ERRO na tool {tool_name}: {exc}",
                })
                continue

            # Compact and append result
            compact_result = dict(tool_result)
            if self._compact_payload:
                compact_result["text"] = self._compact_payload(
                    tool_name, tool_result.get("text", ""),
                )
            clean_text = self._strip_tool(assistant_text) if self._strip_tool else assistant_text
            messages.append({"role": "assistant", "content": clean_text or "(chamei tool)"})
            if self._render_result:
                messages.append({
                    "role": "user",
                    "content": self._render_result(
                        tool_name, tool_call["arguments"], compact_result,
                    ),
                })

        self._status = AgentStatus.DONE
        unresolved_tool_call = (
            self._extract_tool(assistant_text)
            if assistant_text and self._extract_tool
            else None
        )
        if unresolved_tool_call:
            self._status = AgentStatus.FAILED
            return AgentExecution(
                agent_name=self.name,
                task=task,
                output=assistant_text,
                tools_used=tools_used,
                elapsed_sec=round(time.time() - t0, 2),
                success=False,
                metadata={
                    "reason": "unresolved_tool_call",
                    "tool_name": unresolved_tool_call.get("name", ""),
                },
            )
        return AgentExecution(
            agent_name=self.name,
            task=task,
            output=assistant_text,
            tools_used=tools_used,
            elapsed_sec=round(time.time() - t0, 2),
            success=True,
        )


# ======================================================================
# IntentAnalyzer — lightweight routing intelligence
# (Merged from mike_orchestrator.py — only the useful pattern matching)
# ======================================================================

class IntentAnalyzer:
    """Fast regex-based intent analysis for smarter agent routing."""

    DOMAIN_SIGNALS: dict[str, list[str]] = {
        "email": [
            r"\b(?:email|e-mail|inbox|gmail|enviar?|mandar?|remetente|assunto)\b",
        ],
        "research": [
            r"\b(?:pesquis|busca|busque|procur|encontr|investig|descubra)\w*\b",
            r"\b(?:search|find|research|lookup|discover)\b",
            r"\b(?:o que é|quem é|quando foi|como funciona)\b",
        ],
        "github": [
            r"\b(?:github|reposit|repo|commit|issue|pull request|pr|branch)\b",
        ],
        "calendar": [
            r"\b(?:agenda|calendár|calendar|evento|compromisso|agendar|marcar|reunião|meeting|consulta|appointment)\b",
        ],
        "drive": [
            r"\b(?:drive|arquivo|file|pasta|folder|diretório|directory)\b",
        ],
        "data": [
            r"\b(?:planilha|excel|spreadsheet|csv|workbook|sheet)\b",
        ],
        "system": [
            r"\b(?:reinici|restart|status|health|monitor|gpu|vram|disco|backup|processo|pid|porta|serviço)\b",
        ],
        "huggingface": [
            r"\b(?:huggingface|hugging face|hf)\b",
        ],
    }

    COMPLEXITY_PATTERNS = [
        (r"\b(?:primeiro|segundo|terceiro|depois|em seguida|then|next|finally)\b", 0.3),
        (r"\b(?:todos?|todas?|cada|all|every)\b.*\b(?:arquivo|email|pasta|file)\b", 0.3),
        (r"\b(?:analise|compare|avalie|review|audit)\b", 0.2),
    ]

    def analyze_domain(self, text: str) -> tuple[str, float]:
        """Return (primary_domain, confidence)."""
        scores: dict[str, float] = {}
        for domain, patterns in self.DOMAIN_SIGNALS.items():
            score = sum(
                len(re.findall(p, text, re.IGNORECASE)) for p in patterns
            )
            scores[domain] = score
        best = max(scores, key=scores.get) if scores else "general"
        total = sum(scores.values()) + 0.01
        return (best, scores[best] / total) if scores[best] > 0 else ("general", 0.0)

    def estimate_complexity(self, text: str) -> float:
        """Return 0.0–1.0 complexity estimate."""
        c = 0.0
        for pattern, weight in self.COMPLEXITY_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                c += weight
        if len(text) > 300:
            c += 0.15
        return min(1.0, c)


# ======================================================================
# DynamicSkillAgent — auto-configured from SkillRegistry YAML
# ======================================================================

class DynamicSkillAgent(SkillAgent):
    """
    Agent that composes its capabilities from one or more Skills (YAML).

    Instead of hard-coded tool_patterns and keywords, this agent
    receives a ComposedSkill (merged tools, keywords, persona) at
    construction time and delegates all matching/filtering to it.
    """

    def __init__(
        self,
        composed_skill: Any,  # ComposedSkill from mike_skills
        generate_fn: Optional[Callable] = None,
        execute_tool_fn: Optional[Callable] = None,
        extract_tool_call_fn: Optional[Callable] = None,
        strip_tool_call_fn: Optional[Callable] = None,
        render_tool_result_fn: Optional[Callable] = None,
        compact_tool_payload_fn: Optional[Callable] = None,
    ) -> None:
        super().__init__(
            generate_fn=generate_fn,
            execute_tool_fn=execute_tool_fn,
            extract_tool_call_fn=extract_tool_call_fn,
            strip_tool_call_fn=strip_tool_call_fn,
            render_tool_result_fn=render_tool_result_fn,
            compact_tool_payload_fn=compact_tool_payload_fn,
        )
        self._composed = composed_skill
        self.name = composed_skill.name
        self.description = f"Dynamic agent: {', '.join(composed_skill.skills)}"
        self.tool_patterns = composed_skill.tools
        self.keywords = composed_skill.keywords
        self.persona = composed_skill.persona
        self.max_tool_steps = composed_skill.max_steps

    @property
    def skill_names(self) -> list[str]:
        return self._composed.skills

    def filter_tools(self, tool_manifest: list[dict]) -> list[dict]:
        """Delegate to ComposedSkill for tool filtering."""
        return self._composed.filter_tools(tool_manifest)


# ======================================================================
# Agent Registry — manages all agents, routing, spawning
# ======================================================================

class AgentRegistry:
    """
    Central registry for all SkillAgents.
    Handles agent creation, routing, and sub-agent spawning.

    If a SkillRegistry is provided, DynamicSkillAgents are created
    from skill packs and take priority over legacy hardcoded agents.
    """

    def __init__(
        self,
        generate_fn: Optional[Callable] = None,
        execute_tool_fn: Optional[Callable] = None,
        extract_tool_call_fn: Optional[Callable] = None,
        strip_tool_call_fn: Optional[Callable] = None,
        render_tool_result_fn: Optional[Callable] = None,
        compact_tool_payload_fn: Optional[Callable] = None,
        skill_registry: Optional[Any] = None,  # SkillRegistry from mike_skills
    ) -> None:
        self._fns = {
            "generate_fn": generate_fn,
            "execute_tool_fn": execute_tool_fn,
            "extract_tool_call_fn": extract_tool_call_fn,
            "strip_tool_call_fn": strip_tool_call_fn,
            "render_tool_result_fn": render_tool_result_fn,
            "compact_tool_payload_fn": compact_tool_payload_fn,
        }
        self._agents: dict[str, SkillAgent] = {}
        self._skill_registry = skill_registry
        self._intent_analyzer = IntentAnalyzer()
        if skill_registry:
            self._register_from_skills(skill_registry)
        if not self._agents:
            log.warning("AgentRegistry: no agents registered (no SkillRegistry?)")

    def _register_from_skills(self, skill_registry: Any) -> None:
        """
        Create DynamicSkillAgents from SkillRegistry packs.
        All agents are now YAML-driven — no more hardcoded fallbacks.
        """
        from mike_skills import SkillRegistry as SR
        if not isinstance(skill_registry, SR):
            return
        # Auto-load skills if not loaded yet
        if not skill_registry.list_skills():
            skill_registry.load_all()
        self._agents = {
            name: agent
            for name, agent in self._agents.items()
            if not isinstance(agent, DynamicSkillAgent)
        }
        covered_skills: set[str] = set()
        for pack in skill_registry.list_packs():
            skills = skill_registry.resolve_pack(pack.name)
            if not skills:
                continue
            composed = skill_registry.compose_skills([s.name for s in skills])
            composed.name = pack.name
            dyn_agent = DynamicSkillAgent(composed_skill=composed, **self._fns)
            dyn_agent.description = pack.description
            self._agents[pack.name] = dyn_agent
            covered_skills.update(composed.skills)
            log.info("DynamicSkillAgent '%s' registered (skills: %s)",
                     pack.name, ", ".join(composed.skills))
        fallback_count = 0
        for skill in sorted(skill_registry.list_skills(), key=lambda s: s.name):
            if skill.name in covered_skills:
                continue
            composed = skill_registry.compose_skills([skill.name])
            composed.name = f"skill_{skill.name}"
            dyn_agent = DynamicSkillAgent(composed_skill=composed, **self._fns)
            dyn_agent.description = f"Fallback single-skill agent: {skill.description}"
            self._agents[composed.name] = dyn_agent
            covered_skills.update(composed.skills)
            fallback_count += 1
            log.warning("Fallback DynamicSkillAgent '%s' registered for unpacked skill '%s'",
                        composed.name, skill.name)
        log.info("AgentRegistry: %d agents from %d skill packs, %d fallback agents",
                 len(self._agents), len(list(skill_registry.list_packs())), fallback_count)

    def reload_skills(self) -> int:
        """Hot-reload skills from YAML and re-register dynamic agents."""
        if not self._skill_registry:
            return 0
        changes = self._skill_registry.hot_reload()
        if changes:
            self._register_from_skills(self._skill_registry)
        return changes

    def register(self, agent: SkillAgent) -> None:
        """Register a custom agent."""
        self._agents[agent.name] = agent

    def get(self, name: str) -> Optional[SkillAgent]:
        """Get agent by name."""
        return self._agents.get(name)

    def list_agents(self) -> list[dict[str, Any]]:
        """List all registered agents with metadata."""
        result = []
        for a in self._agents.values():
            entry: dict[str, Any] = {
                "name": a.name,
                "description": a.description,
                "tool_patterns": a.tool_patterns,
                "keywords": a.keywords[:5],
                "max_tool_steps": a.max_tool_steps,
                "type": "dynamic" if isinstance(a, DynamicSkillAgent) else "custom",
            }
            if isinstance(a, DynamicSkillAgent):
                entry["skills"] = a.skill_names
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Smart routing
    # ------------------------------------------------------------------
    def match_agent(
        self,
        task: str,
        tool_manifest: list[dict] | None = None,
        threshold: float = 0.5,
    ) -> Optional[SkillAgent]:
        """Find the best agent for a task. Returns None if below threshold."""
        best_agent: Optional[SkillAgent] = None
        best_score = 0.0

        for agent in self._agents.values():
            if tool_manifest:
                score = agent.match_step(task, tool_manifest)
            else:
                score = agent.can_handle(task)
            if score > best_score:
                best_score = score
                best_agent = agent

        if best_score < threshold:
            log.debug("No agent matched task (best=%.2f < %.2f): %s",
                      best_score, threshold, task[:80])
            return None

        log.info("Agent matched: %s (score=%.2f) for: %s",
                 best_agent.name, best_score, task[:80])
        return best_agent

    def match_agents(
        self,
        task: str,
        tool_manifest: list[dict] | None = None,
        threshold: float = 0.5,
        max_agents: int = 3,
    ) -> list[Tuple[SkillAgent, float]]:
        """Find all matching agents above threshold, sorted by score."""
        scored = []
        for agent in self._agents.values():
            if tool_manifest:
                score = agent.match_step(task, tool_manifest)
            else:
                score = agent.can_handle(task)
            if score >= threshold:
                scored.append((agent, score))

        scored.sort(key=lambda x: -x[1])
        return scored[:max_agents]

    # ------------------------------------------------------------------
    # Direct execution
    # ------------------------------------------------------------------
    async def dispatch(
        self,
        task: str,
        system_prompt: str,
        tool_manifest: list[dict],
        context: str = "",
        threshold: float = 0.5,
    ) -> AgentExecution:
        """Route task to best agent and execute."""
        agent = self.match_agent(task, tool_manifest, threshold)
        if agent is None:
            return AgentExecution(
                agent_name="none", task=task,
                output="Nenhum agente especializado encontrado.",
                success=False,
                metadata={"reason": "no_match"},
            )
        return await agent.run(task, system_prompt, tool_manifest, context)

    async def dispatch_multi(
        self,
        task: str,
        system_prompt: str,
        tool_manifest: list[dict],
        context: str = "",
        threshold: float = 0.5,
        max_agents: int = 2,
    ) -> list[AgentExecution]:
        """Run multiple matching agents sequentially, passing output forward."""
        agents = self.match_agents(task, tool_manifest, threshold, max_agents)
        results: list[AgentExecution] = []
        accumulated_context = context

        for agent, score in agents:
            log.info("Dispatch multi: %s (score=%.2f)", agent.name, score)
            result = await agent.run(
                task, system_prompt, tool_manifest, accumulated_context,
            )
            results.append(result)
            if result.success and result.output:
                accumulated_context += f"\n\nResultado do agente {agent.name}:\n{result.output}"

        return results

    async def dispatch_parallel(
        self,
        task: str,
        system_prompt: str,
        tool_manifest: list[dict],
        context: str = "",
        threshold: float = 0.5,
        max_agents: int = 3,
    ) -> list[AgentExecution]:
        """Run multiple matching agents in parallel via asyncio.gather.

        Unlike dispatch_multi (sequential, chained context), this runs
        all matched agents concurrently with the same initial context.
        Useful when agents work on independent aspects of a task.
        """
        agents = self.match_agents(task, tool_manifest, threshold, max_agents)
        if not agents:
            return [AgentExecution(
                agent_name="none", task=task,
                output="Nenhum agente especializado encontrado.",
                success=False,
                metadata={"reason": "no_match"},
            )]

        log.info("Dispatch parallel: %d agents for '%s'",
                 len(agents), task[:80])

        async def _run_agent(agent: SkillAgent, score: float) -> AgentExecution:
            log.info("Parallel agent start: %s (score=%.2f)", agent.name, score)
            return await agent.run(task, system_prompt, tool_manifest, context)

        results = await asyncio.gather(
            *[_run_agent(a, s) for a, s in agents],
            return_exceptions=True,
        )

        executions: list[AgentExecution] = []
        for (agent, score), result in zip(agents, results):
            if isinstance(result, Exception):
                log.error("Parallel agent %s failed: %s", agent.name, result)
                executions.append(AgentExecution(
                    agent_name=agent.name, task=task,
                    output=f"Erro: {result}",
                    success=False,
                ))
            else:
                executions.append(result)

        return executions


# ======================================================================
# SubAgentSpawner — for TaskMesh integration
# ======================================================================

class SubAgentSpawner:
    """
    Bridges TaskMesh and AgentRegistry.

    When TaskMesh is executing steps, SubAgentSpawner can:
    1. Analyze each step description
    2. Match it to a specialized agent
    3. Execute the step via the agent (focused tools + persona)
    4. Fall back to generic TaskMesh execution if no agent matches
    """

    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry
        self._executions: list[AgentExecution] = []

    async def try_agent_for_step(
        self,
        step_description: str,
        system_prompt: str,
        tool_manifest: list[dict],
        context: str = "",
        threshold: float = 0.6,
    ) -> Optional[AgentExecution]:
        """
        Try to find and run a specialized agent for a TaskMesh step.
        Returns None if no agent matches (caller should use generic execution).
        """
        agent = self.registry.match_agent(
            step_description, tool_manifest, threshold,
        )
        if agent is None:
            return None

        log.info("SubAgent spawned: %s for step: %s",
                 agent.name, step_description[:80])

        result = await agent.run(
            step_description, system_prompt, tool_manifest, context,
        )
        self._executions.append(result)
        return result

    @property
    def executions(self) -> list[AgentExecution]:
        return list(self._executions)

    def summary(self) -> str:
        """Summary of all sub-agent executions."""
        if not self._executions:
            return "Nenhum sub-agent usado."
        lines = []
        for ex in self._executions:
            status = "✓" if ex.success else "✗"
            tools = ", ".join(ex.tools_used) if ex.tools_used else "nenhuma"
            lines.append(
                f"  {status} {ex.agent_name}: {ex.task[:60]} "
                f"[{ex.elapsed_sec:.1f}s, tools: {tools}]"
            )
        return "Sub-agents:\n" + "\n".join(lines)
