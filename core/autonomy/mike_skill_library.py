# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Skill Library (Voyager Pattern)
=====================================

Implementa uma biblioteca de habilidades executaveis inspirada no Voyager
(arxiv 2305.16291). Skills sao scripts Python validados que o Mike ja
executou com sucesso. Sao versionadas, testadas, compostaveis e crescem
com o tempo.

Paper: Voyager: An Open-Ended Embodied Agent with Large Language Models
Key results: 3.3x more unique items, 15.3x faster tech tree unlock

Arquitetura:
  TENTAR → FALHAR → DEPURAR → VALIDAR → ARMAZENAR → COMPOR → REUSAR
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from mike_config import env_bool

log = logging.getLogger("mike.skill_library")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SKILL_LIBRARY_ENABLED = env_bool("MIKE_SKILL_LIBRARY_ENABLED", True)
SKILL_LIBRARY_MAX_SKILLS = int(os.getenv("MIKE_SKILL_LIBRARY_MAX", "200"))
SKILL_VALIDATION_TIMEOUT_SEC = int(os.getenv("MIKE_SKILL_VALIDATION_TIMEOUT", "15"))
SKILL_MAX_CODE_CHARS = int(os.getenv("MIKE_SKILL_MAX_CODE_CHARS", "8000"))

# ---------------------------------------------------------------------------
# Sandbox Security
# ---------------------------------------------------------------------------

# Allowlist of modules permitted inside the skill sandbox.
# Any module NOT in this set is blocked at import time, closing the RCE vector
# where __import__('os').system('cmd') bypasses the string blocklist filter.
_SKILL_SAFE_MODULES: frozenset[str] = frozenset({
    "base64", "binascii", "collections", "copy", "csv",
    "datetime", "decimal", "enum", "fractions",
    "functools", "hashlib", "hmac", "itertools",
    "json", "math", "numbers", "pprint",
    "re", "secrets", "statistics", "string",
    "textwrap", "time", "typing", "unicodedata", "uuid",
})

# Filesystem-capable modules are intentionally excluded instead of attempting
# to monkey-patch Path/open calls. Python module objects expose too many ways
# to recover unrestricted filesystem access, so file skills must use Mike's
# separately-authorized MCP filesystem tools.
_SKILL_FILESYSTEM_MODULES: frozenset[str] = frozenset({
    "glob", "os", "pathlib", "shutil", "tempfile",
})

_SKILL_FORBIDDEN_NAMES: frozenset[str] = frozenset({
    "__import__", "breakpoint", "compile", "eval", "exec", "getattr",
    "globals", "help", "input", "locals", "open", "setattr", "vars",
})

_SKILL_FORBIDDEN_ATTRIBUTES: frozenset[str] = frozenset({
    "environ", "glob", "mkdir", "modules", "open", "os", "path",
    "popen", "read_bytes", "read_text", "remove", "rename", "replace",
    "rmdir", "rmtree", "shutil", "sys", "system", "unlink", "walk",
    "write_bytes", "write_text",
})


