# Mike Search Routes Policy

Captured at: 2026-04-08

Purpose:
- Make Mike choose the right source before answering.
- Prefer local memory and structured tools over guessing.
- Use DDGS for fresh facts and save reusable findings back into local RAG.

Routing order:
1. `memoria_local`
   Use for personal context, family, work history, preferences, company structure, and prior conversations.
   Source: SQLite conversation memory + Mem0 long-term memory.

2. `docs_locais_rag`
   Use for procedures, API notes, implementation details, cached web notes, and current_docs snapshots.
   Source: `mike/knowledge/current_docs` and `mike/knowledge/web_cache`.

3. `email_mcp`
   Use for inbox, reading messages, drafting or sending email, and checking the configured Gmail account.
   Source: Gmail MCP tools.

4. `agenda_mcp`
   Use for events, reminders, availability, planning workdays, and scheduling tasks.
   Source: Google Calendar MCP tools.

5. `planilha_mcp`
   Use for `.xlsx`, `.csv`, budgets, workbooks, sheets, formulas, rows, and cell updates.
   Source: Excel MCP tools.

6. `workspace_mcp`
   Use for local files, folders, renames, edits, reads, deletes, and path inspection.
   Source: workspace MCP tools.

7. `visao_foto`
   Use when the request includes an image or asks about what is shown in a photo.
   Source: Mike vision handler.

8. `ddgs_web`
   Use for live facts, recent docs, unclear APIs, current news, or when local context is missing or weak.
   Source: DDGS / DuckDuckGo via `web.search_and_cache`.

Search policy:
- Start with the most specific route for the request.
- If the first route is insufficient, fall back to the next relevant route.
- Save reusable DDGS results into local RAG when the query is not real-time.
- Do not use DDGS for purely local file or spreadsheet tasks.

Notes from official docs:
- Mem0 search should stay scoped by user/session context whenever possible to avoid mixing memories.
- DDGS provides live DuckDuckGo retrieval without requiring a Brave API key.

Sources:
- https://docs.mem0.ai/core-concepts/memory-operations/search
- https://pypi.org/project/ddgs/
