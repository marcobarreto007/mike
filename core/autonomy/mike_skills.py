# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Skills Engine — Dynamic Skill-Based Agent Capabilities
=============================================================

Skills are atomic units of capability defined in YAML files.
Each skill declares:
  - tools it needs from the MCP manifest
  - keywords for routing
  - a persona prompt fragment
  - cost / max_steps constraints

SkillRegistry loads skills from ``skills/*.yaml``, provides
hot-reload, search, and matching.  The refactored SkillAgent
in ``mike_agent_sdk.py`` composes skills dynamically instead
of hard-coding tool_patterns.

Usage:
  from mike_skills import SkillRegistry
  registry = SkillRegistry("skills/")
  registry.load_all()
  matches = registry.match("buscar emails do Raphael", tool_manifest)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

log = logging.getLogger("mike.skills")

# Historical skill files used provider-neutral or legacy tool names. Keep
# those declarations useful while matching them to concrete current tools.
TOOL_PATTERN_ALIASES: dict[str, tuple[str, ...]] = {
    "workspace.read_file": ("filesystem.read_file", "filesystem.read_text_file", "read_text_file"),
    "workspace.search_in_files": ("filesystem.search_files",),
    "workspace.grep": ("filesystem.search_files",),
    "workspace.list_directory": ("filesystem.list_directory", "list_directory"),
    "workspace.edit_file": ("filesystem.edit_file", "edit_file"),
    "workspace.run_command": ("workspace.run_command", "run_command"),
    "browse_search": ("web.search_and_cache",),
    "browse_open": ("fetch.fetch_readable", "fetch.fetch_markdown"),
    "scrape_url": ("fetch.fetch_readable", "fetch.fetch_markdown"),
    "knowledge.query": ("memory-persistent.search_nodes", "mike.hot_cache_list"),
    "playwright.goto": ("puppeteer.puppeteer_navigate",),
    "playwright.click": ("puppeteer.puppeteer_click",),
    "playwright.fill": ("puppeteer.puppeteer_fill",),
    "playwright.screenshot": ("puppeteer.puppeteer_screenshot",),
    "playwright.evaluate": ("puppeteer.puppeteer_evaluate",),
    "test_automator.run_tests": ("workspace.run_command", "run_command"),
    "javascript_frontend.build": ("workspace.run_command", "run_command"),
    "writing_architect.structure": ("filesystem.read_text_file", "filesystem.write_file"),
    "codebase_navigator.map": ("mike.introspect", "filesystem.directory_tree"),
    "skill_governance.validate_skill": (
        "filesystem.read_text_file", "workspace.run_command", "run_command",
    ),
    "autonomous_routines.create": ("create_routine",),
    "list_repos": ("github.search_repositories",),
    "list_repo_contents": ("github.get_file_contents",),
    "read_repo_file": ("github.get_file_contents",),
    "email.read_email": ("gmail.read_email",),
    "email.send_email": ("gmail.send_email",),
    "drive.list_files": ("drive.list_drive_files",),
    "drive.read_file": ("drive.read_drive_file",),
}


def tool_pattern_matches(pattern: str, tool_name: str) -> bool:
    """Return whether a skill tool declaration resolves to a manifest tool."""
    pattern_lower = str(pattern or "").strip().lower()
    tool_lower = str(tool_name or "").strip().lower()
    if not pattern_lower or not tool_lower:
        return False
    if pattern_lower in tool_lower:
        return True
    aliases = TOOL_PATTERN_ALIASES.get(pattern_lower, ())
    if any(alias.lower() == tool_lower or alias.lower() in tool_lower for alias in aliases):
        return True
    try:
        return re.search(pattern_lower, tool_lower, re.IGNORECASE) is not None
    except re.error:
        return False

# ======================================================================
# Skill — atomic capability unit
# ======================================================================

@dataclass
class Skill:
    """One atomic capability loaded from a YAML file."""
    name: str
    domain: str
    version: str = "1.0"
    description: str = ""
    tools: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    persona: str = ""
    max_steps: int = 3
    cost: str = "low"          # low | medium | high
    depends_on: list[str] = field(default_factory=list)
    # internal
    _source_file: str = ""
    _file_hash: str = ""

    @classmethod
    def from_yaml(cls, path: str) -> "Skill":
        """Load a Skill from a YAML file."""
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
            data = yaml.safe_load(raw)
        if not data or not isinstance(data, dict):
            raise ValueError(f"Invalid skill YAML: {path}")
        file_hash = hashlib.md5(raw.encode()).hexdigest()
        return cls(
            name=data["name"],
            domain=data.get("domain", "general"),
            version=str(data.get("version", "1.0")),
            description=data.get("description", ""),
            tools=data.get("tools", []),
            keywords=data.get("keywords", []),
            persona=data.get("persona", ""),
            max_steps=int(data.get("max_steps", 3)),
            cost=data.get("cost", "low"),
            depends_on=data.get("depends_on", []),
            _source_file=str(path),
            _file_hash=file_hash,
        )

    def filter_tools(self, tool_manifest: list[dict]) -> list[dict]:
        """Return tools from manifest that match this skill's tool list."""
        if not self.tools:
            return []
        filtered = []
        for tool in tool_manifest:
            tool_name = tool.get("name", "")
            for pattern in self.tools:
                if tool_pattern_matches(pattern, tool_name):
                    filtered.append(tool)
                    break
        return filtered

    def score_task(self, task: str) -> float:
        """Return 0.0–1.0 keyword-based relevance score."""
        lower = task.lower()
        matches = sum(1 for kw in self.keywords if kw.lower() in lower)
        if matches >= 4:
            return 0.98
        if matches >= 3:
            return 0.95
        if matches >= 2:
            return 0.85
        if matches >= 1:
            return 0.6
        return 0.0

    def score_step(self, step: str, tool_manifest: list[dict]) -> float:
        """Score with keyword + tool availability boost."""
        kw_score = self.score_task(step)
        my_tools = self.filter_tools(tool_manifest)
        tool_boost = min(0.2, len(my_tools) * 0.05) if my_tools else -0.1

        step_lower = step.lower()
        for pattern in self.tools:
            if re.search(pattern, step_lower, re.IGNORECASE):
                tool_boost += 0.2
                break

        return min(1.0, max(0.0, kw_score + tool_boost))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "version": self.version,
            "description": self.description,
            "tools": self.tools,
            "keywords": self.keywords,
            "max_steps": self.max_steps,
            "cost": self.cost,
            "depends_on": self.depends_on,
        }


# ======================================================================
# SkillPack — bundle of related skills
# ======================================================================

@dataclass
class SkillPack:
    """A named bundle of skills, typically mapping to an agent domain."""
    name: str
    description: str = ""
    skills: list[str] = field(default_factory=list)   # skill names

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "skills": self.skills,
        }


# Dynamic packs selected from the current Mike skill set plus the best agent
# skill patterns researched from Claude Code / GitHub / Hugging Face ecosystems.
# Rule: every checked-in skill should be reachable through at least one pack.
DEFAULT_PACKS: list[SkillPack] = [
    SkillPack("email", "Email - envio, busca, leitura e follow-up", [
        "email_ops", "email_tracking",
    ]),
    SkillPack("google_workspace", "Google Workspace - Gmail, Calendar e Drive em fluxo unico", [
        "google_workspace_ops", "email_ops", "calendar_ops", "drive_google",
    ]),
    SkillPack("researcher", "Pesquisa - web, scraping, deep research e mercado", [
        "deep_research",
    ]),
    SkillPack("github", "GitHub - repos, issues, PRs, CI e operacao de codigo", [
        "github_ops",
    ]),
    SkillPack("calendar", "Agenda - eventos e agendamentos", [
        "calendar_ops",
    ]),
    SkillPack("drive", "Arquivos - Google Drive e filesystem local", [
        "drive_google", "drive_local", "document_processing",
    ]),
    SkillPack("data", "Dados - Excel, CSV, analise e relatorios", [
        "data_analyst",
    ]),
    SkillPack("system", "Sistema - execucao remota, saude e operacao", [
        "devops_sre",
    ]),
    SkillPack("huggingface", "Hugging Face - modelos, datasets, Spaces, evals e papers", [
        "huggingface_ops",
    ]),
    SkillPack("autonomy", "Autonomia - lousa, rotinas, estado, checkpoints e familia", [
        "autonomy_status", "autonomous_routines", "task_board",
        "email_tracking", "memory_checkpoint",
        "project_manager", "family_operator",
    ]),
    SkillPack("quality", "Qualidade - verificacao, testes, review, debug e governanca", [
        "verification_loop", "code_review", "test_automator", "debugger",
        "skill_governance",
    ]),
    SkillPack("security", "Seguranca - codigo, configs, secrets, MCP e permissoes", [
        "security_auditor",
    ]),
    SkillPack("orchestration", "Orquestracao - agentes dinamicos, MCP, contexto e apps LLM", [
        "agent_orchestrator", "context_manager", "mcp_builder", "llm_apps",
    ]),
    SkillPack("backend", "Backend - Python, FastAPI, memoria, RAG e servicos", [
        "python_fastapi", "rag_memory_engineer",
    ]),
    SkillPack("frontend", "Frontend - dashboard, PWA e Android Capacitor", [
        "javascript_frontend", "android_capacitor",
    ]),
    SkillPack("product", "Produto - specs, roadmap, prioridades e mercado", [
        "product_manager", "project_manager",
    ]),
    SkillPack("writer_studio", "Escrita - arquitetura, personagens, rascunho e edicao", [
        "writing_architect", "writing_character", "writing_drafter", "writing_editor",
    ]),
    SkillPack("reasoning", "Qwen - segunda passagem critica e validacao", ["qwen_reasoning"]),
    SkillPack("claude_code", "Claude Code - MCP, skills dinâmicas, artifacts e automação web", [
        "mcp_builder", "skill_creator", "webapp_testing", "web_artifacts_builder", "doc_coauthoring",
    ]),
    SkillPack("codex_legacy", "Codex Legacy - arquitetura de código, testes agressivos e refatoração", [
        "code_architect", "test_harness", "refactor_expert", "codebase_navigator", "workflow_automator",
    ]),
    SkillPack("evolution", "Evolução - capacidade de auto-análise e melhoria do próprio código do Mike", [
        "self_evolution", "refactor_expert", "code_review", "test_automator",
    ]),
]


class SkillRegistry:
    """
    Central registry for skills. Loads from a directory of YAML files,
    supports hot-reload (detects file changes), and provides skill
    matching for tasks.
    """

    def __init__(self, skills_dir: str = "skills") -> None:
        self._skills_dir = Path(skills_dir)
        self._skills: dict[str, Skill] = {}
        self._packs: dict[str, SkillPack] = {}
        self._last_scan: float = 0.0
        self._scan_interval: float = 30.0  # seconds between hot-reload checks
        # register default packs
        for pack in DEFAULT_PACKS:
            self._packs[pack.name] = pack

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load_all(self) -> int:
        """Load (or reload) all YAML skills from the skills directory."""
        if not self._skills_dir.exists():
            log.warning("Skills directory not found: %s", self._skills_dir)
            return 0

        loaded = 0
        for yaml_file in sorted(self._skills_dir.glob("*.yaml")):
            try:
                skill = Skill.from_yaml(str(yaml_file))
                self._skills[skill.name] = skill
                loaded += 1
            except Exception as exc:
                log.error("Failed to load skill %s: %s", yaml_file.name, exc)

        self._last_scan = time.time()
        log.info("SkillRegistry loaded %d skills from %s", loaded, self._skills_dir)
        return loaded

    def hot_reload(self) -> int:
        """
        Check for new/changed/deleted skill files and reload as needed.
        Returns count of changes applied.
        """
        if time.time() - self._last_scan < self._scan_interval:
            return 0
        if not self._skills_dir.exists():
            return 0

        changes = 0
        current_files: set[str] = set()

        for yaml_file in self._skills_dir.glob("*.yaml"):
            fpath = str(yaml_file)
            current_files.add(fpath)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    new_hash = hashlib.md5(f.read().encode()).hexdigest()
            except Exception:
                continue

            # find existing skill from this file
            existing = None
            for s in self._skills.values():
                if s._source_file == fpath:
                    existing = s
                    break

            if existing is None or existing._file_hash != new_hash:
                try:
                    skill = Skill.from_yaml(fpath)
                    # remove old name if renamed
                    if existing and existing.name != skill.name:
                        self._skills.pop(existing.name, None)
                    self._skills[skill.name] = skill
                    changes += 1
                    log.info("Skill %s: %s", "reloaded" if existing else "loaded", skill.name)
                except Exception as exc:
                    log.error("Failed to reload %s: %s", yaml_file.name, exc)

        # detect deleted files
        to_remove = [
            name for name, s in self._skills.items()
            if s._source_file and s._source_file not in current_files
        ]
        for name in to_remove:
            del self._skills[name]
            changes += 1
            log.info("Skill removed (file deleted): %s", name)

        self._last_scan = time.time()
        if changes:
            log.info("Hot-reload: %d changes applied", changes)
        return changes

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------
    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def get_pack(self, name: str) -> Optional[SkillPack]:
        return self._packs.get(name)

    def list_skills(self) -> list[Skill]:
        """Return all loaded skills."""
        self.hot_reload()
        return list(self._skills.values())

    def list_packs(self) -> list[SkillPack]:
        return list(self._packs.values())

    def packed_skill_names(self) -> set[str]:
        """Return loaded skill names referenced by at least one pack."""
        self.hot_reload()
        packed: set[str] = set()
        for pack in self._packs.values():
            packed.update(name for name in pack.skills if name in self._skills)
        return packed

    def unpacked_skill_names(self) -> list[str]:
        """Return loaded skills that are not covered by any registered pack."""
        packed = self.packed_skill_names()
        return sorted(name for name in self._skills if name not in packed)

    def coverage_summary(
        self,
        tool_manifest: Optional[list[dict]] = None,
    ) -> dict[str, Any]:
        """Summarize pack reachability and optional real manifest coverage."""
        self.hot_reload()
        loaded = set(self._skills)
        packed = self.packed_skill_names()
        missing_pack_refs = sorted(
            name
            for pack in self._packs.values()
            for name in pack.skills
            if name not in loaded
        )
        packs_by_skill: dict[str, list[str]] = {name: [] for name in sorted(loaded)}
        for pack in self._packs.values():
            for name in pack.skills:
                if name in packs_by_skill:
                    packs_by_skill[name].append(pack.name)
        unpacked = sorted(loaded - packed)
        summary: dict[str, Any] = {
            "loaded_skill_count": len(loaded),
            "pack_count": len(self._packs),
            "packed_skill_count": len(packed),
            "unpacked_skill_count": len(unpacked),
            "all_skills_packed": not unpacked,
            "unpacked_skills": unpacked,
            "missing_pack_refs": missing_pack_refs,
            "packs_by_skill": packs_by_skill,
        }
        if tool_manifest is not None:
            manifest_names = [
                str(tool.get("name") or "")
                for tool in tool_manifest
                if str(tool.get("name") or "").strip()
            ]
            tool_coverage: dict[str, dict[str, Any]] = {}
            ready_count = 0
            all_patterns = 0
            matched_patterns = 0
            for skill_name in sorted(loaded):
                skill = self._skills[skill_name]
                matches: dict[str, list[str]] = {}
                missing_patterns: list[str] = []
                for pattern in skill.tools:
                    all_patterns += 1
                    resolved = [
                        tool_name
                        for tool_name in manifest_names
                        if tool_pattern_matches(pattern, tool_name)
                    ]
                    if resolved:
                        matched_patterns += 1
                        matches[pattern] = sorted(set(resolved))
                    else:
                        missing_patterns.append(pattern)
                available_tools = sorted(
                    {name for values in matches.values() for name in values}
                )
                ready = not skill.tools or bool(available_tools)
                if ready:
                    ready_count += 1
                tool_coverage[skill_name] = {
                    "ready": ready,
                    "available_tool_count": len(available_tools),
                    "available_tools": available_tools,
                    "declared_pattern_count": len(skill.tools),
                    "matched_pattern_count": len(matches),
                    "missing_patterns": missing_patterns,
                }
            summary.update({
                "manifest_tool_count": len(manifest_names),
                "tool_ready_skill_count": ready_count,
                "tool_unready_skill_count": len(loaded) - ready_count,
                "all_skills_have_tools": ready_count == len(loaded),
                "declared_tool_pattern_count": all_patterns,
                "matched_tool_pattern_count": matched_patterns,
                "tool_pattern_coverage_percent": (
                    round(100.0 * matched_patterns / all_patterns, 1)
                    if all_patterns else 100.0
                ),
                "tool_coverage": tool_coverage,
            })
        return summary

    def skills_by_domain(self, domain: str) -> list[Skill]:
        return [s for s in self._skills.values() if s.domain == domain]

    @property
    def domains(self) -> list[str]:
        return sorted({s.domain for s in self._skills.values()})

    @property
    def count(self) -> int:
        return len(self._skills)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def match(
        self,
        task: str,
        tool_manifest: list[dict] | None = None,
        threshold: float = 0.5,
        max_results: int = 5,
    ) -> list[Tuple[Skill, float]]:
        """
        Find skills matching a task, sorted by relevance score.
        Uses keyword+tool scoring (same as legacy agents).
        """
        self.hot_reload()
        scored: list[Tuple[Skill, float]] = []
        for skill in self._skills.values():
            if tool_manifest:
                score = skill.score_step(task, tool_manifest)
            else:
                score = skill.score_task(task)
            if score >= threshold:
                scored.append((skill, score))

        scored.sort(key=lambda x: -x[1])
        return scored[:max_results]

    def match_best(
        self,
        task: str,
        tool_manifest: list[dict] | None = None,
        threshold: float = 0.5,
    ) -> Optional[Tuple[Skill, float]]:
        """Find the single best skill for a task."""
        results = self.match(task, tool_manifest, threshold, max_results=1)
        return results[0] if results else None

    def resolve_pack(self, pack_name: str) -> list[Skill]:
        """Resolve a SkillPack to its constituent skills."""
        pack = self._packs.get(pack_name)
        if not pack:
            return []
        return [self._skills[n] for n in pack.skills if n in self._skills]

    def compose_skills(self, skill_names: list[str]) -> "ComposedSkill":
        """
        Compose multiple skills into a single virtual skill.
        Tools, keywords, and personas are merged.
        """
        skills = [self._skills[n] for n in skill_names if n in self._skills]
        return ComposedSkill.from_skills(skills)

    # ------------------------------------------------------------------
    # Pack management
    # ------------------------------------------------------------------
    def register_pack(self, pack: SkillPack) -> None:
        self._packs[pack.name] = pack

    def register_skill(self, skill: Skill) -> None:
        """Register a skill programmatically (not from YAML)."""
        self._skills[skill.name] = skill

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "skills_dir": str(self._skills_dir),
            "skill_count": len(self._skills),
            "pack_count": len(self._packs),
            "domains": self.domains,
            "skills": [s.to_dict() for s in self._skills.values()],
            "packs": [p.to_dict() for p in self._packs.values()],
            "coverage": self.coverage_summary(),
        }


# ======================================================================
# ComposedSkill — virtual skill from merging multiple skills
# ======================================================================

@dataclass
class ComposedSkill:
    """
    A virtual skill created by merging multiple skills.
    Used when an agent needs capabilities from multiple domains.
    """
    name: str
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    persona: str = ""
    max_steps: int = 5
    cost: str = "low"

    @classmethod
    def from_skills(cls, skills: list[Skill]) -> "ComposedSkill":
        if not skills:
            return cls(name="empty")
        all_tools: list[str] = []
        all_keywords: list[str] = []
        personas: list[str] = []
        max_steps = 0
        names: list[str] = []
        cost_val = {"low": 1, "medium": 2, "high": 3}
        max_cost = 0

        for sk in skills:
            names.append(sk.name)
            all_tools.extend(t for t in sk.tools if t not in all_tools)
            all_keywords.extend(k for k in sk.keywords if k not in all_keywords)
            if sk.persona:
                personas.append(f"[{sk.name}] {sk.persona.strip()}")
            max_steps = max(max_steps, sk.max_steps)
            max_cost = max(max_cost, cost_val.get(sk.cost, 1))

        cost_map = {1: "low", 2: "medium", 3: "high"}
        return cls(
            name="+".join(names),
            skills=names,
            tools=all_tools,
            keywords=all_keywords,
            persona="\n\n".join(personas),
            max_steps=min(max_steps + len(skills) - 1, 8),  # bonus steps for composition
            cost=cost_map.get(max_cost, "low"),
        )

    def filter_tools(self, tool_manifest: list[dict]) -> list[dict]:
        if not self.tools:
            return []
        filtered = []
        for tool in tool_manifest:
            tool_name = tool.get("name", "")
            for pattern in self.tools:
                if tool_pattern_matches(pattern, tool_name):
                    filtered.append(tool)
                    break
        return filtered

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "skills": self.skills,
            "tools": self.tools,
            "max_steps": self.max_steps,
            "cost": self.cost,
        }

