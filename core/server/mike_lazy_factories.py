# Extracted from mike_server.py — Phase 3 refactor
# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Lazy Factory Functions — Phase 4-7 Subsystem Initializers
===============================================================

Lazy-init factory functions for all governance, autonomy, memory, and
skill subsystems.  Each factory checks ``shared_state`` for an existing
instance and creates one on first access.

Originally part of ``mike_server.py`` (4 287 lines); extracted during
the Phase 3 modularization refactor.

Usage:
  from mike_lazy_factories import _get_autonomy, _get_governance, _get_monitor
  autonomy = _get_autonomy()
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

# ── Local project imports ────────────────────────────────────────────────
from mike_config import DEFAULT_MAX_TOKENS, PROJECT_ROOT, TASK_MESH_MAX_PLAN_STEPS
from mike_models import ChatRequest
from mike_chat_completion import (
    _clean_completion_text,
    _generate_model_response,
    _response_completion_tokens,
    _response_text,
)
from mike_mcp_client import (
    extract_tool_call,
    render_tool_result_message,
    strip_tool_call_text,
    tool_instruction_block,
)
from mike_tools_local import _compact_tool_payload, _execute_mcp_tool
from mike_auth import filter_tool_manifest
from mike_agent_sdk import AgentRegistry
from mike_output_guard import OutputGuard
from mike_context_virtual import VirtualContextManager
from mike_skills import SkillRegistry
from mike_skill_library import SkillLibrary
from mike_curriculum import AutoCurriculum
from mike_tool_analyzer import ToolFailureAnalyzer
from mike_governance import GovernanceLoop
from mike_mission_engine import MissionEngine
from mike_autonomy import MikeAutonomy
from mike_monitor import MikeMonitor
from mike_learner import MikeLearner
from mike_verifier import OutputVerifier
from mike_project_memory import ProjectConsciousness
from mike_reflection import EpisodicReflectionStore
import shared_state as _shared_state

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared SDK helper factories — single source of truth for LLM generate + tool
# execution closures used by Missions, Autonomy, and Agent dispatch endpoints.
# ---------------------------------------------------------------------------

def _make_sdk_generate_fn():
    """Create a reusable async LLM generate function for AgentRegistry / MikeAutonomy."""
    async def _sdk_generate(messages, _req=None):
        response = await _generate_model_response(
            messages,
            ChatRequest(messages=[], max_tokens=DEFAULT_MAX_TOKENS, temperature=0.7),
        )
        return {
            "assistant_text": _clean_completion_text(_response_text(response)),
            "completion_tokens": _response_completion_tokens(response, _response_text(response)),
        }
    return _sdk_generate


def _make_sdk_execute_fn(profile_key: str = "marco"):
    """Create a reusable async tool execution function bound to a profile."""
    async def _sdk_execute_tool(name, arguments):
        return await _execute_mcp_tool(name, arguments, profile_key=profile_key)
    return _sdk_execute_tool


def _make_agent_registry(profile_key: str = "marco"):
    """Create a fully-wired AgentRegistry — used by dispatch, dispatch_chain, and missions."""
    from mike_agent_sdk import AgentRegistry
    return AgentRegistry(
        generate_fn=_get_cached_sdk_generate(),
        execute_tool_fn=_make_sdk_execute_fn(profile_key),
        extract_tool_call_fn=extract_tool_call,
        strip_tool_call_fn=strip_tool_call_text,
        render_tool_result_fn=render_tool_result_message,
        compact_tool_payload_fn=_compact_tool_payload,
        skill_registry=_get_skill_registry(),
    )


def _get_cached_sdk_generate():
    """Lazy-init cached SDK generate closure — avoids per-request allocation."""
    if _shared_state._cached_sdk_generate is None:
        _shared_state._cached_sdk_generate = _make_sdk_generate_fn()
    return _shared_state._cached_sdk_generate


