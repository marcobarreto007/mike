---
name: monolith-phase1-extraction-2026-07-23
description: Phase 1 of mike_server.py monolith breakup — 5 modules extracted, all imports verified
metadata:
  type: project
---

# Monolith Phase 1 Extraction Complete (2026-07-23)

**Why:** Phase 1 of the mike_server.py monolith breakup per swat-architect plan. Low-risk extraction of pure helpers and models.

**Modules created (all in `core/server/`):**

1. **`mike_models.py`** (100 lines) — All Pydantic request/response models:
   - ChatMessage, ChatRequest, ProfileLoginRequest, PasswordChangeRequest
   - MagicLinkGenerateRequest, MagicLinkUseRequest, MagicLinkRevokeRequest
   - ManualToolCallRequest, KnowledgeUpsertRequest, VisionInputError

2. **`mike_sse.py`** (97 lines) — SSE streaming utilities (pure, no state):
   - _sse_event, _sse_comment, _sse_content_chunk, _stream_headers
   - _stream_error, _request_disconnected, _normalize_reasoning_text
   - _UNICODE_SMART_QUOTES_RE

3. **`mike_stats.py`** (186 lines) — Runtime stats singleton + helpers:
   - stats dict, _stats_lock, _inc_stat, _vision_limits, _error_payload
   - _update_mcp_stats (refactored to accept mcp_workspace as parameter)

4. **`mike_request_helpers.py`** (169 lines) — Request inspection helpers:
   - _request_private_mode, _request_raw_mode, _request_persist_conversation
   - _request_full_chat_context, _use_light_chat_context, _light_system_prompt
   - _requested_profile_key, _request_profile_scope, _request_has_valid_api_key
   - _can_view_operational_details, _FULL_CONTEXT_REQUEST_RE

5. **`mike_payload_helpers.py`** (204 lines) — API response payload builders:
   - _tool_summary_payload, _sanitize_tool_manifest, _tools_payload
   - _sanitized_runtime_payload, _sanitized_stats_payload, _health_payload
   - _models_payload, _chat_capabilities_payload, _vision_capabilities_payload
   - _monitor_payload

**How to apply:** mike_server.py keeps thin wrappers for functions that need singletons (mcp_workspace, llm, memory_service, SOUL_PROMPT). All routers import from mike_server and need no changes. Phase 2 should move the wrapper functions to the modules and update router imports to point directly at the new modules.

**Key design decisions:**
- No circular dependencies — modules form a clean DAG
- shared_state.py used for lazy runtime singleton access (mcp_workspace, memory_service, llm)
- PROFILE_AUTH_ENABLED and PROFILE_CREDENTIALS live in mike_auth.py, not mike_config.py
- mike_server.py reduced from 6272 to 5909 lines (-363, -5.8%)
