# DDGS Search Notes

Captured: 2026-04-08

Why this matters to Mike:
- Mike uses DDGS as the preferred web provider for fresh factual lookup.
- These notes help him remember the request shape and what comes back in the response.

Key points:
- The `ddgs` Python package provides a `DDGS` client for text and news search.
- Text results typically include `title`, `href`, and `body`.
- News results typically include `title`, `url`, `body`, `source`, and `date`.
- Mike uses DDGS with the DuckDuckGo backend and falls back to DuckDuckGo HTML/API when needed.

Operational notes for Mike:
- Use DDGS for current docs, factual uncertainty, and fresh web verification.
- Save useful non-realtime results into the local RAG cache so later answers can reuse them.
- Realtime items like weather or live scores should still be fetched fresh.

Sources:
- https://pypi.org/project/ddgs/
- https://pypi.org/project/duckduckgo-search/
