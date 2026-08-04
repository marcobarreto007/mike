# Padrões do Mike

As dimensões genéricas de review deste agente (correção, segurança, performance,
manutenibilidade, padrões, design) aplicam-se, mas no projeto Mike há padrões de casa
específicos. **Para review profunda contra os padrões do Mike, delegar ao `mike-review`**
(ele tem a checklist completa). Resumo rápido:

- **Local-only (veto):** nunca cloud/OpenAI/DeepSeek como LLM do MIKE; sem fallback silencioso.
- **Two-process:** MIKE (FastAPI:8083, não gera texto) ↔ Qwen (llama-server:8081).
- **Segurança:** HMAC-SHA256, PBKDF2, comparação constante, isolamento por perfil, confinamento PowerShell, integração indisponível → erro real.
- **Estrutura:** rotas em `mike_routes_*.py`, helpers em `mike_*_helpers.py`, estado em `shared_state`.
- **Testes:** `tests/unit` (pytest, offline); runtime em `tests/integration`+`tests/e2e`.
