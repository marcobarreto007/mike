"""
mike_kill_switch.py — Kill-switch global do MIKE (panic-stop).

Quando ENGAGED, congela a autonomia do MIKE:
- o loop de governance salta todas as acoes autonomas (run_cycle retorna frozen)
- pontos de envio outbound (auto_reply, mission engine, shell) DEVEM chamar
  assert_not_frozen() antes de executar acoes irreversiveis (enviar email,
  correr PowerShell, apagar ficheiros).

Backed por um FICHEIRO-FLAG (sobrevive a restarts; pode ser acionado externamente
so tocando o ficheiro). Nao tem dependencias do runtime do MIKE (apenas stdlib).

CLI (botao de panico a partir do terminal):
    python core/autonomy/mike_kill_switch.py engage "motivo opcional"
    python core/autonomy/mike_kill_switch.py disengage
    python core/autonomy/mike_kill_switch.py status
"""
from __future__ import annotations

import functools
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("mike.kill_switch")

_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parents[2]                       # core/autonomy -> core -> raiz
_DEFAULT_FLAG = _PROJECT_ROOT / "runtime" / "state" / "kill_switch"


def _flag_path() -> Path:
    override = os.getenv("MIKE_KILL_SWITCH_FILE")
    return Path(override) if override else _DEFAULT_FLAG


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------

def is_engaged() -> bool:
    """True se o kill-switch estiver engaged (ficheiro-flag existe). Chamada barata."""
    try:
        return _flag_path().is_file()
    except OSError:
        return False


def engage(reason: str = "") -> dict:
    """Ativa o kill-switch: a partir daqui a autonomia do MIKE fica congelada."""
    p = _flag_path()
    payload = {
        "engaged_at": datetime.now(timezone.utc).isoformat(),
        "reason": (reason or "manual engage").strip(),
        "pid": os.getpid(),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log.warning("KILL_SWITCH ENGAGED — autonomia do MIKE congelada. reason=%s", payload["reason"])
        return {"engaged": True, **payload, "path": str(p)}
    except OSError as exc:
        log.error("Falha ao ativar kill-switch (%s): %s", p, exc)
        return {"engaged": False, "error": str(exc), "path": str(p)}


def disengage() -> dict:
    """Desativa o kill-switch: a autonomia retoma."""
    p = _flag_path()
    try:
        if p.is_file():
            p.unlink()
        log.info("Kill-switch DESATIVADO — autonomia do MIKE retomada.")
        return {"engaged": False, "path": str(p)}
    except OSError as exc:
        log.error("Falha ao desativar kill-switch: %s", exc)
        return {"engaged": is_engaged(), "error": str(exc), "path": str(p)}


def status() -> dict:
    """Estado completo do kill-switch (engaged + motivo + quando)."""
    p = _flag_path()
    if not p.is_file():
        return {"engaged": False, "path": str(p)}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {"engaged": True, "path": str(p), **data}


# ---------------------------------------------------------------------------
# Guard para pontos de envio outbound
# ---------------------------------------------------------------------------

class KillSwitchEngaged(RuntimeError):
    """Levantada quando uma acao autonomas tentou executar com o kill-switch engaged."""


def assert_not_frozen(action_desc: str = "") -> None:
    """
    Chamar ANTES de acoes autonomas irreversiveis (enviar email, correr shell,
    apagar ficheiros, etc.). Levanta KillSwitchEngaged se o kill-switch estiver engaged.
    """
    if is_engaged():
        raise KillSwitchEngaged(
            f"Acao bloqueada pelo kill-switch: {action_desc or 'acao autonomas'}. "
            f"Desativa com: python core/autonomy/mike_kill_switch.py disengage"
        )


def require_unfrozen(fn):
    """Decorator: bloqueia a chamada se o kill-switch estiver engaged."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        assert_not_frozen(getattr(fn, "__name__", "acao"))
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import sys
    args = sys.argv[1:]
    cmd = args[0].lower() if args else "status"
    if cmd == "engage":
        reason = " ".join(args[1:]).strip() or "CLI manual"
        print(json.dumps(engage(reason), ensure_ascii=False, indent=2))
    elif cmd == "disengage":
        print(json.dumps(disengage(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
