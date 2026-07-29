# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx

_log = logging.getLogger("mike.deepseek")

# ------------------------------------------------------------------
# LLM Response Cache — TTL-based LRU dict
# ------------------------------------------------------------------

class _LLMResponseCache:
    """Thread-safe TTL-based LRU cache for LLM chat completions.
    Evicts entries when maxsize is exceeded (oldest first) or TTL expires.
    """

    def __init__(self, maxsize: int = 100, default_ttl_s: float = 300.0) -> None:
        self._maxsize = max(maxsize, 1)
        self._default_ttl_s = default_ttl_s
        self._store: OrderedDict[str, Tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def _make_key(self, model: str, messages_json: str, temperature: float, max_tokens: int) -> str:
        payload = json.dumps(
            {"model": model, "messages": messages_json, "temperature": temperature, "max_tokens": max_tokens},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, model: str, messages: List[dict], temperature: float, max_tokens: int) -> Optional[dict]:
        messages_json = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        key = self._make_key(model, messages_json, temperature, max_tokens)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            # Move to end (most-recently-used)
            self._store.move_to_end(key)
            return value

    def set(
        self,
        model: str,
        messages: List[dict],
        temperature: float,
        max_tokens: int,
        value: dict,
        ttl_s: Optional[float] = None,
    ) -> None:
        messages_json = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        key = self._make_key(model, messages_json, temperature, max_tokens)
        expires_at = time.time() + (ttl_s if ttl_s is not None else self._default_ttl_s)
        with self._lock:
            # Evict expired entries while we're here
            now = time.time()
            expired = [k for k, (exp, _) in self._store.items() if now > exp]
            for k in expired:
                del self._store[k]
            # Insert / update
            self._store[key] = (expires_at, value)
            self._store.move_to_end(key)
            # Enforce maxsize
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            expired = sum(1 for exp, _ in self._store.values() if now > exp)
            return {"entries": len(self._store), "expired": expired, "maxsize": self._maxsize}


# Module-level cache instance (lazy init via env var)
_llm_cache: Optional[_LLMResponseCache] = None
_cache_lock = threading.Lock()


def _get_llm_cache() -> Optional[_LLMResponseCache]:
    global _llm_cache
    if not _is_cache_enabled():
        return None
    if _llm_cache is None:
        with _cache_lock:
            if _llm_cache is None:
                maxsize = int(os.getenv("MIKE_LLM_CACHE_MAXSIZE", "100"))
                default_ttl = float(os.getenv("MIKE_LLM_CACHE_TTL_SECONDS", "300"))
                _llm_cache = _LLMResponseCache(maxsize=maxsize, default_ttl_s=default_ttl)
    return _llm_cache


def _is_cache_enabled() -> bool:
    val = (os.getenv("MIKE_LLM_CACHE_ENABLED", "true")).strip().lower()
    return val in ("1", "true", "yes", "on")

def mcp_tools_to_openai(tool_manifest: List[dict]) -> List[dict]:
    """Convert MCP-style tool manifest to OpenAI function-calling format."""
    openai_tools = []
    for tool in tool_manifest:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        schema = tool.get("inputSchema", tool.get("parameters", {}))
        if not name:
            continue
        # Sanitize name: DeepSeek requires ^[a-zA-Z0-9_-]+$ (no dots allowed)
    safe_name = name.replace(".", "_").replace(":", "_")
    openai_tools.append({
            "type": "function",
            "function": {
                "name": safe_name,
                "description": desc[:1024] if desc else "",
                "parameters": schema if schema else {"type": "object", "properties": {}},
            },
        })
    return openai_tools


def parse_openai_tool_calls(response: dict) -> List[dict]:
    """Extract tool calls from OpenAI-format chat completion response.
    Returns list of {"name": str, "arguments": dict, "id": str}.
    Handles truncated JSON from finish_reason=length.
    """
    choices = response.get("choices", [])
    if not choices:
        return []
    message = choices[0].get("message", {})
    raw_calls = message.get("tool_calls", [])
    if not raw_calls:
        return []
    parsed = []
    for tc in raw_calls:
        func = tc.get("function", {})
        name = func.get("name", "")
        args_str = func.get("arguments", "{}")
        try:
            arguments = json.loads(args_str) if isinstance(args_str, str) else args_str
        except (json.JSONDecodeError, TypeError):
            # Try to recover truncated JSON by completing braces/brackets
            if isinstance(args_str, str):
                args_str = _repair_truncated_json(args_str)
            try:
                arguments = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, TypeError):
                _log.warning("Failed to parse tool call arguments for %s (len=%d)", name, len(str(args_str)))
                arguments = {}
        parsed.append({
            "name": name,
            "arguments": arguments,
            "id": tc.get("id", ""),
        })
    return parsed


