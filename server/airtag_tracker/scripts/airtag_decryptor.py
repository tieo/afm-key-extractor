#!/usr/bin/env python3
"""Decrypt AirTag beacon records from macOS keychain and searchpartyd cache.

Based on OpenTagViewer's airtag_decryptor.py and airy10's Swift implementation.
Runs inside the macOS VM over SSH.
"""

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

try:
    from Crypto.Cipher import AES
except ImportError:
    print("Installing pycryptodome...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pycryptodome", "-q"])
    from Crypto.Cipher import AES


def get_beaconstore_key():
    """Extract BeaconStore AES key from macOS keychain."""
    # Try the -w flag first (works on macOS <= 14)
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-l", "BeaconStore", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            key_hex = result.stdout.strip()
            return bytes.fromhex(key_hex)
    except Exception:
        pass

    # Fallback: parse the full output for gena blob
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-l", "BeaconStore"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if '"gena"<blob>=0x' in line:
                hex_str = line.split("0x")[1].split('"')[0].strip()
                return bytes.fromhex(hex_str)
    except Exception:
        pass

    raise RuntimeError("Could not extract BeaconStore key from keychain")


def decrypt_record(record_path, key):
    """Decrypt a .record file using AES-GCM."""
    with open(record_path, "rb") as f:
        encrypted = plistlib.load(f)

    if isinstance(encrypted, list) and len(encrypted) >= 3:
        nonce, tag, ciphertext = encrypted[0], encrypted[1], encrypted[2]
    elif isinstance(encrypted, dict):
        nonce = encrypted.get("Nonce") or encrypted.get("nonce")
        tag = encrypted.get("Tag") or encrypted.get("tag")
        ciphertext = encrypted.get("Ciphertext") or encrypted.get("ciphertext")
    else:
        raise ValueError(f"Unknown .record format in {record_path}")

    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    decrypted = cipher.decrypt_and_verify(ciphertext, tag)
    return plistlib.loads(decrypted)


def main():
    parser = argparse.ArgumentParser(description="Decrypt AirTag beacon records")
    parser.add_argument("--path", default="/tmp/airtag-export", help="Output directory")
    parser.add_argument("--rename-legacy", action="store_true",
                        help="Handle legacy MasterBeacons directory name")
    parser.add_argument("--key", help="BeaconStore key in hex (skip keychain lookup)")
    args = parser.parse_args()

    output = Path(args.path)
    output.mkdir(parents=True, exist_ok=True)
    (output / "OwnedBeacons").mkdir(exist_ok=True)
    (output / "BeaconNamingRecord").mkdir(exist_ok=True)

    # Get decryption key
    if args.key:
        key = bytes.fromhex(args.key)
    else:
        key = get_beaconstore_key()
    print(f"Got BeaconStore key ({len(key)} bytes)")

    # Find beacon records
    searchpartyd = Path.home() / "Library" / "com.apple.icloud.searchpartyd"

    # Handle legacy directory name
    beacons_dir = searchpartyd / "OwnedBeacons"
    if not beacons_dir.exists() and args.rename_legacy:
        legacy = searchpartyd / "MasterBeacons"
        if legacy.exists():
            beacons_dir = legacy

    naming_dir = searchpartyd / "BeaconNamingRecord"

    if not beacons_dir.exists():
        print(f"No beacon records found at {beacons_dir}")
        sys.exit(1)

    # Decrypt OwnedBeacons
    count = 0
    for record in beacons_dir.glob("*.record"):
        try:
            decrypted = decrypt_record(record, key)
            out_file = output / "OwnedBeacons" / f"{record.stem}.plist"
            with open(out_file, "wb") as f:
                plistlib.dump(decrypted, f)
            print(f"  Decrypted: {record.stem}")
            count += 1
        except Exception as e:
            print(f"  Failed: {record.stem}: {e}")

    # Decrypt BeaconNamingRecord
    if naming_dir.exists():
        for record in naming_dir.glob("*.record"):
            try:
                decrypted = decrypt_record(record, key)
                out_file = output / "BeaconNamingRecord" / f"{record.stem}.plist"
                with open(out_file, "wb") as f:
                    plistlib.dump(decrypted, f)
            except Exception as e:
                print(f"  Failed naming record: {record.stem}: {e}")

    print(f"\nDecrypted {count} AirTag(s) to {output}")


if __name__ == "__main__":
    main()
