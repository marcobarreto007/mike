"""
Mike family profile helpers.

Loads and formats family member profiles from ``config/family_profiles.json``
for injection into the LLM system prompt.

Extracted from mike_server.py — Phase 2 monolith breakup.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from mike_config import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_family_profiles_cache: Optional[dict] = None
_family_profiles_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _get_family_profiles_path() -> Path:
    global _family_profiles_path
    if _family_profiles_path is None:
        _family_profiles_path = PROJECT_ROOT / "config" / "family_profiles.json"
    return _family_profiles_path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_family_profiles() -> dict:
    global _family_profiles_cache
    if _family_profiles_cache is not None:
        return _family_profiles_cache
    path = _get_family_profiles_path()
    if not path.exists():
        _family_profiles_cache = {}
        return _family_profiles_cache
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        _family_profiles_cache = data.get("profiles", {})
    except Exception:
        _family_profiles_cache = {}
    return _family_profiles_cache


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def _get_family_profile(profile_key: str) -> Optional[dict]:
    profiles = _load_family_profiles()
    return profiles.get(profile_key)


# ---------------------------------------------------------------------------
# LLM formatting
# ---------------------------------------------------------------------------

def _format_family_profile_for_llm(profile: dict) -> str:
    name = profile.get("name", "alguém")
    desc = profile.get("description_short", "")
    role = profile.get("role_in_family", "")
    memory = profile.get("memory_summary_for_mike", "")
    how = profile.get("how_mike_should_treat_her", {})
    tone = how.get("tone", "respeitoso e acolhedor")
    priorities = how.get("priorities", [])
    do_nots = how.get("do_not", [])

    lines = [
        f"[PERFIL ATIVO: {name}]",
        f"ATENÇÃO: Você NÃO está falando com o Marco. Você está falando com {name}.",
        f"Quem é: {desc}",
        f"Papel na família: {role}",
        f"Resumo para você, Mike: {memory}",
        "",
        f"Tom: {tone}.",
    ]
    if priorities:
        lines.append("Prioridades:")
        for p in priorities:
            lines.append(f"  - {p}")
    if do_nots:
        lines.append("NÃO faça:")
        for d in do_nots:
            lines.append(f"  - {d}")
    lines.append("")
    lines.append(f"DIRETRIZ FINAL: Voce esta falando com {name}. NAO trate esta pessoa como Marco. "
                  f"Use o nome {name} e o tom descrito acima. Personalize TUDO para {name}.")
    return "\n".join(lines)