def _get_cached_agent_registry(profile_key: str = None):
    """Lazy-init cached AgentRegistry for the default 'marco' profile.

    Only caches the default profile.  Custom keys (e.g., mission dispatch
    with a variable profile) still create fresh registries per-call.
    """
    if profile_key is None or profile_key == "marco":
        if _shared_state._cached_agent_registry is None:
            _shared_state._cached_agent_registry = _make_agent_registry("marco")
        return _shared_state._cached_agent_registry
    return _make_agent_registry(profile_key)


# ---------------------------------------------------------------------------
# Phase 7 — Governance (4 Pillars of Autonomy) subsystem lazy initializers
# ---------------------------------------------------------------------------

def _get_output_guard():
    """Lazy-init OutputGuard — anti-simulation sentinel."""
    if _shared_state.output_guard is None:
        try:
            from mike_output_guard import OutputGuard
            _shared_state.output_guard = OutputGuard(
                log_dir=PROJECT_ROOT / "runtime" / "memory" / "guard",
                log_fn=log.info,
            )
            log.info("OutputGuard initialized (anti-simulation sentinel)")
        except Exception as exc:
            log.warning("OutputGuard init failed: %s", exc)
    return _shared_state.output_guard


def _get_virtual_context():
    """Lazy-init VirtualContextManager (MemGPT pattern) for infinite context."""
    if _shared_state.virtual_context is None:
        try:
            _shared_state.virtual_context = VirtualContextManager(
                store_dir=PROJECT_ROOT / "runtime" / "memory" / "vctx",
                log_fn=log.info,
            )
            log.info("VirtualContextManager initialized (MemGPT pattern)")
        except Exception as exc:
            log.warning("VirtualContextManager init failed: %s", exc)
    return _shared_state.virtual_context


def _get_skill_registry():
    if _shared_state.skill_registry is None:
        try:
            from mike_skills import SkillRegistry
            skills_dir = str(PROJECT_ROOT / "skills")
            _shared_state.skill_registry = SkillRegistry(skills_dir)
            count = _shared_state.skill_registry.load_all()
            log.info("SkillRegistry: %d skills loaded from %s", count, skills_dir)
        except Exception as exc:
            log.warning("SkillRegistry not available: %s", exc)
    return _shared_state.skill_registry


def _get_skill_library():
    """Lazy-init SkillLibrary (Voyager pattern) for executable validated skills."""
    if _shared_state.skill_library is None:
        try:
            from mike_skill_library import SkillLibrary
            _shared_state.skill_library = SkillLibrary(
                store_dir=PROJECT_ROOT / "runtime" / "memory" / "skills_lib",
                log_fn=log.info,
            )
            bootstrapped = _shared_state.skill_library.bootstrap_defaults()
            if bootstrapped:
                log.info("SkillLibrary bootstrapped with %d default skills", bootstrapped)
            else:
                log.info("SkillLibrary initialized (Voyager pattern)")
        except Exception as exc:
            log.warning("SkillLibrary init failed: %s", exc)
    return _shared_state.skill_library


def _get_curriculum():
    if _shared_state.curriculum is None:
        try:
            from mike_curriculum import AutoCurriculum
            _shared_state.curriculum = AutoCurriculum(
                store_dir=PROJECT_ROOT / "runtime" / "memory" / "curriculum",
                reflection_store=_shared_state.reflection_store,
                skill_library=_shared_state.skill_library,
                log_fn=log.info,
            )
            log.info("Auto-curriculum initialized")
        except Exception as exc:
            log.warning("Curriculum init failed: %s", exc)
    return _shared_state.curriculum


def _get_tool_analyzer():
    if _shared_state.tool_analyzer is None:
        try:
            from mike_tool_analyzer import ToolFailureAnalyzer
            _shared_state.tool_analyzer = ToolFailureAnalyzer(
                store_dir=PROJECT_ROOT / "runtime" / "memory" / "tool_analyzer",
                reflection_store=_shared_state.reflection_store,
                log_fn=log.info,
            )
            log.info("Tool failure analyzer initialized")
        except Exception as exc:
            log.warning("Tool analyzer init failed: %s", exc)
    return _shared_state.tool_analyzer


