# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike - Tool-Call Parser
=======================
Extraction, validation, and formatting of tool calls from LLM output.
Extracted from mike_mcp_client.py to keep parsing logic self-contained.
"""
import json
import logging
import re
from contextlib import suppress
from typing import List, Optional

_log = logging.getLogger("mike.tool_parser")

# ---------------------------------------------------------------------------
# Tool-call extraction & formatting (used by chat loop)
# ---------------------------------------------------------------------------

# Match tagged tool calls: <tool_call>{...}</tool_call> OR <tool_call>{...} (missing closing tag)
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*\})\s*(?:</tool_call>)?", re.DOTALL)
LEGACY_TOOL_CALL_RE = re.compile(
    r"<\|tool_call\>\s*call:\s*([A-Za-z0-9_.:-]+)\s*(?:\((\{.*?\})\)|(\{.*?\}))\s*(?:<tool_call\|>)?",
    re.DOTALL,
)


def format_tool_manifest(tool_manifest: List[dict]) -> str:
    lines = []
    for tool in tool_manifest:
        schema = tool.get("input_schema") or tool.get("inputSchema") or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        arg_names = ", ".join(properties.keys()) if properties else "sem argumentos"
        meta = []
        if tool.get("server_name"):
            meta.append(f"server={tool['server_name']}")
        if tool.get("capabilities"):
            meta.append("caps=" + "/".join(tool["capabilities"]))
        if tool.get("access"):
            meta.append(f"acesso={tool['access']}")
        meta_suffix = f" [{' | '.join(meta)}]" if meta else ""
        lines.append(f"- {tool['name']}({arg_names}){meta_suffix}")
    return "\n".join(lines)

# Fallback: bare JSON with "name" + "arguments" keys, not wrapped in tags.
# Only matches JSON objects that look like tool calls (have both keys).
_BARE_TOOL_JSON_RE = re.compile(
    r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{',
    re.DOTALL,
)

# Alternative XML style emitted by some models:
# <function_calls><invoke name="tool"><parameter name="x">y</parameter></invoke></function_calls>
_FUNCTION_CALL_INVOKE_RE = re.compile(
    r"<invoke\s+name=\"([^\"]+)\"\s*>(.*?)</invoke>",
    re.DOTALL | re.IGNORECASE,
)
_FUNCTION_CALL_PARAM_RE = re.compile(
    r"<parameter\s+name=\"([^\"]+)\"\s*>(.*?)</parameter>",
    re.DOTALL | re.IGNORECASE,
)
_FUNCTION_CALL_WRAPPER_RE = re.compile(
    r"</?function_calls[^>]*>",
    re.IGNORECASE,
)


# Markdown code-block wrapper the LLM sometimes emits around tool calls
_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# Python-style tool call: <tool_call>DDGS(query="...")</tool_call>
_PYTHON_STYLE_RE = re.compile(
    r"<tool_call>\s*([\w.]+)\s*\(([^)]*)\)\s*(?:</tool_call>)?",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Documented tool-call detection patterns
# ---------------------------------------------------------------------------
_TOOL_CALL_PATTERNS = {
    # 1) Tagged JSON: <tool_call>{"name":"...","arguments":{...}}</tool_call>
    #    Handles both with and without closing tag, arbitrary whitespace.
    "tagged_json": re.compile(
        r"<tool_call>\s*(\{.*\})\s*(?:</tool_call>)?",
        re.DOTALL,
    ),
    # 2) Markdown code block: ```json {"name":"...","arguments":{...}} ```
    "markdown_code_block": re.compile(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        re.DOTALL,
    ),
    # 3) Bare JSON object containing "name" and "arguments" keys
    "bare_json": re.compile(
        r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{',
        re.DOTALL,
    ),
    # 4) XML function_calls: <function_calls><invoke name="X"><parameter>...</invoke></function_calls>
    "function_call_invoke": re.compile(
        r"<invoke\s+name=\"([^\"]+)\"\s*>(.*?)</invoke>",
        re.DOTALL | re.IGNORECASE,
    ),
    # 5) Legacy llama.cpp style: <|tool_call>call:NAME({...})<tool_call|>
    "legacy_llama": re.compile(
        r"<\|tool_call\>\s*call:\s*([A-Za-z0-9_.:-]+)\s*(?:\((\{.*?\})\)|(\{.*?\}))\s*(?:<tool_call\|>)?",
        re.DOTALL,
    ),
    # 6) Python function-style: <tool_call>FUNC_NAME(key="val")</tool_call>
    "python_style": re.compile(
        r"<tool_call>\s*([\w.]+)\s*\(([^)]*)\)\s*(?:</tool_call>)?",
        re.DOTALL,
    ),
}


def _try_parse_tool_json(raw: str) -> Optional[dict]:
    """Try to parse raw JSON string as a tool-call payload."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = _parse_legacy_mapping(raw)
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("name") or payload.get("tool") or "").strip()
    arguments = payload.get("arguments") or payload.get("args") or payload.get("parameters") or {}
    if name and isinstance(arguments, dict):
        return {"name": name.replace(":", "."), "arguments": arguments}
    return None


