"""
Health routes: /v1/health/models — per-backend circuit breaker status.

The fallback chain (if configured) is read from shared_state at call time,
so the endpoint always reflects the current live state.
"""

from fastapi import APIRouter

import shared_state

router = APIRouter()


@router.get("/v1/health/models")
async def health_models():
    """
    Returns per-backend health including circuit-breaker state.

    Response shape::

        {
            "backends": {
                "mock":      {"healthy": true, "circuit": "closed", "failures": 0},
                "deepseek":  {"healthy": true, "circuit": "closed", "failures": 0},
                "local":     {"healthy": false, "circuit": "open", "failures": 5},
            },
            "active_backend": "mock",
            "fallback_chain_order": ["mock", "deepseek", "local"],
        }
    """
    fallback_chain = shared_state.fallback_chain

    if fallback_chain is None:
        return {
            "backends": {},
            "active_backend": None,
            "fallback_chain_order": [],
            "note": "Fallback chain not configured.",
        }

    return fallback_chain.get_status()
