"""Key file listing and download endpoints.

Prefix: /api/keys
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from ...config import APPLE_EMAIL, KEYS_DIR, PLISTS_DIR

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


@router.get("/zip")
def download_keys_zip(include: list[str] = Query(default=[])):
    """Return key JSON files bundled as airtag-keys.zip.

    Pass ?include=file.json one or more times to select specific keys.
    Omit to download all keys.
    """
    if not KEYS_DIR.exists():
        raise HTTPException(status_code=404, detail="No keys directory found")

    if include:
        # Validate each requested filename and resolve paths.
        files = []
        for name in include:
            if not _SAFE_FILENAME.match(name):
                raise HTTPException(status_code=400, detail=f"Invalid filename: {name!r}")
            p = KEYS_DIR / name
            if not p.exists() or not p.is_file():
                raise HTTPException(status_code=404, detail=f"Key file not found: {name!r}")
            try:
                p.resolve().relative_to(KEYS_DIR.resolve())
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid filename: {name!r}")
            files.append(p)
        files.sort(key=lambda p: p.name)
    else:
        files = sorted(KEYS_DIR.glob("*.json"), key=lambda p: p.name)

    if not files:
        raise HTTPException(status_code=404, detail="No key files found")

    # Restrict the OpenTagViewer-format payload (OwnedBeacons/, BeaconNamingRecord/)
    # to the beacon UUIDs that correspond to the selected JSON files. The JSON
    # filename is a slug of the AirTag name, so we read each JSON's `identifier`
    # to map back to the beacon UUID.
    selected_ids: set[str] | None = None
    if include:
        import json as _json
        selected_ids = set()
        for f in files:
            try:
                ident = _json.loads(f.read_text()).get("identifier")
                if ident:
                    selected_ids.add(ident.upper())
            except Exception:
                pass

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1) Existing flat JSON files at zip root (FindMy.py / generic consumers).
        for f in files:
            zf.write(f, f.name)

        # 2) OpenTagViewer-format payload: OPENTAGVIEWER.yml + raw decrypted
        #    plists laid out as TagHistory's AppleExportParser expects:
        #      OPENTAGVIEWER.yml                          (manifest, version >= 1)
        #      OwnedBeacons/<beaconUUID>.plist            (Apple binary plist)
        #      BeaconNamingRecord/<beaconUUID>/<recordUUID>.plist
        owned_dir = PLISTS_DIR / "OwnedBeacons"
        naming_dir = PLISTS_DIR / "BeaconNamingRecord"

        if owned_dir.exists():
            owned_plists = sorted(owned_dir.glob("*.plist"))
            if selected_ids is not None:
                owned_plists = [p for p in owned_plists if p.stem.upper() in selected_ids]
            for p in owned_plists:
                zf.write(p, f"OwnedBeacons/{p.name}")

        if naming_dir.exists():
            for beacon_dir in sorted(naming_dir.iterdir()):
                if not beacon_dir.is_dir():
                    continue
                if selected_ids is not None and beacon_dir.name.upper() not in selected_ids:
                    continue
                for p in sorted(beacon_dir.glob("*.plist")):
                    zf.write(p, f"BeaconNamingRecord/{beacon_dir.name}/{p.name}")

        # Manifest — at least `version` is required by TagHistory's parser.
        manifest = (
            "version: '1'\n"
            f"exportTimestamp: '{datetime.now(UTC).isoformat()}'\n"
            f"sourceUser: '{APPLE_EMAIL}'\n"
            "via: 'AFM Key Extractor'\n"
        )
        zf.writestr("OPENTAGVIEWER.yml", manifest)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=airtag-keys.zip"},
    )


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
