#!/usr/bin/env python3
"""Convert decrypted AirTag plists to FindMy.py JSON format."""

import json
import plistlib
import sys
from datetime import datetime, timezone
from pathlib import Path


def plist_to_findmy_json(plist_path, naming_dir=None):
    """Convert a decrypted OwnedBeacons plist to FindMy.py accessory JSON."""
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)

    # Extract the private key (P-224 elliptic curve, 28 bytes)
    private_key = data.get("privateKey", {}).get("key", {}).get("data", b"")
    shared_secret = data.get("sharedSecret", {}).get("key", {}).get("data", b"")
    secondary_shared = data.get("secondarySharedSecret", {}).get("key", {}).get("data", b"")
    pairing_date = data.get("pairingDate", datetime.now(timezone.utc))
    # macOS plists store pairingDate as timezone-naive UTC; findmy warns otherwise.
    if isinstance(pairing_date, datetime) and pairing_date.tzinfo is None:
        pairing_date = pairing_date.replace(tzinfo=timezone.utc)
    identifier = data.get("identifier", plist_path.stem)
    model = data.get("model", "AirTag")

    if not private_key:
        raise ValueError(f"No privateKey found in {plist_path}")
    if not secondary_shared:
        # Non-AirTag Find My items (iPhones, iPads, AirPods, Macs) only have a
        # primary shared secret. findmy's FindMyAccessory requires both sks+skn.
        raise ValueError(f"No secondarySharedSecret (likely not an AirTag) in {plist_path}")

    # Look up name from BeaconNamingRecord
    name = identifier
    if naming_dir:
        for naming_file in Path(naming_dir).rglob("*.plist"):
            try:
                with open(naming_file, "rb") as f:
                    naming = plistlib.load(f)
                if naming.get("associatedBeacon") == identifier:
                    name = naming.get("name", identifier)
                    break
            except Exception:
                continue

    # findmy expects the last 28 bytes of the P-224 private key
    # (matches how FindMyAccessory.from_device_dump slices device_data["privateKey"]["key"]["data"][-28:])
    return {
        "type": "accessory",
        "master_key": private_key[-28:].hex(),
        "skn": shared_secret.hex(),
        "sks": secondary_shared.hex(),
        "paired_at": pairing_date.isoformat() if isinstance(pairing_date, datetime) else str(pairing_date),
        "name": name,
        "model": model,
        "identifier": identifier,
        "alignment_date": None,
        "alignment_index": 0,
    }


def convert_dir(plist_dir: Path, output_dir: Path, naming_dir: Path | None = None) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for plist_file in plist_dir.glob("*.plist"):
        try:
            data = plist_to_findmy_json(plist_file, naming_dir)
            safe_name = data["name"].replace(" ", "_").replace("/", "_")
            out_path = output_dir / f"{safe_name}.json"
            with open(out_path, "w") as f:
                json.dump(data, f, indent=2)
            count += 1
        except Exception:
            continue
    return count


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <plist_dir> <output_dir>")
        sys.exit(1)
    plist_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    naming_dir = plist_dir.parent / "BeaconNamingRecord"
    count = convert_dir(
        plist_dir, output_dir,
        naming_dir=naming_dir if naming_dir.exists() else None,
    )
    print(f"Converted {count} AirTag(s) to {output_dir}")