def _extract_balanced_json(text: str, start: int) -> Optional[str]:
    """Extract a balanced JSON object starting at *start* index."""
    depth = 0
    end = start
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            if in_string:
                escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                return text[start:end]
    return None


def _extract_balanced_fragment(text: str, start: int) -> Optional[tuple[str, int]]:
    pairs = {"{": "}", "[": "]", "(": ")"}
    opener = text[start] if 0 <= start < len(text) else ""
    closer = pairs.get(opener)
    if not closer:
        return None

    stack = [closer]
    in_string: Optional[str] = None
    escape = False
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == in_string:
                in_string = None
            continue
        if ch in {'"', "'"}:
            in_string = ch
            continue
        if ch in pairs:
            stack.append(pairs[ch])
            continue
        if stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return text[start:i + 1], i + 1
    return None


def _parse_legacy_string(text: str, start: int) -> tuple[str, int]:
    quote = text[start]
    chars: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            chars.append(
                {
                    "n": "\n",
                    "r": "\r",
                    "t": "\t",
                    "\\": "\\",
                    '"': '"',
                    "'": "'",
                }.get(nxt, nxt)
            )
            i += 2
            continue
        if ch == quote:
            return "".join(chars), i + 1
        chars.append(ch)
        i += 1
    return "".join(chars), i


def _parse_legacy_scalar(value: str):
    text = value.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        with suppress(ValueError):
            return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        with suppress(ValueError):
            return float(text)
    return text


def _consume_legacy_value(text: str, start: int) -> tuple[object, int]:
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        return "", i

    ch = text[i]
    if ch in {'"', "'"}:
        return _parse_legacy_string(text, i)
    if ch == "{":
        fragment = _extract_balanced_fragment(text, i)
        if fragment:
            raw, end = fragment
            parsed = _parse_legacy_mapping(raw)
            return (parsed if parsed is not None else raw), end
    if ch == "[":
        fragment = _extract_balanced_fragment(text, i)
        if fragment:
            raw, end = fragment
            parsed = _parse_legacy_list(raw)
            return (parsed if parsed is not None else raw), end

    end = i
    while end < len(text) and text[end] not in {",", "}", "]"}:
        end += 1
    return _parse_legacy_scalar(text[i:end]), end


def _parse_legacy_list(source: str) -> Optional[list[object]]:
    text = source.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    items: list[object] = []
    i = 1
    limit = len(text) - 1
    while i < limit:
        while i < limit and (text[i].isspace() or text[i] == ","):
            i += 1
        if i >= limit:
            break
        value, i = _consume_legacy_value(text, i)
        items.append(value)
        while i < limit and text[i].isspace():
            i += 1
        if i < limit and text[i] == ",":
            i += 1
    return items


def _parse_legacy_mapping(source: str) -> Optional[dict]:
    text = source.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None

    result: dict[str, object] = {}
    i = 1
    limit = len(text) - 1
    while i < limit:
        while i < limit and (text[i].isspace() or text[i] == ","):
            i += 1
        if i >= limit:
            break

        if text[i] in {'"', "'"}:
            key, i = _parse_legacy_string(text, i)
        else:
            key_start = i
            while i < limit and text[i] not in {":", "}"}:
                i += 1
            key = text[key_start:i].strip()

        if not key or i >= limit or text[i] != ":":
            return None
        i += 1
        value, i = _consume_legacy_value(text, i)
        result[key] = value

        while i < limit and text[i].isspace():
            i += 1
        if i < limit and text[i] == ",":
            i += 1
    return result


def _extract_legacy_tool_call(text: str) -> Optional[dict]:
    if not text:
        return None
    match = LEGACY_TOOL_CALL_RE.search(text)
    if not match:
        return None

    raw_name = match.group(1).strip()
    normalized_name = ".".join(part for part in raw_name.split(":") if part)
    arguments = _parse_legacy_mapping(match.group(2) or match.group(3) or "{}")
    if normalized_name and isinstance(arguments, dict):
        return {"name": normalized_name, "arguments": arguments}
    return None


