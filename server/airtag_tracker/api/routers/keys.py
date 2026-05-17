"""Key file listing and download endpoints.

Prefix: /api/keys
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...config import KEYS_DIR

router = APIRouter(prefix="/api/keys", tags=["keys"])

# Only allow simple filenames — no path traversal.
_SAFE_FILENAME = re.compile(r'^[\w\-. ]+\.json$')


def _key_meta(p: Path) -> dict:
    stat = p.stat()
    return {
        "name": p.name,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    }


@router.get("/")
def list_keys() -> list[dict]:
    if not KEYS_DIR.exists():
        return []
    files = sorted(KEYS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [_key_meta(f) for f in files]


@router.get("/{filename}")
def get_key(filename: str):
    if not _SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = KEYS_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Key file not found")
    # Ensure the resolved path is still inside KEYS_DIR (defence in depth).
    try:
        path.resolve().relative_to(KEYS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return FileResponse(path, media_type="application/json", filename=filename)
