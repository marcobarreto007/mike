"""Regression tests for Mike's Qwen-only production runtime."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for relative in (
    "core/server",
    "core/integrations",
    "core/autonomy",
    "core/memory",
    "core/orchestration",
):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

from mike_auto_reply import auto_reply_to_family
from mike_agent_sdk import AgentExecution, SkillAgent
from mike_llama_server_client import MikeLlamaServerClient
from mike_request_helpers import _use_light_chat_context
from mike_mcp_client import (
    MikeHttpMcpClient,
    MikeMcpHub,
    MikeMcpServerConfig,
    _mcp_result_payload,
)
from mike_task_mesh import (
    StepStatus,
    TaskMesh,
    TaskPlan,
    TaskStep,
    fallback_plan_from_goal,
    looks_complex,
)


class QwenSingleBrainTests(unittest.TestCase):
    def test_explicit_tool_request_never_falls_into_toolless_light_mode(self):
        class Request:
            raw_mode = False
            mcp_tools = None

        self.assertFalse(_use_light_chat_context(
            Request(),
            "Use obrigatoriamente a ferramenta autonomy_status e consulte o estado real.",
        ))

    def test_llama_server_root_removes_v1_suffix(self):
        client = MikeLlamaServerClient(base_url="http://127.0.0.1:8081/v1")
        self.assertEqual(client.server_root, "http://127.0.0.1:8081")
        self.assertFalse(client.enable_thinking)
        client.close()

    def test_remote_mcp_server_is_retained_by_hub(self):
        config = MikeMcpServerConfig.from_dict({
            "name": "remote",
            "url": "https://example.test/mcp",
            "transport": "streamable-http",
            "capabilities": ["remote_test"],
        })
        hub = MikeMcpHub(extra_servers=[config])
        self.assertEqual(len(hub.extra_clients), 1)
        self.assertIsInstance(hub.extra_clients[0], MikeHttpMcpClient)
        self.assertEqual(hub.server_summaries()[0]["url"], "https://example.test/mcp")

    def test_structured_mcp_result_is_rendered_for_qwen(self):
        from mcp.types import CallToolResult

        payload, content_types = _mcp_result_payload(
            CallToolResult(
                content=[],
                structuredContent={"result": [{"name": "Barreto"}]},
            )
        )
        self.assertIn('"Barreto"', payload)
        self.assertIn("StructuredContent", content_types)

    def test_auto_reply_reports_missing_email_configuration(self):
        os.environ["MIKE_AUTO_REPLY_ENABLED"] = "true"
        self.addCleanup(os.environ.pop, "MIKE_AUTO_REPLY_ENABLED", None)
        result = auto_reply_to_family(
            list_inbox_fn=lambda **_: [{"error": "OAuth token missing"}],
        )
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["details"][0]["action"], "configuration_error")

    def test_one_read_tool_then_summary_is_not_task_mesh(self):
        self.assertFalse(looks_complex(
            "Liste os diretórios permitidos com uma tool e depois resuma o resultado."
        ))

    def test_sequential_goal_has_deterministic_fallback_plan(self):
        steps = fallback_plan_from_goal(
            "Execute duas ações: primeiro liste os diretórios; "
            "depois liste as tabelas. Responda com um resumo."
        )
        self.assertEqual([step for _, step in steps], [
            "liste os diretórios",
            "liste as tabelas",
        ])


class OrchestrationFallbackTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _extract(text):
        if "<tool_call>" not in text:
            return None
        return {"name": "execute_powershell", "arguments": {"command": "Get-Date"}}

    async def test_agent_marks_unauthorized_tool_call_as_failed(self):
        async def generate(_messages, _request):
            return {
                "assistant_text": (
                    '<tool_call>{"name":"execute_powershell",'
                    '"arguments":{"command":"Get-Date"}}</tool_call>'
                )
            }

        agent = SkillAgent(
            generate_fn=generate,
            execute_tool_fn=lambda *_: None,
            extract_tool_call_fn=self._extract,
        )
        agent.tool_patterns = ["list_allowed_directories"]
        agent.max_tool_steps = 1
        result = await agent.run(
            "Execute a verificação",
            "system",
            [{"name": "list_allowed_directories", "input_schema": {}}],
        )
        self.assertFalse(result.success)
        self.assertEqual(result.metadata["reason"], "unauthorized_tool")

    async def test_agent_accepts_unambiguous_namespaced_tool_alias(self):
        calls = 0

        async def generate(_messages, _request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "assistant_text": (
                        '<tool_call>{"name":"list_directory",'
                        '"arguments":{"path":"."}}</tool_call>'
                    )
                }
            return {
                "assistant_text": (
                    '<tool_call>{"name":"execute_powershell",'
                    '"arguments":{"command":"Get-Date"}}</tool_call>'
                )
            }

        async def execute(name, arguments):
            return {"ok": True, "text": f"{name}: {arguments['path']}"}

        agent = SkillAgent(
            generate_fn=generate,
            execute_tool_fn=execute,
            extract_tool_call_fn=lambda text: (
                {"name": "list_directory", "arguments": {"path": "."}}
                if "<tool_call>" in text else None
            ),
            strip_tool_call_fn=lambda _text: "",
            render_tool_result_fn=lambda *_: "resultado",
        )
        agent.tool_patterns = ["list_directory"]
        result = await agent.run(
            "Liste o diretório",
            "system",
            [{"name": "filesystem.list_directory", "input_schema": {}}],
        )
        self.assertTrue(result.success)
        self.assertEqual(result.tools_used, ["list_directory"])
        self.assertIn("list_directory", result.output)

    async def test_task_mesh_falls_back_when_specialized_agent_fails(self):
        class FailingSpawner:
            async def try_agent_for_step(self, *args, **kwargs):
                return AgentExecution(
                    agent_name="restricted",
                    task="step",
                    output="<tool_call>{}</tool_call>",
                    success=False,
                    metadata={"reason": "unresolved_tool_call"},
                )

        async def generate(_messages, _request):
            return {"assistant_text": "Execução genérica concluída."}

        mesh = TaskMesh(
            generate_fn=generate,
            execute_tool_fn=lambda *_: None,
            extract_tool_call_fn=lambda _text: None,
            strip_tool_call_fn=lambda text: text,
            render_tool_result_fn=lambda *_: "",
            compact_tool_payload_fn=lambda _name, text: text,
            tool_manifest=[{"name": "generic_tool", "input_schema": {}}],
            sub_agent_spawner=FailingSpawner(),
        )
        plan = TaskPlan(goal="teste", steps=[TaskStep(id=1, description="passo")])
        step = await mesh.execute_step(plan, plan.steps[0], "system")
        self.assertEqual(step.result, "Execução genérica concluída.")
        self.assertEqual(step.status.value, "done")

    async def test_consolidation_never_leaks_tool_call(self):
        async def generate(_messages, _request):
            return {
                "assistant_text": (
                    '<tool_call>{"name":"execute_powershell",'
                    '"arguments":{"command":"Get-Date"}}</tool_call>'
                )
            }

        mesh = TaskMesh(
            generate_fn=generate,
            execute_tool_fn=lambda *_: None,
            extract_tool_call_fn=self._extract,
            strip_tool_call_fn=lambda _text: "",
            render_tool_result_fn=lambda *_: "",
            compact_tool_payload_fn=lambda _name, text: text,
        )
        plan = TaskPlan(
            goal="teste",
            steps=[
                TaskStep(
                    id=1,
                    description="ação",
                    status=StepStatus.DONE,
                    result="resultado real",
                )
            ],
        )
        summary = await mesh.consolidate(plan, "system")
        self.assertNotIn("<tool_call>", summary)
        self.assertIn("resultado real", summary)

    async def test_task_mesh_scopes_tools_to_step_domain(self):
        manifest = [
            {
                "name": "sqlite.list-tables",
                "server_name": "sqlite",
                "description": "List tables in the SQLite database",
            },
            {
                "name": "filesystem.list_directory",
                "server_name": "filesystem",
                "description": "List directory contents",
            },
            {
                "name": "execute_powershell",
                "server_name": "local",
                "description": "Execute an arbitrary PowerShell command",
            },
        ]
        mesh = TaskMesh(
            generate_fn=lambda *_: None,
            execute_tool_fn=lambda *_: None,
            extract_tool_call_fn=lambda _text: None,
            strip_tool_call_fn=lambda text: text,
            render_tool_result_fn=lambda *_: "",
            compact_tool_payload_fn=lambda _name, text: text,
            tool_manifest=manifest,
        )
        selected = {
            tool["name"]
            for tool in mesh._tools_for_step("Listar as tabelas do banco SQLite")
        }
        self.assertIn("sqlite.list-tables", selected)
        self.assertNotIn("execute_powershell", selected)


if __name__ == "__main__":
    unittest.main()
