# Padrões de review do Mike

Rever código contra estes padrões (read-only; output no formato 🔴/🟡/🔵/✅ do `swat-review`):

- **Local-only (veto automático):** qualquer sugestão de cloud/OpenAI/DeepSeek/Anthropic como LLM do MIKE = bloqueante. Sem fallback silencioso p/ mock/cloud.
- **Two-process:** MIKE (FastAPI:8083, corpo, **não gera texto**) ↔ llama-server (Qwen:8081, cérebro). Código que acede LLM deve ir pelo backend `llama_server` em `config/.env.runtime`, nunca instanciar outro modelo.
- **Segurança:** HMAC-SHA256 (sessões), PBKDF2-HMAC-SHA256 (senhas), comparação constante, isolamento por perfil, `_can_view_operational_details` antes de dados operacionais, confinamento PowerShell às raízes permitidas (NUNCA alargar sem justificação), integração indisponível → erro real (não simular sucesso).
- **Estrutura FastAPI:** rotas em `core/server/mike_routes_*.py`, helpers em `mike_*_helpers.py`, estado via `shared_state`. Manter extrações (não reintroduzir lógica no monólito).
- **Testes:** unitários em `tests/unit` (offline, coletados pelo pytest); dependentes de runtime em `tests/integration` + `tests/e2e` (não coletados). Código novo de lógica → teste unitário.
- **Sem secrets no código** (.env.runtime não é versionado); sem `select *`; sem N+1.
