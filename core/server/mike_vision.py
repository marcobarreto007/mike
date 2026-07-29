"""
Mike vision / multimodal helpers.

Pure functions for data URI validation, image extraction,
PIL inspection, and vision message construction.

Extracted from mike_server.py — Phase 2 monolith breakup.
"""
from __future__ import annotations

import base64
import binascii
import re
from io import BytesIO
from typing import Any, List, Optional

from mike_config import (
    VISION_ALLOWED_MIME_TYPES,
    VISION_MAX_DECODED_BYTES,
    VISION_MAX_IMAGES,
)
from mike_models import ChatRequest, VisionInputError
from mike_stats import _vision_limits, stats


# ---------------------------------------------------------------------------
# Data URI regex (shared)
# ---------------------------------------------------------------------------

_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[-\w.+]+/[-\w.+]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

def _image_url_from_part(part: dict) -> Optional[str]:
    image_url = part.get("image_url")
    if isinstance(image_url, str):
        return image_url
    if isinstance(image_url, dict):
        return image_url.get("url")
    return None


def _extract_image_parts(messages) -> List[dict]:
    parts: List[dict] = []
    for message_index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                parts.append({
                    "message_index": message_index,
                    "part_index": part_index,
                    "url": _image_url_from_part(part),
                })
    return parts


def _has_images(messages) -> bool:
    """Retorna True se alguma mensagem contem image_url (visao)."""
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


# ---------------------------------------------------------------------------
# Data URI decode + image inspection
# ---------------------------------------------------------------------------

def _decode_data_uri_image(url: Optional[str]) -> tuple[str, bytes]:
    if not isinstance(url, str) or not url.strip():
        raise VisionInputError(
            "Imagem invalida: faltou image_url.url.",
            code="vision_missing_url",
            details=_vision_limits(),
        )
    match = _DATA_URI_RE.match(url.strip())
    if not match:
        raise VisionInputError(
            "Formato de imagem invalido. Envie a foto como data URL base64.",
            code="vision_invalid_format",
            details=_vision_limits(),
        )
    mime_type = match.group("mime").lower()
    if mime_type not in VISION_ALLOWED_MIME_TYPES:
        raise VisionInputError(
            "Formato de imagem nao suportado. Use JPG, PNG ou WebP.",
            code="vision_unsupported_mime",
            details={**_vision_limits(), "mime_type": mime_type},
        )
    encoded = re.sub(r"\s+", "", match.group("data"))
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VisionInputError(
            "Imagem corrompida ou base64 invalida.",
            code="vision_invalid_base64",
            details={**_vision_limits(), "mime_type": mime_type},
        ) from exc
    return mime_type, decoded


def _inspect_decoded_image(mime_type: str, decoded: bytes) -> None:
    try:
        from PIL import Image, UnidentifiedImageError

        with Image.open(BytesIO(decoded)) as image:
            image.load()
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise VisionInputError(
            "A foto parece corrompida ou invalida. Envie uma imagem real em JPG, PNG ou WebP.",
            code="vision_invalid_image_data",
            details={**_vision_limits(), "mime_type": mime_type},
        ) from exc

    if width < 2 or height < 2:
        raise VisionInputError(
            "A foto ficou pequena demais para o modo visao do Mike. Envie uma imagem com pelo menos 2x2 pixels.",
            code="vision_image_too_small",
            details={
                **_vision_limits(),
                "mime_type": mime_type,
                "width": width,
                "height": height,
            },
        )


# ---------------------------------------------------------------------------
# Vision validation
# ---------------------------------------------------------------------------

def _validate_vision_messages(messages) -> dict:
    image_parts = _extract_image_parts(messages)
    if not image_parts:
        stats["last_vision_image_count"] = 0
        stats["last_vision_decoded_bytes"] = 0
        return {"image_count": 0, "decoded_bytes": 0, "mime_types": []}

    if len(image_parts) > VISION_MAX_IMAGES:
        raise VisionInputError(
            f"O Mike aceita {VISION_MAX_IMAGES} foto por vez por enquanto.",
            code="vision_too_many_images",
            details={**_vision_limits(), "image_count": len(image_parts)},
        )

    total_decoded_bytes = 0
    mime_types: List[str] = []

    for image_part in image_parts:
        mime_type, decoded = _decode_data_uri_image(image_part["url"])
        decoded_size = len(decoded)
        if decoded_size > VISION_MAX_DECODED_BYTES:
            raise VisionInputError(
                "A foto ficou grande demais para o Mike processar com seguranca. Tente enviar uma imagem menor.",
                code="vision_payload_too_large",
                details={
                    **_vision_limits(),
                    "image_count": len(image_parts),
                    "decoded_bytes": decoded_size,
                    "mime_type": mime_type,
                },
            )
        _inspect_decoded_image(mime_type, decoded)
        total_decoded_bytes += decoded_size
        mime_types.append(mime_type)

    if total_decoded_bytes > VISION_MAX_DECODED_BYTES:
        raise VisionInputError(
            "As fotos enviadas excederam o limite seguro de memoria para visao.",
            code="vision_payload_too_large",
            details={
                **_vision_limits(),
                "image_count": len(image_parts),
                "decoded_bytes": total_decoded_bytes,
            },
        )

    stats["last_vision_image_count"] = len(image_parts)
    stats["last_vision_decoded_bytes"] = total_decoded_bytes
    return {
        "image_count": len(image_parts),
        "decoded_bytes": total_decoded_bytes,
        "mime_types": mime_types,
    }


# ---------------------------------------------------------------------------
# Vision messages builder
# ---------------------------------------------------------------------------

_VISION_SYSTEM_PROMPT = (
    "Voce e Mike, um assistente multimodal da familia Barreto. "
    "Responda no idioma do usuario, analise fotos com precisao e seja direto. "
    "Se a imagem nao permitir concluir algo com seguranca, diga isso claramente."
)


def _build_vision_messages(req: ChatRequest) -> List[dict]:
    from mike_web import _clean_query as _web_clean

    compact_messages: List[dict] = [{"role": "system", "content": _VISION_SYSTEM_PROMPT}]
    history = req.messages[-4:] if len(req.messages) > 4 else req.messages

    for message in history:
        content = message.content
        if isinstance(content, str):
            compact_content = _web_clean(content) if message.role == "user" else content
        elif isinstance(content, list):
            compact_content = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    compact_content.append({
                        "type": "text",
                        "text": _web_clean(part.get("text", "")),
                    })
                else:
                    compact_content.append(part)
        else:
            compact_content = content
        compact_messages.append({"role": message.role, "content": compact_content})
    return compact_messages


# ---------------------------------------------------------------------------
# Vision stop sequences
# ---------------------------------------------------------------------------

def _vision_stop_sequences(req) -> Optional[List[str]]:
    stop = list(getattr(req, "stop", None) or [])
    if stats.get("vision_handler_backend") == "native-gemma4":
        return stop or None
    return stop + ["<end_of_turn>", "<start_of_turn>"]