def _extract_function_call_xml(text: str) -> Optional[dict]:
    if not text:
        return None
    match = _FUNCTION_CALL_INVOKE_RE.search(text)
    if not match:
        return None
    name = (match.group(1) or "").strip().replace(":", ".")
    body = match.group(2) or ""
    if not name:
        return None
    arguments: dict[str, str] = {}
    for param in _FUNCTION_CALL_PARAM_RE.finditer(body):
        key = (param.group(1) or "").strip()
        value = (param.group(2) or "").strip()
        if key:
            arguments[key] = value
    return {"name": name, "arguments": arguments}


_TOOL_NAME_MAP = {
    "DDGS": "web.search_and_cache",
    "ddgs": "web.search_and_cache",
    "browse_search": "browse_search",
    "web_search": "web.search_and_cache",
    "search": "web.search_and_cache",
}


def _validate_tool_call(payload: Optional[dict]) -> Optional[dict]:
    """Validate and sanitize an extracted tool-call payload.

    Returns the payload if valid, or None if the payload is malformed.
    Logs a WARNING for malformed tool calls with a snippet of the raw text.
    """
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("name") or "").strip()
    arguments = payload.get("arguments")
    if not name:
        _log.warning(
            "Malformed tool call: empty or missing 'name' field. Payload keys: %s",
            list(payload.keys())[:10],
        )
        return None
    if not isinstance(arguments, dict):
        _log.warning(
            "Malformed tool call: 'arguments' is not a dict (type=%s) for tool %r",
            type(arguments).__name__,
            name,
        )
        return None
    return {"name": name, "arguments": arguments}


def extract_tool_call(text: str) -> Optional[dict]:
    """Extract a tool call from LLM output text.

    Attempts 6 detection strategies in priority order. Each successfully
    extracted payload is validated via :func:`_validate_tool_call` before
    returning.

    Returns a ``{"name": str, "arguments": dict}`` payload, or ``None`` if
    no valid tool call was found.
    """
    if not text:
        return None

    # ── 1) Tagged JSON: <tool_call>{"name":"...","arguments":{...}}</tool_call> ──
    match = _TOOL_CALL_PATTERNS["tagged_json"].search(text)
    if match:
        result = _try_parse_tool_json(match.group(1))
        validated = _validate_tool_call(result)
        if validated:
            return validated

        # Regex may have captured too much (greedy .*) or too little.
        # Fall back to balanced-brace extraction from the tag position.
        tag_end = text.index("<tool_call>") + len("<tool_call>")
        brace_start = text.find("{", tag_end)
        if brace_start >= 0:
            balanced = _extract_balanced_json(text, brace_start)
            if balanced:
                result = _try_parse_tool_json(balanced)
                validated = _validate_tool_call(result)
                if validated:
                    return validated

        # If we matched a <tool_call> tag but couldn't parse valid JSON,
        # log a warning so we can debug malformed tool calls.
        snippet = text[match.start():match.start() + 500]
        _log.warning(
            "Malformed tool call: <tool_call> tag found but JSON parse failed. Snippet: %r",
            snippet,
        )

    # ── 2) Markdown code block: ```json {"name":"...","arguments":{...}} ``` ──
    code_match = _TOOL_CALL_PATTERNS["markdown_code_block"].search(text)
    if code_match:
        result = _try_parse_tool_json(code_match.group(1))
        validated = _validate_tool_call(result)
        if validated:
            return validated
        snippet = text[code_match.start():code_match.start() + 500]
        _log.warning(
            "Malformed tool call: markdown code block found but JSON parse failed. Snippet: %r",
            snippet,
        )

    # ── 3) Bare JSON: {"name":"X","arguments":{...}} without any tag ──
    bare = _BARE_TOOL_JSON_RE.search(text)
    if bare:
        balanced = _extract_balanced_json(text, bare.start())
        if balanced:
            result = _try_parse_tool_json(balanced)
            validated = _validate_tool_call(result)
            if validated:
                return validated

    # ── 4) XML function_calls/invoke style ──
    xml_call = _extract_function_call_xml(text)
    validated_xml = _validate_tool_call(xml_call)
    if validated_xml:
        return validated_xml

    # ── 5) Legacy llama.cpp: <|tool_call>call:NAME({...})<tool_call|> ──
    legacy = _extract_legacy_tool_call(text)
    validated_legacy = _validate_tool_call(legacy)
    if validated_legacy:
        return validated_legacy

    # ── 6) Python function-style: <tool_call>FUNC(key="val")</tool_call> ──
    py_match = _TOOL_CALL_PATTERNS["python_style"].search(text)
    if py_match:
        raw_name = py_match.group(1).strip()
        raw_args = py_match.group(2).strip()
        tool_name = _TOOL_NAME_MAP.get(raw_name, raw_name)
        # Parse kwargs: key="val" or key='val' or key=val
        args: dict = {}
        for kv in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*?)"|\'([^\']*?)\'|(\S+))', raw_args):
            key = kv.group(1)
            val = kv.group(2) or kv.group(3) or kv.group(4) or ""
            args[key] = val
        # If no kwargs parsed, treat as positional "query"
        if not args and raw_args:
            args = {"query": raw_args.strip("\"' ")}
        validated_py = _validate_tool_call(
            {"name": tool_name, "arguments": args} if (tool_name and args) else None
        )
        if validated_py:
            return validated_py

    # ── Final cleanup: check if text contains <tool_call> tags at all ──
    # Models sometimes emit malformed or empty tool-call tags that we should log.
    if "<tool_call>" in text[:2000]:
        snippet = text[:500]
        _log.warning(
            "Malformed tool call: <tool_call> present in text but no pattern matched. Snippet: %r",
            snippet,
        )

    return None


