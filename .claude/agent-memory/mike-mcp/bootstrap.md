# Bootstrap dos MCP servers/tools

- **Ficheiros de config:** `.mcp.json` (raiz) e manifesto MCP em `config/`. Servidores MCP em `core/mcp/` (Gmail, Calendar, Drive, Excel, GA4, Ads, Shopify...).
- **Escala (validado 30/07/2026):** 18 servidores MCP, 186 tools descobertas.
- **Descoberta:** `GET /v1/client/bootstrap` → `tool_summary.tool_count` + manifest filtrado por perfil. Stats em `GET /stats` (`_update_mcp_stats`).
- **Auth/perfil:** manifest filtrado por `filter_tool_manifest` + `_request_profile_scope`; tools sensíveis só ao proprietário.
- **MCP do Claude Code (não do MIKE):** `.claude/settings.local.json` ativa `mike-ai` e `context7`.
- **Problemas típicos:** conflito de nomes de tools entre servidores, servidor que arranca mas não publica tools, scopes OAuth em falta (Google Workspace).
- **Integrações indisponíveis:** devem devolver erro real, nunca simular sucesso.
