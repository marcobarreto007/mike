"""Read-only readiness audit for Mike's Qwen, memory, autonomy, and tools."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def request_json(
    base_url: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def tool_probe(
    base_url: str,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = request_json(
            base_url,
            "/v1/tools/call",
            payload={"name": name, "arguments": arguments},
            timeout=timeout,
        )
        result = response.get("result") or {}
        text = str(result.get("text") or "")
        return {
            "ok": bool(result.get("ok")),
            "elapsed_sec": round(time.perf_counter() - started, 2),
            "detail": text[:240],
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_sec": round(time.perf_counter() - started, 2),
            "detail": f"{type(exc).__name__}: {exc}",
        }


def build_report(base_url: str, run_smoke: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "base_url": base_url,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "core": {},
        "inventory": {},
        "integrations": {},
        "servers": {},
        "tool_probes": {},
        "external_blockers": [],
    }

    health = request_json(base_url, "/health")
    models = request_json(base_url, "/v1/health/models")
    stats = request_json(base_url, "/stats")
    tools = request_json(base_url, "/v1/tools")
    skills = request_json(base_url, "/v1/skills/coverage")
    autonomy = request_json(base_url, "/v1/autonomy/status")
    governance = request_json(base_url, "/v1/governance")
    integration_status = request_json(base_url, "/v1/integrations/status")

    backends = models.get("backends") or {}
    chain = models.get("fallback_chain_order") or []
    lightrag = stats.get("lightrag") or {}
    last_governance = governance.get("last_cycle") or {}

    report["core"] = {
        "api_healthy": health.get("status") == "healthy",
        "active_model": health.get("active_model"),
        "qwen_single_brain": (
            health.get("llm_backend") == "llama_server"
            and list(backends) == ["llama_server"]
            and chain == ["llama_server"]
            and bool(backends.get("llama_server", {}).get("healthy"))
        ),
        "memory_ready": (
            stats.get("memory_backend") == "mem0-oss+sqlite"
            and bool(stats.get("vector_search_enabled"))
            and bool(lightrag.get("ready"))
            and bool(lightrag.get("has_indexed_data"))
        ),
        "autonomy_running": bool(autonomy.get("running")),
        "governance_running": bool(governance.get("running")),
        "governance_healthy": bool(last_governance.get("healthy")),
        "skills_tools_ready": bool(skills.get("all_skills_have_tools")),
        "gpu_detected": bool(stats.get("cuda_detected")),
        "gpu_name": stats.get("gpu_name"),
        "vision_enabled": bool(stats.get("vision_enabled")),
    }
    report["inventory"] = {
        "tool_count": len(tools.get("tools") or []),
        "mcp_server_count": len(tools.get("servers") or []),
        "skill_count": skills.get("loaded_skill_count"),
        "all_skills_packed": bool(skills.get("all_skills_packed")),
        "skills_with_tools": skills.get("tool_ready_skill_count"),
        "tool_pattern_coverage_percent": skills.get("tool_pattern_coverage_percent"),
        "memory_backend": stats.get("memory_backend"),
        "vector_model": stats.get("vector_search_model"),
        "mem0_model": stats.get("mem0_embed_model"),
        "knowledge_documents": stats.get("knowledge_documents"),
        "knowledge_chunks": stats.get("knowledge_chunks"),
    }
    report["integrations"] = integration_status.get("integrations") or {}
    for name, state in report["integrations"].items():
        if not state.get("ready"):
            report["external_blockers"].append(
                {"integration": name, "reason": state.get("state") or "not_ready"}
            )

    for server in tools.get("servers") or []:
        name = str(server.get("name") or "")
        server_state = {
            "enabled": bool(server.get("enabled")),
            "transport": server.get("transport") or "stdio",
            "last_error": server.get("last_error"),
        }
        report["servers"][name] = server_state
        if server_state["last_error"]:
            if not any(
                blocker["integration"] == name
                for blocker in report["external_blockers"]
            ):
                report["external_blockers"].append(
                    {"integration": name, "reason": server_state["last_error"]}
                )

    if run_smoke:
        probes: dict[str, tuple[str, dict[str, Any]]] = {
            "workspace": ("list_allowed_directories", {}),
            "workspace-command": (
                "run_command",
                {
                    "command": "Write-Output 'MIKE_WORKSPACE_COMMAND_OK'",
                    "cwd": ".",
                    "timeout_seconds": 10,
                },
            ),
            "memory-persistent": (
                "memory-persistent.search_nodes",
                {"query": "MIKE_READINESS_PROBE_NONDESTRUCTIVE"},
            ),
            "sequential-thinking": (
                "sequential-thinking.sequentialthinking",
                {
                    "thought": "Read-only readiness probe complete.",
                    "thoughtNumber": 1,
                    "totalThoughts": 1,
                    "nextThoughtNeeded": False,
                },
            ),
            "filesystem": ("filesystem.list_allowed_directories", {}),
            "github": (
                "github.search_repositories",
                {"query": "mcp server", "perPage": 1},
            ),
            "sqlite": ("sqlite.list-tables", {}),
            "web-search": (
                "web.search_and_cache",
                {"query": "OpenAI official website", "limit": 1},
            ),
            "puppeteer": (
                "puppeteer.puppeteer_navigate",
                {"url": f"{base_url.rstrip('/')}/"},
            ),
            "excel": (
                "excel.list_spreadsheets",
                {"directory": ".", "recursive": False, "limit": 2},
            ),
            "appointments": ("appointments.appointment_status", {}),
            "huggingface": ("hf.hf_whoami", {}),
            "autonomy": ("autonomy_status", {}),
            "tasks": ("list_tasks", {"limit": 2}),
            "drive": ("drive.list_drive_files", {"limit": 1}),
            "remote-agent": ("remote.check_remote_agent_health", {}),
            "calendar": ("calendar.list_calendars", {}),
            "gmail": ("gmail.list_inbox", {"limit": 1}),
            "local": ("mike.introspect", {"section": "core"}),
            "email": ("email.list_inbox", {"limit": 1, "unread_only": True}),
        }
        for label, (tool_name, arguments) in probes.items():
            report["tool_probes"][label] = tool_probe(
                base_url, tool_name, arguments
            )
            detail_lower = report["tool_probes"][label]["detail"].lower()
            if label in {"calendar", "gmail", "drive"} and (
                "oauth2" in detail_lower or '"error"' in detail_lower
            ):
                report["tool_probes"][label]["ok"] = False
            if label == "remote-agent" and '"reachable": false' in detail_lower:
                report["tool_probes"][label]["ok"] = False
        if not report["tool_probes"]["email"]["ok"]:
            report["external_blockers"].append({
                "integration": "email_live_probe",
                "reason": report["tool_probes"]["email"]["detail"],
            })

    # Vision is intentionally false for the text-only Qwen and is not a failure.
    required_flags = {
        key: value
        for key, value in report["core"].items()
        if key not in {"vision_enabled", "gpu_detected"}
        and isinstance(value, bool)
    }
    report["core_ready"] = all(required_flags.values())
    report["local_tools_ready"] = all(
        value.get("ok")
        for key, value in report["tool_probes"].items()
        if key not in {"email", "gmail", "calendar", "drive", "remote-agent"}
    ) if run_smoke else None
    report["all_integrations_ready"] = (
        report["core_ready"]
        and (report["local_tools_ready"] is not False)
        and not report["external_blockers"]
    )
    return report


def print_human(report: dict[str, Any]) -> None:
    print("MIKE READINESS")
    print(f"  Core: {'OK' if report['core_ready'] else 'FAIL'}")
    print(
        "  Brain:",
        report["core"].get("active_model"),
        "(single Qwen)" if report["core"].get("qwen_single_brain") else "(invalid)",
    )
    print(
        f"  Tools: {report['inventory'].get('tool_count')} | "
        f"Skills: {report['inventory'].get('skill_count')} | "
        f"Skill tool coverage: {report['inventory'].get('tool_pattern_coverage_percent')}% | "
        f"Local probes: {report.get('local_tools_ready')}"
    )
    for name, result in report.get("tool_probes", {}).items():
        marker = "OK" if result.get("ok") else "FAIL"
        print(f"    [{marker}] {name} ({result.get('elapsed_sec')}s)")
        if not result.get("ok") and result.get("detail"):
            print(f"      {result['detail']}")
    if report["external_blockers"]:
        print("  External blockers:")
        for blocker in report["external_blockers"]:
            reason = str(blocker["reason"]).splitlines()[0][:180]
            print(f"    - {blocker['integration']}: {reason}")
    else:
        print("  External integrations: OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8083")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when credential-dependent external integrations are unavailable.",
    )
    args = parser.parse_args()

    try:
        report = build_report(args.base_url, not args.skip_smoke)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Readiness audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_human(report)

    if not report["core_ready"] or report["local_tools_ready"] is False:
        return 1
    if args.strict and not report["all_integrations_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
