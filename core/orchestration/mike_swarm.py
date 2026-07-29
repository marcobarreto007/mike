# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Swarm — Parallel Agent Coordination
=========================================

Parallel-aware step detection for TaskMesh integration.
"""

from __future__ import annotations

import logging
import re
from typing import Dict

log = logging.getLogger(__name__)


# ======================================================================
# Parallel-aware step detection (for TaskMesh integration)
# ======================================================================

def detect_parallel_groups(steps: list[dict]) -> list[list[int]]:
    """Analyze task steps and group independent ones for parallel execution.

    Returns groups of step indices. Steps in the same group can run in parallel.
    Steps in different groups must run sequentially.

    Uses simple heuristics:
    - Steps that reference previous step results are sequential
    - Steps that share tool names may conflict
    - Steps with "after", "using result of", "based on" are dependent
    """
    n = len(steps)
    if n <= 1:
        return [[0]] if n == 1 else []

    # Build dependency graph
    deps: Dict[int, set] = {i: set() for i in range(n)}

    _DEP_PATTERNS = [
        r"(?:usando|com\s+o|baseado|a\s+partir|resultado|using|based\s+on|from)\s+(?:passo|step|etapa)\s*(\d+)",
        r"(?:após|depois\s+d[eo]|after)\s+(?:passo|step|etapa)\s*(\d+)",
        r"(?:passo|step|etapa)\s*(\d+)\s+(?:acima|anterior|previous)",
    ]

    for i, step in enumerate(steps):
        desc = step.get("description", "").lower()
        for pat in _DEP_PATTERNS:
            for m in re.finditer(pat, desc, re.IGNORECASE):
                dep_id = int(m.group(1)) - 1  # 0-indexed
                if 0 <= dep_id < n and dep_id != i:
                    deps[i].add(dep_id)

        # Implicit sequential: if step mentions output/result of earlier steps
        if i > 0 and re.search(r"(?:resultado|output|anterior|acima|previous)", desc):
            deps[i].add(i - 1)

    # Group into parallel batches using topological sort
    groups: list[list[int]] = []
    completed: set = set()
    remaining = set(range(n))

    while remaining:
        # Find all steps whose dependencies are satisfied
        ready = [i for i in remaining if deps[i].issubset(completed)]
        if not ready:
            # Break cycle: force the first remaining step
            ready = [min(remaining)]

        groups.append(sorted(ready))
        completed.update(ready)
        remaining -= set(ready)

    return groups