def _validate_skill_ast(code: str) -> Optional[str]:
    """Return a policy violation for code that escapes the pure-data sandbox."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return f"Syntax error: {exc}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                base_module = alias.name.split(".", 1)[0]
                if base_module in _SKILL_FILESYSTEM_MODULES:
                    return f"Filesystem module is not allowed: {base_module}"
                if base_module not in _SKILL_SAFE_MODULES:
                    return f"Module is not allowed: {base_module}"
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                return "Relative imports are not allowed"
            base_module = (node.module or "").split(".", 1)[0]
            if base_module in _SKILL_FILESYSTEM_MODULES:
                return f"Filesystem module is not allowed: {base_module}"
            if base_module not in _SKILL_SAFE_MODULES:
                return f"Module is not allowed: {base_module}"
            for alias in node.names:
                if (
                    alias.name.startswith("_")
                    or alias.name in _SKILL_FORBIDDEN_ATTRIBUTES
                    or alias.name in _SKILL_FORBIDDEN_NAMES
                ):
                    return f"Imported symbol is not allowed: {alias.name}"
        elif isinstance(node, ast.Name) and node.id in _SKILL_FORBIDDEN_NAMES:
            return f"Builtin is not allowed: {node.id}"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return f"Private attribute access is not allowed: {node.attr}"
            if node.attr in _SKILL_FORBIDDEN_ATTRIBUTES:
                return f"Filesystem/system attribute is not allowed: {node.attr}"
        elif isinstance(node, ast.While):
            # A timed-out thread cannot be killed. Execution is also isolated in
            # a subprocess, but rejecting while loops prevents trivial CPU bombs.
            return "While loops are not allowed"
    return None


def _safe_import(name: str, globals=None, locals=None, fromlist=(), level: int = 0):
    """Sandbox-safe __import__ replacement.

    Replaces the builtin __import__ inside skill execution sandboxes.
    Only modules listed in _SKILL_SAFE_MODULES are importable.
    Dangerous modules (os, subprocess, ctypes, importlib, sys, socket, etc.)
    are blocked at the import layer.

    This closes the RCE vector:
        getattr(__import__('os'), 'system')('rm -rf /')
    because __import__('os') now raises ImportError.
    """
    base_module = name.split(".")[0]
    if base_module not in _SKILL_SAFE_MODULES:
        raise ImportError(
            f"Module '{name}' is not allowed in the skill sandbox. "
            f"Allowed modules: {sorted(_SKILL_SAFE_MODULES)}"
        )
    return __import__(name, globals, locals, fromlist, level)


from core.shared.time_utils import utc_now, utc_now_iso


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class Skill:
    """Uma habilidade executavel na biblioteca."""
    id: str
    name: str                          # Nome unico da skill
    description: str                   # O que faz, quando usar
    code: str                          # Codigo Python executavel
    category: str = "general"          # file_ops | web | email | data | system | general
    version: int = 1
    success_count: int = 0
    fail_count: int = 0
    last_used_at: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    dependencies: list[str] = field(default_factory=list)  # IDs de skills que esta compoe
    composed_of: list[str] = field(default_factory=list)   # IDs de skills que usa
    tags: list[str] = field(default_factory=list)
    validated: bool = False
    validation_output: str = ""
    checksum: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "code": self.code,
            "category": self.category,
            "version": self.version,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "last_used_at": self.last_used_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dependencies": self.dependencies,
            "composed_of": self.composed_of,
            "tags": self.tags,
            "validated": self.validated,
            "checksum": self.checksum,
        }

    @staticmethod
    def from_dict(data: dict) -> "Skill":
        return Skill(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            code=str(data.get("code", "")),
            category=str(data.get("category", "general")),
            version=int(data.get("version", 1)),
            success_count=int(data.get("success_count", 0)),
            fail_count=int(data.get("fail_count", 0)),
            last_used_at=data.get("last_used_at"),
            created_at=str(data.get("created_at", utc_now_iso())),
            updated_at=str(data.get("updated_at", utc_now_iso())),
            dependencies=list(data.get("dependencies", [])),
            composed_of=list(data.get("composed_of", [])),
            tags=list(data.get("tags", [])),
            validated=bool(data.get("validated", False)),
            validation_output=str(data.get("validation_output", "")),
            checksum=str(data.get("checksum", "")),
        )

    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / max(1, total)

    def compute_checksum(self) -> str:
        return hashlib.sha256(self.code.encode("utf-8", errors="ignore")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# SkillLibrary
# ---------------------------------------------------------------------------

class SkillLibrary:
    """Biblioteca de habilidades executaveis com validacao e composicao."""

    def __init__(
        self,
        store_dir: Optional[Path] = None,
        log_fn: Optional[Callable] = None,
    ):
        self._store_dir = Path(store_dir) if store_dir else Path("runtime/memory/skills")
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._skills_file = self._store_dir / "skill_library.json"
        self._log = log_fn or log.info
        self._skills: dict[str, Skill] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._skills_file.exists():
            try:
                data = json.loads(self._skills_file.read_text(encoding="utf-8"))
                for item in data.get("skills", []):
                    skill = Skill.from_dict(item)
                    if skill.id:
                        # Never trust a persisted `validated` flag. A library
                        # file is data, not an authorization boundary.
                        valid, output = self.validate_skill(skill)
                        skill.validated = valid
                        skill.validation_output = output[:500]
                        skill.checksum = skill.compute_checksum()
                        self._skills[skill.id] = skill
                self._log(f"[skill_lib] Loaded {len(self._skills)} skills")
            except Exception as exc:
                self._log(f"[skill_lib] Load failed: {exc}")
        self._loaded = True

    def _save(self) -> None:
        payload = {
            "updated_at": utc_now_iso(),
            "total_skills": len(self._skills),
            "skills": [s.to_dict() for s in self._skills.values()],
        }
        tmp = self._skills_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._skills_file)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_skill(
        self,
        name: str,
        description: str,
        code: str,
        category: str = "general",
        tags: Optional[list[str]] = None,
        validate: bool = True,
    ) -> Optional[Skill]:
        """Add a new skill to the library. Validates before storing."""
        self._ensure_loaded()

        # Deduplicate by name
        for existing in self._skills.values():
            if existing.name.lower() == name.lower():
                self._log(f"[skill_lib] Skill '{name}' already exists, updating instead")
                return self.update_skill(existing.id, code=code, description=description)

        if len(self._skills) >= SKILL_LIBRARY_MAX_SKILLS:
            self._prune_oldest()

        skill = Skill(
            id=f"skill_{uuid.uuid4().hex[:8]}",
            name=name.strip(),
            description=description.strip(),
            code=code.strip(),
            category=category,
            tags=tags or [],
            checksum=hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:16],
        )

        if validate:
            valid, output = self.validate_skill(skill)
            skill.validated = valid
            skill.validation_output = output[:500]
            if not valid:
                self._log(f"[skill_lib] Skill '{name}' failed validation: {output[:200]}")
                return None

        self._skills[skill.id] = skill
        self._save()
        self._log(f"[skill_lib] Skill added: {name} (validated={skill.validated})")
        return skill

    def update_skill(
        self,
        skill_id: str,
        *,
        code: Optional[str] = None,
        description: Optional[str] = None,
        increment_version: bool = True,
    ) -> Optional[Skill]:
        """Update an existing skill's code or description."""
        self._ensure_loaded()
        skill = self._skills.get(skill_id)
        if not skill:
            return None

        if code is not None:
            skill.code = code.strip()
            skill.checksum = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:16]
            if increment_version:
                skill.version += 1
            # Re-validate
            valid, output = self.validate_skill(skill)
            skill.validated = valid
            skill.validation_output = output[:500]

        if description is not None:
            skill.description = description.strip()

        skill.updated_at = utc_now_iso()
        self._save()
        return skill

    def get_skill(self, skill_id: str) -> Optional[Skill]:
        self._ensure_loaded()
        return self._skills.get(skill_id)

    def get_skill_by_name(self, name: str) -> Optional[Skill]:
        self._ensure_loaded()
        name_lower = name.strip().lower()
        for skill in self._skills.values():
            if skill.name.lower() == name_lower:
                return skill
        return None

    def delete_skill(self, skill_id: str) -> bool:
        self._ensure_loaded()
        if skill_id in self._skills:
            # Check if other skills depend on this one
            dependents = [
                s.name for s in self._skills.values()
                if skill_id in s.composed_of
            ]
            if dependents:
                self._log(f"[skill_lib] Cannot delete: depended on by {dependents}")
                return False
            del self._skills[skill_id]
            self._save()
            return True
        return False

    def list_skills(
        self,
        category: Optional[str] = None,
        validated_only: bool = False,
    ) -> list[Skill]:
        self._ensure_loaded()
        skills = list(self._skills.values())
        if category:
            skills = [s for s in skills if s.category == category]
        if validated_only:
            skills = [s for s in skills if s.validated]
        skills.sort(key=lambda s: (-s.success_count, -s.success_rate(), s.name))
        return skills

    def _prune_oldest(self) -> None:
        """Remove least-used skill when over capacity."""
        if len(self._skills) < SKILL_LIBRARY_MAX_SKILLS:
            return
        sorted_skills = sorted(
            self._skills.values(),
            key=lambda s: (s.success_rate(), s.last_used_at or ""),
        )
        to_remove = sorted_skills[0]
        self._log(f"[skill_lib] Pruning least-used skill: {to_remove.name}")
        del self._skills[to_remove.id]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_skill(self, skill: Skill) -> tuple[bool, str]:
        """Validate syntax and enforce the pure-data execution policy."""
        if not isinstance(skill.code, str) or not skill.code.strip():
            return False, "Skill code is empty"
        if len(skill.code) > SKILL_MAX_CODE_CHARS:
            return False, f"Skill code exceeds {SKILL_MAX_CODE_CHARS} characters"
        violation = _validate_skill_ast(skill.code)
        if violation:
            return False, violation
        return True, "Syntax OK, restricted sandbox policy pass"

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_skill(self, skill_id: str, params: Optional[dict] = None) -> dict:
        """Execute a skill and record the result."""
        self._ensure_loaded()
        skill = self._skills.get(skill_id)
        if not skill:
            return {"success": False, "error": f"Skill not found: {skill_id}"}
        valid, validation_output = self.validate_skill(skill)
        skill.validated = valid
        skill.validation_output = validation_output[:500]
        if not valid:
            return {"success": False, "error": f"Skill not validated: {validation_output}"}

        runner = r'''
import builtins
import base64
import json
import sys

SAFE_MODULES = frozenset(json.loads(base64.b64decode(sys.argv[1]).decode("utf-8")))
code = base64.b64decode(sys.argv[2]).decode("utf-8")
params = json.loads(sys.stdin.read() or "{}")

def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    base_module = name.split(".", 1)[0]
    if level or base_module not in SAFE_MODULES:
        raise ImportError("Module is not allowed in the skill sandbox: " + name)
    return builtins.__import__(name, globals, locals, fromlist, level)

safe_builtins = {
    "abs": abs, "all": all, "any": any, "bin": bin,
    "bool": bool, "chr": chr, "complex": complex,
    "dict": dict, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "format": format,
    "frozenset": frozenset, "hasattr": hasattr, "hash": hash,
    "hex": hex, "int": int, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len,
    "list": list, "map": map, "max": max, "min": min,
    "next": next, "object": object, "oct": oct, "ord": ord,
    "pow": pow, "print": print, "range": range, "repr": repr,
    "reversed": reversed, "round": round, "set": set,
    "slice": slice, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
    "__import__": safe_import,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError,
}
scope = {
    "__builtins__": safe_builtins,
    "__name__": "__skill_exec__",
    "params": params,
    "result": None,
}
try:
    exec(compile(code, "<skill>", "exec"), scope, scope)
    print(json.dumps({"success": True, "output": str(scope.get("result", "OK"))}))
except Exception as exc:
    print(json.dumps({"success": False, "error": str(exc)}))
'''
        encoded_modules = base64.b64encode(
            json.dumps(sorted(_SKILL_SAFE_MODULES)).encode("utf-8")
        ).decode("ascii")
        encoded_code = base64.b64encode(skill.code.encode("utf-8")).decode("ascii")
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-c", runner, encoded_modules, encoded_code],
                input=json.dumps(params or {}, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=SKILL_VALIDATION_TIMEOUT_SEC,
                check=False,
                env={},
            )
            stdout_lines = completed.stdout.strip().splitlines()
            if completed.returncode != 0 or not stdout_lines:
                error = completed.stderr.strip() or "Skill process failed"
                raise RuntimeError(error[:500])
            execution = json.loads(stdout_lines[-1])
            if not execution.get("success"):
                raise RuntimeError(str(execution.get("error") or "Skill execution failed"))
            skill.success_count += 1
            skill.last_used_at = utc_now_iso()
            self._save()
            return {"success": True, "output": str(execution.get("output", "OK"))}
        except subprocess.TimeoutExpired:
            skill.fail_count += 1
            skill.last_used_at = utc_now_iso()
            self._save()
            return {"success": False, "error": "Execution timeout"}
        except Exception as exc:
            skill.fail_count += 1
            skill.last_used_at = utc_now_iso()
            self._save()
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[Skill]:
        """Search skills by name, description, tags, or category."""
        self._ensure_loaded()
        query_lower = query.strip().lower()
        if not query_lower:
            return self.list_skills()[:limit]

        scored: list[tuple[float, Skill]] = []
        for skill in self._skills.values():
            score = 0.0
            name_lower = skill.name.lower()
            desc_lower = skill.description.lower()

            # Exact name match
            if query_lower == name_lower:
                score += 10.0
            # Name contains query
            elif query_lower in name_lower:
                score += 5.0
            # Description contains query
            if query_lower in desc_lower:
                score += 3.0
            # Tag match
            if any(query_lower in tag.lower() for tag in skill.tags):
                score += 4.0
            # Category match
            if query_lower == skill.category.lower():
                score += 2.0
            # Success rate bonus
            score += skill.success_rate() * 2.0
            # Recency bonus
            if skill.last_used_at:
                score += 0.5

            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:limit]]

    def find_composable(self, goal: str) -> list[list[Skill]]:
        """Find chains of skills that could compose to achieve a goal.
        Returns list of possible skill chains.
        """
        self._ensure_loaded()
        goal_lower = goal.lower()
        chains: list[list[Skill]] = []

        # Direct matches
        direct = self.search(goal, limit=3)
        for skill in direct:
            chains.append([skill])

        # Try to build chains from keywords in the goal
        keywords = [w for w in re.findall(r'\w{4,}', goal_lower) if w not in
                     ("para", "com", "como", "que", "uma", "isso", "esse", "essa")]

        keyword_matches: dict[str, list[Skill]] = {}
        for kw in keywords:
            matches = self.search(kw, limit=2)
            if matches:
                keyword_matches[kw] = matches

        # Build chains from keyword-matched skills
        if len(keyword_matches) >= 2:
            chain = []
            for kw, skills in keyword_matches.items():
                if skills and not any(s.id in {c.id for c in chain} for s in skills):
                    chain.append(skills[0])
            if len(chain) >= 2:
                chains.append(chain)

        return chains

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose_skill(
        self,
        name: str,
        description: str,
        skill_ids: list[str],
        glue_code: str = "",
    ) -> Optional[Skill]:
        """Create a new skill by composing existing skills."""
        self._ensure_loaded()

        # Validate all component skills exist
        components = []
        for sid in skill_ids:
            skill = self._skills.get(sid)
            if not skill:
                return None
            components.append(skill)

        # Build composed code
        lines = [
            f"# Composed skill: {name}",
            f"# Components: {', '.join(s.name for s in components)}",
            "",
        ]
        for i, comp in enumerate(components):
            lines.append(f"# --- Component {i+1}: {comp.name} ---")
            lines.append(comp.code)
            lines.append("")

        if glue_code:
            lines.append("# --- Glue code ---")
            lines.append(glue_code)

        composed_code = "\n".join(lines)

        return self.add_skill(
            name=name,
            description=description,
            code=composed_code,
            category="composed",
            tags=[comp.category for comp in components],
            validate=True,
        )

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def bootstrap_defaults(self) -> int:
        """Pre-populate the library with foundational skills. Returns count added."""
        self._ensure_loaded()
        defaults = [
            {
                "name": "listar_arquivos_diretorio",
                "description": "Lista arquivos em um diretorio e filtra por extensao",
                "code": "from pathlib import Path\nd = Path(params.get('path', '.'))\next = params.get('ext', '*')\nfiles = list(d.glob(f'*.{ext}')) if ext != '*' else list(d.glob('*'))\nresult = [str(f) for f in files[:20]]",
                "category": "file_ops",
                "tags": ["arquivos", "listar", "pathlib"],
            },
            {
                "name": "ler_arquivo_texto",
                "description": "Le o conteudo de um arquivo de texto",
                "code": "from pathlib import Path\np = Path(params.get('path', ''))\nif p.exists():\n    result = p.read_text(encoding='utf-8', errors='replace')[:5000]\nelse:\n    result = 'Arquivo nao encontrado'",
                "category": "file_ops",
                "tags": ["arquivos", "ler", "texto"],
            },
            {
                "name": "verificar_espaco_disco",
                "description": "Verifica espaco livre em disco",
                "code": "import shutil\npath = params.get('path', '.')\ntotal, used, free = shutil.disk_usage(path)\nfree_gb = free / (1024**3)\ntotal_gb = total / (1024**3)\npct = (free / total) * 100\nresult = f'{free_gb:.1f}GB livres de {total_gb:.1f}GB ({pct:.1f}%)'",
                "category": "system",
                "tags": ["sistema", "disco", "monitor"],
            },
            {
                "name": "contar_linhas_arquivo",
                "description": "Conta o numero de linhas de um arquivo de texto",
                "code": "from pathlib import Path\np = Path(params.get('path', ''))\nif p.exists():\n    result = len(p.read_text(encoding='utf-8', errors='replace').splitlines())\nelse:\n    result = 0",
                "category": "file_ops",
                "tags": ["arquivos", "contar", "linhas"],
            },
            {
                "name": "buscar_texto_arquivo",
                "description": "Busca um termo em um arquivo e retorna linhas com match",
                "code": "from pathlib import Path\np = Path(params.get('path', ''))\nquery = params.get('query', '')\nif p.exists() and query:\n    lines = p.read_text(encoding='utf-8', errors='replace').splitlines()\n    result = [l for l in lines if query.lower() in l.lower()][:20]\nelse:\n    result = []",
                "category": "file_ops",
                "tags": ["arquivos", "buscar", "grep"],
            },
            {
                "name": "criar_backup_arquivo",
                "description": "Cria backup de um arquivo com timestamp",
                "code": "from pathlib import Path\nfrom datetime import datetime\np = Path(params.get('path', ''))\nif p.exists():\n    ts = datetime.now().strftime('%Y%m%d_%H%M%S')\n    backup = p.with_suffix(f'.backup_{ts}{p.suffix}')\n    backup.write_bytes(p.read_bytes())\n    result = str(backup)\nelse:\n    result = 'Arquivo nao encontrado'",
                "category": "file_ops",
                "tags": ["arquivos", "backup", "seguranca"],
            },
            {
                "name": "extrair_dados_json",
                "description": "Le arquivo JSON e extrai campos especificos",
                "code": "import json\nfrom pathlib import Path\np = Path(params.get('path', ''))\nkeys = params.get('keys', [])\nif p.exists():\n    data = json.loads(p.read_text(encoding='utf-8'))\n    if keys and isinstance(data, dict):\n        result = {k: data.get(k) for k in keys}\n    else:\n        result = data\nelse:\n    result = {}",
                "category": "data",
                "tags": ["json", "extrair", "dados"],
            },
            {
                "name": "formatar_tabela_markdown",
                "description": "Converte lista de dicionarios em tabela markdown",
                "code": "data = params.get('data', [])\nif not data:\n    result = 'Sem dados'\nelse:\n    keys = list(data[0].keys())\n    header = '| ' + ' | '.join(keys) + ' |'\n    sep = '|' + '|'.join(['---' for _ in keys]) + '|'\n    rows = ['| ' + ' | '.join(str(row.get(k, '')) for k in keys) + ' |' for row in data]\n    result = '\\n'.join([header, sep] + rows)",
                "category": "data",
                "tags": ["markdown", "tabela", "formatar"],
            },
            {
                "name": "calcular_estatisticas",
                "description": "Calcula estatisticas basicas de uma lista de numeros",
                "code": "nums = params.get('numbers', [])\nif nums:\n    n = len(nums)\n    mean = sum(nums) / n\n    sorted_nums = sorted(nums)\n    median = sorted_nums[n//2] if n % 2 else (sorted_nums[n//2-1] + sorted_nums[n//2]) / 2\n    result = {'count': n, 'mean': round(mean, 2), 'median': median, 'min': min(nums), 'max': max(nums), 'sum': sum(nums)}\nelse:\n    result = {'error': 'No numbers provided'}",
                "category": "data",
                "tags": ["estatisticas", "numeros", "calculo"],
            },
            {
                "name": "validar_email_formato",
                "description": "Valida formato basico de endereco de email",
                "code": "import re\nemail = params.get('email', '')\npattern = r'^[\\w\\.\\-]+@[\\w\\.\\-]+\\.\\w{2,}$'\nresult = bool(re.match(pattern, email))",
                "category": "data",
                "tags": ["email", "validar", "formato"],
            },
            {
                "name": "gerar_slug_url",
                "description": "Converte texto em slug amigavel para URL",
                "code": "import re\ntext = params.get('text', '')\nslug = text.lower().strip()\nslug = re.sub(r'[^\\w\\s-]', '', slug)\nslug = re.sub(r'[\\s_]+', '-', slug)\nslug = re.sub(r'-+', '-', slug)\nresult = slug[:100]",
                "category": "data",
                "tags": ["texto", "slug", "url"],
            },
            {
                "name": "timestamp_atual",
                "description": "Retorna timestamp atual em varios formatos",
                "code": "from datetime import datetime, timezone\nfmt = params.get('format', 'iso')\nnow = datetime.now(timezone.utc)\nif fmt == 'iso':\n    result = now.isoformat()\nelif fmt == 'date':\n    result = now.strftime('%Y-%m-%d')\nelif fmt == 'datetime':\n    result = now.strftime('%Y-%m-%d %H:%M:%S')\nelif fmt == 'unix':\n    result = int(now.timestamp())\nelse:\n    result = now.isoformat()",
                "category": "system",
                "tags": ["data", "hora", "timestamp"],
            },
        ]

        added = 0
        for tmpl in defaults:
            existing = self.get_skill_by_name(tmpl["name"])
            if existing:
                continue
            skill = self.add_skill(
                name=tmpl["name"],
                description=tmpl["description"],
                code=tmpl["code"],
                category=tmpl["category"],
                tags=tmpl["tags"],
                validate=True,
            )
            if skill:
                added += 1

        if added:
            self._log(f"[skill_lib] Bootstrapped {added} default skills")
        return added

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        self._ensure_loaded()
        skills = list(self._skills.values())
        validated = sum(1 for s in skills if s.validated)
        total_uses = sum(s.success_count + s.fail_count for s in skills)
        return {
            "enabled": SKILL_LIBRARY_ENABLED,
            "total_skills": len(skills),
            "validated_skills": validated,
            "categories": {cat: sum(1 for s in skills if s.category == cat)
                          for cat in set(s.category for s in skills)},
            "total_uses": total_uses,
            "avg_success_rate": round(
                sum(s.success_rate() for s in skills) / max(1, len(skills)) * 100, 1
            ),
            "most_used": max(skills, key=lambda s: s.success_count).name if skills else "",
            "composed_skills": sum(1 for s in skills if s.composed_of),
        }
