"""Key file listing and download endpoints.

Prefix: /api/keys
"""

from __future__ import annotations

import io
import json as _json
import plistlib
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from ...config import APPLE_EMAIL, KEYS_DIR, PLISTS_DIR

router = APIRouter(prefix="/api/keys", tags=["keys"])

# Only allow simple filenames — no path traversal. Accepts AirTag JSONs
# (KEYS_DIR/*.json) and raw OwnedBeacons plists named by UUID (UUID.plist).
_SAFE_FILENAME = re.compile(r'^[\w\-. ]+\.(json|plist)$')


def _airtag_meta(p: Path) -> dict:
    """Metadata for an AirTag JSON in KEYS_DIR."""
    stat = p.stat()
    try:
        data = _json.loads(p.read_text())
        display = data.get("name") or p.stem
        model = data.get("model") or "AirTag"
        identifier = (data.get("identifier") or "").upper()
    except Exception:
        display, model, identifier = p.stem, "AirTag", ""
    return {
        "id": p.name,                     # JSON filename - per-row select key
        "name": p.name,                   # legacy field (kept for ?include= compatibility)
        "display_name": display,
        "kind": "airtag",
        "model": model,
        "identifier": identifier,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    }


def _naming_lookup(beacon_uuid: str) -> str | None:
    """Best-effort display name from BeaconNamingRecord/<beaconUUID>/*.plist."""
    naming_root = PLISTS_DIR / "BeaconNamingRecord" / beacon_uuid.upper()
    if not naming_root.exists():
        return None
    for plist_path in naming_root.glob("*.plist"):
        try:
            with plist_path.open("rb") as f:
                pl = plistlib.load(f)
            name = pl.get("name")
            if name:
                return name
        except Exception:
            continue
    return None


def _other_meta(p: Path) -> dict | None:
    """Metadata for a non-AirTag OwnedBeacons plist."""
    try:
        with p.open("rb") as f:
            pl = plistlib.load(f)
    except Exception:
        return None
    model = pl.get("model") or "Unknown"
    identifier = (pl.get("identifier") or p.stem).upper()
    display = _naming_lookup(p.stem) or pl.get("name") or identifier
    stat = p.stat()
    return {
        "id": p.name,
        "name": p.name,
        "display_name": display,
        "kind": "other",
        "model": model,
        "identifier": identifier,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    }


@router.get("/")
def list_keys(include_others: bool = Query(default=False)) -> list[dict]:
    """List extracted entries.

    Default: only AirTags (the JSON files in KEYS_DIR). Pass include_others=true
    to also include non-AirTag Find My items (iPhones, AirPods, Macs, ...) from
    the raw OwnedBeacons plists.
    """
    out: list[dict] = []

    if KEYS_DIR.exists():
        out.extend(_airtag_meta(p) for p in KEYS_DIR.glob("*.json"))

    if include_others:
        airtag_ids = {e["identifier"] for e in out if e["identifier"]}
        owned_dir = PLISTS_DIR / "OwnedBeacons"
        if owned_dir.exists():
            for p in owned_dir.glob("*.plist"):
                if p.stem.upper() in airtag_ids:
                    continue
                meta = _other_meta(p)
                if meta is not None:
                    out.append(meta)

    out.sort(key=lambda e: (e["kind"] == "other", e["display_name"].lower()))
    return out


@router.get("/zip")
def download_keys_zip(
    include: list[str] = Query(default=[]),
    include_other_devices: bool = Query(default=False),
):
    """Return key files bundled as airtag-keys.zip.

    Pass ?include=<filename> one or more times to select specific entries.
    Filenames can be AirTag JSONs (e.g. Marius_Keys.json) or non-AirTag
    OwnedBeacons plists named by UUID (e.g. 41932D98-...-583CCFDFBF5A.plist).

    Without any ?include= the zip contains every AirTag plus
    OPENTAGVIEWER.yml. Pass include_other_devices=true (no per-item
    filtering) to also bundle every non-AirTag plist on disk.
    """
    if not KEYS_DIR.exists():
        raise HTTPException(status_code=404, detail="No keys directory found")

    json_files: list[Path] = []
    explicit_other_uuids: set[str] = set()

    if include:
        # Validate each requested filename and bucket into AirTag JSONs vs
        # non-AirTag plist UUIDs.
        for name in include:
            if not _SAFE_FILENAME.match(name):
                raise HTTPException(status_code=400, detail=f"Invalid filename: {name!r}")
            if name.endswith(".json"):
                p = KEYS_DIR / name
                if not p.exists() or not p.is_file():
                    raise HTTPException(status_code=404, detail=f"Key file not found: {name!r}")
                try:
                    p.resolve().relative_to(KEYS_DIR.resolve())
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid filename: {name!r}")
                json_files.append(p)
            else:  # .plist
                owned = PLISTS_DIR / "OwnedBeacons" / name
                if not owned.exists() or not owned.is_file():
                    raise HTTPException(status_code=404, detail=f"Plist not found: {name!r}")
                try:
                    owned.resolve().relative_to((PLISTS_DIR / "OwnedBeacons").resolve())
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid filename: {name!r}")
                explicit_other_uuids.add(owned.stem.upper())
        json_files.sort(key=lambda p: p.name)
    else:
        json_files = sorted(KEYS_DIR.glob("*.json"), key=lambda p: p.name)

    if not json_files and not explicit_other_uuids:
        raise HTTPException(status_code=404, detail="No entries selected")

    # Build the AirTag UUID set from the selected JSONs' `identifier` field.
    # Each AirTag JSON exists only because plist_conversion accepted it (i.e.
    # the plist had both sharedSecret AND secondarySharedSecret) so this set
    # is the authoritative "this beacon is an AirTag" filter.
    import json as _json
    airtag_ids: set[str] = set()
    for f in json_files:
        try:
            ident = _json.loads(f.read_text()).get("identifier")
            if ident:
                airtag_ids.add(ident.upper())
        except Exception:
            pass

    # `selected_ids` controls which OwnedBeacons/BeaconNamingRecord plists
    # land in the OpenTagViewer-format payload.  Three modes:
    #   * include_other_devices=true (no explicit list): every plist on disk
    #   * explicit_other_uuids non-empty: airtag_ids ∪ explicit_other_uuids
    #   * default: only airtag_ids
    if include_other_devices and not explicit_other_uuids:
        selected_ids: set[str] | None = None
    else:
        selected_ids = airtag_ids | explicit_other_uuids

    # Keep the existing variable name used below.
    files = json_files

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
