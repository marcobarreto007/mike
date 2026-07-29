"""
Memory routes: /v1/memory/*, /v1/knowledge/search, checkpoints, session summaries.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mike_config import RAG_ENABLED, MEMORY_TOP_K, KNOWLEDGE_TOP_K
from mike_auth import profile_from_request, scoped_session_id
import shared_state

router = APIRouter()

def _ms():
    """Access memory_service at call time (not import time)."""
    return shared_state.memory_service


def _belongs_to_profile(session_id: Optional[str], profile_key: Optional[str]) -> bool:
    """Return whether a stored session is inside the authenticated namespace."""
    if not profile_key:
        # Local/API-key callers are trusted by the outer authentication layer.
        return True
    normalized = str(session_id or "").strip().lower()
    return normalized == profile_key or normalized.startswith(f"{profile_key}-")


def _conversation_session_id(conversation_id: int) -> Optional[str]:
    ms = _ms()
    if ms is None:
        return None
    with ms.local._connect() as conn:
        row = conn.execute(
            "SELECT session_id FROM conversations WHERE id = ?",
            (int(conversation_id),),
        ).fetchone()
    return str(row["session_id"]) if row else None


# ── Memory search / add ─────────────────────────────────────────
@router.get("/v1/memory/search")
async def search_memory(
    request: Request, q: str, limit: int = 5, session_id: Optional[str] = None
):
    profile_key = profile_from_request(request)
    # An authenticated user may search only one session in their own
    # namespace. Defaulting to their main session avoids the global vector
    # search path, which otherwise mixes every profile.
    scoped = (
        scoped_session_id(session_id or "main", profile_key)
        if profile_key
        else (scoped_session_id(session_id, None) if session_id else None)
    )
    ms = _ms()
    if ms is None:
        return JSONResponse(status_code=503, content={"error": "Memory service is not available"})
    return {
        "query": q,
        "session_id": scoped,
        "results": [
            {
                "source": hit.source,
                "title": hit.title,
                "content": hit.content,
                "metadata": hit.metadata,
            }
            for hit in ms.search_memories(
                q, limit=limit, session_id=scoped, session_only=bool(scoped)
            )
        ],
    }


@router.post("/v1/memory/add")
async def memory_add(request: Request):
    profile_key = profile_from_request(request)
    body = await request.json()
    user_text = body.get("content", "").strip()
    if not user_text:
        return JSONResponse(status_code=400, content={"error": "content is required"})
    session_id = body.get("session_id", "main")
    scoped = scoped_session_id(session_id, profile_key)
    try:
        ts = datetime.now(timezone.utc).isoformat()
        assistant_text = body.get("assistant_text", "")
        _ms().add_conversation(
            timestamp=ts,
            user_text=user_text,
            assistant_text=assistant_text,
            session_id=scoped or "main",
            promote_long_term=True,
        )
        return {"status": "ok", "added": user_text[:80]}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ── Memory Mesh ─────────────────────────────────────────────────
@router.get("/v1/memory/mesh/neighbors")
async def mesh_neighbors(request: Request, conversation_id: int, limit: int = 10):
    profile_key = profile_from_request(request)
    if not _belongs_to_profile(_conversation_session_id(conversation_id), profile_key):
        return JSONResponse(status_code=404, content={"error": "Conversation not found"})
    return {"conversation_id": conversation_id, "neighbors": _ms().mesh_neighbors(conversation_id, limit=limit)}


@router.get("/v1/memory/mesh/stats")
async def mesh_stats(request: Request):
    profile_key = profile_from_request(request)
    if profile_key and profile_key not in {"marco", "anapaula"}:
        return JSONResponse(status_code=403, content={"error": "Owner profile required"})
    return _ms().mesh_stats()


@router.post("/v1/memory/mesh/link")
async def mesh_link(request: Request):
    profile_key = profile_from_request(request)
    body = await request.json()
    source_id = body.get("source_id")
    target_id = body.get("target_id")
    if not source_id or not target_id:
        return JSONResponse(status_code=400, content={"error": "source_id and target_id are required"})
    if (
        not _belongs_to_profile(_conversation_session_id(int(source_id)), profile_key)
        or not _belongs_to_profile(_conversation_session_id(int(target_id)), profile_key)
    ):
        return JSONResponse(status_code=404, content={"error": "Conversation not found"})
    relation = body.get("relation", "similar")
    strength = float(body.get("strength", 0.8))
    ok = _ms().local.mesh_link(int(source_id), int(target_id), relation, strength)
    return {"status": "ok" if ok else "error", "source_id": source_id, "target_id": target_id}


# ── Checkpoints ─────────────────────────────────────────────────
@router.post("/v1/memory/checkpoint/save")
async def checkpoint_save(request: Request):
    profile_key = profile_from_request(request)
    body = await request.json()
    session_id = body.get("session_id", "main")
    scoped = scoped_session_id(session_id, profile_key)
    label = body.get("label")
    metadata = body.get("metadata")
    checkpoint_id = _ms().checkpoint_save(scoped, label=label, metadata=metadata)
    return {"status": "ok", "checkpoint_id": checkpoint_id}


@router.get("/v1/memory/checkpoint/list")
async def checkpoint_list(
    request: Request,
    session_id: Optional[str] = None,
    limit: int = 20,
):
    profile_key = profile_from_request(request)
    scoped = scoped_session_id(session_id, profile_key) if session_id else None
    return {
        "checkpoints": _ms().checkpoint_list(
            session_id=scoped, profile=profile_key, limit=limit
        )
    }


@router.post("/v1/memory/checkpoint/restore")
async def checkpoint_restore(request: Request):
    profile_key = profile_from_request(request)
    body = await request.json()
    checkpoint_id = body.get("checkpoint_id", "").strip()
    if not checkpoint_id:
        return JSONResponse(status_code=400, content={"error": "checkpoint_id is required"})
    result = _ms().checkpoint_restore(checkpoint_id)
    if not result:
        return JSONResponse(status_code=404, content={"error": "Checkpoint not found"})
    if not _belongs_to_profile(result.get("session_id"), profile_key):
        return JSONResponse(status_code=404, content={"error": "Checkpoint not found"})
    return result


# ── Session summaries ───────────────────────────────────────────
@router.post("/v1/memory/session/summary")
async def session_summary_save(request: Request):
    profile_key = profile_from_request(request)
    body = await request.json()
    session_id = body.get("session_id", "main")
    scoped = scoped_session_id(session_id, profile_key)
    summary = body.get("summary", "").strip()
    if not summary:
        return JSONResponse(status_code=400, content={"error": "summary is required"})
    topics = body.get("topics", [])
    ok = _ms().session_summary_save(scoped, summary, topics=topics)
    return {"status": "ok" if ok else "error", "session_id": scoped}


@router.get("/v1/memory/session/summaries")
async def session_summaries_recent(
    request: Request,
    limit: int = 5,
    profile: Optional[str] = None,
    session_id: Optional[str] = None,
):
    authenticated_profile = profile_from_request(request)
    profile_key = authenticated_profile or profile or "default"
    return {
        "profile": profile_key,
        "summaries": _ms().session_summaries_recent(profile_key, limit=limit),
    }


# ── Knowledge ───────────────────────────────────────────────────
@router.get("/v1/knowledge/search")
async def search_knowledge(q: str, limit: int = 5):
    return {
        "query": q,
        "results": [
            {
                "source": hit.source,
                "title": hit.title,
                "content": hit.content,
                "metadata": hit.metadata,
            }
            for hit in _ms().search_knowledge(q, limit=limit)
        ],
    }
