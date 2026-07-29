# OpenPyXL And Excel Notes

Captured: 2026-04-08

Why this matters to Mike:
- Mike writes `.xlsx` files directly through the Excel MCP server.
- These notes describe the formula behavior that matters when Mike builds more advanced spreadsheets.

Key points:
- In openpyxl, formulas are written by assigning a string that starts with `=`.
- Excel function names should be written in English, and function arguments use commas.
- openpyxl preserves formulas in workbook files but does not evaluate them itself.
- Array formulas and table-related formulas exist, but they are more limited and require careful placement.

Operational notes for Mike:
- It is safe to write formulas like `=SUM(B2:B10)` or `=IF(C2>0,"ok","check")` into cells.
- Formula-heavy files can be generated without Microsoft Excel being installed.
- If the user wants a complex workbook, Mike can combine multiple sheets, raw data tabs, summaries, and formula cells.

Sources:
- https://openpyxl.readthedocs.io/en/3.1.2/simple_formulae.html
