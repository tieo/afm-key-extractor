"""Key extraction handlers for the runtime automation flow.

Covers three states:
- WAITING_ICLOUD_SYNC → poll until OwnedBeacons directory is populated
- EXTRACTING_KEYS     → tar records, fetch BeaconStore key via Terminal, decrypt, write JSONs
- SHUTTING_DOWN       → gracefully power off the VM

Extraction logic is ported directly from key_extraction._run().
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess as sp
import tarfile
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ... import plist_conversion, qmp, vm, vm_ui, vm_password
from ...config import (
    KEYS_DIR,
    PLISTS_DIR,
    VM_ICLOUD_SIGNED_IN_MARKER,
)
from ...events import emit
from ..context import AutomationContext
from ..states import RuntimeState

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VM_USER = "airtag"
VM_HOST = "localhost"
VM_PORT = 2222


# ---------------------------------------------------------------------------
# SSH / SCP helpers (mirrors key_extraction.py)
# ---------------------------------------------------------------------------

def _ssh(cmd: str, timeout: int = 60) -> sp.CompletedProcess:
    pw = vm_password.get() or ""
    return sp.run(
        [
            "sshpass", "-p", pw,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            "-p", str(VM_PORT),
            f"{VM_USER}@{VM_HOST}",
            cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def _scp_from(remote: str, local: Path, timeout: int = 60) -> sp.CompletedProcess:
    pw = vm_password.get() or ""
    return sp.run(
        [
            "sshpass", "-p", pw,
            "scp", "-r",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-P", str(VM_PORT),
            f"{VM_USER}@{VM_HOST}:{remote}", str(local),
        ],
        capture_output=True, text=True, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Extraction helpers (ported from key_extraction.py)
# ---------------------------------------------------------------------------

def _decrypt(record_path: Path, key: bytes) -> dict:
    with record_path.open("rb") as f:
        enc = plistlib.load(f)
    if isinstance(enc, list) and len(enc) >= 3:
        nonce, tag, ct = enc[0], enc[1], enc[2]
    else:
        nonce = enc.get("Nonce") or enc.get("nonce")
        tag = enc.get("Tag") or enc.get("tag")
        ct = enc.get("Ciphertext") or enc.get("ciphertext")
    pt = AESGCM(key).decrypt(nonce, ct + tag, None)
    return plistlib.loads(pt)


def _records(p: Path) -> list[Path]:
    """All *.record files under p, excluding macOS AppleDouble metadata (._*)."""
    return [r for r in p.rglob("*.record") if not r.name.startswith("._")]


def _extract_beacon_key_via_terminal(pw: str) -> str:
    """Open Terminal in the VM and retrieve the BeaconStore key via keychain.

    SSH-direct 'security' calls return errSecAuthFailed (-25308) because
    they have no UI session to present the ACL prompt — only Terminal works.
    Once granted in this GUI session, repeat Terminal calls succeed without
    re-prompting.

    Ported directly from key_extraction._extract_beacon_key_via_terminal().
    """
    cmd = (
        "clear; security find-generic-password -s BeaconStore "
        "-a BeaconStoreKey -w > /tmp/beacon-key.hex 2>/tmp/beacon-key.err; "
        "echo RC=$?"
    )

    # Spotlight → Terminal
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "spc"])
    time.sleep(1.5)
    qmp.type_text("Terminal", gap_s=0.10)
    time.sleep(0.6)
    qmp.send_keys(["ret"])
    time.sleep(6.0)  # cold launch

    qmp.type_text(cmd, gap_s=0.04)
    time.sleep(0.5)
    qmp.send_keys(["ret"])

    # Poll for SecurityAgent ACL dialog OR for the key file to be populated.
    # If keychain access is cached for this session, no dialog appears.
    dialog_seen = False
    for _ in range(24):
        time.sleep(0.5)
        try:
            dump_path = "/tmp/_keychain_dialog.ppm"
            qmp.screendump(dump_path)
            time.sleep(0.2)
            txt = vm_ui.screen_text(dump_path).lower()
        except Exception:
            txt = ""
        if "beaconstore" in txt and "always allow" in txt:
            dialog_seen = True
            emit("info", "extract", "Keychain ACL prompt visible — entering password")
            break
        # If no dialog and key file is already populated, we're done.
        check = _ssh("test -s /tmp/beacon-key.hex && echo READY", timeout=5)
        if "READY" in check.stdout:
            break

    if dialog_seen:
        qmp.type_text(pw, gap_s=0.06)
        time.sleep(0.4)
        qmp.send_keys(["ret"])  # Default button is Allow
        time.sleep(3.0)

    # Quit Terminal so windows don't pile up across runs.
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "q"])
    time.sleep(0.5)

    r = _ssh("cat /tmp/beacon-key.hex 2>/dev/null", timeout=10)
    key_hex = r.stdout.strip()
    if not key_hex:
        err = _ssh("cat /tmp/beacon-key.err 2>/dev/null", timeout=5).stdout.strip()
        raise RuntimeError(
            f"Beacon key empty after Terminal extraction: {err or '(no error output)'}"
        )
    return key_hex


# ---------------------------------------------------------------------------
# State handlers
# ---------------------------------------------------------------------------

def wait_icloud_sync(ctx: AutomationContext) -> RuntimeState:
    """Poll until iCloud has synced at least one OwnedBeacons record.

    Uses SSH to count entries in the OwnedBeacons directory.  Polls every
    30 s.  Emits a progress event every 5 minutes so the UI stays alive.

    Deadline comes from ``ctx.icloud_sync_timeout_s`` (default 1800 s /
    30 min).  Raises RuntimeError on timeout.
    """
    deadline_s = ctx.icloud_sync_timeout_s
    poll_s = 30
    progress_interval_s = 300
    t0 = time.time()
    last_progress = t0

    emit("info", "extract",
         f"Waiting for iCloud OwnedBeacons sync (timeout {deadline_s}s)")

    while time.time() - t0 < deadline_s:
        r = _ssh(
            "ls ~/Library/com.apple.icloud.searchpartyd/OwnedBeacons/ "
            "2>/dev/null | wc -l",
            timeout=15,
        )
        try:
            count = int(r.stdout.strip() or "0")
        except ValueError:
            count = 0

        if count > 0:
            emit("info", "extract",
                 f"iCloud sync complete — {count} OwnedBeacons record(s) found")
            return RuntimeState.EXTRACTING_KEYS

        now = time.time()
        if now - last_progress >= progress_interval_s:
            elapsed = int(now - t0)
            emit("info", "extract",
                 f"Still waiting for iCloud sync ({elapsed}s elapsed, "
                 f"timeout {deadline_s}s)")
            last_progress = now

        time.sleep(poll_s)

    raise RuntimeError(
        f"iCloud OwnedBeacons never appeared after {deadline_s}s. "
        "Check that Find My is enabled and the Apple ID is signed in."
    )


def run(ctx: AutomationContext) -> RuntimeState:
    """Extract AirTag decryption keys from the VM.

    Steps (ported from key_extraction._run()):
    1. SSH: tar OwnedBeacons (and BeaconNamingRecord if present).
    2. GUI Terminal trick: fetch BeaconStore key from keychain.
    3. SCP both artefacts to a local tempdir.
    4. Decrypt each .record file with AESGCM.
    5. Write decrypted plists to PLISTS_DIR (persists across runs).
    6. Run plist_conversion.convert_dir → write JSON keys to KEYS_DIR.

    Raises RuntimeError on any unrecoverable failure.
    """
    emit("info", "extract", "Starting AirTag key extraction")

    pw = vm_password.get() or ""
    if not pw:
        raise RuntimeError("VM password not available — cannot extract keychain key")

    # Step 1: tar OwnedBeacons (and BeaconNamingRecord) inside the VM.
    emit("info", "extract", "Archiving AirTag beacon records")
    tar_cmd = (
        "SPD=~/Library/com.apple.icloud.searchpartyd; "
        "if [ ! -d \"$SPD/OwnedBeacons\" ] || "
        "[ -z \"$(ls -A $SPD/OwnedBeacons 2>/dev/null)\" ]; then "
        "  echo EMPTY; exit 0; "
        "fi; "
        "cd \"$SPD\" && "
        "tar czf /tmp/airtag-records.tar.gz "
        "OwnedBeacons $(test -d BeaconNamingRecord && echo BeaconNamingRecord) && "
        "echo OK"
    )
    r = _ssh(tar_cmd, timeout=60)
    if r.returncode != 0 or ("OK" not in r.stdout and "EMPTY" not in r.stdout):
        raise RuntimeError(
            f"VM tar failed (rc={r.returncode}): "
            f"{(r.stdout + r.stderr).strip()[:500]}"
        )
    if "EMPTY" in r.stdout:
        emit("info", "extract", "No AirTags paired in VM yet — nothing to extract")
        return RuntimeState.SHUTTING_DOWN

    # Step 2: fetch the BeaconStore encryption key via Terminal GUI trick.
    emit("info", "extract", "Fetching BeaconStore key via GUI Terminal")
    key_hex = _extract_beacon_key_via_terminal(pw)
    # Persist hex key in VM so the scp path below is uniform.
    _ssh(f"printf '%s' {key_hex} > /tmp/beacon-key.hex", timeout=10)

    # Step 3: scp artefacts to host tempdir.
    emit("info", "extract", "Copying records and key to server")
    with tempfile.TemporaryDirectory() as td:
        local = Path(td)

        r = _scp_from("/tmp/beacon-key.hex", local / "key.hex")
        if r.returncode != 0:
            raise RuntimeError(f"scp key failed: {r.stderr.strip()}")

        r = _scp_from("/tmp/airtag-records.tar.gz", local / "records.tar.gz")
        if r.returncode != 0:
            raise RuntimeError(f"scp records failed: {r.stderr.strip()}")

        # Step 4: decrypt records.
        key = bytes.fromhex((local / "key.hex").read_text().strip())
        with tarfile.open(local / "records.tar.gz") as tf:
            tf.extractall(local)

        owned = local / "OwnedBeacons"
        if not owned.exists() or not _records(owned):
            emit("warning", "extract", "No beacon records found — no AirTags paired?")
            return RuntimeState.SHUTTING_DOWN

        # Step 5: persist decrypted plists to PLISTS_DIR.
        plist_dir = PLISTS_DIR / "OwnedBeacons"
        naming_dir = PLISTS_DIR / "BeaconNamingRecord"
        for d in (plist_dir, naming_dir):
            if d.exists():
                shutil.rmtree(d)
        plist_dir.mkdir(parents=True, exist_ok=True)

        for rec in _records(owned):
            try:
                (plist_dir / f"{rec.stem}.plist").write_bytes(
                    plistlib.dumps(_decrypt(rec, key))
                )
            except Exception as e:
                emit("warning", "extract", f"decrypt {rec.stem}: {e}")

        named = local / "BeaconNamingRecord"
        if named.exists():
            naming_dir.mkdir(parents=True, exist_ok=True)
            for rec in _records(named):
                try:
                    pl = _decrypt(rec, key)
                except Exception:
                    continue
                bid = (pl.get("associatedBeacon") or rec.parent.name).upper()
                rid = (pl.get("identifier") or rec.stem).upper()
                (naming_dir / bid).mkdir(parents=True, exist_ok=True)
                (naming_dir / bid / f"{rid}.plist").write_bytes(plistlib.dumps(pl))

        # Step 6: convert plists → FindMy.py JSON keys.
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        count = plist_conversion.convert_dir(
            plist_dir,
            KEYS_DIR,
            naming_dir=naming_dir if naming_dir.exists() else None,
        )
        emit("info", "extract", f"Extracted {count} AirTag key(s) → {KEYS_DIR}")

    return RuntimeState.SHUTTING_DOWN


def shutdown(ctx: AutomationContext) -> RuntimeState:
    """Gracefully shut down the VM and wait for it to stop.

    Attempts a clean shutdown via SSH first ('sudo shutdown -h now'),
    then sends QMP system_powerdown as a fallback.  Polls vm.is_running()
    every 2 s for up to 60 s.

    Always writes VM_ICLOUD_SIGNED_IN_MARKER so the next run knows the
    image is already signed into iCloud (skips credential entry).
    """
    emit("info", "extract", "Shutting down VM")

    # Best-effort SSH shutdown first (allows macOS to flush disk caches).
    try:
        vm_ui.ssh("sudo shutdown -h now", timeout=10)
    except Exception as e:
        emit("warning", "extract", f"SSH shutdown failed (will try QMP): {e}")

    # QMP powerdown as belt-and-suspenders.
    try:
        qmp.system_powerdown()
    except Exception as e:
        emit("warning", "extract", f"QMP system_powerdown failed: {e}")

    # Wait for VM to stop.
    deadline_s = 60
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if not vm.is_running():
            emit("info", "extract", "VM stopped")
            break
        time.sleep(2)
    else:
        emit("warning", "extract", "VM did not stop within 60 s — forcing stop")
        try:
            vm.stop()
        except Exception as e:
            emit("warning", "extract", f"vm.stop() failed: {e}")

    # Write signed-in marker so next run can skip credentials.
    try:
        VM_ICLOUD_SIGNED_IN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        VM_ICLOUD_SIGNED_IN_MARKER.write_text("1")
    except Exception:
        pass

    return RuntimeState.DONE
