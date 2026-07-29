# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Drive Indexer — Auto-index Google Drive documents
=======================================================

Background task: runs every 24 hours (configurable via MIKE_DRIVE_INDEX_INTERVAL_HOURS).
Lists PDF, DOCX, Google Docs/Sheets from Drive, exports them to text and saves to
mike/knowledge/drive_docs/ for RAG indexing.

Also exposed as a REST endpoint: POST /v1/drive/index
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("mike.drive_indexer")

_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent.parent  # core/integrations -> core -> mike (project root)

DRIVE_INDEX_DIR = _PROJECT_ROOT / "runtime" / "knowledge" / "drive_docs"

DRIVE_INDEX_MANIFEST = DRIVE_INDEX_DIR / ".manifest.json"

_DID_ENSURE_DIRS = False


def _ensure_drive_index_dir() -> None:
    """Create the Drive index directory on first use (deferred from import time)."""
    global _DID_ENSURE_DIRS
    if not _DID_ENSURE_DIRS:
        DRIVE_INDEX_DIR.mkdir(parents=True, exist_ok=True)
        _DID_ENSURE_DIRS = True

# MIME types to export/download
_SUPPORTED_MIMES = {
    "application/vnd.google-apps.document":       ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet":    ("text/csv",   ".csv"),
    "application/vnd.google-apps.presentation":   ("text/plain", ".txt"),
    "application/pdf":                            (None,         ".pdf"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (None, ".docx"),
}

_MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MB per file safety limit
_MAX_FILES_PER_RUN = int(os.getenv("MIKE_DRIVE_INDEX_MAX_FILES", "50"))


def _load_manifest() -> dict:
    if DRIVE_INDEX_MANIFEST.exists():
        try:
            return json.loads(DRIVE_INDEX_MANIFEST.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("[drive_indexer] Failed to parse manifest: %s", e)
    return {}


def _save_manifest(manifest: dict) -> None:
    DRIVE_INDEX_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _safe_filename(name: str, ext: str) -> str:
    """Sanitise a Drive file name into a safe filesystem name."""
    safe = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name)
    safe = safe.strip().replace(" ", "_")[:80]
    return (safe or "doc") + ext


class MikeDriveIndexer:
    """Downloads changed Drive files and saves them to the knowledge base."""

    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
    TOKEN_ENV_NAMES = ["MIKE_DRIVE_TOKEN", "MIKE_GOOGLE_TOKEN"]
    TOKEN_DEFAULTS = [
        "config/google_workspace_token.json",
        "config/drive_token.json",
    ]

    def __init__(self, log_fn=None):
        self.log = log_fn or log.info

    def _get_service(self):
        try:
            from mike_google_auth import build_google_service
            service, _ = build_google_service(
                "drive", "v3", self.SCOPES,
                token_env_names=self.TOKEN_ENV_NAMES,
                token_defaults=self.TOKEN_DEFAULTS,
            )
            return service
        except Exception as exc:
            raise RuntimeError(f"Drive auth failed: {exc}") from exc

    def _list_files(self, service, limit: int = _MAX_FILES_PER_RUN) -> list[dict]:
        """Return most recently modified supported files from Drive."""
        mime_filter = " or ".join(f"mimeType='{m}'" for m in _SUPPORTED_MIMES)
        query = f"({mime_filter}) and trashed=false"
        results = []
        page_token = None
        while len(results) < limit:
            resp = (
                service.files()
                .list(
                    q=query,
                    pageSize=min(50, limit - len(results)),
                    orderBy="modifiedTime desc",
                    fields="nextPageToken,files(id,name,mimeType,modifiedTime,size)",
                    pageToken=page_token,
                )
                .execute()
            )
            results.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results[:limit]

    def _export_or_download(self, service, file_meta: dict) -> Optional[bytes]:
        """Export Google native formats or download binary files."""
        mime = file_meta["mimeType"]
        fid = file_meta["id"]
        export_mime, _ext = _SUPPORTED_MIMES.get(mime, (None, ".bin"))
        try:
            if export_mime:
                # Google Workspace format — export to plain text/csv
                data = service.files().export(fileId=fid, mimeType=export_mime).execute()
                if isinstance(data, bytes):
                    return data
                return str(data).encode("utf-8")
            else:
                # Binary (PDF, DOCX) — download
                request = service.files().get_media(fileId=fid)
                buf = io.BytesIO()
                from googleapiclient.http import MediaIoBaseDownload
                dl = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = dl.next_chunk()
                content = buf.getvalue()
                if len(content) > _MAX_FILE_BYTES:
                    self.log(f"[DRIVE] Skipped {file_meta['name']}: too large ({len(content)//1024}KB)")
                    return None
                return content
        except Exception as exc:
            self.log(f"[DRIVE] Export/download failed for {file_meta['name']}: {exc}")
            return None

    def _fingerprint(self, content: bytes) -> str:
        return hashlib.md5(content, usedforsecurity=False).hexdigest()

    def run(self) -> dict:
        """Run the indexing pass. Returns a summary dict."""
        _ensure_drive_index_dir()
        t0 = time.time()
        result = {"indexed": 0, "skipped": 0, "failed": 0, "files": []}
        try:
            service = self._get_service()
        except RuntimeError as exc:
            self.log(f"[DRIVE] Cannot connect to Drive: {exc}")
            return {**result, "error": str(exc)}

        manifest = _load_manifest()
        files = self._list_files(service)
        self.log(f"[DRIVE] Found {len(files)} candidate files in Drive")

        changed = False
        for meta in files:
            fid = meta["id"]
            mod = meta.get("modifiedTime", "")
            if manifest.get(fid, {}).get("modifiedTime") == mod:
                result["skipped"] += 1
                continue

            content = self._export_or_download(service, meta)
            if content is None:
                result["failed"] += 1
                continue

            mime = meta["mimeType"]
            _export_mime, ext = _SUPPORTED_MIMES.get(mime, (None, ".bin"))
            fname = _safe_filename(meta["name"], ext)
            dest = DRIVE_INDEX_DIR / fname
            try:
                dest.write_bytes(content)
                manifest[fid] = {"modifiedTime": mod, "name": meta["name"], "file": fname}
                result["indexed"] += 1
                result["files"].append(fname)
                changed = True
                self.log(f"[DRIVE] Indexed: {fname}")
            except Exception as exc:
                self.log(f"[DRIVE] Save failed for {fname}: {exc}")
                result["failed"] += 1

        if changed:
            _save_manifest(manifest)

        result["elapsed"] = round(time.time() - t0, 2)
        result["drive_docs_dir"] = str(DRIVE_INDEX_DIR)
        self.log(f"[DRIVE] Indexing done: {result}")
        return result
