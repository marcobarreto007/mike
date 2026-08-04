#!/usr/bin/env python3
"""Repository security gates that are safe to run locally and in CI.

The scanner only examines files tracked by Git. It reports the location and
credential key, never the credential value.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_ASSIGNMENT_RE = re.compile(
    r"""^\s*(?:(?:export|const|let|var|readonly)\s+)?"""
    r"""(?P<key>\$?["']?[A-Za-z_][A-Za-z0-9_.-]*["']?)"""
    r"""\s*(?:=|:)\s*(?P<value>.*?)\s*$"""
)
_ENV_ASSIGNMENT_RE = re.compile(
    r"""^\s*(?:\$env:(?P<ps_key>[A-Za-z_][A-Za-z0-9_]*)"""
    r"""|os\.environ\[\s*["'](?P<py_key>[A-Za-z_][A-Za-z0-9_]*)["']\s*\]"""
    r"""|process\.env\.(?P<js_key>[A-Za-z_][A-Za-z0-9_]*))"""
    r"""\s*=\s*(?P<value>.*?)\s*$"""
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|API_KEY)(?:_HASH)?$"
)
_EXACT_VERSION_RE = re.compile(
    r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?(?:\+[0-9A-Za-z.-]+)?$"
)
_PLACEHOLDER_MARKERS = (
    "changeme",
    "change_me",
    "dummy",
    "example",
    "fixture",
    "not-needed",
    "not-set",
    "placeholder",
    "redacted",
    "replace_me",
    "sample",
    "test",
    "your_",
    "your-",
)
_PLACEHOLDER_VALUES = {
    "",
    "-",
    "false",
    "none",
    "null",
    "password",
    "secret",
    "token",
    "true",
    "x",
    "xx",
    "xxx",
    "xxxx",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    subject: str
    message: str


def _tracked_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [
        root / name.decode("utf-8", errors="surrogateescape")
        for name in completed.stdout.split(b"\0")
        if name
    ]


def _sensitive_key(key: str) -> bool:
    normalized = key.strip().lstrip("$").strip("\"'").replace("-", "_").replace(".", "_")
    # Credential configuration keys are conventionally upper-case. Restricting
    # this rule avoids mistaking ordinary variables such as token_count for an
    # embedded credential while still covering env, JSON, YAML and PowerShell.
    if normalized != normalized.upper():
        return False
    return bool(_SENSITIVE_KEY_RE.search(normalized))


def _literal_value(raw: str) -> str | None:
    value = raw.strip()
    if value.endswith((",", ";")):
        value = value[:-1].rstrip()
    if not value:
        return ""

    if value[0] in "\"'":
        quote = value[0]
        escaped = False
        for index in range(1, len(value)):
            char = value[index]
            if char == quote and not escaped:
                remainder = value[index + 1 :].strip()
                if remainder and not remainder.startswith("#"):
                    return None
                return value[1:index]
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        return None

    # Environment references and code expressions are not embedded credentials.
    lowered = value.lower()
    if (
        value.startswith(("$", "${", "{{", "%"))
        or lowered.startswith(("os.getenv(", "os.environ", "getenv(", "env(", "secret("))
        or any(char in value for char in "()[]{}")
    ):
        return None

    # Bare .env values may be credentials. Strip a conventional inline comment.
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if any(char.isspace() for char in value):
        return None
    return value


def _is_placeholder(value: str) -> bool:
    candidate = value.strip()
    lowered = candidate.lower()
    if lowered in _PLACEHOLDER_VALUES:
        return True
    if candidate.startswith("${") and candidate.endswith("}"):
        return True
    if candidate.startswith("{{") and candidate.endswith("}}"):
        return True
    if candidate.startswith("<") and candidate.endswith(">"):
        return True
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def scan_secret_text(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        env_match = _ENV_ASSIGNMENT_RE.match(line)
        match = env_match or _ASSIGNMENT_RE.match(line)
        if not match:
            continue
        if env_match:
            key = next(
                value
                for value in (
                    env_match.group("ps_key"),
                    env_match.group("py_key"),
                    env_match.group("js_key"),
                )
                if value
            )
        else:
            key = match.group("key").strip().lstrip("$").strip("\"'")
        if not _sensitive_key(key):
            continue
        value = _literal_value(match.group("value"))
        if value is None or _is_placeholder(value):
            continue
        findings.append(
            Finding(
                path=str(path),
                line=line_number,
                kind="secret-assignment",
                subject=key,
                message="contains a non-placeholder credential value",
            )
        )
    return findings


def _package_from_npx_args(args: list[Any]) -> str | None:
    index = 0
    while index < len(args):
        arg = str(args[index])
        if arg in {"-y", "--yes", "--quiet", "-q"}:
            index += 1
            continue
        if arg in {"-p", "--package"}:
            return str(args[index + 1]) if index + 1 < len(args) else None
        if arg.startswith("--package="):
            return arg.split("=", 1)[1]
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return None


def _is_exactly_pinned(package: str) -> bool:
    if package.startswith((".", "/", "\\", "file:", "workspace:")):
        return True
    if package.startswith("@"):
        slash = package.find("/")
        version_at = package.rfind("@")
        if slash < 0 or version_at <= slash:
            return False
        version = package[version_at + 1 :]
    else:
        if "@" not in package:
            return False
        _, version = package.rsplit("@", 1)
    return bool(_EXACT_VERSION_RE.fullmatch(version))


def _walk_npx_servers(value: Any, location: str = "") -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        command = str(value.get("command", "")).lower()
        if Path(command).name.lower() in {"npx", "npx.cmd", "npx.exe"}:
            yield location or "<root>", value
        for key, child in value.items():
            child_location = f"{location}.{key}" if location else str(key)
            yield from _walk_npx_servers(child, child_location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_location = f"{location}[{index}]"
            yield from _walk_npx_servers(child, child_location)


def scan_mcp_manifest(path: Path, text: str) -> list[Finding]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return [
            Finding(
                path=str(path),
                line=exc.lineno,
                kind="mcp-manifest",
                subject="invalid-json",
                message="cannot verify MCP package pinning",
            )
        ]

    findings: list[Finding] = []
    for location, server in _walk_npx_servers(document):
        args = server.get("args", [])
        package = _package_from_npx_args(args if isinstance(args, list) else [])
        if not package or not _is_exactly_pinned(package):
            name = str(server.get("name") or location)
            findings.append(
                Finding(
                    path=str(path),
                    line=1,
                    kind="unpinned-npx",
                    subject=name,
                    message="uses npx without an exact package version",
                )
            )
    return findings


def _ignored_paths(root: Path) -> set[str]:
    """Paths allowlisted in .gitleaksignore, as POSIX-relative strings.

    Test fixtures for this scanner necessarily embed credential-shaped
    strings; the repository already lists them for gitleaks, so honour the
    same file instead of maintaining a second allowlist.
    """
    ignore_file = root / ".gitleaksignore"
    if not ignore_file.is_file():
        return set()
    entries = set()
    for line in ignore_file.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        # gitleaks fingerprints look like "path:rule:line"; keep the path.
        entries.add(entry.split(":")[0].replace("\\", "/"))
    return entries


def scan_repository(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    ignored = _ignored_paths(root)
    for path in _tracked_files(root):
        if not path.is_file():
            continue
        if path.relative_to(root).as_posix() in ignored:
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        relative = path.relative_to(root)
        findings.extend(scan_secret_text(relative, text))
        lowered_name = path.name.lower()
        if lowered_name == ".mcp.json" or (
            lowered_name.startswith("mcp") and path.suffix.lower() == ".json"
        ):
            findings.extend(scan_mcp_manifest(relative, text))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Git worktree to scan (defaults to the Mike repository).",
    )
    args = parser.parse_args(argv)

    try:
        findings = scan_repository(args.root.resolve())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"SECURITY GATE ERROR: {type(exc).__name__}", file=sys.stderr)
        return 2

    if findings:
        print(f"SECURITY GATE FAILED: {len(findings)} finding(s).")
        for finding in findings:
            print(
                f"{finding.path}:{finding.line} [{finding.kind}] "
                f"{finding.subject}: {finding.message}"
            )
        return 1

    print("SECURITY GATE PASSED: tracked secrets and MCP package pins are clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
