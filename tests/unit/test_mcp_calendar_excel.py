"""Tests for Mike's Calendar and Excel MCP servers."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for relative in ("core/mcp", "core/server", "core/integrations"):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

import mike_calendar_mcp as calendar_mcp
import mike_excel_mcp as excel_mcp


class CalendarManifestTests(unittest.TestCase):
    def test_calendar_server_exposes_full_tool_set_without_oauth_at_boot(self):
        names = {
            tool.name for tool in calendar_mcp.mcp._tool_manager.list_tools()
        }
        self.assertEqual(names, {
            "list_calendars",
            "list_events",
            "create_event",
            "update_event",
            "delete_event",
            "free_busy",
        })


class ExcelMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_root = excel_mcp.ROOT
        excel_mcp.ROOT = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        excel_mcp.ROOT = self.previous_root
        self.temp_dir.cleanup()

    def test_workbook_round_trip(self):
        created = excel_mcp.create_workbook(
            "familia.xlsx",
            sheet_name="Barreto",
            headers=["nome", "papel"],
        )
        self.assertTrue(created["created"])

        excel_mcp.append_rows(
            "familia.xlsx",
            [["Mike", "braco direito"]],
            sheet_name="Barreto",
        )
        excel_mcp.write_cells(
            "familia.xlsx",
            "C1",
            [["status"], ["operacional"]],
            sheet_name="Barreto",
        )

        inspected = excel_mcp.inspect_workbook("familia.xlsx")
        self.assertEqual(inspected["sheet_names"], ["Barreto"])
        read = excel_mcp.read_range(
            "familia.xlsx", "A1:C2", sheet_name="Barreto"
        )
        self.assertEqual(read["values"][1], ["Mike", "braco direito", "operacional"])
        listed = excel_mcp.list_spreadsheets()
        self.assertEqual(listed[0]["relative_path"], "familia.xlsx")

    def test_rejects_path_outside_allowed_root(self):
        with self.assertRaises(ValueError):
            excel_mcp.inspect_workbook("../fora.xlsx")


if __name__ == "__main__":
    unittest.main()
