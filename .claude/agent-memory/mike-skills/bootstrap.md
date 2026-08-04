# Bootstrap do catálogo de skills

- **Catálogo:** `skills/*.yaml` — skills DO PRÓPRIO MIKE (não confundir com skills do Claude Code). 54 skills (validado 30/07/2026).
- **Governance:** `skills/skill_governance.yaml` + `skills/skill_creator.yaml` (criação) — respeitar antes de adicionar/editar.
- **Cobertura de tools:** README reporta 100% de cobertura dos padrões de tools das skills. Skills referenciam tools dos MCP servers; quebra de cobertura = regressão.
- **Exemplos relevantes:** `rag_memory_engineer.yaml`, `test_automator.yaml`, `test_harness.yaml`, `code_review.yaml`, `code_architect.yaml`, `deep_research.yaml`, `document_processing.yaml`.
- **Stack das skills:** YAML declarativo; algumas acionam rotinas de `core/autonomy/` (skills + task board).
- **Validação ao editar:** manter schema YAML consistente, garantir que tools referenciadas existem no manifest, correr readiness depois.
