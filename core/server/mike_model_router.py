# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Dynamic Model Router for Mike -- selects the best LLM backend per request.

Routes based on:
  - Request complexity (simple / normal / complex)
  - Backend health (is_healthy / set_unhealthy)
  - User model override (ChatRequest.model field)
  - Env-configured priority list (MIKE_LLM_BACKENDS)
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("mike.model_router")

# ---------------------------------------------------------------------------
# Complexity estimation keywords
# ---------------------------------------------------------------------------

_COMPLEX_KEYWORDS = [
    "analise", "analisar", "analise", "analyze", "analyzing",
    "planeje", "planejar", "planejamento", "plan", "planning",
    "implemente", "implementar", "implementacao", "implement", "implementation",
    "crie", "criar", "criacao", "create", "creating",
    "complexo", "complexa", "complex",
    "arquitetura", "architecture",
    "refatore", "refatorar", "refactor", "refactoring",
    "debug", "debugar", "depurar", "debugging",
    "otimize", "otimizar", "optimize", "optimization",
    "resolva", "resolver", "solve", "resolving",
    "explique", "explicar", "explain", "explaining",
    "documente", "documentar",
    "compile", "compilar", "compilation",
    "estrategia", "strategy",
    "detalhado", "detalhada", "detailed",
    "completo", "completa", "complete",
]


# ---------------------------------------------------------------------------
# MikeModelRouter
# ---------------------------------------------------------------------------

class MikeModelRouter:
    """Selects which LLM backend to use for each request.

    Reads MIKE_LLM_BACKENDS for priority ordering (e.g. "mock,deepseek,local").
    Tracks backend health and supports manual unhealthy marking.

    Usage:
        router = MikeModelRouter(backends={"deepseek": client, "mock": mock})
        backend_name = router.select_backend("complex")
        client = router.get_backend(backend_name)
    """

    def __init__(
        self,
        backends: Dict[str, Any],
        priority_order: Optional[List[str]] = None,
    ):
        """
        Args:
            backends: Dict mapping backend name to client instance.
            priority_order: Ordered list of preferred backends (first = highest
                            priority). If None, reads MIKE_LLM_BACKENDS env var.
        """
        self._backends: Dict[str, Any] = dict(backends)
        self._health: Dict[str, bool] = {name: True for name in backends}

        if priority_order is None:
            raw = os.getenv("MIKE_LLM_BACKENDS", "").strip()
            if raw:
                priority_order = [b.strip() for b in raw.split(",") if b.strip()]
            else:
                priority_order = list(backends.keys())

        self._priority: List[str] = [p for p in priority_order if p in self._backends]
        # Append any backends not in the explicit priority list
        for name in sorted(self._backends):
            if name not in self._priority:
                self._priority.append(name)

        self._default: str = self._priority[0] if self._priority else ""

        if self._priority:
            log.info(
                "Model router initialized: priority=%s healthy=%s",
                self._priority,
                self.healthy_backends,
            )

    # -- Properties -------------------------------------------------------

    @property
    def backends(self) -> Dict[str, Any]:
        return dict(self._backends)

    @property
    def priority(self) -> List[str]:
        return list(self._priority)

    @property
    def healthy_backends(self) -> List[str]:
        """Return priority-ordered list of healthy backend names."""
        return [
            name
            for name in self._priority
            if self._health.get(name, False) and name in self._backends
        ]

    # -- Complexity estimation --------------------------------------------

    @staticmethod
    def estimate_complexity(messages: list) -> str:
        """Score request complexity based on message content.

        Factors:
          - Message count
          - Estimated token count (~4 chars/token)
          - Presence of complexity keywords (analyze, planeje, implemente, crie, etc.)

        Returns:
            "simple", "normal", or "complex"
        """
        if not messages:
            return "simple"

        # Collect all text
        all_text = " ".join(
            (m.get("content", "") if isinstance(m.get("content"), str) else "")
            if isinstance(m, dict)
            else ""
            for m in messages
        ).lower()

        # Estimate token count
        token_estimate = sum(
            len(
                m.get("content", "")
                if isinstance(m.get("content"), str)
                else ""
            )
            // 4
            for m in messages
            if isinstance(m, dict)
        )

        # Count complexity keywords
        complex_score = sum(1 for kw in _COMPLEX_KEYWORDS if kw in all_text)

        # Count messages
        msg_count = len(messages)

        # Thresholds (cumulative — first match wins)
        if complex_score >= 3 or token_estimate > 8000 or msg_count > 20:
            return "complex"
        if complex_score >= 1 or token_estimate > 3000 or msg_count > 10:
            return "normal"
        return "simple"

    # -- Routing ----------------------------------------------------------

    def select_backend(self, requested_complexity: str = "normal") -> str:
        """Select the best available backend for the requested complexity.

        Complexity logic:
          - "complex"  → highest priority healthy backend
          - "normal"   → first healthy backend
          - "simple"   → cheapest healthy backend (lowest priority)

        Args:
            requested_complexity: "simple", "normal", or "complex"

        Returns:
            Backend name string.
        """
        healthy = self.healthy_backends

        if not healthy:
            log.error(
                "No healthy backends available! Falling back to '%s'.",
                self._default,
            )
            return self._default or self._priority[0]

        if requested_complexity == "complex":
            # Best available: highest priority that is healthy
            return healthy[0]

        if requested_complexity == "simple":
            # Cheapest available: lowest priority that is healthy
            if len(healthy) > 1:
                return healthy[-1]
            return healthy[0]

        # "normal" — first available
        return healthy[0]

    def get_backend(self, name: str) -> Any:
        """Return the client instance for *name*, or None if not registered."""
        return self._backends.get(name)

    def resolve_backend(
        self,
        messages: list,
        model_override: Optional[str] = None,
    ) -> Tuple[str, Any]:
        """Full resolution pipeline: override > complexity > router.

        Args:
            messages: Chat messages list (used for complexity estimation).
            model_override: Explicit model requested by user (e.g. "mock").

        Returns:
            Tuple of (backend_name, client_instance). Client may be None
            if no backend is available.
        """
        # 1. User explicitly requested a model → use it directly
        if model_override:
            client = self.get_backend(model_override)
            if client is not None:
                return (model_override, client)
            log.debug(
                "User requested unknown model '%s'; falling back to router.",
                model_override,
            )

        # 2. Estimate complexity and select best backend
        complexity = self.estimate_complexity(messages)
        name = self.select_backend(complexity)
        client = self.get_backend(name)

        if client is None:
            # 3. Last resort: try any healthy backend
            healthy = self.healthy_backends
            if healthy:
                name = healthy[0]
                client = self.get_backend(name)
                if client is not None:
                    return (name, client)

        return (name or self._default, client)

    # -- Health tracking --------------------------------------------------

    def is_healthy(self, name: str) -> bool:
        """Check whether a backend is currently marked healthy."""
        if name not in self._backends:
            return False
        return self._health.get(name, False)

    def set_unhealthy(self, name: str) -> None:
        """Mark a backend as temporarily unhealthy."""
        if name in self._health:
            self._health[name] = False
            log.warning("Backend '%s' marked UNHEALTHY.", name)
        else:
            log.debug("set_unhealthy: unknown backend '%s'", name)

    def set_healthy(self, name: str) -> None:
        """Restore a backend to healthy status."""
        if name in self._health:
            self._health[name] = True
            log.info("Backend '%s' marked HEALTHY.", name)
        else:
            log.debug("set_healthy: unknown backend '%s'", name)

    def health_status(self) -> Dict[str, bool]:
        """Return dict mapping backend names to health status."""
        return dict(self._health)