def extract_tool_call_streaming(accumulated_text: str) -> dict:
    """Detect a tool call in partially-streamed text.

    Designed for real-time streaming: checks whether a tool-call XML tag
    has started but not completed.

    Returns a dict with:
    - ``complete``: ``True`` if a complete, valid tool call was found.
      ``False`` if a tool-call tag was opened but is still being streamed.
    - ``tool_call``: the parsed call (``{"name": str, "arguments": dict}``)
      when ``complete`` is ``True``.
    - ``partial_text``: the portion of text that looks like an open tag
      when ``complete`` is ``False`` (so the caller can decide to wait).
    """
    if not accumulated_text:
        return {"complete": False, "partial_text": ""}

    # 1) Try full extraction first
    full = extract_tool_call(accumulated_text)
    if full is not None:
        return {"complete": True, "tool_call": full}

    # 2) Check for incomplete <tool_call> opening tag
    stripped = accumulated_text.lstrip()
    if not stripped:
        return {"complete": False, "partial_text": ""}

    # Is there an opened <tool_call> tag?
    tag_start = accumulated_text.find("<tool_call>")
    if tag_start < 0:
        # Check XML function_calls style
        fc_start = accumulated_text.find("<function_calls")
        if fc_start >= 0:
            fc_end = accumulated_text.find("</function_calls>", fc_start)
            if fc_end < 0:
                return {
                    "complete": False,
                    "partial_text": accumulated_text[fc_start:],
                }
        # Check Lloyd.cpp legacy style
        lc_start = accumulated_text.find("<|tool_call>")
        if lc_start >= 0:
            lc_end = accumulated_text.find("<tool_call|>", lc_start)
            if lc_end < 0:
                return {
                    "complete": False,
                    "partial_text": accumulated_text[lc_start:],
                }
        return {"complete": False, "partial_text": ""}

    # Tag opened but not completed — check if we have enough to parse
    tag_end_pos = tag_start + len("<tool_call>")
    remaining = accumulated_text[tag_end_pos:]

    # Look for closing </tool_call>
    close_pos = remaining.find("</tool_call>")
    if close_pos < 0:
        # No closing tag yet — check if we have at least a complete JSON object
        brace_start = remaining.find("{")
        if brace_start >= 0:
            balanced = _extract_balanced_json(accumulated_text, tag_end_pos + brace_start)
            if balanced is not None:
                # We have balanced braces but no closing tag — could be valid
                # Try parsing what we have
                result = _try_parse_tool_json(balanced)
                validated = _validate_tool_call(result)
                if validated:
                    return {"complete": True, "tool_call": validated}
        # Still waiting for more text
        return {
            "complete": False,
            "partial_text": accumulated_text[tag_start:],
        }

    # Closing tag exists — extract from between tags
    content = remaining[:close_pos]
    brace_start_inner = content.find("{")
    if brace_start_inner >= 0:
        balanced = _extract_balanced_json(content, brace_start_inner)
        if balanced is not None:
            result = _try_parse_tool_json(balanced)
            validated = _validate_tool_call(result)
            if validated:
                return {"complete": True, "tool_call": validated}

    return {"complete": False, "partial_text": accumulated_text[tag_start:]}


