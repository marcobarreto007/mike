# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Graph — Graphiti / Neo4j temporal knowledge graph
======================================================

Wraps the Graphiti library to maintain a temporal graph of entities,
relationships and episodes sourced from conversations and documents.

Configuration (.env):
    MIKE_NEO4J_URI=bolt://localhost:7687
    MIKE_NEO4J_USER=neo4j
    MIKE_NEO4J_PASS=<password>
    MIKE_GRAPH_ENABLED=true
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import typing
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

import openai
from pydantic import BaseModel
from mike_config import env_bool

log = logging.getLogger("mike.graph")


def _extract_json(text: str) -> str:
    """Strip markdown code fences and thinking tags, return raw JSON string."""
    # Remove <think>...</think> blocks (Qwen3, etc.)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return m.group(1).strip()
    # Find first { ... } or [ ... ] block
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m:
        return m.group(1).strip()
    return text.strip()


class _LocalLLMClient:
    """Graphiti LLM client compatible with local OpenAI-compatible servers.

    Uses chat.completions.create with response_format=json_object and
    appends the JSON schema to the prompt, then robustly extracts the JSON
    from the response (handles markdown fences and thinking tags).
    """

    MAX_RETRIES: typing.ClassVar[int] = 2

    def __init__(self, config: Any) -> None:
        from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS

        self.model: str = config.model or "mike"
        self.temperature: float = getattr(config, "temperature", 0.0)
        self.max_tokens: int = getattr(config, "max_tokens", DEFAULT_MAX_TOKENS)
        self._client = openai.AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    def _clean_input(self, text: str) -> str:
        return text.strip() if text else ""

    async def _generate_response(
        self,
        messages: list[Any],
        response_model: type[BaseModel] | None = None,  # noqa: ARG002 — kept for interface compat
        max_tokens: int = 0,
    ) -> dict[str, Any]:
        openai_messages: list[dict] = []
        for m in messages:
            content = self._clean_input(m.content)
            if m.role in ("user", "system"):
                openai_messages.append({"role": m.role, "content": content})

        response = await self._client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            temperature=self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        return json.loads(_extract_json(raw))

    async def generate_response(
        self,
        messages: list[Any],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 0,
    ) -> dict[str, Any]:
        from graphiti_core.prompts.models import Message as GMessage

        # Append JSON schema hint to last user message
        if response_model is not None:
            schema = json.dumps(response_model.model_json_schema(), indent=2)
            messages[-1].content += (
                f"\n\nRespond with a JSON object matching this schema:\n{schema}"
                "\n\nReturn ONLY the JSON object, no explanation, no markdown."
            )

        retry_count = 0
        last_error: Exception | None = None
        while retry_count <= self.MAX_RETRIES:
            try:
                result = await self._generate_response(messages, response_model, max_tokens)
                return result
            except Exception as exc:
                last_error = exc
                if retry_count >= self.MAX_RETRIES:
                    log.error(f"Graph LLM max retries exceeded: {exc}")
                    raise
                retry_count += 1
                messages.append(
                    GMessage(
                        role="user",
                        content=(
                            f"Invalid response. Error: {exc}. "
                            "Return ONLY a valid JSON object matching the schema."
                        ),
                    )
                )
                log.warning(f"Graph LLM retry {retry_count}/{self.MAX_RETRIES}: {exc}")

        raise last_error or Exception("Max retries exceeded")





