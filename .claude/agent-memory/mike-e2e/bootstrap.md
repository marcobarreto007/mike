# Bootstrap do harness a-to-z

- **Script:** `tests/e2e/run_mike_a_to_z.ps1` (params: `-Port 8080` default, `-AuthKey`, `-ReportPath`).
  Nota: o script usa `$Port` default 8080 internamente para alguns checks, mas o MIKE real corre em 8083 — confirmar a porta esperada antes de invocar.
- **Pré-requisitos:** Qwen `:8081` E MIKE `:8083` de pé; `python`, `nssm` no PATH.
- **Report JSON** (escrito em `mike/roadmap/a_to_z_test_report_<ts>.json`, schema `mike.a_to_z.v1`):
  `overall_status` ∈ {`passed`, `passed_with_blockers`, `failed`}; `checks[]` c/ `id`, `status` ∈ {`passed`,`failed`,`blocked`}, `error`, `details`.
- **Check IDs e o que validam:**
  - `bootstrap_normal_runtime` — arranca MIKE normal + `/health`.
  - `py_compile` — `python -m py_compile` de `src/mike_server.py`, `mike_memory.py`, `mike_web.py` + testes.
  - `unit_tests` — `python -m unittest discover -s tests`.
  - `integration_levels_1_to_5` — `tests/run_mike_levels.py`.
  - `core_endpoints` — `/health`, `/stats`, `/v1/runtime`, `/v1/client/bootstrap`, `/v1/chat/sessions`, `/v1/roadmap`, `/v1/backups`, `/v1/models`.
  - `dashboard_assets` — HTML/JS/CSS do dashboard ("Mike Operator Console", `refreshBootstrap`, etc.).
  - `backup_export_restore_cycle` — `scripts/backup_mike.ps1` backup/export/restore + restore-guard.
  - `operational_scripts` — `scripts/recover_mike.ps1` status/logs/restart.
  - `auth_hardening_cycle` — liga `MIKE_API_KEY` + `MIKE_TRUST_LOCALHOST=false`; espera 401 sem auth, 200 com auth.
  - `windows_service_cycle` — instala/remove serviço via NSSM (precisa SCM elevado; senão `status=blocked`).
- **Exit code:** 1 se algum `failed`. `blocked` não falha o exit.
- **Diagnóstico:** para falhas de startup/bootstrap → delegar causa-raiz a `mike-architect`.
