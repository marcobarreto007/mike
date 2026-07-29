"""
Autonomy + Missions + Monitor + Governance + Agents + Skills + Notifications routes.
"""
import asyncio
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mike_auth import profile_from_request
import shared_state

from mike_lazy_factories import (
    _get_missions,
    _get_governance,
    _get_consciousness,
    _get_verifier,
    _get_cached_agent_registry,
    _get_skill_registry,
    _get_skill_library,
)
from mike_request_helpers import _can_view_operational_details, _request_profile_scope
from mike_payload_helpers import _monitor_payload
from mike_tools_local import _visible_tool_manifest
from mike_notifications import _notification_queues, _notification_queues_lock, _broadcast_notification

router = APIRouter()


def _can_view(request: Request, profile_key=None) -> bool:
    return _can_view_operational_details(request, profile_key or profile_from_request(request))


# ── AUTONOMY ────────────────────────────────────────────────────
@router.get("/v1/autonomy/status")
async def autonomy_status(request: Request):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    return a.status()

@router.get("/v1/autonomy/tasks")
async def autonomy_tasks():
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    return a._loaded and a._data.get("tasks", {}) or {}

@router.post("/v1/autonomy/tasks")
async def autonomy_tasks_create(request: Request):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    body = await request.json()
    task_id = await a.add_task(body.get("description", ""), body.get("priority", "normal"))
    return {"status": "ok", "task_id": task_id}

@router.delete("/v1/autonomy/tasks/{task_id}")
async def autonomy_task_delete(task_id: str):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    await a.delete_task(task_id)
    return {"status": "ok"}

@router.post("/v1/autonomy/tasks/{task_id}/cancel")
async def autonomy_task_cancel(task_id: str):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    await a.cancel_task(task_id)
    return {"status": "ok"}

@router.get("/v1/autonomy/routines")
async def autonomy_routines():
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    return a._loaded and a._data.get("routines", []) or []

@router.post("/v1/autonomy/routines/{routine_id}/toggle")
async def autonomy_routine_toggle(routine_id: str):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    await a.toggle_routine(routine_id)
    return {"status": "ok"}

@router.post("/v1/autonomy/routines/{routine_id}/run")
async def autonomy_routine_run(routine_id: str):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    await a.run_routine_now(routine_id)
    return {"status": "ok"}

@router.get("/v1/autonomy/email-tracking")
async def autonomy_email_tracking():
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    return a._loaded and a._data.get("email_tracking", {}) or {}

@router.post("/v1/autonomy/email-tracking")
async def autonomy_email_tracking_update(request: Request):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    body = await request.json()
    await a.update_email_tracking(body)
    return {"status": "ok"}

@router.get("/v1/autonomy/log")
async def autonomy_log(limit: int = 50):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    return {"entries": await a.get_log(limit)}


# ── REFLECTIONS (Reflexion Pattern) ────────────────────────────────
@router.get("/v1/autonomy/reflections")
async def reflections_list(limit: int = 20, source: Optional[str] = None):
    rs = shared_state.reflection_store
    if rs is None: return JSONResponse(status_code=503, content={"error": "Reflection store nao disponivel"})
    recent = rs.get_recent(limit=limit, source=source)
    return {"reflections": [r.to_dict() for r in recent]}

@router.get("/v1/autonomy/reflections/stats")
async def reflections_stats():
    rs = shared_state.reflection_store
    if rs is None: return JSONResponse(status_code=503, content={"error": "Reflection store nao disponivel"})
    return rs.stats()

@router.get("/v1/autonomy/reflections/{reflection_id}")
async def reflections_get(reflection_id: str):
    rs = shared_state.reflection_store
    if rs is None: return JSONResponse(status_code=503, content={"error": "Reflection store nao disponivel"})
    r = rs.get_by_id(reflection_id)
    if r is None: return JSONResponse(status_code=404, content={"error": "Reflexao nao encontrada"})
    return r.to_dict()