class MikeGraph:
    """Temporal knowledge graph powered by Graphiti + Neo4j."""

    def __init__(
        self,
        log_fn: Optional[Callable[[str], None]] = None,
        *,
        enabled: Optional[bool] = None,
        neo4j_uri: Optional[str] = None,
        neo4j_user: Optional[str] = None,
        neo4j_pass: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_api_key: Optional[str] = None,
    ) -> None:
        self.log = log_fn or log.info
        self.enabled = env_bool("MIKE_GRAPH_ENABLED", False) if enabled is None else bool(enabled)
        self.neo4j_uri = (neo4j_uri or os.getenv("MIKE_NEO4J_URI", "bolt://localhost:7687")).strip()
        self.neo4j_user = (neo4j_user or os.getenv("MIKE_NEO4J_USER", "neo4j")).strip()
        self.neo4j_pass = (neo4j_pass or os.getenv("MIKE_NEO4J_PASS", "")).strip()
        self.llm_base_url = (
            llm_base_url
            or os.getenv("MIKE_GRAPH_LLM_BASE_URL")
            or os.getenv("MIKE_LIGHTRAG_LLM_BASE_URL")
            or "http://127.0.0.1:8080/v1"
        ).rstrip("/")
        self.llm_model = (
            llm_model
            or os.getenv("MIKE_GRAPH_LLM_MODEL")
            or os.getenv("MIKE_LIGHTRAG_LLM_MODEL", "mike")
        ).strip()
        self.llm_api_key = (
            llm_api_key
            or os.getenv("MIKE_GRAPH_API_KEY")
            or os.getenv("MIKE_API_KEY")
            or "sk-local"
        ).strip()

        self._graphiti = None
        self._driver = None
        self._load_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def ensure_ready(self) -> bool:
        if self._graphiti is not None:
            return True
        if not self.enabled:
            return False
        if self._load_error is not None:
            return False

        try:
            # Graphiti's OpenAI client requires OPENAI_API_KEY in env
            os.environ.setdefault("OPENAI_API_KEY", self.llm_api_key)

            from graphiti_core import Graphiti
            from graphiti_core.llm_client import LLMConfig
            from neo4j import GraphDatabase

            # Verify Neo4j connection
            self._driver = GraphDatabase.driver(
                self.neo4j_uri,
                auth=(self.neo4j_user, self.neo4j_pass),
            )
            self._driver.verify_connectivity()

            # Use local-compatible client (no structured-outputs API required)
            llm_config = LLMConfig(
                api_key=self.llm_api_key,
                base_url=self.llm_base_url,
                model=self.llm_model,
            )
            llm_client = _LocalLLMClient(config=llm_config)

            self._graphiti = Graphiti(
                uri=self.neo4j_uri,
                user=self.neo4j_user,
                password=self.neo4j_pass,
                llm_client=llm_client,
            )

            # Initialize graph schema
            self._run_async(self._graphiti.build_indices_and_constraints())
            self.log("Graphiti graph initialized successfully")
            return True

        except Exception as exc:
            self._load_error = str(exc)
            self.log(f"Graph unavailable: {exc}")
            self._graphiti = None
            return False

    def _get_or_new_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("closed")
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def _run_async(self, coro):
        """Run async coroutine from sync context, even inside a running loop."""
        import concurrent.futures

        try:
            asyncio.get_running_loop()
            # We're inside an event loop (e.g. uvicorn) — run in a fresh thread
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=120)
        except RuntimeError:
            # No running loop — safe to use asyncio.run directly
            return asyncio.run(coro)

    def _rebuild_client(self) -> None:
        """Rebuild the Graphiti client to get a fresh httpx session.

        Graphiti's OpenAI client uses an httpx.AsyncClient that is tied to the
        event loop in which it was created.  When we call ``_run_async`` from
        uvicorn's sync context, the coroutine runs in a *new* event loop inside
        a thread-pool, so the original httpx session is dead
        (``NoneType has no attribute 'send'``).

        The cheapest fix is to recreate the Graphiti instance (which is fast —
        it just creates new httpx clients) while reusing the existing Neo4j
        driver which is thread-safe.
        """
        try:
            from graphiti_core import Graphiti
            from graphiti_core.llm_client import LLMConfig

            llm_config = LLMConfig(
                api_key=self.llm_api_key,
                base_url=self.llm_base_url,
                model=self.llm_model,
            )
            llm_client = _LocalLLMClient(config=llm_config)

            self._graphiti = Graphiti(
                uri=self.neo4j_uri,
                user=self.neo4j_user,
                password=self.neo4j_pass,
                llm_client=llm_client,
            )
        except Exception as exc:
            self.log(f"Graph client rebuild failed: {exc}")

    # ------------------------------------------------------------------
    # Episode management
    # ------------------------------------------------------------------

    def add_episode(
        self,
        text: str,
        timestamp: Optional[str] = None,
        source: str = "conversation",
        *,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Add an episode to the temporal graph.

        Graphiti automatically extracts entities and relationships.
        """
        if not self.ensure_ready() or self._graphiti is None:
            return False

        ts = timestamp or datetime.now(timezone.utc).isoformat()
        episode_metadata = metadata or {}
        episode_metadata.setdefault("source", source)

        try:
            from graphiti_core.nodes import EpisodeType

            source_map = {
                "conversation": EpisodeType.message,
                "document": EpisodeType.text,
                "email": EpisodeType.message,
                "calendar": EpisodeType.text,
            }
            episode_type = source_map.get(source, EpisodeType.text)

            def _do_add():
                return self._run_async(
                    self._graphiti.add_episode(
                        name=f"{source}_{ts}",
                        episode_body=text,
                        source_description=f"Mike {source}",
                        reference_time=datetime.fromisoformat(ts) if isinstance(ts, str) else ts,
                        source=episode_type,
                    )
                )

            try:
                _do_add()
            except (AttributeError, TypeError) as inner:
                # 'NoneType' object has no attribute 'send' — stale httpx session
                if "send" in str(inner) or "NoneType" in str(inner):
                    self.log("Graph session stale, rebuilding client...")
                    self._rebuild_client()
                    _do_add()
                else:
                    raise

            self.log(f"Graph episode added ({source}): {text[:80]}...")
            return True
        except Exception as exc:
            self.log(f"Graph add_episode failed: {exc}")
            return False

    def add_conversation(
        self,
        user_text: str,
        assistant_text: str,
        timestamp: Optional[str] = None,
        *,
        session_id: str = "main",
        metadata: Optional[dict] = None,
    ) -> bool:
        """Add a conversation turn as a graph episode."""
        combined = f"User: {user_text}\nMike: {assistant_text}"
        meta = dict(metadata or {})
        meta["session_id"] = session_id
        return self.add_episode(
            combined,
            timestamp=timestamp,
            source="conversation",
            metadata=meta,
        )

    def add_document(
        self,
        text: str,
        source_path: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> bool:
        """Ingest a document into the graph."""
        meta = {"file_path": source_path} if source_path else {}
        return self.add_episode(text, timestamp=timestamp, source="document", metadata=meta)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def query(self, question: str, *, limit: int = 10) -> list[dict]:
        """Search the graph using hybrid semantic + graph traversal."""
        if not self.ensure_ready() or self._graphiti is None:
            return []

        try:
            results = self._run_async(
                self._graphiti.search(query=question, num_results=limit)
            )
            return [
                {
                    "fact": str(getattr(edge, "fact", "")),
                    "source": str(getattr(edge, "source_node_name", "")),
                    "target": str(getattr(edge, "target_node_name", "")),
                    "created_at": str(getattr(edge, "created_at", "")),
                    "valid_at": str(getattr(edge, "valid_at", "")),
                    "invalid_at": str(getattr(edge, "invalid_at", "")),
                    "score": float(getattr(edge, "score", 0.0)),
                }
                for edge in (results or [])
            ]
        except Exception as exc:
            self.log(f"Graph query failed: {exc}")
            return []

    def get_entity(self, name: str) -> dict:
        """Get entity node and its relationships by name."""
        if not self.ensure_ready() or self._driver is None:
            return {"name": name, "found": False, "relationships": []}

        try:
            with self._driver.session() as session:
                # Find entity node
                result = session.run(
                    "MATCH (n) WHERE toLower(n.name) CONTAINS toLower($name) "
                    "OPTIONAL MATCH (n)-[r]-(m) "
                    "RETURN n, type(r) as rel_type, r, m "
                    "LIMIT 50",
                    name=name,
                )

                entity_info: dict[str, Any] = {"name": name, "found": False, "relationships": []}
                for record in result:
                    node = record["n"]
                    if not entity_info["found"]:
                        entity_info["found"] = True
                        entity_info["properties"] = dict(node)

                    if record["m"] is not None:
                        other = record["m"]
                        entity_info["relationships"].append({
                            "type": record["rel_type"],
                            "target": dict(other).get("name", str(other.id)),
                            "properties": dict(record["r"]) if record["r"] else {},
                        })
                return entity_info
        except Exception as exc:
            self.log(f"Graph get_entity failed: {exc}")
            return {"name": name, "found": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Data migration
    # ------------------------------------------------------------------

    def migrate_conversations(
        self,
        conversations: Sequence[dict],
        *,
        batch_size: int = 10,
    ) -> int:
        """Migrate existing conversations into the graph as episodes.

        Each conversation dict should have:
            timestamp, session_id, user_text, assistant_text
        """
        if not self.ensure_ready():
            return 0

        count = 0
        for conv in conversations:
            user_text = conv.get("user_text", "")
            assistant_text = conv.get("assistant_text", "")
            timestamp = conv.get("timestamp")
            session_id = conv.get("session_id", "main")

            if not user_text and not assistant_text:
                continue

            meta = {
                "session_id": session_id,
                "migrated": True,
                "mood": conv.get("mood"),
                "context": conv.get("context"),
                "importance": conv.get("importance"),
                "people": conv.get("people", []),
            }
            # Remove None values
            meta = {k: v for k, v in meta.items() if v is not None}

            if self.add_conversation(
                user_text, assistant_text,
                timestamp=timestamp,
                session_id=session_id,
                metadata=meta,
            ):
                count += 1

        self.log(f"Migrated {count}/{len(conversations)} conversations to graph")
        return count

    # ------------------------------------------------------------------
    # Close / Status
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._graphiti is not None:
            try:
                self._run_async(self._graphiti.close())
            except Exception as exc:
                self.log(f"Graphiti close failed: {exc}")
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception as exc:
                self.log(f"Neo4j driver close failed: {exc}")
        self._graphiti = None
        self._driver = None

    def status(self) -> dict:
        info = {
            "enabled": self.enabled,
            "ready": self._graphiti is not None,
            "load_error": self._load_error,
            "neo4j_uri": self.neo4j_uri,
            "llm_model": self.llm_model,
        }

        # Count nodes/edges if connected
        if self._driver is not None:
            try:
                with self._driver.session() as session:
                    node_count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
                    edge_count = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
                    info["node_count"] = node_count
                    info["edge_count"] = edge_count
            except Exception:
                pass
        return info
