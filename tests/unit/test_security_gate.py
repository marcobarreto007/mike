"""Unit tests for the local/CI repository security gate."""

import importlib.util
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "security" / "security_gate.py"
SPEC = importlib.util.spec_from_file_location("security_gate", SCRIPT)
security_gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = security_gate
SPEC.loader.exec_module(security_gate)


def test_secret_finding_never_contains_the_secret_value():
    embedded_value = "live-credential-value-with-entropy"
    findings = security_gate.scan_secret_text(
        Path("fixture.env"), f'SERVICE_API_KEY="{embedded_value}"\n'
    )

    assert len(findings) == 1
    assert findings[0].subject == "SERVICE_API_KEY"
    assert embedded_value not in repr(findings[0])


def test_documented_placeholders_are_allowed():
    text = "\n".join(
        (
            "EMPTY_TOKEN=",
            'EXAMPLE_SECRET="${EXAMPLE_SECRET}"',
            'TEST_PASSWORD="ci_test_password"',
            'SERVICE_API_KEY="<set-in-environment>"',
            "$REMOTE_TOKEN = $env:REMOTE_TOKEN",
        )
    )

    assert security_gate.scan_secret_text(Path(".env.example"), text) == []


def test_real_bare_and_quoted_assignments_are_detected_without_values():
    text = "\n".join(
        (
            "SERVICE_TOKEN=live_bare_credential",
            '$API_KEY = "live-quoted-credential"',
            '"SESSION_SECRET": "live-json-credential",',
            'process.env.BUILD_TOKEN = "live-js-credential"',
            'os.environ["DEPLOY_PASSWORD"] = "live-python-credential"',
            'const RELEASE_SECRET = "live-js-semicolon";',
        )
    )

    findings = security_gate.scan_secret_text(Path("credentials.txt"), text)
    assert [finding.subject for finding in findings] == [
        "SERVICE_TOKEN",
        "API_KEY",
        "SESSION_SECRET",
        "BUILD_TOKEN",
        "DEPLOY_PASSWORD",
        "RELEASE_SECRET",
    ]
    for raw_value in (
        "live_bare_credential",
        "live-quoted-credential",
        "live-json-credential",
        "live-js-credential",
        "live-python-credential",
        "live-js-semicolon",
    ):
        assert raw_value not in repr(findings)


def test_npx_package_requires_an_exact_version():
    unpinned = '{"name":"memory","command":"npx","args":["-y","@scope/server"]}'
    pinned = '{"name":"memory","command":"npx","args":["-y","@scope/server@1.2.3"]}'

    findings = security_gate.scan_mcp_manifest(Path("mcp.json"), unpinned)
    assert len(findings) == 1
    assert findings[0].kind == "unpinned-npx"
    assert security_gate.scan_mcp_manifest(Path("mcp.json"), pinned) == []


def test_cli_fails_for_tracked_secret_without_printing_it(tmp_path):
    embedded_value = "live-cli-credential-value"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    fixture = tmp_path / "fixture.env"
    fixture.write_text(f'DEPLOY_API_KEY="{embedded_value}"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "fixture.env"], check=True)

    failed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    output = failed.stdout + failed.stderr
    assert failed.returncode == 1
    assert "DEPLOY_API_KEY" in output
    assert embedded_value not in output

    fixture.write_text('DEPLOY_API_KEY="${DEPLOY_API_KEY}"\n', encoding="utf-8")
    passed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert passed.returncode == 0