@router.delete("/v1/autonomy/reflections/{reflection_id}")
async def reflections_delete(reflection_id: str):
    rs = shared_state.reflection_store
    if rs is None: return JSONResponse(status_code=503, content={"error": "Reflection store nao disponivel"})
    rs.delete(reflection_id)
    return {"status": "ok"}

@router.post("/v1/autonomy/reflections/search")
async def reflections_search(request: Request):
    rs = shared_state.reflection_store
    if rs is None: return JSONResponse(status_code=503, content={"error": "Reflection store nao disponivel"})
    body = await request.json()
    query = body.get("query", "")
    limit = int(body.get("limit", 5))
    threshold = float(body.get("threshold", 0.55))
    similar = rs.search_similar(query, limit=limit, threshold=threshold)
    return {
        "query": query,
        "results": [
            {"reflection": hit.reflection.to_dict(), "score": round(hit.score, 4)}
            for hit in similar
        ],
    }


# ── CURRICULUM (Voyager Pattern) ──────────────────────────────────
@router.get("/v1/autonomy/curriculum")
async def curriculum_list(status: Optional[str] = None, category: Optional[str] = None):
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    return {"goals": c.list_goals(status=status, category=category)}

@router.get("/v1/autonomy/curriculum/progress")
async def curriculum_progress():
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    return c.progress()

@router.get("/v1/autonomy/curriculum/gaps")
async def curriculum_gaps():
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    return {"gaps": c.detect_gaps()}

@router.post("/v1/autonomy/curriculum/generate")
async def curriculum_generate(request: Request):
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    body = await request.json()
    count = int(body.get("count", 3))
    goals = c.generate_goals(count=count)
    return {"goals": [g.to_dict() for g in goals]}

@router.get("/v1/autonomy/curriculum/{goal_id}")
async def curriculum_get(goal_id: str):
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    g = c.get_goal(goal_id)
    if g is None: return JSONResponse(status_code=404, content={"error": "Goal nao encontrado"})
    return g

@router.post("/v1/autonomy/curriculum/{goal_id}/start")
async def curriculum_start(goal_id: str):
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    g = c.start_goal(goal_id)
    if g is None: return JSONResponse(status_code=400, content={"error": "Nao foi possivel iniciar o goal"})
    return g.to_dict()

@router.post("/v1/autonomy/curriculum/{goal_id}/complete")
async def curriculum_complete(goal_id: str, request: Request):
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    body = await request.json()
    score = float(body.get("score", 1.0))
    g = c.complete_goal(goal_id, score=score)
    if g is None: return JSONResponse(status_code=400, content={"error": "Nao foi possivel completar o goal"})
    return g.to_dict()

@router.post("/v1/autonomy/curriculum/{goal_id}/fail")
async def curriculum_fail(goal_id: str, request: Request):
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    body = await request.json()
    error = body.get("error", "")
    g = c.fail_goal(goal_id, error=error)
    if g is None: return JSONResponse(status_code=400, content={"error": "Nao foi possivel falhar o goal"})
    return g.to_dict()

@router.post("/v1/autonomy/curriculum/idle")
async def curriculum_idle():
    c = shared_state.curriculum
    if c is None: return JSONResponse(status_code=503, content={"error": "Curriculum nao disponivel"})
    action = c.idle_action()
    return action or {"action": "nothing_to_do"}


# ── TOOL ANALYZER ──────────────────────────────────────────────────
@router.get("/v1/autonomy/tool-analyzer/stats")
async def tool_analyzer_stats():
    ta = shared_state.tool_analyzer
    if ta is None: return JSONResponse(status_code=503, content={"error": "Tool analyzer nao disponivel"})
    return ta.stats()

@router.get("/v1/autonomy/tool-analyzer/patterns")
async def tool_analyzer_patterns(min_count: int = 1):
    ta = shared_state.tool_analyzer
    if ta is None: return JSONResponse(status_code=503, content={"error": "Tool analyzer nao disponivel"})
    return {"patterns": ta.get_patterns(min_count=min_count)}

