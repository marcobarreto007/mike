# Contexto Mike (Python)

O conteúdo JS/Vitest/Playwright genérico deste agente continua válido, MAS para o projeto
Mike a stack é outra:

- **Stack:** Python 3.11, FastAPI, pytest. Testes em `tests/`.
- **Unitários (offline):** `tests/unit`, coletados por `pytest -q` (234 testes a passar).
- **Dependentes de runtime:** `tests/integration`, `tests/e2e`, `tests/run_mike_levels.py` — **não** coletados por defeito.
- **Harness e2e completo:** `tests/e2e/run_mike_a_to_z.ps1` — **delegar execução/diagnóstico profundo ao `mike-e2e`** (ele conhece o schema do report e os check IDs).
- **Pré-requisitos para testes de runtime:** Qwen `:8081` + MIKE `:8083` de pé.
- **Regra local-only:** o MIKE é 100% local; testes não devem depender de APIs externas (a menos que testem explicitamente essa integração).
- **Factory data:** usar dados realistas, nunca `"test"`/`"foo"` (já está nas anti-padrões).
