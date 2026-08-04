"""Tests for Mike's extended local, Drive and Hugging Face MCP tools."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
for relative in ("core/mcp", "core/server", "core/integrations", "core/autonomy"):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

import mike_appointments_mcp as appointments_mcp
import mike_drive_mcp as drive_mcp
import mike_huggingface_mcp as hf_mcp
import mike_remote_agent_mcp as remote_mcp
import mike_workspace_mcp as workspace_mcp
from mike_appointments import AppointmentDB, AppointmentEngine

# Skip Windows-specific tests on non-Windows platforms
IS_WINDOWS = sys.platform == "win32"


class ExtendedManifestTests(unittest.TestCase):
    def test_manifests_load_without_external_credentials(self):
        self.assertEqual(
            {tool.name for tool in drive_mcp.mcp._tool_manager.list_tools()},
            {
                "list_drive_files",
                "search_drive",
                "read_drive_file",
                "recent_drive_changes",
            },
        )
        self.assertEqual(
            {tool.name for tool in hf_mcp.mcp._tool_manager.list_tools()},
            {
                "list_hf_models",
                "list_hf_datasets",
                "hf_model_info",
                "list_hf_files",
                "read_hf_readme",
                "hf_whoami",
            },
        )
        self.assertIn(
            "run_command",
            {tool.name for tool in workspace_mcp.mcp._tool_manager.list_tools()},
        )
        self.assertEqual(
            {tool.name for tool in remote_mcp.mcp._tool_manager.list_tools()},
            {
                "check_remote_agent_health",
                "execute_remote_powershell",
                "check_remote_process",
                "list_remote_directory",
            },
        )

    def test_hf_whoami_is_honest_without_token(self):
        with patch.dict("os.environ", {"HF_TOKEN": "", "HUGGING_FACE_HUB_TOKEN": ""}):
            self.assertEqual(
                hf_mcp.hf_whoami(),
                {"authenticated": False, "state": "public_read_only"},
            )


class AppointmentMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_engine = appointments_mcp._ENGINE
        db_path = Path(self.temp_dir.name) / "appointments.db"
        appointments_mcp._ENGINE = AppointmentEngine(AppointmentDB(db_path))

    def tearDown(self):
        appointments_mcp._ENGINE = self.previous_engine
        self.temp_dir.cleanup()

    def test_create_read_confirm_and_status(self):
        created = appointments_mcp.create_appointment(
            "Familia Barreto",
            "+14165550123",
            "2099-07-24T10:00:00-04:00",
            service_type="Reuniao",
        )
        fetched = appointments_mcp.get_appointment(created["id"])
        self.assertEqual(fetched["contact_name"], "Familia Barreto")

        confirmed = appointments_mcp.confirm_appointment(created["id"])
        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(appointments_mcp.appointment_status()["by_status"]["CONFIRMED"], 1)


class WorkspaceCommandTests(unittest.TestCase):
    @unittest.skipUnless(IS_WINDOWS, "PowerShell tests require Windows")
    def test_command_runs_in_allowed_root_and_returns_exit_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = workspace_mcp.ALLOWED_ROOTS
            workspace_mcp.ALLOWED_ROOTS = [Path(temp_dir).resolve()]
            try:
                result = workspace_mcp.run_command(
                    "Write-Output 'QWEN_COMMAND_OK'",
                    cwd=temp_dir,
                    timeout_seconds=10,
                )
                alias_result = workspace_mcp.execute_powershell(
                    "Write-Output 'QWEN_ALIAS_OK'",
                    cwd=temp_dir,
                    timeout_seconds=10,
                )
            finally:
                workspace_mcp.ALLOWED_ROOTS = previous
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("QWEN_COMMAND_OK", result["output"])
        self.assertEqual(alias_result["exit_code"], 0)
        self.assertIn("QWEN_ALIAS_OK", alias_result["output"])

    @unittest.skipIf(IS_WINDOWS, "Shell command tests require non-Windows platform")
    def test_command_runs_in_allowed_root_linux(self):
        """Test run_command on Linux with shell commands."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = workspace_mcp.ALLOWED_ROOTS
            workspace_mcp.ALLOWED_ROOTS = [Path(temp_dir).resolve()]
            try:
                result = workspace_mcp.run_command(
                    "echo 'QWEN_COMMAND_OK'",
                    cwd=temp_dir,
                    timeout_seconds=10,
                )
            finally:
                workspace_mcp.ALLOWED_ROOTS = previous
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("QWEN_COMMAND_OK", result["output"])


if __name__ == "__main__":
    unittest.main()
