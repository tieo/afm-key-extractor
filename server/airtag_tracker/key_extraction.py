"""Extract AirTag decryption keys from the macOS VM.

Boots the VM if it isn't already running, SSHes in, unlocks the
keychain with the stored VM password, runs the decryptor to dump
``OwnedBeacons/*.plist`` into ``/tmp/airtag-export`` inside the VM,
copies the plists back, converts them to FindMy.py JSON, and (if we
started it) stops the VM. All of it over the already-forwarded port
2222 — no second QEMU.
"""

from __future__ import annotations

import subprocess as sp
import tempfile
import threading
import time
from importlib.resources import files
from pathlib import Path

from . import plist_conversion, vm, vm_password
from .config import DATA_DIR
from .events import emit

VM_USER = "airtag"
VM_HOST = "localhost"
VM_PORT = 2222

KEYS_DIR = DATA_DIR / "keys"


_lock = threading.Lock()
_running = False


def is_running() -> bool:
    with _lock:
        return _running


def start() -> dict:
    """Kick off an extraction in a background thread. No-op if one is
    already in progress."""
    global _running
    with _lock:
        if _running:
            return {"status": "already_running"}
        _running = True
    threading.Thread(target=_run, daemon=True, name="key-extraction").start()
    return {"status": "started"}


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


def _scp_to(local: Path, remote: str, timeout: int = 60) -> sp.CompletedProcess:
    pw = vm_password.get() or ""
    return sp.run(
        [
            "sshpass", "-p", pw,
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-P", str(VM_PORT),
            str(local), f"{VM_USER}@{VM_HOST}:{remote}",
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


def _wait_ssh(deadline_s: int = 180) -> None:
    emit("info", "extract", f"Waiting for VM SSH (up to {deadline_s}s)")
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        r = _ssh("echo ready", timeout=8)
        if r.returncode == 0 and "ready" in r.stdout:
            emit("info", "extract", "VM SSH is up")
            return
        time.sleep(3)
    raise RuntimeError("VM SSH never came up")


def _run() -> None:
    global _running
    we_started_vm = False
    try:
        if not vm.is_running():
            emit("info", "extract", "VM not running — booting it for extraction")
            vm.start()
            we_started_vm = True

        _wait_ssh()

        pw = vm_password.get() or ""
        if not pw:
            raise RuntimeError("VM password not available")

        # Upload the decryptor into the VM.
        decryptor = files("airtag_tracker.scripts").joinpath("airtag_decryptor.py")
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "airtag_decryptor.py"
            local.write_bytes(decryptor.read_bytes())
            r = _scp_to(local, "/tmp/airtag_decryptor.py")
            if r.returncode != 0:
                raise RuntimeError(f"scp decryptor failed: {r.stderr.strip()}")

        emit("info", "extract", "Running decryptor inside VM")
        # Unlock the keychain then dump plists. security + python3 are
        # both preinstalled on macOS.
        pw_escaped = pw.replace("'", "'\\''")
        cmd = (
            f"set -e; "
            f"security unlock-keychain -p '{pw_escaped}' "
            f"~/Library/Keychains/login.keychain-db; "
            f"rm -rf /tmp/airtag-export; "
            f"python3 /tmp/airtag_decryptor.py --rename-legacy "
            f"--path=/tmp/airtag-export"
        )
        r = _ssh(cmd, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(
                f"decryptor failed (rc={r.returncode}): "
                f"{(r.stderr or r.stdout).strip()[:500]}"
            )
        if r.stdout.strip():
            emit("info", "extract", r.stdout.strip()[:500])

        emit("info", "extract", "Copying plists back")
        with tempfile.TemporaryDirectory() as td:
            local = Path(td)
            r = _scp_from("/tmp/airtag-export/OwnedBeacons", local)
            if r.returncode != 0:
                raise RuntimeError(f"scp plists failed: {r.stderr.strip()}")
            plist_dir = local / "OwnedBeacons"
            if not plist_dir.exists() or not any(plist_dir.glob("*.plist")):
                emit("warning", "extract", "No plists came back — no AirTags paired?")
                return
            KEYS_DIR.mkdir(parents=True, exist_ok=True)
            naming_dir = local / "BeaconNamingRecord"
            _scp_from("/tmp/airtag-export/BeaconNamingRecord", local)  # best-effort
            count = plist_conversion.convert_dir(
                plist_dir, KEYS_DIR,
                naming_dir=naming_dir if naming_dir.exists() else None,
            )
            emit("info", "extract", f"Extracted {count} AirTag key(s) → {KEYS_DIR}")

    except Exception as e:
        emit("error", "extract", f"Key extraction failed: {e}")
    finally:
        if we_started_vm:
            try:
                emit("info", "extract", "Stopping VM (we booted it for extraction)")
                vm.stop()
            except Exception as e:
                emit("warning", "extract", f"VM stop failed: {e}")
        with _lock:
            _running = False
