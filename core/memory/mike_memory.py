# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
MIKE Memory subsystem — re-export shim for backward compatibility.

The implementation has been extracted into focused submodules:
- mike_memory_utils  — text helpers, query sanitization, chunking, context keywords
- mike_memory_store  — LocalMikeStore (SQLite, FTS, embeddings, vector search, mesh,
                       checkpoints, session summaries)
- mike_memory_service — MikeMemoryService (Mem0, LightRAG, Reranker, Graph, RRF fusion,
                         build_context)

External callers should import MikeMemoryService from this module.
"""

from mike_memory_store import LocalMikeStore  # noqa: F401
from mike_memory_service import MikeMemoryService  # noqa: F401
