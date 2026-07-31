"""Regression tests for MCP and executable-skill authorization boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse


ROOT = Path(__file__).resolve().parents[2]
for source_dir in (
    ROOT,
    ROOT / "core" / "server",
    ROOT / "core" / "autonomy",
):
    source = str(source_dir)
    if source not in sys.path:
        sys.path.insert(0, source)

from core.autonomy.mike_skill_library import SkillLibrary
from core.server.routers import autonomy as autonomy_router
from core.server.mike_auth import tool_allowed_for_profile


def _request_with_json(payload: dict) -> Request:
    body = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/skills/library/execute",
            "raw_path": b"/v1/skills/library/execute",
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.10", 43210),
            "server": ("mike.example", 443),
        },
        receive=receive,
    )


def test_skill_library_executes_pure_data_skill_in_isolated_process(tmp_path):
    library = SkillLibrary(store_dir=tmp_path)
    skill = library.add_skill(
        name="sum_numbers",
        description="Sum numeric parameters",
        code="result = sum(params.get('numbers', []))",
    )

    assert skill is not None
    assert library.execute_skill(skill.id, {"numbers": [2, 3, 5]}) == {
        "success": True,
        "output": "10",
    }


def test_skill_library_rejects_filesystem_and_import_escape_vectors(tmp_path):
    library = SkillLibrary(store_dir=tmp_path)
    payloads = (
        "from pathlib import Path\nresult = Path(params['path']).read_text()",
        "import io\nresult = io.open(params['path']).read()",
        "from pprint import sys as leaked_sys\n"
        "result = leaked_sys.modules['pathlib'].Path(params['path']).read_text()",
        "result = open(params['path']).read()",
    )

    for index, code in enumerate(payloads):
        skill = library.add_skill(
            name=f"filesystem_escape_{index}",
            description="Must not be accepted",
            code=code,
        )
        assert skill is None


def test_persisted_validated_flag_is_not_trusted(tmp_path):
    library_file = tmp_path / "skill_library.json"
    library_file.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "id": "skill_untrusted",
                        "name": "untrusted",
                        "description": "Persisted malicious skill",
                        "code": "from pathlib import Path\n"
                        "result = Path(params['path']).read_text()",
                        "validated": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    library = SkillLibrary(store_dir=tmp_path)
    skill = library.get_skill("skill_untrusted")

    assert skill is not None
    assert skill.validated is False
    result = library.execute_skill(skill.id, {"path": str(tmp_path / "secret.txt")})
    assert result["success"] is False
    assert "not validated" in result["error"].lower()


def test_skill_library_execute_endpoint_is_owner_only(monkeypatch):
    calls = []

    class FakeLibrary:
        def execute_skill(self, skill_id, params=None):
            calls.append((skill_id, params))
            return {"success": True, "output": "ok"}

    monkeypatch.setattr(autonomy_router, "_get_skill_library", lambda: FakeLibrary())
    monkeypatch.setattr(autonomy_router, "_request_profile_scope", lambda request: getattr(
        request.state, "profile", None
    ))

    visitor_request = _request_with_json({"skill_id": "safe", "params": {}})
    visitor_request.state.profile = "visitante"
    denied = asyncio.run(autonomy_router.skill_library_execute(visitor_request))

    assert isinstance(denied, JSONResponse)
    assert denied.status_code == 403
    assert calls == []

    owner_request = _request_with_json({"skill_id": "safe", "params": {"x": 1}})
    owner_request.state.profile = "marco"
    allowed = asyncio.run(autonomy_router.skill_library_execute(owner_request))

    assert allowed == {"success": True, "output": "ok"}
    assert calls == [("safe", {"x": 1})]


def test_skill_reload_uses_registry_load_all(monkeypatch):
    class FakeRegistry:
        def load_all(self):
            return 48

    monkeypatch.setattr(autonomy_router, "_get_skill_registry", lambda: FakeRegistry())

    result = asyncio.run(autonomy_router.skills_reload())

    assert result == {"status": "ok", "loaded_skill_count": 48}


def test_sensitive_mcp_servers_are_owner_only_in_all_configs():
    sensitive = {"filesystem", "sqlite", "github", "fetch"}
    for config_name in ("mcp_servers.json", "mcp_servers.example.json"):
        servers = json.loads(
            (ROOT / "config" / config_name).read_text(encoding="utf-8")
        )
        access_by_name = {
            str(server.get("name")): str(server.get("access"))
            for server in servers
        }
        assert sensitive <= access_by_name.keys()
        assert {access_by_name[name] for name in sensitive} == {"owner"}


def test_access_any_cannot_override_mutating_or_sensitive_tools():
    assert not tool_allowed_for_profile(
        "filesystem.delete_file", "visitante", access="any"
    )
    assert not tool_allowed_for_profile(
        "workspace.run_command", "visitante", access="any"
    )
    assert not tool_allowed_for_profile(
        "github.create_issue", "visitante", access="any"
    )
    assert not tool_allowed_for_profile(
        "fetch.fetch", "visitante", access="any"
    )
    assert tool_allowed_for_profile(
        "filesystem.delete_file", "marco", access="any"
    )


def test_unknown_access_any_tool_is_denied_but_reviewed_read_is_allowed():
    assert not tool_allowed_for_profile(
        "untrusted.execute_arbitrary", "visitante", access="any"
    )
    assert tool_allowed_for_profile(
        "safe.list_calendars", "visitante", access="read"
    )
