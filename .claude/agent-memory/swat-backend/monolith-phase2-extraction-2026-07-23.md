---
name: monolith-phase2-extraction-2026-07-23
description: 6 modules extracted from mike_server.py — Phase 2: vision, llm_boot, chat_builder, chat_completion, tools_local, family_profiles
metadata:
  type: project
---

# Monolith Phase 2 Extraction — 2026-07-23

6 modules extracted from `mike_server.py`, completing the planned Phase 2 breakup after Phase 1 (models, sse, stats, request_helpers, payload_helpers).

## Modules extracted

### 6. `mike_vision.py`
- Functions: `_DATA_URI_RE`, `_image_url_from_part()`, `_extract_image_parts()`, `_decode_data_uri_image()`, `_inspect_decoded_image()`, `_validate_vision_messages()`, `_has_images()`, `_build_vision_messages()`, `_vision_stop_sequences()`
- Dependencies: `mike_models`, `mike_stats`, `mike_config`, `mike_web` (local import)

### 7. `mike_llm_boot.py`
- Functions: `_native_gemma4_chat_handler_class()`, `_vision_handler_backend_label()`, `_uses_native_gemma4_vision_handler()`, `_apply_vision_batch_floor()`, `_llama_boot_candidates()`, `_record_llm_boot()`, `_create_llm_with_fallback()`, `_resolve_hf_file()`
- Dependencies: `mike_config`, `mike_stats`, `huggingface_hub`
- Constants: `_VISION_NATIVE_BATCH_FLOOR`, `_VISION_NATIVE_UBATCH_FLOOR`

### 8. `mike_chat_builder.py`
- Function: `_build_messages()` — most complex function, parameterized to accept callables/singletons from `mike_server.py` to avoid circular imports
- Dependencies: `mike_models`, `mike_request_helpers`, `mike_vision`, `mike_config`, `mike_stats`, `mike_web` (local import)
- Thin wrapper in `mike_server.py` injects all server-local callables (family profiles, web search, search routes, memory_service)

### 9. `mike_chat_completion.py`
- Functions: `_estimate_complexity()`, `_blocking_chat_completion()`, `_blocking_chat_completion_stream()`, `_response_completion_tokens()`, `_generate_model_response()`
- Uses `shared_state` for all singletons: `llm`, `llm_lock`, `vision_handler`, `model_router`, `fallback_chain`, `deepseek_client`
- Dependencies: `mike_config`, `mike_models`, `mike_stats`, `mike_token_budget`, `mike_vision`, `mike_fallback_chain`

### 10. `mike_tools_local.py` (largest at ~800 lines)
- Functions: `_local_tool_manifest()`, `_visible_tool_manifest()`, `_execute_local_tool()`, `_compact_tool_payload()`, `_parse_tool_payload_records()`, `_resolve_tool_session_id()`, `_project_root_relative_tool_args()`, `_execute_mcp_tool()`
- `_current_tool_session_id` ContextVar
- Uses `shared_state` for: `memory_service`, `web_search`, `deepseek_client`, `mcp_workspace`, `skill_registry`

### 11. `mike_family_profiles.py`
- Functions: `_get_family_profiles_path()`, `_load_family_profiles()`, `_get_family_profile()`, `_format_family_profile_for_llm()`
- Cache vars: `_family_profiles_cache`, `_family_profiles_path`
- Dependencies: `mike_config`

## shared_state additions
- Added `llm_lock` and `vision_handler` fields to `shared_state.py` for use by `mike_chat_completion.py`
- `mike_server.py` populates `_shared_state.llm_lock`, `_shared_state.vision_handler` at module level and in `_startup()`

## Important notes
- `_build_messages` in `mike_chat_builder.py` uses parameter injection for functions still in `mike_server.py` (family profiles, web search helpers, search routes). The thin wrapper in `mike_server.py` bridges all dependencies.
- `_create_vision_handler()` stays in `mike_server.py` (not extracted); now imports `_native_gemma4_chat_handler_class` from `mike_llm_boot`.
- During extraction, a Python script was used for mass removal of tool functions; restored `_should_search_web()`, `_search_web()`, `_format_web_results()`, and the `_build_messages` thin wrapper that were accidentally removed.
