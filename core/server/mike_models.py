"""
Mike request/response Pydantic models.

Extracted from mike_server.py — Phase 1 monolith breakup.
"""
from __future__ import annotations

from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field

from mike_config import DEFAULT_MAX_TOKENS, MODEL_ALIAS
from mike_payloads import error_payload as _error_payload


# ---------------------------------------------------------------------------
# Vision error (thin exception with API payload)
# ---------------------------------------------------------------------------

class VisionInputError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "vision_invalid_input",
        status_code: int = 400,
        details: Optional[dict] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}

    def payload(self) -> dict:
        return _error_payload(self.message, self.code, self.details)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Any]]


class ChatRequest(BaseModel):
    model: str = MODEL_ALIAS
    messages: List[ChatMessage]
    max_tokens: Optional[int] = DEFAULT_MAX_TOKENS
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.95
    stream: Optional[bool] = False
    stop: Optional[List[str]] = None
    session_id: Optional[str] = "main"
    private_mode: Optional[bool] = False
    raw_mode: Optional[bool] = False


class ProfileLoginRequest(BaseModel):
    profile: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


class MagicLinkGenerateRequest(BaseModel):
    profile: str
    ttl_days: Optional[int] = None


class MagicLinkUseRequest(BaseModel):
    token: str


class MagicLinkRevokeRequest(BaseModel):
    token: str


class ManualToolCallRequest(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)
    parameters: Optional[dict] = None

    def resolved_arguments(self) -> dict:
        args = self.arguments or {}
        legacy = self.parameters or {}
        if args and legacy and args != legacy:
            raise ValueError("Use apenas 'arguments' ou o legado 'parameters', nao ambos.")
        return args or legacy or {}


class KnowledgeUpsertRequest(BaseModel):
    path: str
    enable_vector: bool = True
    enable_lightrag: bool = True
