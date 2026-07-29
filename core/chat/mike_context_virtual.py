# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Virtual Context Manager (MemGPT Pattern)
==============================================

Implementa gerenciamento de contexto virtual inspirado no MemGPT (arxiv 2310.08560).
Cria a ilusao de um context window maior via swapping entre fast memory (contexto
ativo do LLM) e slow memory (SQLite + embeddings).

Arquitetura:
  FAST MEMORY (context window) <-> SLOW MEMORY (persistent storage)
  - Paginas de contexto sao swapped conforme relevancia
  - Interrupcoes quando o contexto atinge 80% de capacidade
  - Sumarizacao automatica de blocos antigos

Paper: MemGPT: Towards LLMs as Operating Systems
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from mike_config import env_bool

log = logging.getLogger("mike.context_virtual")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VCTX_ENABLED = env_bool("MIKE_VCTX_ENABLED", True)
VCTX_MAX_FAST_PAGES = int(os.getenv("MIKE_VCTX_MAX_FAST_PAGES", "20"))       # Max pages in fast memory
VCTX_PAGE_MAX_CHARS = int(os.getenv("MIKE_VCTX_PAGE_MAX_CHARS", "1500"))     # Max chars per page
VCTX_SWAP_THRESHOLD = float(os.getenv("MIKE_VCTX_SWAP_THRESHOLD", "0.80"))   # Swap when 80% full
VCTX_MAX_SLOW_PAGES = int(os.getenv("MIKE_VCTX_MAX_SLOW_PAGES", "200"))      # Max archived pages
VCTX_SUMMARY_KEEP_COUNT = int(os.getenv("MIKE_VCTX_SUMMARY_KEEP", "5"))      # Recent pages to keep in full


