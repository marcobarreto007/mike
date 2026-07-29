"""Public Hugging Face Hub discovery MCP tools for Mike."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi, hf_hub_download
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("Mike Hugging Face MCP", json_response=True)


def _token() -> str | None:
    return (
        os.getenv("HF_TOKEN", "").strip()
        or os.getenv("HUGGING_FACE_HUB_TOKEN", "").strip()
        or None
    )


def _api() -> HfApi:
    return HfApi(token=_token())


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _model_summary(info: Any) -> dict:
    return {
        "id": getattr(info, "id", None) or getattr(info, "modelId", None),
        "author": getattr(info, "author", None),
        "pipeline_tag": getattr(info, "pipeline_tag", None),
        "library_name": getattr(info, "library_name", None),
        "downloads": getattr(info, "downloads", None),
        "likes": getattr(info, "likes", None),
        "last_modified": _iso(getattr(info, "lastModified", None)),
        "gated": getattr(info, "gated", None),
        "private": getattr(info, "private", None),
        "tags": list(getattr(info, "tags", None) or [])[:50],
    }


def _dataset_summary(info: Any) -> dict:
    return {
        "id": getattr(info, "id", None),
        "author": getattr(info, "author", None),
        "downloads": getattr(info, "downloads", None),
        "likes": getattr(info, "likes", None),
        "last_modified": _iso(getattr(info, "lastModified", None)),
        "gated": getattr(info, "gated", None),
        "private": getattr(info, "private", None),
        "tags": list(getattr(info, "tags", None) or [])[:50],
    }


@mcp.tool(description="Search public Hugging Face models.")
def list_hf_models(search: str = "", task: str = "", limit: int = 10) -> list[dict]:
    records = _api().list_models(
        search=search.strip() or None,
        pipeline_tag=task.strip() or None,
        sort="downloads",
        limit=max(1, min(int(limit), 50)),
        token=_token(),
    )
    return [_model_summary(item) for item in records]


@mcp.tool(description="Search public Hugging Face datasets.")
def list_hf_datasets(search: str = "", limit: int = 10) -> list[dict]:
    records = _api().list_datasets(
        search=search.strip() or None,
        sort="downloads",
        limit=max(1, min(int(limit), 50)),
        token=_token(),
    )
    return [_dataset_summary(item) for item in records]


@mcp.tool(description="Get detailed metadata for a Hugging Face model.")
def hf_model_info(repo_id: str, revision: str = "main") -> dict:
    if not repo_id.strip():
        raise ValueError("repo_id is required")
    info = _api().model_info(
        repo_id.strip(),
        revision=revision.strip() or None,
        files_metadata=True,
        token=_token(),
    )
    result = _model_summary(info)
    result["siblings"] = [
        {
            "filename": getattr(item, "rfilename", None),
            "size": getattr(item, "size", None),
        }
        for item in (getattr(info, "siblings", None) or [])[:500]
    ]
    return result


@mcp.tool(description="List repository files from a Hugging Face model or dataset.")
def list_hf_files(
    repo_id: str,
    repo_type: str = "model",
    revision: str = "main",
) -> list[str]:
    if repo_type not in {"model", "dataset", "space"}:
        raise ValueError("repo_type must be model, dataset or space")
    return _api().list_repo_files(
        repo_id=repo_id.strip(),
        repo_type=repo_type,
        revision=revision.strip() or None,
        token=_token(),
    )


@mcp.tool(description="Read a Hugging Face repository README/model card.")
def read_hf_readme(
    repo_id: str,
    repo_type: str = "model",
    revision: str = "main",
    max_chars: int = 50000,
) -> dict:
    if repo_type not in {"model", "dataset", "space"}:
        raise ValueError("repo_type must be model, dataset or space")
    path = hf_hub_download(
        repo_id=repo_id.strip(),
        filename="README.md",
        repo_type=repo_type,
        revision=revision.strip() or None,
        token=_token(),
    )
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    limit = max(1000, min(int(max_chars), 100000))
    return {
        "repo_id": repo_id,
        "repo_type": repo_type,
        "revision": revision,
        "text": text[:limit],
        "truncated": len(text) > limit,
    }


@mcp.tool(description="Show whether a Hugging Face identity token is configured.")
def hf_whoami() -> dict:
    token = _token()
    if not token:
        return {
            "authenticated": False,
            "state": "public_read_only",
        }
    identity = _api().whoami(token=token)
    return {
        "authenticated": True,
        "name": identity.get("name"),
        "fullname": identity.get("fullname"),
        "type": identity.get("type"),
        "orgs": [
            {"name": org.get("name"), "role": org.get("roleInOrg")}
            for org in identity.get("orgs", [])
        ],
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
