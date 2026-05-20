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

from ... import plist_conversion, qmp, vm, vm_ssh
from ...config import (
    KEYS_DIR,
    PLISTS_DIR,
    VM_ICLOUD_SIGNED_IN_MARKER,
)
from ...events import emit
from ..context import AutomationContext
from ..states import RuntimeState


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
        r = vm_ssh.run(
            "ls ~/Library/com.apple.icloud.searchpartyd/OwnedBeacons/ "
            "2>/dev/null | wc -l",
            password=ctx.vm_password,
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
    emit("info", "extract", f"Starting AirTag key extraction ({ctx.adapter.display_name})")

    pw = ctx.vm_password
    if not pw:
        raise RuntimeError("VM password not available — cannot extract keychain key")

    def ssh(cmd: str, timeout: int = 60) -> sp.CompletedProcess:
        return vm_ssh.run(cmd, password=pw, timeout=timeout)

    def scp_from(remote: str, local: Path, timeout: int = 60) -> sp.CompletedProcess:
        return vm_ssh.scp_from(remote, local, password=pw, timeout=timeout)

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
    r = ssh(tar_cmd, timeout=60)
    if r.returncode != 0 or ("OK" not in r.stdout and "EMPTY" not in r.stdout):
        raise RuntimeError(
            f"VM tar failed (rc={r.returncode}): "
            f"{(r.stdout + r.stderr).strip()[:500]}"
        )
    if "EMPTY" in r.stdout:
        emit("info", "extract", "No AirTags paired in VM yet — nothing to extract")
        return RuntimeState.SHUTTING_DOWN

    # Step 2: fetch the BeaconStore encryption key via the adapter's method.
    emit("info", "extract", f"Fetching BeaconStore key ({ctx.adapter.display_name})")
    key_hex = ctx.adapter.extract_beacon_key(vm_password=pw)
    # Persist hex key in VM so the scp path below is uniform.
    ssh(f"printf '%s' {key_hex} > /tmp/beacon-key.hex", timeout=10)

    # Step 3: scp artefacts to host tempdir.
    emit("info", "extract", "Copying records and key to server")
    with tempfile.TemporaryDirectory() as td:
        local = Path(td)

        r = scp_from("/tmp/beacon-key.hex", local / "key.hex")
        if r.returncode != 0:
            raise RuntimeError(f"scp key failed: {r.stderr.strip()}")

        r = scp_from("/tmp/airtag-records.tar.gz", local / "records.tar.gz")
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
    # sudo -S reads password from stdin; pipe via base64 to avoid quoting.
    try:
        import base64 as _b64
        _pw = ctx.vm_password
        _script = f"echo {_pw!r} | sudo -S shutdown -h now\n"
        _b64_cmd = _b64.b64encode(_script.encode()).decode()
        vm_ssh.run(f"echo {_b64_cmd} | base64 -d | bash", password=_pw, timeout=15)
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