from core.shared.time_utils import utc_now, utc_now_iso


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class ContextPage:
    """Uma pagina de contexto — unidade atomica de informacao no sistema virtual."""
    id: str
    content: str
    role: str = "system"  # system | conversation | tool_result | knowledge | reflection
    priority: float = 0.5  # 0.0 (baixa) a 1.0 (alta — nunca swappar)
    importance: float = 0.5
    created_at: str = field(default_factory=utc_now_iso)
    last_accessed_at: str = field(default_factory=utc_now_iso)
    access_count: int = 0
    token_estimate: int = 0
    summary: str = ""  # Populated when page is swapped out

    def size_chars(self) -> int:
        return len(self.content or "")

    def size_tokens(self) -> int:
        """Rough estimate: 1 token ~ 4 chars."""
        return self.token_estimate or max(1, self.size_chars() // 4)

    def age_seconds(self) -> float:
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            return (utc_now() - created).total_seconds()
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# VirtualContextManager
# ---------------------------------------------------------------------------

class VirtualContextManager:
    """Gerencia contexto virtual com fast/slow memory tiers.

    Fast memory = paginas ativas no context window do LLM.
    Slow memory = paginas arquivadas (sumarizadas) em disco.
    """

    def __init__(
        self,
        store_dir: Optional[Path] = None,
        max_fast_pages: int = VCTX_MAX_FAST_PAGES,
        page_max_chars: int = VCTX_PAGE_MAX_CHARS,
        swap_threshold: float = VCTX_SWAP_THRESHOLD,
        log_fn: Optional[Callable] = None,
    ):
        self._store_dir = Path(store_dir) if store_dir else Path("runtime/memory/context_virtual")
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._max_fast_pages = max_fast_pages
        self._page_max_chars = page_max_chars
        self._swap_threshold = swap_threshold
        self._log = log_fn or log.info

        # Memory tiers
        self._fast_memory: list[ContextPage] = []     # Active context pages
        self._slow_memory: dict[str, ContextPage] = {}  # Archived pages (id -> page)

        # Stats
        self._swap_count = 0
        self._total_pages_created = 0
        self._total_chars_swapped = 0

    # ------------------------------------------------------------------
    # Page Management
    # ------------------------------------------------------------------

    def add_page(self, content: str, role: str = "system", priority: float = 0.5,
                 importance: float = 0.5) -> ContextPage:
        """Add a new page to fast memory. Auto-swaps if over threshold."""
        if not content.strip():
            return None

        import uuid
        page = ContextPage(
            id=f"ctx_{uuid.uuid4().hex[:8]}",
            content=content[:self._page_max_chars * 2],  # Allow some overflow
            role=role,
            priority=priority,
            importance=importance,
            token_estimate=max(1, len(content) // 4),
        )
        self._total_pages_created += 1

        # Split large pages
        if page.size_chars() > self._page_max_chars:
            sub_pages = self._split_page(page)
            for sp in sub_pages:
                self._fast_memory.append(sp)
        else:
            self._fast_memory.append(page)

        # Check if we need to swap
        if self.fast_usage_ratio() > self._swap_threshold:
            self._swap_least_important()

        return page

    def add_conversation_turn(self, user_text: str, assistant_text: str) -> list[ContextPage]:
        """Add a conversation turn as two pages (user + assistant)."""
        pages = []
        if user_text.strip():
            pages.append(self.add_page(user_text, role="user", priority=0.7, importance=0.6))
        if assistant_text.strip():
            pages.append(self.add_page(assistant_text, role="assistant", priority=0.6, importance=0.5))
        return [p for p in pages if p is not None]

    def add_tool_result(self, tool_name: str, result: str) -> Optional[ContextPage]:
        """Add a tool execution result as a page."""
        content = f"[Tool: {tool_name}]\n{result[:self._page_max_chars]}"
        return self.add_page(content, role="tool_result", priority=0.8, importance=0.7)

    def add_knowledge(self, knowledge_text: str, source: str = "") -> Optional[ContextPage]:
        """Add a knowledge retrieval result."""
        content = f"[Knowledge: {source}]\n{knowledge_text[:self._page_max_chars]}" if source else knowledge_text
        return self.add_page(content, role="knowledge", priority=0.6, importance=0.5)

    def _split_page(self, page: ContextPage) -> list[ContextPage]:
        """Split a large page into sub-pages."""
        content = page.content
        pages = []
        for i in range(0, len(content), self._page_max_chars):
            sub = content[i:i + self._page_max_chars]
            import uuid
            sub_page = ContextPage(
                id=f"{page.id}_p{i // self._page_max_chars}",
                content=sub,
                role=page.role,
                priority=page.priority,
                importance=page.importance,
            )
            pages.append(sub_page)
        return pages

    # ------------------------------------------------------------------
    # Memory Tier Management
    # ------------------------------------------------------------------

    def fast_usage_ratio(self) -> float:
        """Ratio of fast memory pages used vs max."""
        return len(self._fast_memory) / max(1, self._max_fast_pages)

    def fast_total_tokens(self) -> int:
        """Estimated total tokens in fast memory."""
        return sum(p.size_tokens() for p in self._fast_memory)

    def _swap_least_important(self) -> int:
        """Swap least important pages from fast to slow memory. Returns count swapped."""
        if not self._fast_memory:
            return 0

        # Sort by priority * importance (lowest first), skip pinned pages (priority >= 0.95)
        candidates = [
            (i, p) for i, p in enumerate(self._fast_memory)
            if p.priority < 0.95  # Pinned pages stay
        ]
        candidates.sort(key=lambda x: x[1].priority * x[1].importance)

        # Swap until under threshold
        target = max(1, int(self._max_fast_pages * 0.70))  # Target 70% after swap
        to_remove = max(0, len(self._fast_memory) - target)
        swapped = 0

        for idx, page in candidates[:to_remove]:
            # Summarize before swapping
            page.summary = self._summarize_page(page)
            page.last_accessed_at = utc_now_iso()
            self._slow_memory[page.id] = page
            self._total_chars_swapped += page.size_chars()
            swapped += 1

        # Remove swapped pages from fast memory (in reverse index order)
        if swapped:
            indices_to_remove = {idx for idx, _ in candidates[:swapped]}
            self._fast_memory = [
                p for i, p in enumerate(self._fast_memory)
                if i not in indices_to_remove
            ]
            self._swap_count += swapped
            self._log(f"[vctx] Swapped {swapped} pages to slow memory ({len(self._slow_memory)} total archived)")

        return swapped

    def recall_page(self, page_id: str) -> Optional[ContextPage]:
        """Recall a page from slow memory back to fast memory."""
        page = self._slow_memory.pop(page_id, None)
        if page:
            page.last_accessed_at = utc_now_iso()
            page.access_count += 1
            self._fast_memory.append(page)
            # Might need to swap again
            if self.fast_usage_ratio() > self._swap_threshold:
                self._swap_least_important()
        return page

    def recall_by_keywords(self, keywords: list[str], limit: int = 3) -> list[ContextPage]:
        """Search slow memory by keywords and recall matching pages."""
        recalled = []
        for pid, page in list(self._slow_memory.items()):
            content_lower = (page.content + " " + page.summary).lower()
            if any(kw.lower() in content_lower for kw in keywords):
                recalled.append(self.recall_page(pid))
                if recalled and len(recalled) >= limit:
                    break
        return [p for p in recalled if p is not None]

    def _summarize_page(self, page: ContextPage) -> str:
        """Generate a short summary of page content (heuristic, can be LLM-enhanced)."""
        content = page.content.strip()
        if len(content) <= 200:
            return content
        # Heuristic: first 150 chars + indicator
        return content[:150] + f"... [{page.size_chars()} chars, {page.role}]"

    # ------------------------------------------------------------------
    # Context Assembly — Build LLM-ready context from fast memory
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        max_tokens: Optional[int] = None,
        include_summaries: bool = False,
    ) -> str:
        """Build a single context string from fast memory pages.
        Returns content that fits within the given token budget.
        """
        parts: list[str] = []
        token_count = 0

        # Sort by priority * importance (highest first)
        sorted_pages = sorted(
            self._fast_memory,
            key=lambda p: p.priority * p.importance,
            reverse=True,
        )

        for page in sorted_pages:
            page_tokens = page.size_tokens()
            if max_tokens and token_count + page_tokens > max_tokens:
                # Truncate last page to fit
                available = max_tokens - token_count
                if available > 50:  # Minimum meaningful content
                    content = page.content[:available * 4] + "..."
                    parts.append(f"[{page.role}] {content}")
                break

            parts.append(f"[{page.role}] {page.content}")
            token_count += page_tokens

        # Optionally include summaries of archived pages
        if include_summaries and self._slow_memory:
            summaries = []
            for page in sorted(self._slow_memory.values(),
                              key=lambda p: p.last_accessed_at or "", reverse=True)[:5]:
                if page.summary:
                    summaries.append(f"[archived:{page.role}] {page.summary}")
            if summaries:
                parts.append("\n--- Contexto Arquivado (sumarios) ---\n" + "\n".join(summaries))

        return "\n\n".join(parts)

    def get_recent_pages(self, count: int = VCTX_SUMMARY_KEEP_COUNT) -> list[ContextPage]:
        """Get most recent pages (newest first)."""
        return sorted(
            self._fast_memory,
            key=lambda p: p.created_at,
            reverse=True,
        )[:count]

    def get_page(self, page_id: str) -> Optional[ContextPage]:
        """Get a page from either tier."""
        for p in self._fast_memory:
            if p.id == page_id:
                return p
        return self._slow_memory.get(page_id)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune_slow_memory(self, max_pages: int = VCTX_MAX_SLOW_PAGES) -> int:
        """Remove oldest pages from slow memory if over limit."""
        if len(self._slow_memory) <= max_pages:
            return 0

        sorted_pages = sorted(
            self._slow_memory.values(),
            key=lambda p: (p.last_accessed_at or p.created_at),
        )
        to_remove = len(self._slow_memory) - max_pages
        removed = 0
        for page in sorted_pages[:to_remove]:
            del self._slow_memory[page.id]
            removed += 1
        if removed:
            self._log(f"[vctx] Pruned {removed} old pages from slow memory")
        return removed

    def clear_fast_memory(self) -> int:
        """Clear all pages from fast memory. Returns count cleared."""
        count = len(self._fast_memory)
        self._fast_memory.clear()
        return count

    def compact(self) -> dict:
        """Compact both tiers: swap excess, prune old. Returns stats."""
        swapped = self._swap_least_important()
        pruned = self.prune_slow_memory()
        return {
            "swapped": swapped,
            "pruned": pruned,
            "fast_pages": len(self._fast_memory),
            "slow_pages": len(self._slow_memory),
            "fast_tokens": self.fast_total_tokens(),
            "fast_usage": round(self.fast_usage_ratio(), 2),
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "enabled": VCTX_ENABLED,
            "fast_pages": len(self._fast_memory),
            "slow_pages": len(self._slow_memory),
            "max_fast_pages": self._max_fast_pages,
            "fast_usage_ratio": round(self.fast_usage_ratio(), 2),
            "fast_total_tokens": self.fast_total_tokens(),
            "swap_count": self._swap_count,
            "total_pages_created": self._total_pages_created,
            "total_chars_swapped": self._total_chars_swapped,
            "page_max_chars": self._page_max_chars,
            "swap_threshold": self._swap_threshold,
        }