def _get_governance():
    if _shared_state.governance is None:
        try:
            from mike_governance import GovernanceLoop
            tg_fn = None
            try:
                from mike_telegram import MikeTelegram
                tg = MikeTelegram(log_fn=log.info)
                if tg.enabled and tg.chat_marco:
                    tg_fn = lambda msg: tg.send_marco(msg)
            except Exception as e:
                log.warning("[governance] MikeTelegram unavailable, Telegram alerts disabled: %s", e)
            _shared_state.governance = GovernanceLoop(
                monitor=_get_monitor(),
                telegram_fn=tg_fn,
                stats=_shared_state.stats,
            )
        except Exception as exc:
            log.warning("Governance init failed: %s", exc)
    return _shared_state.governance


def _get_consciousness():
    if _shared_state.consciousness is None:
        try:
            from mike_project_memory import ProjectConsciousness
            _shared_state.consciousness = ProjectConsciousness()
        except Exception as exc:
            log.warning("ProjectConsciousness init failed: %s", exc)
    return _shared_state.consciousness


def _get_verifier():
    if _shared_state.verifier is None:
        try:
            from mike_verifier import OutputVerifier
            _shared_state.verifier = OutputVerifier(consciousness=_get_consciousness())
        except Exception as exc:
            log.warning("Verifier init failed: %s", exc)
    return _shared_state.verifier


def _get_missions():
    if _shared_state.missions is None:
        try:
            from mike_server import _build_task_mesh, _broadcast_notification, SYSTEM_PROMPT
            from mike_context import build_dynamic_prefix

            from mike_agent_sdk import AgentRegistry
            from mike_mission_engine import MissionEngine

            mcp_workspace = _shared_state.mcp_workspace

            async def _mission_planner(goal: str, profile_key: str, max_steps: int) -> list[dict]:
                manifest = await mcp_workspace.list_tools() if mcp_workspace else []
                mesh = _build_task_mesh(profile_key, manifest)
                system_prompt = SYSTEM_PROMPT + "\n\n" + build_dynamic_prefix(goal)
                tool_block = tool_instruction_block(manifest) or ""
                plan = await mesh.create_plan(goal, system_prompt, tool_block)
                return [
                    {
                        "id": str(step.id),
                        "description": step.description,
                    }
                    for step in plan.steps[:max(1, min(max_steps, TASK_MESH_MAX_PLAN_STEPS))]
                ]

            def _mission_registry_factory(profile_key: str):
                return _make_agent_registry(profile_key)


            async def _mission_tool_manifest(profile_key: str) -> list[dict]:
                all_tools = await mcp_workspace.list_tools() if mcp_workspace else []
                return filter_tool_manifest(all_tools, profile_key)

            def _mission_system_prompt(goal: str) -> str:
                return SYSTEM_PROMPT + "\n\n" + build_dynamic_prefix(goal)

            async def _mission_calendar_event(
                title: str,
                start_iso: str,
                goal: str,
                profile_key: str,
            ) -> dict:
                start_text = (start_iso or "").strip()
                if not start_text:
                    return {"error": "start_iso is required"}
                if start_text.endswith("Z"):
                    start_text = start_text[:-1] + "+00:00"
                start_dt = datetime.fromisoformat(start_text)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                end_dt = start_dt + timedelta(minutes=30)
                return await _execute_mcp_tool(
                    "calendar.create_event",
                    {
                        "title": title,
                        "start": start_dt.astimezone(timezone.utc).isoformat(),
                        "end": end_dt.astimezone(timezone.utc).isoformat(),
                        "description": f"Mission: {goal}",
                    },
                    profile_key=profile_key,
                )

            _shared_state.missions = MissionEngine(
                planner_fn=_mission_planner,
                registry_factory=_mission_registry_factory,
                tool_manifest_fn=_mission_tool_manifest,
                system_prompt_fn=_mission_system_prompt,
                notify_fn=_broadcast_notification,
                calendar_event_fn=_mission_calendar_event,
                store_path=PROJECT_ROOT / "runtime" / "memory" / "missions" / "missions.json",
            )
        except Exception as exc:
            log.warning("Mission engine init failed: %s", exc)
    return _shared_state.missions


