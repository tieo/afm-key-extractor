"""Stable, realistic Apple device identity for the macOS VM.

OpenCore's default sample PlatformInfo is all zeros (SystemUUID=0000...,
SystemSerialNumber=W00000000001). Apple's CloudKit/FMIP treats that
identity as fraudulent and refuses to enable iCloud Keychain sync and
Find-My Location Services — the exact services searchpartyd needs to
publish AirTag beacon keys. Root fix: inject a stable, plausible
iMac19,1 identity into OpenCore so macOS reports consistent non-zero
IOPlatformUUID/IOPlatformSerialNumber on every boot.

The identity is generated once and persisted to VM_DIR/vm-identity.json
so it survives redeploys, reprovisions, and golden-image resets.
"""

from __future__ import annotations

import base64
import json
import random
import secrets
import string
import uuid
from pathlib import Path

# iMac19,1 - matches OpenCore.qcow2's baked-in SystemProductName.
# Apple's Recovery "Reinstall macOS" offering is NOT actually steered by
# SystemProductName any more (tested 2026-05-29: spoofing iMac18,3 still
# got Sequoia offered), so there's no upside to spoofing an older model
# and a real downside if the golden image was already registered with
# iCloud under iMac19,1 (HardwareUUID mismatch breaks iCloud trust).
MODEL = "iMac19,1"
MODEL_SUFFIX = "J1GN"   # 4-char model code that ends iMac19,1 serials
# Real Apple plant codes; C02 = Shanghai (most common for iMac), F5K,
# DGK, DMQ, FVH are also observed in the wild.
PLANT_CODES = ["C02", "F5K", "DGK", "DMQ", "FVH"]
# Valid Apple year/week encoding characters — years 2020+ use pairs
# like "X1" (2020 wk1) through "Y9" (2023). Any two-char combo from
# this alphabet parses cleanly.
YEAR_WEEK_ALPHABET = "CDFGHJKLMNPQRTVWXY0123456789"
# Apple serial body alphabet (excludes easily-confused chars).
BODY_ALPHABET = "CDFGHJKLMNPQRTVWXY0123456789"

# QEMU virtio/vmxnet3 MAC used by vm.py — ROM convention is to use the
# device's primary NIC MAC so macOS's HardwareUUID hash stays consistent.
DEFAULT_MAC = "52:54:00:c9:18:27"


def _rand(alphabet: str, n: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _generate_serial() -> str:
    plant = secrets.choice(PLANT_CODES)
    year_week = _rand(YEAR_WEEK_ALPHABET, 2)
    body = _rand(BODY_ALPHABET, 3)
    return f"{plant}{year_week}{body}{MODEL_SUFFIX}"  # 12 chars


def _generate_mlb() -> str:
    # 17-char board serial. Apple MLB structure is opaque to
    # non-Apple observers; a 17-char string over the serial alphabet
    # is what macserial emits and what CloudKit accepts.
    return _rand(BODY_ALPHABET + string.ascii_uppercase, 17)


def _mac_to_rom(mac: str) -> str:
    """MAC '52:54:00:c9:18:27' → base64 of 6 raw bytes (OpenCore ROM format)."""
    bs = bytes(int(x, 16) for x in mac.split(":"))
    return base64.b64encode(bs).decode()


def generate() -> dict:
    """Fresh identity. Random every call — callers should persist and reuse."""
    return {
        "SystemProductName": MODEL,
        "SystemSerialNumber": _generate_serial(),
        "MLB": _generate_mlb(),
        "SystemUUID": str(uuid.uuid4()).upper(),
        "ROM_b64": _mac_to_rom(DEFAULT_MAC),
        "ROM_mac": DEFAULT_MAC,
    }


def load_or_create(path: Path) -> dict:
    """Read identity from disk, generating and persisting it if absent."""
    if path.exists():
        try:
            data = json.loads(path.read_text())
            # Reject stub/zero identities that slipped through.
            if data.get("SystemUUID", "").replace("-", "").strip("0") and \
               data.get("SystemSerialNumber", "").strip("W0"):
                return data
        except Exception:
            pass
    data = generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)
    return data


def patch_config_plist(plist_path: Path, identity: dict) -> None:
    """In-place edit of OpenCore's config.plist PlatformInfo.Generic.

    Uses targeted string replacement rather than a full plist parser:
    the keys are unique and the sample values are distinctive, so a
    before/after substring match is reliable without a plistlib
    round-trip (which would reformat the whole file).
    """
    text = plist_path.read_text()

    # Each replacement is an exact substring match against the known
    # stub value; raise if the stub is missing so we don't silently
    # produce a half-patched config.
    replacements = [
        ("<string>M0000000000000001</string>",
         f"<string>{identity['MLB']}</string>"),
        ("<data>ESIzRFVm</data>",
         f"<data>{identity['ROM_b64']}</data>"),
        ("<string>W00000000001</string>",
         f"<string>{identity['SystemSerialNumber']}</string>"),
        # SystemProductName drives Apple Recovery's "Reinstall macOS" choice:
        # Apple maps the model to its newest supported macOS and serves that.
        # Bundled template has iMac19,1 (Sequoia-eligible) which made Recovery
        # ignore our fetch-MacOS.py shortname. Replace it with whatever MODEL
        # vm_identity is currently set to (default iMac18,3 - caps at Sonoma).
        ("<string>iMac19,1</string>",
         f"<string>{identity['SystemProductName']}</string>"),
    ]
    for old, new in replacements:
        if old not in text:
            raise RuntimeError(
                f"OpenCore config.plist missing expected stub value {old!r} — "
                "already patched? Refusing to overwrite."
            )
        text = text.replace(old, new, 1)

    # SystemUUID must be targeted by key context — the zero UUID also appears
    # in boot device paths, so a plain replace hits the wrong one.
    uuid_stub = "<key>SystemUUID</key>\n\t\t\t<string>00000000-0000-0000-0000-000000000000</string>"
    uuid_new  = f"<key>SystemUUID</key>\n\t\t\t<string>{identity['SystemUUID']}</string>"
    if uuid_stub not in text:
        raise RuntimeError(
            "config.plist missing <key>SystemUUID</key> stub — already patched?"
        )
    text = text.replace(uuid_stub, uuid_new, 1)

    plist_path.write_text(text)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("usage: vm_identity.py <identity.json> <config.plist>", file=sys.stderr)
        sys.exit(2)
    ident_path = Path(sys.argv[1])
    plist_path = Path(sys.argv[2])
    ident = load_or_create(ident_path)
    print(f"Identity: serial={ident['SystemSerialNumber']} uuid={ident['SystemUUID']}")
    patch_config_plist(plist_path, ident)
    print(f"Patched {plist_path}")
