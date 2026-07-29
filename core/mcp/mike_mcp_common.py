# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike MCP Common Utilities
=========================

Shared path-resolution and root-management helpers used by multiple
Mike MCP servers (workspace, excel, etc.).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


def normalize_path(path: Path) -> str:
    """Normalize a path to a canonical string for case-insensitive comparison."""
    return os.path.normcase(str(path.resolve()))


def is_inside_roots(candidate: Path, roots: Iterable[Path]) -> bool:
    """Return True if *candidate* resides under any of *roots*."""
    candidate_norm = normalize_path(candidate)
    for root in roots:
        root_norm = normalize_path(root)
        try:
            common = os.path.commonpath([candidate_norm, root_norm])
        except ValueError:
            continue
        if common == root_norm:
            return True
    return False


def resolve_safe_path(
    raw_path: str,
    allowed_roots: list[Path],
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve *raw_path* and raise if it falls outside *allowed_roots*.

    Relative paths are resolved against *allowed_roots[0]*.
    """
    candidate = Path(raw_path.strip() if raw_path else ".")
    if not candidate.is_absolute():
        candidate = (allowed_roots[0] / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if not is_inside_roots(candidate, allowed_roots):
        raise ValueError(f"Path outside allowed roots: {candidate}")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"Path does not exist: {candidate}")
    return candidate


def collect_allowed_roots(
    argv: list[str],
    *,
    default_roots: Optional[list[Path]] = None,
    validate_cli_args: bool = False,
) -> list[Path]:
    """Collect allowed root directories from CLI args, env var, or defaults.

    Resolution order:

    1. CLI arguments (optionally validated to exist on disk)
    2. ``MIKE_MCP_ALLOWED_ROOTS`` environment variable
    3. *default_roots* when provided
    4. Raise :exc:`SystemExit` if nothing is configured

    Args:
        argv: CLI arguments (script name already stripped).
        default_roots: Fallback when nothing is specified via args or env.
        validate_cli_args: If ``True``, only keep CLI args whose resolved
            path actually exists on disk (Excel MCP behaviour).

    Returns:
        List of resolved :class:`Path` objects.

    Raises:
        SystemExit: If no roots are configured and *default_roots* is
            ``None``.
    """
    raw_roots: list[str] = []
    for part in argv:
        value = part.strip()
        if not value:
            continue
        if validate_cli_args:
            candidate = Path(value).expanduser()
            resolved = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
            if not resolved.exists():
                continue
        raw_roots.append(value)

    if not raw_roots:
        env_roots = os.getenv("MIKE_MCP_ALLOWED_ROOTS", "")
        raw_roots = [part.strip() for part in env_roots.split(os.pathsep) if part.strip()]

    if not raw_roots and default_roots:
        raw_roots = [str(root) for root in default_roots]

    roots = [Path(root).expanduser().resolve() for root in raw_roots]

    if not roots:
        raise SystemExit(
            "MCP server requires at least one allowed root. "
            "Pass directories as CLI args or set MIKE_MCP_ALLOWED_ROOTS."
        )

    return roots