def _repair_truncated_json(s: str) -> str:
    """Attempt to repair truncated JSON by closing unclosed braces and strings."""
    s = s.strip()
    # Remove trailing incomplete escape sequences
    if s.endswith("\\"):
        s = s[:-1]
    # Count braces
    open_braces = s.count("{") - s.count("}")
    open_brackets = s.count("[") - s.count("]")
    # Check if we're inside a string
    in_string = False
    for i, c in enumerate(s):
        if c == '"' and (i == 0 or s[i-1] != '\\'):
            in_string = not in_string
    # Close unclosed string
    if in_string:
        s += '"'
    # Close unclosed braces/brackets
    s += "}" * open_braces
    s += "]" * open_brackets
    return s


class MikeDeepSeekClient:
    """
    Client for DeepSeek API.
    - consult()          → Anthropic-compatible endpoint (secretary/tool)
    - chat_completion()  → OpenAI-compatible endpoint, blocking, non-streaming
    - chat_completion_stream() → OpenAI-compatible endpoint, blocking, yields chunks
    """
    def __init__(self, api_key: str = "", base_url: str = "https://api.deepseek.com"):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(120.0, connect=10.0)
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            limits=httpx.Limits(max_keepalive_connections=10),
        )

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    async def close(self):
        """Fechar o client httpx persistente e libertar conexoes."""
        await self._client.aclose()

    async def consult(
        self, 
        prompt: str, 
        model: str = "deepseek-v4-pro", 
        system: str = "Você é o secretário especialista do Mike. Forneça o melhor código ou opinião técnica.",
        max_tokens: int = 4096
    ) -> dict:
        """
        Consult DeepSeek using the Anthropic-compatible API.
        """
        if not self.ready:
            return {"ok": False, "error": "DEEPSEEK_API_KEY não configurada."}

        # DeepSeek Anthropic endpoint is base_url + /anthropic/messages if base_url is api.deepseek.com
        if "deepseek.com" in self.base_url and "/anthropic" not in self.base_url:
            url = f"{self.base_url}/anthropic/messages"
        else:
            url = f"{self.base_url}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }

        t0 = time.time()
        try:
            response = await self._client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

            # Anthropic format response: data["content"][0]["text"]
            content = data.get("content", [])
            text = ""
            for part in content:
                if part.get("type") == "text":
                    text += part.get("text", "")

            return {
                "ok": True,
                "text": text,
                "model": data.get("model", model),
                "usage": data.get("usage", {}),
                "elapsed": time.time() - t0,
            }
        except Exception as exc:
            _log.error("DeepSeek consultation failed: %s", exc)
            return {"ok": False, "error": str(exc), "elapsed": time.time() - t0}

    # ------------------------------------------------------------------
    # OpenAI-compatible blocking methods (used as main chat backend)
    # ------------------------------------------------------------------

    def _openai_headers(self) -> dict:
        # Re-read from env at call time in case key was set after init (autonomy boot race)
        key = self.api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise ValueError(
                "DEEPSEEK_API_KEY nao configurada. "
                "Verifique config/.env.runtime e reinicie o servidor."
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def chat_completion(
        self,
        messages: List[dict],
        model: str = "deepseek-v4-pro",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.95,
        stop: Optional[List[str]] = None,
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        *,
        cache_ttl_s: Optional[float] = None,
    ) -> dict:
        """Blocking, non-streaming OpenAI-compatible chat completion.
        Supports native function calling via `tools` parameter.

        Cached by default (MIKE_LLM_CACHE_ENABLED=true). Tool-calling
        requests are never cached because they depend on external state.
        """
        # Check cache (skip for tool calls)
        cache = _get_llm_cache()
        if cache is not None and not tools:
            cached = cache.get(model, messages, temperature, max_tokens)
            if cached is not None:
                _log.debug("LLM cache hit (size=%d)", cache.size)
                return cached

        url = f"{self.base_url}/v1/chat/completions"
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=self._openai_headers(), json=payload)
            if resp.status_code >= 400:
                _log.error("DeepSeek API error %d: %s", resp.status_code, resp.text[:1000])
            resp.raise_for_status()
            result = resp.json()

        # Cache successful responses (skip for tool calls and errors)
        if cache is not None and not tools and result.get("choices"):
            ttl = cache_ttl_s if cache_ttl_s is not None else float(os.getenv("MIKE_LLM_CACHE_TTL_SECONDS", "300"))
            cache.set(model, messages, temperature, max_tokens, result, ttl_s=ttl)

        return result

    def chat_completion_stream(
        self,
        messages: List[dict],
        model: str = "deepseek-v4-pro",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.95,
        stop: Optional[List[str]] = None,
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
    ) -> Iterator[dict]:
        """Blocking streaming: yields OpenAI-compatible chunk dicts.
        Supports native function calling via `tools` parameter.

        Cached by default with shorter TTL (60s, via MIKE_LLM_CACHE_STREAM_TTL_SECONDS).
        Tool-calling requests are never cached.
        """
        # Check cache (skip for tool calls)
        cache = _get_llm_cache()
        if cache is not None and not tools:
            cached = cache.get(model, messages, temperature, max_tokens)
            if cached is not None:
                _log.debug("LLM cache hit for stream (size=%d)", cache.size)
                content = ""
                choices = cached.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "") or ""
                # Yield cached content as a single delta chunk
                yield {
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": content},
                        "finish_reason": choices[0].get("finish_reason", "stop") if choices else "stop",
                    }],
                    "model": cached.get("model", model),
                    "usage": cached.get("usage", {}),
                }
                return

        url = f"{self.base_url}/v1/chat/completions"
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
        }
        if stop:
            payload["stop"] = stop
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        # Buffer all chunks so we can cache the assembled response
        all_chunks: List[dict] = []
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, headers=self._openai_headers(), json=payload) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        all_chunks.append(chunk)
                        yield chunk
                    except Exception:
                        continue

        # Cache the assembled response for future calls (skip tool calls and errors)
        if cache is not None and not tools and all_chunks:
            assembled = self._assemble_stream_chunks(all_chunks, model)
            if assembled and assembled.get("choices"):
                ttl = float(os.getenv("MIKE_LLM_CACHE_STREAM_TTL_SECONDS", "60"))
                cache.set(model, messages, temperature, max_tokens, assembled, ttl_s=ttl)

    @staticmethod
    def _assemble_stream_chunks(chunks: List[dict], model: str) -> Optional[dict]:
        """Assemble streaming delta chunks into a single chat completion response dict."""
        if not chunks:
            return None
        content_parts: List[str] = []
        finish_reason = "stop"
        usage = {}
        for chunk in chunks:
            choices = chunk.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                role = delta.get("role", "")
                text = delta.get("content", "")
                if text:
                    content_parts.append(text)
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
            if chunk.get("usage"):
                usage = chunk["usage"]
        return {
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(content_parts),
                },
                "finish_reason": finish_reason,
            }],
            "model": model,
            "usage": usage,
        }
