# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.
# Extracted from mike_server.py — Phase 3 refactor

"""
Mike - Install Routes
=====================
Profile-specific install page, desktop shortcut generation, and tunnel URL resolution.
"""

import logging

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from mike_config import HOST, PORT, PROJECT_ROOT
from mike_auth import (
    extract_api_key,
    extract_profile_session,
    generate_magic_token,
    MAGIC_LINK_TTL_DAYS,
    verify_api_key,
)
from mike_family_profiles import _get_family_profile

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Install HTML Template
# ---------------------------------------------------------------------------

_INSTALL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Instalar Mike — {display_name}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: linear-gradient(135deg, #0a0806 0%, #1a1410 50%, #0d0a07 100%);
    color: #e8d5c0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  }}
  .card {{
    background: rgba(20,16,12,0.95); border-radius: 24px; padding: 48px 40px;
    max-width: 480px; width: 90%; text-align: center;
    border: 1px solid rgba(232,160,76,0.2); box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }}
  .avatar {{
    width: 100px; height: 100px; border-radius: 50%; object-fit: cover;
    border: 3px solid rgba(232,160,76,0.4); box-shadow: 0 8px 32px rgba(232,160,76,0.15);
    margin-bottom: 24px;
  }}
  h1 {{ font-family: 'Georgia', serif; font-size: 2rem; color: #e8a04c; margin-bottom: 8px; }}
  .sub {{ color: #a89880; font-size: 1rem; margin-bottom: 24px; }}
  .greeting {{ font-size: 1.1rem; color: #c8b8a0; margin-bottom: 32px; line-height: 1.6; }}
  .btn {{
    display: inline-block; padding: 16px 40px; border-radius: 50px;
    font-size: 1.1rem; font-weight: 600; text-decoration: none; cursor: pointer;
    transition: all 0.2s; border: none; margin: 8px;
  }}
  .btn-primary {{
    background: linear-gradient(135deg, #e8a04c, #c87a2c);
    color: #0a0806; box-shadow: 0 4px 20px rgba(232,160,76,0.3);
  }}
  .btn-primary:hover {{ transform: translateY(-2px); box-shadow: 0 8px 30px rgba(232,160,76,0.4); }}
  .btn-outline {{
    background: transparent; color: #e8a04c;
    border: 2px solid rgba(232,160,76,0.4);
  }}
  .btn-outline:hover {{ background: rgba(232,160,76,0.08); }}
  .steps {{ text-align: left; margin: 32px 0; padding: 0 20px; }}
  .steps li {{ color: #a89880; margin-bottom: 12px; font-size: 0.95rem; line-height: 1.5; }}
  .steps li span {{ color: #e8a04c; font-weight: 600; }}
  .footer {{ margin-top: 32px; font-size: 0.8rem; color: #605040; }}
  .footer a {{ color: #907050; }}
</style>
</head>
<body>
<div class="card">
  <img src="/static/icons/mike-icon-512.png" alt="Mike" class="avatar"
       onerror="this.style.display='none'">
  <h1>MIKE</h1>
  <p class="sub">Yorkshire Digital da Família Barreto</p>
  <p class="greeting">Olá, <strong>{display_name}</strong>! 🐾<br>
  Sou o Mike, seu ajudante fiel. Vamos me instalar no seu dispositivo?</p>

  <div id="desktop-actions">
    <a href="/install/{profile_key}/shortcut?token={magic_token}" class="btn btn-primary" download>
      ⬇ Baixar Ícone para Área de Trabalho
    </a>
    <p style="color:#706050;font-size:0.85rem;margin-top:12px;">
      Depois de baixar, clique duas vezes no arquivo.<br>
      Ele vai abrir o Mike direto no seu perfil.
    </p>
    <button class="btn btn-outline" onclick="window.open('/?magic={magic_token}', '_blank')">
      🚀 Abrir Mike Agora
    </button>
  </div>

  <div id="mobile-actions" style="display:none;">
    <a href="/?magic={magic_token}" class="btn btn-primary" style="display:block;width:100%;font-size:1.3rem;padding:20px;">
      Falar com o Mike 🐾
    </a>
    <p style="color:#907050;font-size:0.85rem;margin-top:16px;">
      Guarde este link nos favoritos do navegador.<br>
      Assim o Mike fica sempre à mão.
    </p>
  </div>

  <p class="footer">
    Este link é só seu, {display_name}.<br>
    Se precisar de ajuda, fale com o Marco.
  </p>
</div>
<script>
  var isMobile = /Android|iPhone|iPad|iPod|webOS/i.test(navigator.userAgent);
  document.getElementById('desktop-actions').style.display = isMobile ? 'none' : '';
  document.getElementById('mobile-actions').style.display = isMobile ? '' : 'none';
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Install Route Handlers
# Note: the @app.get decorators are applied in mike_server.py
# ---------------------------------------------------------------------------

async def install_page(profile_key: str, request: Request):
    session_payload = extract_profile_session(request)
    session_profile = str((session_payload or {}).get("profile") or "").strip().lower()
    if (
        session_profile not in {"marco", "anapaula"}
        and not verify_api_key(extract_api_key(request))
    ):
        return JSONResponse(
            status_code=403,
            content={"error": "Acesso negado. Requer sessao de owner ou MIKE_API_KEY."},
        )

    normalized = str(profile_key or "").strip().lower()
    profile = _get_family_profile(normalized)
    if not profile:
        return JSONResponse(status_code=404, content={"error": "Perfil nao encontrado"})

    magic_token = generate_magic_token(normalized, ttl_days=MAGIC_LINK_TTL_DAYS)
    display_name = profile.get("name", normalized.capitalize())

    html = _INSTALL_HTML_TEMPLATE.format(
        display_name=display_name,
        profile_key=normalized,
        magic_token=magic_token,
    )
    return HTMLResponse(content=html, status_code=200)


def _get_tunnel_base() -> str:
    tunnel_file = PROJECT_ROOT / "data" / "tunnel_url_atual.txt"
    if tunnel_file.exists():
        try:
            url = tunnel_file.read_text(encoding="utf-8-sig").splitlines()[0].strip()
            if url:
                return url.rstrip("/")
        except Exception as e:
            log.warning("[server] Failed to read tunnel URL file: %s", e)
    return f"http://{HOST}:{PORT}"


async def install_shortcut(profile_key: str, token: str = ""):
    normalized = str(profile_key or "").strip().lower()
    profile = _get_family_profile(normalized)
    if not profile or not token:
        return JSONResponse(status_code=404, content={"error": "Perfil ou token invalido"})

    display_name = profile.get("name", normalized.capitalize())
    base = _get_tunnel_base()
    magic_url = f"{base}/?magic={token}"

    url_content = f"""[InternetShortcut]
URL={magic_url}
IconFile={base}/static/icons/mike-icon-64.png
IconIndex=0
"""
    filename = f"Mike - {display_name}.url"
    return Response(
        content=url_content.encode("utf-8-sig"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
