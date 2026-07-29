# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.
# Extracted from mike_server.py — Phase 3 refactor

"""
Mike - TTS (Text-to-Speech) Routes
====================================
Edge-TTS synthesis endpoint for generating speech audio from text.
"""

import logging
import os
from typing import Optional

from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTS Constants
# ---------------------------------------------------------------------------

TTS_VOICE = os.getenv("MIKE_TTS_VOICE", "pt-BR-AntonioNeural")
TTS_RATE = os.getenv("MIKE_TTS_RATE", "+0%")
TTS_MAX_CHARS = 4000


# ---------------------------------------------------------------------------
# TTS Request Model
# ---------------------------------------------------------------------------

class TtsRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    rate: Optional[str] = None


# ---------------------------------------------------------------------------
# TTS Route Handler
# Note: the @app.post("/v1/tts") decorator is applied in mike_server.py
# ---------------------------------------------------------------------------

async def tts_synthesize(req: TtsRequest):
    text = (req.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "Texto vazio."})
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]

    voice = (req.voice or TTS_VOICE).strip() or TTS_VOICE
    rate = (req.rate or TTS_RATE).strip() or TTS_RATE

    try:
        import edge_tts
    except ImportError:
        return JSONResponse(
            status_code=503,
            content={"error": "edge-tts nao esta instalado."},
        )

    async def audio_stream():
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]

    return StreamingResponse(
        audio_stream(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="mike_tts.mp3"',
        },
    )
