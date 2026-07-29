# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike - Configuration Module
============================
Environment loading, runtime profile resolution, GPU detection,
and all project-wide constants.
"""
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("mike.config")


_FILE_LOADED_KEYS: set[str] = set()

# ---------------------------------------------------------------------------
# Runtime overrides — in-memory values that take priority over os.environ
# without touching any filesystem. Safe for concurrent readers.
# ---------------------------------------------------------------------------

_RUNTIME_OVERRIDES: dict[str, str] = {}


def set_runtime_override(key: str, value: str) -> None:
    """Set an in-memory override that shadows os.environ for the given key."""
    _RUNTIME_OVERRIDES[key] = value


def get_runtime_override(key: str) -> Optional[str]:
    """Return the override value for *key*, or None if no override exists."""
    return _RUNTIME_OVERRIDES.get(key)


def clear_runtime_override(key: str) -> None:
    """Remove an in-memory override, restoring os.environ as the source."""
    _RUNTIME_OVERRIDES.pop(key, None)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (key not in os.environ or key in _FILE_LOADED_KEYS):
            os.environ[key] = value
            _FILE_LOADED_KEYS.add(key)


def env_bool(name: str, default: bool) -> bool:
    value = _RUNTIME_OVERRIDES.get(name)
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = _RUNTIME_OVERRIDES.get(name)
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = _RUNTIME_OVERRIDES.get(name)
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def normalize_session_id(raw: Optional[str]) -> str:
    candidate = (raw or "main").strip().lower()
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", candidate).strip("-")
    return normalized[:64] or "main"


def is_public_bind_host(host: Optional[str]) -> bool:
    normalized = (host or "").strip().lower().strip("[]")
    if not normalized:
        return False
    if normalized in {"localhost"}:
        return False
    if normalized in {"0.0.0.0", "::", "*"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return True
    return not address.is_loopback


def cors_origins_from_env(port: int) -> List[str]:
    raw = os.getenv("MIKE_CORS_ORIGINS")
    if raw:
        if raw.strip() == "*":
            return ["*"]
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [
        "http://127.0.0.1",
        "http://localhost",
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "https://localhost",
        "capacitor://localhost",
        "http://localhost:8100",
    ]


_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Z0-9_]+)\}", re.IGNORECASE)


def _expand_env_templates(value):
    if isinstance(value, str):
        return _ENV_TEMPLATE_RE.sub(lambda match: os.getenv(match.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env_templates(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _expand_env_templates(item)
            for key, item in value.items()
        }
    return value


def _normalize_mcp_arg_path(project_root: Path, raw_arg: str) -> str:
    value = _expand_env_templates(raw_arg)
    if not isinstance(value, str):
        return value
    candidate = Path(value)
    if candidate.suffix.lower() != ".py":
        return value
    if candidate.is_absolute():
        if candidate.exists():
            return str(candidate.resolve())
        src_fallback = (project_root / "src" / candidate.name).resolve()
        return str(src_fallback if src_fallback.exists() else candidate)

    relative_candidate = (project_root / candidate).resolve()
    if relative_candidate.exists():
        return str(relative_candidate)

    src_fallback = (project_root / "src" / candidate.name).resolve()
    if src_fallback.exists():
        return str(src_fallback)
    return str(relative_candidate)


def mcp_servers_from_env(project_root: Path) -> List[dict]:
    raw_json = os.getenv("MIKE_MCP_SERVERS_JSON", "").strip()
    raw_file = os.getenv("MIKE_MCP_SERVERS_FILE", "").strip()

    payload = ""
    if raw_json:
        payload = raw_json
    else:
        candidate_path = None
        if raw_file:
            candidate_path = Path(raw_file)
            if not candidate_path.is_absolute():
                candidate_path = (project_root / candidate_path).resolve()
        else:
            default_file = project_root / "config" / "mcp_servers.json"
            if default_file.exists():
                candidate_path = default_file
        if candidate_path and candidate_path.exists():
            payload = candidate_path.read_text(encoding="utf-8-sig")

    if not payload:
        return []

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    normalized: List[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        url = str(item.get("url") or "").strip()
        args = item.get("args") or []
        if not name or (not command and not url) or not isinstance(args, list):
            continue
        env_map = item.get("env") if isinstance(item.get("env"), dict) else {}
        cwd = item.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            cwd_path = Path(_expand_env_templates(cwd.strip()))
            cwd = str((project_root / cwd_path).resolve()) if not cwd_path.is_absolute() else str(cwd_path.resolve())
        else:
            cwd = None
        capability_list = item.get("capabilities") if isinstance(item.get("capabilities"), list) else []
        normalized.append({
            "name": name,
            "command": _expand_env_templates(command),
            "url": _expand_env_templates(url),
            "transport": str(item.get("transport") or ("streamable-http" if url else "stdio")).strip().lower(),
            "args": [
                _normalize_mcp_arg_path(project_root, str(arg))
                for arg in args
            ],
            "env": _expand_env_templates({str(k): str(v) for k, v in env_map.items()}),
            "headers": _expand_env_templates({
                str(k): str(v)
                for k, v in (item.get("headers") or {}).items()
            }) if isinstance(item.get("headers"), dict) else {},
            "cwd": cwd,
            "enabled": bool(item.get("enabled", True)),
            "tool_prefix": str(item.get("tool_prefix") or name).strip() or name,
            "capabilities": [
                str(cap).strip().lower()
                for cap in capability_list
                if str(cap).strip()
            ],
            "access": str(item.get("access") or "owner").strip().lower() or "owner",
        })
    return normalized


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------

def setup_cuda_dll_path():
    """Add NVIDIA pip package DLLs to the search path for Windows."""
    if sys.platform != "win32":
        return
    
    # Common locations for nvidia-* pip packages DLLs
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    if not site_packages.exists():
        return
        
    # List of known DLL directories in nvidia-* packages
    added = 0
    for pkg in site_packages.glob("nvidia/*/bin"):
        if pkg.is_dir():
            try:
                # Use absolute path for DLL directory
                abs_pkg = str(pkg.resolve())
                os.add_dll_directory(abs_pkg)
                added += 1
            except (OSError, AttributeError):
                continue
    
    # Also check the bin directory of the current env for extra safety
    venv_bin = Path(sys.prefix) / "Scripts"
    if venv_bin.exists():
        os.environ["PATH"] = str(venv_bin.resolve()) + os.pathsep + os.environ.get("PATH", "")


def nvidia_smi_path() -> Optional[str]:
    """Locate nvidia-smi.exe in common Windows locations."""
    candidates = [
        os.getenv("MIKE_NVIDIA_SMI"),
        shutil.which("nvidia-smi"),
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        r"C:\Windows\System32\nvidia-smi.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


def detect_gpu_info() -> dict:
    empty = {
        "cuda_detected": False,
        "gpu_name": None,
        "gpu_memory_total_mb": None,
        "gpu_memory_free_mb": None,
        "cuda_driver": None,
        "all_gpus": [],
        "gpu_count": 0,
        "tensor_split": None,
    }

    if env_bool("MIKE_FORCE_CPU", False) or env_bool("MIKE_DISABLE_CUDA", False):
        return empty

    smi = nvidia_smi_path()
    if not smi:
        return empty

    try:
        # Query all GPUs: name, total VRAM, free VRAM, driver version
        res = subprocess.run(
            [smi, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False
        )
        # Some NVIDIA drivers report an unavailable secondary adapter on
        # stderr while still returning valid rows for the active RTX GPU.
        if not res.stdout.strip():
            return empty
        
        gpus = []
        for i, line in enumerate(res.stdout.strip().splitlines()):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            
            try:
                total = int(parts[1])
                free = int(parts[2])
                driver = parts[3]
                major = int(driver.split(".")[0]) if "." in driver else 0
                
                # Minimum driver for modern llama-cpp-python is ~450+
                if major >= 450:
                    gpus.append({
                        "cuda_detected": True,
                        "gpu_index": i,
                        "gpu_name": parts[0],
                        "gpu_memory_total_mb": total,
                        "gpu_memory_free_mb": free,
                        "cuda_driver": driver,
                    })
            except (ValueError, IndexError):
                continue

        if not gpus:
            return empty

        # Pick the one with the most total VRAM as the primary GPU
        primary = sorted(gpus, key=lambda x: x["gpu_memory_total_mb"], reverse=True)[0]
        
        result = primary.copy()
        result["all_gpus"] = gpus
        result["gpu_count"] = len(gpus)
        result["total_vram_mb"] = sum(g["gpu_memory_total_mb"] for g in gpus)
        
        # Calculate tensor split (normalized ratios)
        if len(gpus) > 1:
            total_vram = sum(g["gpu_memory_total_mb"] for g in gpus)
            result["tensor_split"] = [g["gpu_memory_total_mb"] / total_vram for g in gpus]
        else:
            result["tensor_split"] = None
            
        return result

    except Exception as e:
        log.warning("[config] GPU detection failed: %s", e)
        return empty


# ---------------------------------------------------------------------------
# Runtime profiles
# ---------------------------------------------------------------------------

_RUNTIME_TIERS = [
    (22000, "cuda-24gb",      32768, 60, 1536, 768, True,  True),
    # Tier para setups dual-GPU com ~20 GB combinados (ex: RTX 3060 12GB + RTX 2070 8GB)
    (18000, "cuda-20gb",      32768, 80, 1280, 640, True,  True),
    (15000, "cuda-16gb",      16384, 42, 1280, 640, True,  True),
    # Tier para setups dual-GPU com ~14 GB combinados
    (13000, "cuda-14gb",      8192, 50, 1024, 512, True,  True),
    (11000, "cuda-12gb",      4096, 33, 1024, 512, False, True),
    (8000,  "cuda-8gb",       3072, 22, 768,  384, False, True),
    (0,     "cuda-low-vram",  2048, 12, 512,  256, False, True),
]


def recommended_runtime_profile(gpu_info: dict) -> dict:
    if not gpu_info["cuda_detected"]:
        return {
            "profile": "cpu-safe",
            "ctx_size": 2048,
            "gpu_layers": 0,
            "n_batch": 256,
            "n_ubatch": 128,
            "flash_attn": False,
            "offload_kqv": False,
        }
    # Para multi-GPU usa VRAM total combinada; single-GPU usa VRAM da GPU primaria
    total_mb = gpu_info.get("total_vram_mb") or gpu_info.get("gpu_memory_total_mb") or 0
    for min_mb, name, ctx, layers, batch, ubatch, flash, kqv in _RUNTIME_TIERS:
        if total_mb >= min_mb:
            return {
                "profile": name,
                "ctx_size": ctx,
                "gpu_layers": layers,
                "n_batch": batch,
                "n_ubatch": ubatch,
                "flash_attn": flash,
                "offload_kqv": kqv,
            }
    _, name, ctx, layers, batch, ubatch, flash, kqv = _RUNTIME_TIERS[-1]
    return {
        "profile": name, "ctx_size": ctx, "gpu_layers": layers,
        "n_batch": batch, "n_ubatch": ubatch, "flash_attn": flash, "offload_kqv": kqv,
    }


_PROFILE_ADJUSTMENTS = {
    "stability": lambda b, _g: {**b, "profile": "stability", "n_batch": min(b["n_batch"], 768), "n_ubatch": min(b["n_ubatch"], 384)},
    "context": lambda b, g: {**b, "profile": "context", "ctx_size": 6144 if (g["gpu_memory_total_mb"] or 0) >= 11000 else b["ctx_size"], "n_batch": min(b["n_batch"], 768), "n_ubatch": min(b["n_ubatch"], 384)},
    "aggressive": lambda b, _g: {**b, "profile": "aggressive", "gpu_layers": b["gpu_layers"] + (2 if b["gpu_layers"] > 0 else 0), "n_batch": min(max(b["n_batch"], 1024), 1536), "n_ubatch": min(max(b["n_ubatch"], 512), 768)},
    "flash": lambda b, _g: {**b, "profile": "flash", "flash_attn": True},
    "manual": lambda b, _g: {**b, "profile": "manual"},
}


def resolve_runtime_profile(profile_name: str, gpu_info: dict) -> dict:
    base = recommended_runtime_profile(gpu_info)
    name = (profile_name or "auto").strip().lower()
    if name in {"", "auto", "balanced"}:
        return base
    adjuster = _PROFILE_ADJUSTMENTS.get(name)
    return adjuster(base, gpu_info) if adjuster else base


# ---------------------------------------------------------------------------
# Knowledge paths
# ---------------------------------------------------------------------------

def default_knowledge_paths(project_root: Path) -> List[Path]:
    paths: List[Path] = [
        project_root / "runtime" / "knowledge",
        project_root / "runtime" / "memory" / "soul.json",
        project_root / "ecc" / "README.md",
        project_root / "ecc" / "AGENTS.md",
    ]
    docs_root = project_root / "ecc" / "docs"
    if docs_root.exists():
        paths.extend(sorted(docs_root.glob("*.md")))
    return [path for path in paths if path.exists()]


def knowledge_paths_from_env(project_root: Path) -> List[Path]:
    raw = os.getenv("MIKE_KNOWLEDGE_PATHS")
    if not raw:
        return default_knowledge_paths(project_root)
    paths = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        paths.append(
            (project_root / part).resolve()
            if not Path(part).is_absolute()
            else Path(part).resolve()
        )
    return paths


# ---------------------------------------------------------------------------
# Materialise all constants once at import time
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(
    os.getenv("MIKE_HOME", Path(__file__).resolve().parents[2])
).expanduser().resolve()

load_env_file(PROJECT_ROOT / "config" / ".env")
load_env_file(PROJECT_ROOT / "config" / ".env.runtime")

setup_cuda_dll_path()

GPU_INFO = detect_gpu_info()
RUNTIME_PROFILE = os.getenv("MIKE_RUNTIME_PROFILE", "auto")
RUNTIME_DEFAULTS = resolve_runtime_profile(RUNTIME_PROFILE, GPU_INFO)
_tensor_split_env = os.getenv("MIKE_TENSOR_SPLIT", "").strip()
if _tensor_split_env.lower() in ("0", "false", "none", "off", "disable"):
    TENSOR_SPLIT = None  # força GPU única
elif "," in _tensor_split_env:
    # Valores customizados ex: "0.485,0.515" ou "5698,6057" (normalizado automaticamente)
    try:
        _parts = [float(x.strip()) for x in _tensor_split_env.split(",")]
        _total = sum(_parts)
        TENSOR_SPLIT = [v / _total for v in _parts]
    except ValueError:
        TENSOR_SPLIT = GPU_INFO.get("tensor_split")
else:
    TENSOR_SPLIT = GPU_INFO.get("tensor_split")
DEFAULT_MAX_TOKENS = max(128, env_int("MIKE_DEFAULT_MAX_TOKENS", 2048))
STREAM_KEEPALIVE_SECONDS = max(1.0, env_float("MIKE_STREAM_KEEPALIVE_SECONDS", 10.0))
STREAM_TOOL_TIMEOUT_SEC = max(10.0, env_float("MIKE_STREAM_TOOL_TIMEOUT_SEC", 60.0))
WEB_REQUEST_TIMEOUT_SECONDS = max(0.0, env_float("MIKE_WEB_REQUEST_TIMEOUT_SECONDS", 0.0))

MODEL_REPO = os.getenv("MIKE_MODEL_REPO", "")
MODEL_FILE = os.getenv("MIKE_MODEL_FILE", "")
MMPROJ_FILE = os.getenv("MIKE_MMPROJ_FILE", "")
MMPROJ_REPO = os.getenv("MIKE_MMPROJ_REPO", "")
MODEL_REVISION = os.getenv("MIKE_MODEL_REVISION") or None
MODEL_ALIAS = os.getenv("MIKE_MODEL_ALIAS", "mike")

HOST = os.getenv("MIKE_HOST", "127.0.0.1")
PORT = env_int("MIKE_PORT", 8080)

CTX_SIZE = env_int("MIKE_CTX_SIZE", RUNTIME_DEFAULTS["ctx_size"])
GPU_LAYERS = env_int("MIKE_GPU_LAYERS", RUNTIME_DEFAULTS["gpu_layers"])
N_BATCH = env_int("MIKE_N_BATCH", min(RUNTIME_DEFAULTS["n_batch"], CTX_SIZE))
N_UBATCH = env_int("MIKE_N_UBATCH", min(RUNTIME_DEFAULTS["n_ubatch"], N_BATCH))
N_THREADS = env_int("MIKE_N_THREADS", max(1, (os.cpu_count() or 4) - 1))
N_THREADS_BATCH = env_int("MIKE_N_THREADS_BATCH", os.cpu_count() or 4)
FLASH_ATTN = env_bool("MIKE_FLASH_ATTN", RUNTIME_DEFAULTS["flash_attn"])
OFFLOAD_KQV = env_bool("MIKE_OFFLOAD_KQV", RUNTIME_DEFAULTS["offload_kqv"])

KV_TYPE_K = env_int("MIKE_KV_TYPE_K", 8)
KV_TYPE_V = env_int("MIKE_KV_TYPE_V", 8)
USE_MMAP = env_bool("MIKE_USE_MMAP", True)
USE_MLOCK = env_bool("MIKE_USE_MLOCK", False)
N_CPU_MOE = env_int("MIKE_N_CPU_MOE", 99)  # MoE expert layers on CPU (0=auto, 99=all on CPU)
VERBOSE = env_bool("MIKE_VERBOSE", False)

# Qwen3.6-35B-A3B is text-only. Vision must be explicitly enabled only when a
# compatible projector/runtime is configured; advertising it by default makes
# the dashboard promise a capability the single-brain runtime cannot fulfill.
VISION_ENABLED = env_bool("MIKE_ENABLE_VISION", False)
VISION_MAX_IMAGES = max(1, env_int("MIKE_VISION_MAX_IMAGES", 1))
VISION_MAX_DECODED_BYTES = max(262144, env_int("MIKE_VISION_MAX_DECODED_BYTES", 2 * 1024 * 1024))
VISION_RUNTIME_PROFILE = os.getenv("MIKE_VISION_RUNTIME_PROFILE", "safe-vision")
MMPROJ_USE_GPU = env_bool("MIKE_MMPROJ_USE_GPU", False)
VISION_ALLOWED_MIME_TYPES = ("image/jpeg", "image/png", "image/webp")

RAG_ENABLED = env_bool("MIKE_ENABLE_RAG", True)
WEB_SEARCH_ENABLED = env_bool("MIKE_ENABLE_WEB_SEARCH", True)
WEB_SEARCH_PROVIDER = os.getenv("MIKE_WEB_SEARCH_PROVIDER", "auto")

MEMORY_TOP_K = env_int("MIKE_MEMORY_TOP_K", 3)
KNOWLEDGE_TOP_K = env_int("MIKE_KNOWLEDGE_TOP_K", 4)
WEB_TOP_K = env_int("MIKE_WEB_TOP_K", 3)
RECENT_MEMORY_LIMIT = env_int("MIKE_RECENT_MEMORY_LIMIT", 10)
MEM0_SAVE_ALL = env_bool("MIKE_MEM0_SAVE_ALL", False)
SEARCH_ROUTE_HINTS = env_bool("MIKE_SEARCH_ROUTE_HINTS", True)
SEARCH_ROUTE_LIMIT = max(3, env_int("MIKE_SEARCH_ROUTE_LIMIT", 6))

API_KEY = os.getenv("MIKE_API_KEY", "").strip()
TRUST_LOCALHOST = env_bool("MIKE_TRUST_LOCALHOST", True)
ALLOW_UNAUTH_HEALTHCHECK = env_bool("MIKE_ALLOW_UNAUTH_HEALTHCHECK", True)
ALLOW_INSECURE_LAN = env_bool("MIKE_ALLOW_INSECURE_LAN", False)

MCP_TOOLS_ENABLED = env_bool("MIKE_ENABLE_MCP_TOOLS", True)
MCP_TOOL_MAX_STEPS = max(1, env_int("MIKE_MCP_MAX_STEPS", 3))
FORCE_TOOL_USE = env_bool("MIKE_FORCE_TOOL_USE", False)
TASK_MESH_ENABLED = env_bool("MIKE_TASK_MESH_ENABLED", True)
TASK_MESH_MAX_PLAN_STEPS = max(1, env_int("MIKE_TASK_MESH_MAX_STEPS", 8))
CORS_ORIGINS = cors_origins_from_env(PORT)

DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
LOG_DIR = PROJECT_ROOT / "logs"
SOUL_FILE = PROJECT_ROOT / "runtime" / "memory" / "soul.json"
MEMORY_DB = PROJECT_ROOT / "runtime" / "memory" / "mike_memory.db"
_memory_db_env = os.getenv("MIKE_MEMORY_DB", "").strip()
if _memory_db_env:
    MEMORY_DB = Path(_memory_db_env).expanduser().resolve() if Path(_memory_db_env).is_absolute() else (PROJECT_ROOT / _memory_db_env).resolve()
WEB_CACHE_DIR = PROJECT_ROOT / "runtime" / "knowledge" / "web_cache"
ROADMAP_DIR = PROJECT_ROOT / "runtime" / "roadmap"
ROADMAP_FILE = ROADMAP_DIR / "agent_evolution_roadmap.json"
BACKUP_DIR = PROJECT_ROOT / "runtime" / "backups"
BACKUP_SCRIPT = PROJECT_ROOT / "scripts" / "backup_mike.ps1"
KNOWLEDGE_PATHS = knowledge_paths_from_env(PROJECT_ROOT)

MEM0_USER_ID = os.getenv("MIKE_MEMORY_USER_ID", "marco")
MEM0_AGENT_ID = os.getenv("MIKE_MEMORY_AGENT_ID", "mike")

MCP_TOOL_SERVER = Path(os.getenv("MIKE_MCP_TOOL_SERVER", str(PROJECT_ROOT / "core" / "mcp" / "mike_workspace_mcp.py"))).expanduser().resolve()
MCP_SERVER_CONFIGS = mcp_servers_from_env(PROJECT_ROOT)

MCP_ALLOWED_ROOTS = [
    Path(part).expanduser().resolve()
    for part in ([entry.strip() for entry in os.getenv("MIKE_MCP_ALLOWED_ROOTS", "").split(os.pathsep) if entry.strip()] or [str(PROJECT_ROOT)])
]

TELEGRAM_ENABLED = env_bool("MIKE_TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN = os.getenv("MIKE_TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_MARCO = os.getenv("MIKE_TELEGRAM_CHAT_MARCO", "").strip()
TELEGRAM_CHAT_FAMILIA = os.getenv("MIKE_TELEGRAM_CHAT_FAMILIA", "").strip()

GRAPH_ENABLED = env_bool("MIKE_GRAPH_ENABLED", False)
NEO4J_URI = os.getenv("MIKE_NEO4J_URI", "bolt://localhost:7687").strip()
NEO4J_USER = os.getenv("MIKE_NEO4J_USER", "neo4j").strip()
NEO4J_PASS = os.getenv("MIKE_NEO4J_PASS", "").strip()

APPT_ENABLED = env_bool("MIKE_APPT_ENABLED", False)
APPT_CALL_HOURS_BEFORE = env_int("MIKE_APPT_CALL_HOURS_BEFORE", 24)
APPT_REMINDER_MINUTES_BEFORE = env_int("MIKE_APPT_REMINDER_MINUTES_BEFORE", 60)
APPT_MAX_CALL_RETRIES = env_int("MIKE_APPT_MAX_CALL_RETRIES", 2)
APPT_NOSHOW_GRACE_MINUTES = env_int("MIKE_APPT_NOSHOW_GRACE_MINUTES", 15)
PIPEDRIVE_API_TOKEN = os.getenv("MIKE_PIPEDRIVE_API_TOKEN", "").strip()
PIPEDRIVE_PIPELINE_ID = env_int("MIKE_PIPEDRIVE_PIPELINE_ID", 1)
PIPEDRIVE_STAGE_SCHEDULED = env_int("MIKE_PIPEDRIVE_STAGE_SCHEDULED", 1)
PIPEDRIVE_STAGE_CONFIRMED = env_int("MIKE_PIPEDRIVE_STAGE_CONFIRMED", 2)
PIPEDRIVE_STAGE_DONE = env_int("MIKE_PIPEDRIVE_STAGE_DONE", 3)
TWILIO_ACCOUNT_SID = os.getenv("MIKE_TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("MIKE_TWILIO_AUTH_TOKEN", "").strip()
TWILIO_PHONE_FROM = os.getenv("MIKE_TWILIO_PHONE_FROM", "").strip()
TWILIO_WEBHOOK_BASE = os.getenv("MIKE_TWILIO_WEBHOOK_BASE", "").strip()
ELEVENLABS_API_KEY = os.getenv("MIKE_ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("MIKE_ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip()
ELEVENLABS_MODEL_ID = os.getenv("MIKE_ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_CHAT_MODEL = os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-v4-pro").strip()
# "local" = llama.cpp GGUF | "deepseek" = DeepSeek API (HTTP)
LLM_BACKEND = os.getenv("MIKE_LLM_BACKEND", "local").strip().lower()
