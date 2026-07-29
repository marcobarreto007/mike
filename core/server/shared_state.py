"""
Shared mutable state for Mike server.
Populated by boot.py at startup, imported by routers.

All values start as None/empty — safe to import before initialization.

Thread-safety: use set_shared() / get_shared() for runtime mutations that may
race across coroutines. Direct attribute access (e.g., shared_state.llm) is
deprecated for writes but remains available for reads during startup.
"""
import asyncio
from typing import Any, Dict, List, Optional

# ── Concurrency guard ──────────────────────────────────────────────────
# Async lock for runtime mutations. Use set_shared() / get_shared() instead
# of direct attribute assignment in hot paths (streaming, request handling).
_lock = asyncio.Lock()

# ── LLM ──
llm: Any = None
llm_lock: Any = None       # threading.Lock, populated by mike_server
vision_handler: Any = None  # Gemma4VisionChatHandler / native handler
deepseek_client: Any = None

# ── Services ──
mcp_workspace: Any = None
memory_service: Any = None
web_search: Any = None
task_mesh: Any = None
autonomy: Any = None
monitor: Any = None
learner: Any = None
governance: Any = None
consciousness: Any = None
verifier: Any = None
missions: Any = None
skill_registry: Any = None
reflection_store: Any = None  # EpisodicReflectionStore (Reflexion pattern)
skill_library: Any = None     # SkillLibrary (Voyager pattern)
virtual_context: Any = None   # VirtualContextManager (MemGPT pattern)
curriculum: Any = None        # AutoCurriculum (Voyager pattern)
tool_analyzer: Any = None     # ToolFailureAnalyzer
output_guard: Any = None      # OutputGuard (anti-simulation sentinel)
event_bus: Any = None         # MikeEventBus (event-driven autonomy)
_cached_sdk_generate: Any = None     # Cached closure from _make_sdk_generate_fn()
_cached_agent_registry: Any = None   # Cached AgentRegistry (default "marco" profile)
model_router: Any = None     # MikeModelRouter (dynamic backend selection)
fallback_chain: Any = None   # FallbackChain (resilience layer with circuit breakers)

# ── Prompts (built at boot from soul.json) ──
SYSTEM_PROMPT: str = ""
SOUL_PROMPT: str = ""
SOUL: Dict[str, Any] = {}

# ── Runtime state ──
stats: Dict[str, Any] = {}
log: Any = None
ready: bool = False  # True when full startup (LLM, MCP, background tasks) is complete

# ── MCP config ──
mcp_server_configs: List[dict] = []
mcp_allowed_roots: List[Any] = []


# ── Thread-safe accessors ──────────────────────────────────────────────
# Prefer these over direct attribute access for runtime mutations (streaming,
# tool execution, request handling). Startup-only writes (boot.py) may still
# assign directly — those are single-threaded by construction.

async def set_shared(key: str, value: Any) -> None:
    """Atomically set a shared-state attribute.

    Usage:
        await set_shared("llm", new_llm_instance)
    """
    async with _lock:
        setattr(_module(), key, value)


async def get_shared(key: str, default: Any = None) -> Any:
    """Atomically read a shared-state attribute.

    Usage:
        llm = await get_shared("llm")
    """
    async with _lock:
        return getattr(_module(), key, default)


def _module():
    """Return a reference to this module so setattr/getattr work correctly."""
    import sys
    return sys.modules[__name__]
