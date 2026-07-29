---
name: mcp-common-module
description: Shared path utilities extracted from duplicated MCP code into mike_mcp_common.py
metadata:
  type: project
---

`core/mcp/mike_mcp_common.py` was created on 2026-07-23 to deduplicate four functions that were identical (or near-identical) in `mike_excel_mcp.py` and `mike_workspace_mcp.py`.

**Why:** Both MCP servers had their own copies of the same path-resolution logic. Any bug fix or improvement would need to be applied in two places.

**Module contents:**
- `normalize_path(path: Path) -> str` — former `_norm`
- `is_inside_roots(candidate, roots) -> bool` — former `_inside_any_root`
- `resolve_safe_path(raw_path, allowed_roots, *, must_exist=False) -> Path` — former `_resolve_path`; now takes `allowed_roots` as explicit parameter instead of module global
- `collect_allowed_roots(argv, *, default_roots=None, validate_cli_args=False) -> list[Path]` — unified version of both `_collect_allowed_roots` variants. `default_roots` provides fallback (Excel uses `Path(__file__).parent.parent`). `validate_cli_args=True` preserves Excel's existing-path-only CLI arg filtering. When `default_roots` is None and nothing is configured, raises `SystemExit` (original Workspace behaviour).

**Files updated:**
- `mike_workspace_mcp.py` — imports `collect_allowed_roots`, `normalize_path`, `resolve_safe_path`
- `mike_excel_mcp.py` — imports `collect_allowed_roots`, `resolve_safe_path`

**Other MCPs checked:** `mike_drive_mcp.py`, `mike_github_mcp.py`, `mike_huggingface_mcp.py`, `mike_remote_exec_mcp.py`, `mike_scraper_mcp.py`, `mike_calendar_mcp.py`, `mike_email_mcp.py`, `mike_appointments_mcp.py` — none had the same duplicated path utilities. Only the scraper had a `_normalize_session_id` function, which is unrelated.
