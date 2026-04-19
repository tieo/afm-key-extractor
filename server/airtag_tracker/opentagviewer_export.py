"""Build an OpenTagViewer-compatible import zip from persisted plists.

Zip layout expected by the app's AppleZipImporterUtil:

    OPENTAGVIEWER.yml
    OwnedBeacons/<BEACON-UUID>.plist
    BeaconNamingRecord/<BEACON-UUID>/<RECORD-UUID>.plist

The importer inner-joins OwnedBeacons with BeaconNamingRecord, so tags
without a naming record are skipped.
"""

from __future__ import annotations

import io
import plistlib
import re
import time
import zipfile
from dataclasses import dataclass

from .config import PLISTS_DIR

UUID_RE = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}$"
)


@dataclass
class ExportResult:
    data: bytes
    tag_count: int


class NoPlistsError(RuntimeError):
    """No decrypted plists on disk — extraction hasn't been run yet."""


def build_zip() -> ExportResult:
    owned_dir = PLISTS_DIR / "OwnedBeacons"
    naming_dir = PLISTS_DIR / "BeaconNamingRecord"
    if not owned_dir.exists() or not any(owned_dir.glob("*.plist")):
        raise NoPlistsError(
            "No decrypted AirTag plists on disk. Run 'Sync from VM' first."
        )

    beacons: dict[str, dict] = {}
    for plist_file in owned_dir.glob("*.plist"):
        try:
            pl = plistlib.loads(plist_file.read_bytes())
        except Exception:
            continue
        ident = (pl.get("identifier") or plist_file.stem).upper()
        if not UUID_RE.match(ident):
            continue
        # OpenTagViewer only understands items with both a primary and
        # secondary shared secret (true AirTags). iPhones/AirPods/etc have
        # just a primary and won't work — skip them silently.
        if not pl.get("secondarySharedSecret", {}).get("key", {}).get("data"):
            continue
        beacons[ident] = pl

    namings: dict[str, list[tuple[str, dict]]] = {}
    if naming_dir.exists():
        for plist_file in naming_dir.rglob("*.plist"):
            try:
                pl = plistlib.loads(plist_file.read_bytes())
            except Exception:
                continue
            ab = (pl.get("associatedBeacon") or plist_file.parent.name).upper()
            rid = (pl.get("identifier") or plist_file.stem).upper()
            if UUID_RE.match(ab) and UUID_RE.match(rid):
                namings.setdefault(ab, []).append((rid, pl))

    airtags = [bid for bid in beacons if bid in namings]

    manifest = (
        'version: "0.0.1"\n'
        'via: "airtag-tracker:web"\n'
        'sourceUser: "airtag-tracker"\n'
        f'exportTimestamp: {int(time.time() * 1000)}\n'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("OPENTAGVIEWER.yml", manifest)
        for bid in airtags:
            z.writestr(
                f"OwnedBeacons/{bid}.plist",
                plistlib.dumps(beacons[bid], fmt=plistlib.FMT_XML),
            )
            for rid, pl in namings[bid]:
                z.writestr(
                    f"BeaconNamingRecord/{bid}/{rid}.plist",
                    plistlib.dumps(pl, fmt=plistlib.FMT_XML),
                )

    return ExportResult(data=buf.getvalue(), tag_count=len(airtags))