@router.get("/v1/autonomy/tool-analyzer/failures")
async def tool_analyzer_failures(tool_name: Optional[str] = None, error_type: Optional[str] = None, limit: int = 50):
    ta = shared_state.tool_analyzer
    if ta is None: return JSONResponse(status_code=503, content={"error": "Tool analyzer nao disponivel"})
    return {"failures": ta.get_failures(tool_name=tool_name, error_type=error_type, limit=limit)}

@router.post("/v1/autonomy/tool-analyzer/classify")
async def tool_analyzer_classify(request: Request):
    ta = shared_state.tool_analyzer
    if ta is None: return JSONResponse(status_code=503, content={"error": "Tool analyzer nao disponivel"})
    body = await request.json()
    tool_name = body.get("tool_name", "unknown")
    error_message = body.get("error_message", "")
    recovery = ta.get_recovery_strategy(tool_name, error_message)
    ta.record_failure(
        tool_name=tool_name,
        error_message=error_message,
        context=body.get("context", ""),
        source=body.get("source", "api"),
    )
    return recovery


# ── MISSIONS ────────────────────────────────────────────────────
@router.get("/v1/missions")
async def missions_list():
    m = _get_missions()
    if m is None: return JSONResponse(status_code=503, content={"error": "Mission engine nao disponivel"})
    return await m.list_missions()

@router.post("/v1/missions")
async def missions_create(request: Request):
    m = _get_missions()
    if m is None: return JSONResponse(status_code=503, content={"error": "Mission engine nao disponivel"})
    body = await request.json()
    profile_key = profile_from_request(request) or "marco"
    result = await m.create_mission(
        title=body.get("goal", ""),
        goal=body.get("goal", ""),
        steps=body.get("steps", []),
        profile_key=profile_key,
    )
    return {"status": "ok", "mission_id": result.get("id", result.get("mission_id", ""))}

@router.get("/v1/missions/{mission_id}")
async def missions_get(mission_id: str):
    m = _get_missions()
    if m is None: return JSONResponse(status_code=503, content={"error": "Mission engine nao disponivel"})
    return await m.get_mission(mission_id) or JSONResponse(status_code=404, content={"error": "Mission not found"})

@router.post("/v1/missions/{mission_id}/cancel")
async def missions_cancel(mission_id: str):
    m = _get_missions()
    if m is None: return JSONResponse(status_code=503, content={"error": "Mission engine nao disponivel"})
    await m.cancel_mission(mission_id)
    return {"status": "ok"}

@router.post("/v1/missions/{mission_id}/step/{step_id}/complete")
async def missions_step_complete(mission_id: str, step_id: str):
    m = _get_missions()
    if m is None: return JSONResponse(status_code=503, content={"error": "Mission engine nao disponivel"})
    await m.complete_step(mission_id, int(step_id))
    return {"status": "ok"}

@router.post("/v1/missions/run_once")
async def missions_run_once():
    m = _get_missions()
    if m is None: return JSONResponse(status_code=503, content={"error": "Mission engine nao disponivel"})
    await m.run_once()
    return {"status": "ok"}


# ── MONITOR ─────────────────────────────────────────────────────
@router.get("/v1/monitor")
async def monitor_status(request: Request):
    if not _can_view(request): return JSONResponse(status_code=403, content={"error": "Forbidden"})
    mon = shared_state.monitor
    if mon is None: return JSONResponse(status_code=503, content={"error": "Monitor nao disponivel"})
    return _monitor_payload(request, snapshot=False)

@router.get("/v1/monitor/snapshot")
async def monitor_snapshot(request: Request):
    if not _can_view(request): return JSONResponse(status_code=403, content={"error": "Forbidden"})
    mon = shared_state.monitor
    if mon is None: return JSONResponse(status_code=503, content={"error": "Monitor nao disponivel"})
    return _monitor_payload(request, snapshot=True)

