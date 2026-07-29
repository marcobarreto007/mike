"""
Mike Auto-Reply — responde automaticamente emails de familiares.

Quando ativado (MIKE_AUTO_REPLY_ENABLED=true), verifica emails nao lidos. Se o
remetente for um familiar cadastrado (identity.json), gera uma resposta via LLM
(reutilizando o cerebro do Mike) com fallback seguro. Resposta automatica.

Seguranca (hardening):
- AUTO-SEND OFF por default (MIKE_AUTO_REPLY_ENABLED=true para ativar).
- Match de destinatario EXATO/token-exato (nao substring) — evita responder a
  pessoas erradas (ex-bug: "marco" batia em "marcos@...").
- Kill-switch global respeitado: se engaged, NENHUM email e enviado.
- Rate-limit por destinatario (cooldown) — evita tempestade de respostas.
- Anti-loop: remetente = o proprio Mike (MIKE_SMTP_USER) nunca e respondido.

Requer credenciais Gmail configuradas em .env.runtime.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("mike.auto_reply")

# Kill-switch global (panic-stop). Import defensivo: se faltar, o auto-reply
# continua (so sem o guard). Evita dependencia dura num modulo eventualmente ausente.
try:
    from mike_kill_switch import assert_not_frozen  # type: ignore
except Exception:  # pragma: no cover
    assert_not_frozen = None  # type: ignore

# ── Action memory log ─────────────────────────────────────────────────
_ACTION_LOG_PATH = Path(os.getenv("MIKE_HOME", "")) / "runtime" / "memory" / "auto_reply_actions.jsonl"


def _save_action(action_type: str, to_email: str, to_name: str, subject: str, summary: str) -> None:
    """Grava uma acao no log de memoria do Mike para consulta futura."""
    try:
        _ACTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": action_type,
            "to_email": to_email,
            "to_name": to_name,
            "subject": subject,
            "summary": summary,
        }
        with open(_ACTION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # nunca quebra o fluxo principal


def get_recent_actions(limit: int = 20) -> list[dict]:
    """Retorna as acoes recentes do Mike (para consulta)."""
    if not _ACTION_LOG_PATH.exists():
        return []
    actions = []
    try:
        with open(_ACTION_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    actions.append(json.loads(line))
    except Exception:
        return []
    return actions[-limit:][::-1]

# ── Helpers ──────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _load_identity() -> dict:
    """Load identity.json for email contacts."""
    path = Path(os.getenv("MIKE_HOME", "")) / "config" / "identity.json"
    if not path.exists():
        path = Path(__file__).parent.parent.parent / "config" / "identity.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log.warning("[auto_reply] Failed to load identity.json: %s", e)
        return {}


def _load_family_profiles() -> dict:
    """Load family_profiles.json for rich personality context."""
    path = Path(os.getenv("MIKE_HOME", "")) / "config" / "family_profiles.json"
    if not path.exists():
        path = Path(__file__).parent.parent.parent / "config" / "family_profiles.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data.get("profiles", {})
    except Exception as e:
        log.warning("[auto_reply] Failed to load family_profiles.json: %s", e)
        return {}


# ── Rate-limit por destinatario ───────────────────────────────────────

_RATELIMIT_PATH = Path(os.getenv("MIKE_HOME", "")) / "runtime" / "memory" / "auto_reply_ratelimit.json"
_COOLDOWN_SEC = int(os.getenv("MIKE_AUTO_REPLY_COOLDOWN_SEC", "21600"))  # 6h default


def _rate_limit_allows(sender_email: str) -> bool:
    """True se ainda nao foi respondido neste cooldown."""
    try:
        data = json.loads(_RATELIMIT_PATH.read_text(encoding="utf-8")) if _RATELIMIT_PATH.exists() else {}
    except Exception:
        data = {}
    last = float(data.get(sender_email, 0))
    return (time.time() - last) >= _COOLDOWN_SEC


def _rate_limit_mark(sender_email: str) -> None:
    """Regista resposta agora para este destinatario."""
    try:
        _RATELIMIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if _RATELIMIT_PATH.exists():
            data = json.loads(_RATELIMIT_PATH.read_text(encoding="utf-8"))
        data[sender_email] = time.time()
        _RATELIMIT_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


# ── LLM wiring ────────────────────────────────────────────────────────

AUTO_REPLY_SYSTEM_PROMPT = (
    "Es o Mike, assistente digital da familia Barreto. Escreve respostas de email "
    "CURTAS (3 a 6 frases), carinhosas, claras e uteis em portugues do Brasil. "
    "Responde ao CONTEUDO do email recebido. NUNCA inventes factos, promessas, "
    "valores, datas ou tarefas. Se nao souberes algo, diz que vais verificar. "
    "Nao incluis assinatura longa — termina apenas com 'Mike'."
)


def build_llm_fn(shared_state) -> Optional[Callable[[str], str]]:
    """
    Constroi um llm_fn(prompt) -> str a partir do cliente LLM do runtime
    (shared_state.llm, com metodo chat_completion). Devolve None se indisponivel.
    """
    client = getattr(shared_state, "llm", None)
    if client is None or not callable(getattr(client, "chat_completion", None)):
        log.warning("[auto_reply] shared_state.llm sem chat_completion — llm_fn indisponivel.")
        return None

    def _llm(prompt: str) -> str:
        data = client.chat_completion(
            messages=[
                {"role": "system", "content": AUTO_REPLY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.6,
        )
        # formato OpenAI: data["choices"][0]["message"]["content"]
        return str(data["choices"][0]["message"]["content"]).strip()

    return _llm


def _find_profile_by_email(email_address: str) -> Optional[dict]:
    """Match an email address to a family profile. Match EXATO/token-exato (nao substring)."""
    identity = _load_identity()
    contacts = identity.get("email_contacts", {})
    profiles = _load_family_profiles()

    email_lower = email_address.lower().strip()

    # 1. Match direto em email_contacts (exato)
    for profile_key, contact_email in contacts.items():
        if contact_email.lower().strip() == email_lower:
            return profiles.get(profile_key) or {"name": profile_key}

    # 2. Match CONSERVATIVO por token do local-part (exato, nao substring).
    #    Isto evita o bug "marco" bater em "marcos@...": so casa se uma token
    #    inteira do local-part for IGUAL a um nome do perfil.
    local_part = email_lower.split("@", 1)[0]
    tokens = {t for t in re.split(r"[^a-z0-9]+", local_part) if t}
    for profile_key, profile in profiles.items():
        name_parts = profile.get("name", "").lower().split()
        for part in name_parts:
            if len(part) >= 3 and part in tokens:
                return profile

    return None


def _generate_reply(email_from: str, email_subject: str, email_body: str,
                    profile: dict, llm_fn: Optional[Callable[[str], str]] = None) -> str:
    """Generate a reply. LLM se llm_fn disponivel; fallback seguro caso contrario."""
    name = profile.get("name", "Família")
    how = profile.get("how_mike_should_treat_her", profile.get("how_mike_should_treat_him", {}))
    tone = how.get("tone", "carinhoso e respeitoso") if isinstance(how, dict) else "carinhoso e respeitoso"
    memory = profile.get("memory_summary_for_mike", "")

    body_excerpt = (email_body or "").strip()
    if len(body_excerpt) > 1200:
        body_excerpt = body_excerpt[:1200] + "..."

    # ---- Caminho LLM ----
    if llm_fn is not None:
        prompt = (
            f"Escreve uma resposta de email para {name} (tom: {tone}).\n"
            f"Contexto sobre {name}: {memory or '(sem contexto extra)'}\n"
            f"Assunto do email recebido: {email_subject}\n"
            f"Corpo do email recebido:\n{body_excerpt}\n\n"
            f"Responde de forma util e especifica ao que ele/ela escreveu."
        )
        try:
            text = (llm_fn(prompt) or "").strip()
            if text:
                if len(text) > 1200:  # saneamento: limitar tamanho
                    text = text[:1200].rsplit("\n", 1)[0]
                return text
            log.warning("[auto_reply] llm_fn devolveu vazio; uso fallback.")
        except Exception as e:
            log.warning("[auto_reply] LLM falhou (%s); uso fallback seguro.", e)

    # ---- Fallback SEGURO (curto, honesto; NAO e o marketing canned antigo) ----
    subj_ref = f' "{email_subject.strip()}"' if email_subject.strip() else ""
    return (
        f"Olá, {name}!\n\n"
        f"Recebi o seu email{subj_ref} e vou tratar disso assim que possível. "
        f"Se for urgente, o Marco será avisado.\n\n"
        f"Com carinho,\nMike"
    )


# ── Main entry point ─────────────────────────────────────────────────

def auto_reply_to_family(list_inbox_fn=None, read_email_fn=None, send_email_fn=None,
                         llm_fn: Optional[Callable[[str], str]] = None) -> dict:
    """
    Check unread emails, auto-reply to family members.

    Args:
        list_inbox_fn: callable(limit, query) -> list of email dicts
        read_email_fn: callable(email_id) -> dict with full content
        send_email_fn: callable(to, subject, body) -> result dict (with "ok")
        llm_fn: optional callable(prompt) -> str (resposta LLM). Se None, usa fallback.

    Returns:
        {"replied": int, "skipped": int, "errors": int, "details": [...]}
    """
    # Seguranca: auto-send OFF por default.
    if not _env_bool("MIKE_AUTO_REPLY_ENABLED", False):
        return {
            "replied": 0, "skipped": 0, "errors": 0, "details": [],
            "note": "auto-reply desativado (define MIKE_AUTO_REPLY_ENABLED=true para ativar)",
        }

    if not list_inbox_fn:
        return {"replied": 0, "skipped": 0, "errors": 0, "details": [], "note": "no list_inbox_fn"}

    result = {"replied": 0, "skipped": 0, "errors": 0, "details": []}

    try:
        # Get unread emails
        unread = list_inbox_fn(limit=10, query="is:unread")
        if not unread:
            return result
        if isinstance(unread, list) and unread and unread[0].get("error"):
            result["errors"] += 1
            result["details"].append({
                "action": "configuration_error",
                "error": str(unread[0]["error"])[:200],
            })
            return result

        identity = _load_identity()
        contacts = identity.get("email_contacts", {})
        family_emails = {v.lower().strip(): k for k, v in contacts.items()}

        for email in unread:
            email_id = email.get("id", "")
            sender = email.get("sender", email.get("from", ""))
            subject = email.get("subject", "")
            snippet = email.get("snippet", "")

            # Extract email address from sender
            sender_email = ""
            match = re.search(r'<([^>]+)>', sender)
            if match:
                sender_email = match.group(1).lower().strip()
            else:
                sender_email = sender.lower().strip()

            # NEVER reply to Mike's own emails (prevents infinite loop)
            mike_own_emails = {
                _env("MIKE_SMTP_USER", "").lower().strip(),
                "marcobarreto007@gmail.com",
                "marcobarreto548@gmail.com",
                "mike@familia-barreto.local",
            }
            mike_own_emails.discard("")
            if sender_email in mike_own_emails:
                result["skipped"] += 1
                result["details"].append({
                    "sender": sender_email,
                    "subject": subject,
                    "action": "skipped",
                    "reason": "mike_self",
                })
                continue

            # Check if sender is family
            profile_key = family_emails.get(sender_email)
            profile = _find_profile_by_email(sender_email)

            if not profile:
                result["skipped"] += 1
                result["details"].append({
                    "sender": sender_email,
                    "subject": subject,
                    "action": "skipped",
                    "reason": "not family",
                })
                continue

            # Rate-limit por destinatario (evita tempestade de respostas)
            if not _rate_limit_allows(sender_email):
                result["skipped"] += 1
                result["details"].append({
                    "sender": sender_email,
                    "subject": subject,
                    "action": "skipped",
                    "reason": "rate_limited",
                })
                continue

            # Read full email content
            full_body = snippet
            if read_email_fn:
                try:
                    full = read_email_fn(email_id)
                    full_body = full.get("body", full.get("snippet", snippet))
                except Exception as e:
                    log.warning("[auto_reply] Failed to read email %s: %s", email_id, e)

            # Generate reply (LLM ou fallback)
            reply_text = _generate_reply(sender, subject, full_body, profile, llm_fn=llm_fn)

            # Kill-switch: se engaged, NAO envia (congela outbound).
            if assert_not_frozen is not None:
                try:
                    assert_not_frozen(f"auto-reply email to {sender_email}")
                except Exception:  # KillSwitchEngaged
                    result["skipped"] += 1
                    result["details"].append({
                        "sender": sender_email,
                        "subject": subject,
                        "action": "skipped",
                        "reason": "kill_switch",
                    })
                    log.warning("[auto_reply] Envio bloqueado pelo kill-switch: %s", sender_email)
                    continue

            # Send reply
            if send_email_fn:
                try:
                    send_result = send_email_fn(sender_email, f"Re: {subject}", reply_text)
                    if send_result.get("ok"):
                        profile_name = profile.get("name", "?")
                        result["replied"] += 1
                        _rate_limit_mark(sender_email)
                        result["details"].append({
                            "sender": sender_email,
                            "profile": profile_name,
                            "subject": subject,
                            "action": "replied",
                            "via": "llm" if llm_fn else "fallback",
                        })
                        _save_action("auto_reply", sender_email, profile_name,
                                     f"Re: {subject}", f"Respondi a um email de {profile_name}")
                        log.info("Auto-replied to %s (%s)", profile.get("name"), subject)
                    else:
                        result["errors"] += 1
                        result["details"].append({
                            "sender": sender_email,
                            "subject": subject,
                            "action": "error",
                            "error": send_result.get("error", "unknown"),
                        })
                except Exception as e:
                    result["errors"] += 1
                    result["details"].append({
                        "sender": sender_email,
                        "subject": subject,
                        "action": "error",
                        "error": str(e)[:100],
                    })
            else:
                result["skipped"] += 1
                result["details"].append({
                    "sender": sender_email,
                    "subject": subject,
                    "action": "skipped",
                    "reason": "no send_email_fn",
                })

    except Exception as e:
        log.error("auto_reply_to_family failed: %s", e)
        result["errors"] += 1
        result["details"].append({"action": "fatal_error", "error": str(e)[:200]})

    return result
