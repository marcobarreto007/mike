"""
Chat routes: /v1/chat/completions, /v1/chat/sessions, /v1/chat/history
The main chat completions handler lives in mike_server (lazy import to avoid circular deps).
"""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from mike_auth import profile_from_request

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Main chat endpoint. Delegates to mike_server handler via lazy import."""
    from mike_server import _chat_completions_handler
    return await _chat_completions_handler(request)


@router.get("/v1/chat/sessions")
async def chat_sessions(request: Request):
    """List active chat sessions."""
    from mike_server import _get_chat_sessions
    return await _get_chat_sessions(request)


@router.get("/v1/chat/history")
async def chat_history(request: Request, session_id: str = "main", limit: int = 50):
    """Get chat history for a session."""
    from mike_server import _get_chat_history
    return await _get_chat_history(request)