@router.post("/v1/monitor/recover")
async def monitor_recover(request: Request):
    if not _can_view(request): return JSONResponse(status_code=403, content={"error": "Forbidden"})
    mon = shared_state.monitor
    if mon is None: return JSONResponse(status_code=503, content={"error": "Monitor nao disponivel"})
    return {"actions": mon.auto_recover()}


# ── GOVERNANCE ──────────────────────────────────────────────────
@router.get("/v1/governance")
async def governance_status(request: Request):
    if not _can_view(request): return JSONResponse(status_code=403, content={"error": "Forbidden"})
    g = _get_governance()
    if g is None: return JSONResponse(status_code=503, content={"error": "Governance nao disponivel"})
    return g.status()

@router.post("/v1/governance/cycle")
async def governance_cycle(request: Request):
    if not _can_view(request): return JSONResponse(status_code=403, content={"error": "Forbidden"})
    g = _get_governance()
    if g is None: return JSONResponse(status_code=503, content={"error": "Governance nao disponivel"})
    await g.run_cycle()
    return {"status": "ok"}


# ── CONSCIOUSNESS / VERIFIER ────────────────────────────────────
@router.get("/v1/consciousness")
async def consciousness_status():
    c = _get_consciousness()
    if c is None:
        return JSONResponse(status_code=503, content={"error": "Consciousness nao disponivel"})
    if hasattr(c, "status") and callable(getattr(c, "status")):
        return c.status()
    if hasattr(c, "summary") and callable(getattr(c, "summary")):
        return c.summary()
    if hasattr(c, "stats") and callable(getattr(c, "stats")):
        return c.stats()
    return {"available": True, "type": type(c).__name__}

@router.get("/v1/verifier")
async def verifier_status():
    v = _get_verifier()
    if v is None:
        return JSONResponse(status_code=503, content={"error": "Verifier nao disponivel"})
    if hasattr(v, "status") and callable(getattr(v, "status")):
        return v.status()
    if hasattr(v, "stats") and callable(getattr(v, "stats")):
        return v.stats()
    if hasattr(v, "summary") and callable(getattr(v, "summary")):
        return {"summary": v.summary()}
    return {"available": True, "type": type(v).__name__}


# ── LEARNER ─────────────────────────────────────────────────────
@router.get("/v1/learner/summary")
async def learner_summary():
    l = shared_state.learner
    if l is None: return JSONResponse(status_code=503, content={"error": "Learner nao disponivel"})
    return l.summary()

@router.get("/v1/learner/topics")
async def learner_topics():
    l = shared_state.learner
    if l is None: return JSONResponse(status_code=503, content={"error": "Learner nao disponivel"})
    return {"topics": l.get_topics()}

@router.get("/v1/learner/errors")
async def learner_errors(limit: int = 20):
    l = shared_state.learner
    if l is None: return JSONResponse(status_code=503, content={"error": "Learner nao disponivel"})
    return {"errors": l.get_errors(limit)}


# ── AGENTS ──────────────────────────────────────────────────────
@router.get("/v1/agents")
async def agents_list():
    reg = _get_cached_agent_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Agent registry nao disponivel"})
    return {"agents": reg.list_agents()}

@router.post("/v1/agents/dispatch")
async def agents_dispatch(request: Request):
    reg = _get_cached_agent_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Agent registry nao disponivel"})
    body = await request.json()
    return await reg.dispatch(body.get("agent"), body.get("payload", {}))

@router.post("/v1/agents/dispatch_chain")
async def agents_dispatch_chain(request: Request):
    reg = _get_cached_agent_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Agent registry nao disponivel"})
    body = await request.json()
    return await reg.dispatch_chain(body.get("chain", []))

@router.get("/v1/agents/sdk")
async def agents_sdk():
    sdk = _get_cached_agent_registry()
    return {"sdk": str(type(sdk)) if sdk else None}

@router.post("/v1/agents/sdk/dispatch")
async def agents_sdk_dispatch(request: Request):
    sdk = _get_cached_agent_registry()
    if sdk is None: return JSONResponse(status_code=503, content={"error": "Agent SDK nao disponivel"})
    body = await request.json()
    return await sdk.dispatch(body)