def tool_instruction_block(tool_manifest: List[dict]) -> str:
    """Build tool guidance only from names that are actually loaded."""
    if not tool_manifest:
        return ""
    manifest_text = format_tool_manifest(tool_manifest)
    tool_names = sorted({
        str(tool.get("name") or "").strip()
        for tool in tool_manifest
        if str(tool.get("name") or "").strip()
    })
    categories = {
        "WEB": ("web.", "fetch.", "puppeteer."),
        "EMAIL": ("gmail.", "email."),
        "AGENDA": ("calendar.", "appointments."),
        "DRIVE": ("drive.",),
        "PLANILHAS": ("excel.",),
        "ARQUIVOS/COMANDOS": (
            "filesystem.", "list_directory", "read_text_file", "write_file",
            "edit_file", "run_command", "execute_powershell",
        ),
        "GITHUB": ("github.",),
        "HUGGING FACE": ("hf.",),
        "AUTONOMIA": (
            "autonomy_", "list_tasks", "create_task", "complete_task",
            "cancel_task", "list_routines", "toggle_routine",
            "run_routine_now", "create_routine", "track_sent_email",
            "list_tracked_emails", "check_email_responses",
            "dismiss_tracked_email",
        ),
        "MEMORIA": ("memory.", "memory-persistent.", "mike.hot_cache"),
    }
    guide_lines = []
    for label, prefixes in categories.items():
        available = [
            name
            for name in tool_names
            if any(
                name == prefix
                or name.startswith(prefix)
                or name.endswith("." + prefix)
                for prefix in prefixes
            )
        ]
        if available:
            guide_lines.append(f"{label}: {', '.join(available)}")
    dynamic_guide = "\n".join(guide_lines)
    return (
        "TOOLS DISPONIVEIS (MCP):\n"
        f"{manifest_text}\n\n"
        "FORMATO OBRIGATORIO para chamar tool:\n"
        "<tool_call>{\"name\":\"NOME\",\"arguments\":{\"arg\":\"valor\"}}</tool_call>\n\n"
        "PERFIL/SESSAO ATUAL: respeite o perfil autenticado e a capacidade conectada de cada tool.\n\n"
        "GUIA DINAMICO POR CATEGORIA (somente nomes realmente carregados):\n"
        f"{dynamic_guide}\n\n"
        "REGRAS:\n"
        "1. Use uma tool carregada para acoes reais; nunca invente nomes.\n"
        "2. Uma tool por vez. Espere o resultado antes de chamar outra.\n"
        "3. Nunca invente resultado. Se falhar, informe a falha real.\n"
        "4. Copie o nome exato do manifesto e respeite o schema.\n"
        "5. Para pastas/arquivos/comandos, use workspace/filesystem.\n"
        "6. Para dados atuais e precos, pesquise antes de responder.\n"
        "7. Preserve exatamente paths, IDs, labels e queries fornecidos.\n"
        "8. Nao envie argumentos vazios quando o schema exige path/query/id.\n"
        "9. Se o usuario autorizou criar ou editar, execute sem confirmacao redundante.\n"
        "10. Respeite permissoes, aprovacao e limites de cada ferramenta."
    )


def render_tool_result_message(
    tool_name: str, tool_arguments: dict, tool_result: dict
) -> str:
    status = "ok" if tool_result.get("ok") else "erro"
    payload = tool_result.get("text") or "(sem texto)"
    return (
        f"RESULTADO DA TOOL {tool_name} ({status})\n"
        f"Argumentos: {json.dumps(tool_arguments, ensure_ascii=False)}\n"
        f"Saida:\n{payload}\n\n"
        "Se a tarefa estiver concluida, responda ao usuario normalmente. "
        "Se precisar de outra tool, emita um novo bloco <tool_call>...</tool_call>."
    )


def strip_tool_call_text(text: str) -> str:
    """Remove all tool-call markup (tagged, code-block, and bare JSON) from *text*.

    Used to clean assistant output before re-injecting into conversation
    context or saving to memory.
    """
    # 1) Strip tagged tool calls (with or without closing tag)
    cleaned = TOOL_CALL_RE.sub("", text)
    # 2) Strip markdown code-block wrapped tool calls
    cleaned = _CODE_BLOCK_RE.sub("", cleaned)
    # 3) Strip bare JSON tool calls
    while True:
        m = _BARE_TOOL_JSON_RE.search(cleaned)
        if not m:
            break
        balanced = _extract_balanced_json(cleaned, m.start())
        if not balanced:
            break
        end = m.start() + len(balanced)
        cleaned = cleaned[:m.start()] + cleaned[end:]
    # 4) Strip legacy llama.cpp-style tool calls
    cleaned = LEGACY_TOOL_CALL_RE.sub("", cleaned)
    # 5) Strip XML function_calls wrappers and invoke blocks
    cleaned = _FUNCTION_CALL_INVOKE_RE.sub("", cleaned)
    cleaned = _FUNCTION_CALL_WRAPPER_RE.sub("", cleaned)
    return cleaned.strip()