def _get_autonomy():
    if _shared_state.autonomy is not None:
        return _shared_state.autonomy
    try:
        from mike_server import _broadcast_notification, SYSTEM_PROMPT
        from mike_autonomy import MikeAutonomy

        memory_service = _shared_state.memory_service
        mcp_workspace = _shared_state.mcp_workspace

        # Initialize Reflection Store (Reflexion pattern)
        if _shared_state.reflection_store is None:
            try:
                from mike_reflection import EpisodicReflectionStore
                _shared_state.reflection_store = EpisodicReflectionStore(
                    db_path=PROJECT_ROOT / "runtime" / "memory" / "reflections.db",
                    embedder=memory_service.local_store.embedder if memory_service else None,
                    log_fn=log.info,
                )
                _shared_state.reflection_store.initialize()
                log.info("Reflection store initialized")
            except Exception as exc:
                log.warning("Reflection store init failed: %s", exc)

        async def _auto_tool_block():
            manifest = await mcp_workspace.list_tools() if mcp_workspace else []
            return tool_instruction_block(manifest) or ""

        # Email search function for response tracking
        email_search_fn = None
        try:
            from mike_email_mcp import search_emails
            email_search_fn = search_emails
        except Exception as e:
            log.warning("[server] Failed to import email search for auto-reply: %s", e)

        # Wire auto-reply function for family email events
        auto_reply_fn = None
        try:
            from mike_auto_reply import auto_reply_to_family, build_llm_fn
            from mike_email_mcp import (
                list_inbox as _gmail_list_inbox,
                read_email as _gmail_read_email,
                send_email as _gmail_send_email,
            )

            def _auto_reply_send(to: str, subject: str, body: str) -> dict:
                response = _gmail_send_email(to, subject, body)
                ok = isinstance(response, str) and not response.lower().startswith("erro")
                return {"ok": ok, "text": response, "error": "" if ok else response}

            auto_reply_fn = lambda: auto_reply_to_family(
                list_inbox_fn=_gmail_list_inbox,
                read_email_fn=_gmail_read_email,
                send_email_fn=_auto_reply_send,
                llm_fn=build_llm_fn(_shared_state),
            )
        except Exception as exc:
            log.warning("Family auto-reply wiring unavailable: %s", exc)

        _shared_state.autonomy = MikeAutonomy(
            generate_fn=_get_cached_sdk_generate(),
            execute_tool_fn=_make_sdk_execute_fn("marco"),
            extract_tool_call_fn=extract_tool_call,
            strip_tool_call_fn=strip_tool_call_text,
            render_tool_result_fn=render_tool_result_message,
            compact_tool_payload_fn=_compact_tool_payload,
            tool_block_fn=_auto_tool_block,
            system_prompt=SYSTEM_PROMPT,
            notify_fn=_broadcast_notification,
            email_search_fn=email_search_fn,
            reflection_store=_shared_state.reflection_store,
            curriculum=_shared_state.curriculum,
            tool_analyzer=_shared_state.tool_analyzer,
            event_bus=_shared_state.event_bus,
            auto_reply_fn=auto_reply_fn,
            store_dir=PROJECT_ROOT / "runtime" / "memory" / "autonomy",
        )
        log.info("Autonomy engine initialized")
    except Exception as exc:
        log.warning("Autonomy engine init failed: %s", exc)
    return _shared_state.autonomy


def _get_monitor():
    if _shared_state.monitor is None:
        try:
            from mike_monitor import MikeMonitor
            _shared_state.monitor = MikeMonitor(log_fn=log.info)
        except Exception as exc:
            log.warning("Monitor init failed: %s", exc)
    return _shared_state.monitor


def _get_learner():
    if _shared_state.learner is None:
        try:
            from mike_learner import MikeLearner
            memory_service = _shared_state.memory_service
            _shared_state.learner = MikeLearner(
                log_fn=log.info,
                memory=memory_service,
                graph=memory_service.graph if hasattr(memory_service, "graph") else None,
            )
        except Exception as exc:
            log.warning("Learner init failed: %s", exc)
    return _shared_state.learner