# ── SKILLS ──────────────────────────────────────────────────────
@router.get("/v1/skills")
async def skills_list():
    reg = _get_skill_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Skills registry nao disponivel"})
    return {"skills": reg.list_skills()}

@router.get("/v1/skills/packs")
async def skills_packs():
    reg = _get_skill_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Skills registry nao disponivel"})
    return {"packs": reg.list_packs()}

@router.get("/v1/skills/coverage")
async def skills_coverage(request: Request):
    reg = _get_skill_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Skills registry nao disponivel"})
    profile_key = _request_profile_scope(request)
    manifest = await _visible_tool_manifest(profile_key)
    return reg.coverage_summary(manifest)

@router.get("/v1/skills/{skill_name}")
async def skills_get(skill_name: str):
    reg = _get_skill_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Skills registry nao disponivel"})
    return reg.get(skill_name) or JSONResponse(status_code=404, content={"error": "Skill not found"})

@router.post("/v1/skills/match")
async def skills_match(request: Request):
    reg = _get_skill_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Skills registry nao disponivel"})
    body = await request.json()
    return {"matches": reg.match(body.get("query", ""))}

@router.post("/v1/skills/reload")
async def skills_reload():
    reg = _get_skill_registry()
    if reg is None: return JSONResponse(status_code=503, content={"error": "Skills registry nao disponivel"})
    reg.reload()
    return {"status": "ok"}


# ── SkillLibrary (Voyager pattern) ─────────────────────────────────
@router.get("/v1/skills/library")
async def skill_library_list():
    lib = _get_skill_library()
    if lib is None: return JSONResponse(status_code=503, content={"error": "Skill library not initialized"})
    return {"skills": [s.to_dict() for s in lib.list_skills()], "stats": lib.stats()}


@router.post("/v1/skills/library/search")
async def skill_library_search(request: Request):
    lib = _get_skill_library()
    if lib is None: return JSONResponse(status_code=503, content={"error": "Skill library not initialized"})
    body = await request.json()
    results = lib.search(body.get("query", ""), limit=body.get("limit", 5))
    return {"results": [r.to_dict() for r in results]}


@router.post("/v1/skills/library/execute")
async def skill_library_execute(request: Request):
    if _request_profile_scope(request) != "marco":
        return JSONResponse(
            status_code=403,
            content={"error": "Skill library execution is restricted to the owner."},
        )
    lib = _get_skill_library()
    if lib is None: return JSONResponse(status_code=503, content={"error": "Skill library not initialized"})
    body = await request.json()
    result = lib.execute_skill(body.get("skill_id", ""), params=body.get("params"))
    return result


# ── HEARTBEAT / BRIEFING / NOTIFICATIONS ────────────────────────
@router.post("/v1/heartbeat")
async def heartbeat(request: Request):
    body = await request.json()
    a = shared_state.autonomy
    if a: a.record_heartbeat(body)
    return {"status": "ok"}

@router.get("/v1/briefing")
async def briefing(request: Request):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    return a.generate_briefing()

@router.post("/v1/briefing/send")
async def briefing_send(request: Request):
    a = shared_state.autonomy
    if a is None: return JSONResponse(status_code=503, content={"error": "Autonomia nao disponivel"})
    await a.send_briefing()
    return {"status": "ok"}

@router.get("/v1/notifications/stream")
async def notifications_stream(request: Request):
    import threading
    q: asyncio.Queue = asyncio.Queue(maxsize=32)

    # Register with the broadcast system
    with _notification_queues_lock:
        _notification_queues.append(q)

    async def _event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'timestamp': datetime.now().isoformat(), 'type': 'ping'})}\n\n"
        finally:
            with _notification_queues_lock:
                try:
                    _notification_queues.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(_event_stream(), media_type="text/event-stream")

@router.post("/v1/notifications/send")
async def notifications_send(request: Request):
    body = await request.json()
    await _broadcast_notification(body.get("message", ""), body.get("level", "info"))
    return {"status": "ok"}
