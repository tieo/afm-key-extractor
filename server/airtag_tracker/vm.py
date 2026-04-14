"""macOS QEMU VM lifecycle.

start() / stop() / start_manual() / bake_golden() plus the OpenCore
auto-boot keystroke dance — all of it in one place.
"""

from __future__ import annotations

import os
import shutil
import subprocess as sp
import threading
import time
from pathlib import Path

from . import login_autotyper, qmp, systemd, vm_password
from .config import (
    DATA_DIR,
    MONITOR_SOCK,
    QMP_SOCK,
    VM_DIR,
    VM_ENABLED,
    VM_PASSWORD_PATH,
    VM_PID_FILE,
    VNC_WS_PORT,
)
from .events import emit

MAC_HDD = VM_DIR / "mac_hdd_ng.img"
GOLDEN_HDD = VM_DIR / "mac_hdd_golden.img"
OVMF_CODE = VM_DIR / "OVMF_CODE_4M.fd"
OVMF_VARS = VM_DIR / "OVMF_VARS-1920x1080.fd"
OPENCORE_QCOW = VM_DIR / "OpenCore" / "OpenCore.qcow2"

_CPU = (
    "Skylake-Client,-hle,-rtm,kvm=on,vendor=GenuineIntel,+invtsc,"
    "vmware-cpuid-freq=on,+ssse3,+sse4.2,+popcnt,+avx,+aes,+xsave,+xsaveopt,check"
)
_OSK = "ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc"


class VmError(Exception):
    pass


def _qemu_args() -> list[str]:
    return [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-m", "8192",
        "-cpu", _CPU,
        "-machine", "q35",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd,bus=xhci.0",
        "-device", "usb-tablet,bus=xhci.0",
        "-smp", "4,cores=2",
        "-global", "ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off",
        "-device", f"isa-applesmc,osk={_OSK}",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        "-drive", f"if=pflash,format=raw,file={OVMF_VARS}",
        "-smbios", "type=2",
        "-device", "ich9-ahci,id=sata",
        "-drive", f"id=OpenCoreBoot,if=none,snapshot=on,format=qcow2,file={OPENCORE_QCOW}",
        "-device", "ide-hd,bus=sata.2,drive=OpenCoreBoot",
        "-drive", f"id=MacHDD,if=none,file={MAC_HDD},format=qcow2",
        "-device", "ide-hd,bus=sata.4,drive=MacHDD",
        "-netdev", "user,id=net0,hostfwd=tcp::2222-:22",
        "-device", "vmxnet3,netdev=net0,id=net0,mac=52:54:00:c9:18:27",
        "-device", "vmware-svga",
        "-vnc", "127.0.0.1:1",
        "-monitor", f"unix:{MONITOR_SOCK},server,nowait",
        "-qmp", f"unix:{QMP_SOCK},server,nowait",
        "-daemonize",
        "-pidfile", str(VM_PID_FILE),
    ]


def is_running() -> bool:
    if not VM_PID_FILE.exists():
        return False
    try:
        pid = int(VM_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError):
        VM_PID_FILE.unlink(missing_ok=True)
        return False


def status() -> dict:
    if not VM_ENABLED:
        return {"enabled": False}
    return {
        "enabled": True,
        "provisioned": MAC_HDD.exists(),
        "setup_complete": VM_PASSWORD_PATH.exists(),
        "vm_running": is_running(),
        "vnc_ws_port": VNC_WS_PORT,
    }


def _launch_qemu() -> None:
    result = sp.run(_qemu_args(), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        emit("error", "vm", f"QEMU failed to start: {result.stderr}")
        raise VmError(f"Failed to start VM: {result.stderr}")
    systemd.ctl("start", "airtag-novnc")
    emit("info", "vm", f"VM started, noVNC proxy active on port {VNC_WS_PORT}")


def _restore_golden_if_available() -> bool:
    if not GOLDEN_HDD.exists():
        return False
    emit("info", "vm", f"Golden image found — restoring {GOLDEN_HDD.name} → {MAC_HDD.name}")
    shutil.copy2(GOLDEN_HDD, MAC_HDD)
    return True


def _auto_boot_opencore() -> None:
    """Defeat the OpenCore picker.

    NVRAM is wiped every boot (snapshot=on on the OpenCore disk), so the
    picker always appears. The installed macOS entry is the second one
    (right of the default 'EFI' entry, which just re-enters OpenCore).
    Retry right+Enter over ~18s to catch the picker whenever it appears.
    """
    def worker() -> None:
        last = 0
        for delay in (3, 6, 10, 15):
            time.sleep(max(0, delay - last))
            last = delay
            try:
                qmp.send_keys(["right", "ret"])
                emit("info", "vm", f"Sent right+Enter to OpenCore picker (@{delay}s)")
            except Exception as e:
                emit("warning", "vm", f"QMP send-key @{delay}s failed: {e}")
    threading.Thread(target=worker, daemon=True).start()


def start() -> dict:
    """Boot the existing VM disk with OpenCore auto-pick.

    Never wipes state — ``mac_hdd_ng.img`` is booted as-is so session
    state (logins, preferences, extracted keys) survives across reboots.
    If the main disk is missing but a golden image exists, seed from
    golden as a one-time bootstrap. To deliberately reset to golden,
    call ``reset_to_golden()``.
    """
    if not VM_ENABLED:
        raise VmError("VM not enabled")
    if is_running():
        return {"status": "already_running", "vnc_ws_port": VNC_WS_PORT}

    seeded = False
    if not MAC_HDD.exists():
        if not _restore_golden_if_available():
            raise VmError("VM not provisioned yet. Waiting for auto-provision.")
        seeded = True
    emit("info", "vm", f"Starting VM (seeded from golden: {seeded})")

    try:
        _launch_qemu()
    except VmError:
        raise
    except Exception as e:
        emit("error", "vm", f"VM start error: {e}")
        raise VmError(str(e))

    _auto_boot_opencore()
    # Only attempt login-typing if we have a stored password (i.e. the
    # VM has been through Setup Assistant at least once). Fresh installs
    # hit the wizard path instead.
    if vm_password.get():
        login_autotyper.start()
    return {"status": "started", "vnc_ws_port": VNC_WS_PORT}


def start_manual() -> dict:
    """Boot with no automation — operator drives the Setup Assistant via VNC."""
    if not VM_ENABLED:
        raise VmError("VM not enabled")
    if not MAC_HDD.exists():
        raise VmError("VM not provisioned yet")
    if is_running():
        return {"status": "already_running", "vnc_ws_port": VNC_WS_PORT}
    emit("info", "vm", "Starting VM in MANUAL mode (no automation)")
    _launch_qemu()
    return {"status": "started", "vnc_ws_port": VNC_WS_PORT, "mode": "manual"}


def stop() -> dict:
    emit("info", "vm", "Stopping VM")
    if VM_PID_FILE.exists():
        try:
            pid = int(VM_PID_FILE.read_text().strip())
            os.kill(pid, 15)
            emit("info", "vm", f"Sent SIGTERM to QEMU (PID {pid})")
        except (ValueError, ProcessLookupError):
            emit("info", "vm", "VM process already gone")
        VM_PID_FILE.unlink(missing_ok=True)
    systemd.ctl("stop", "airtag-novnc")
    return {"status": "stopped"}


def reset_to_golden() -> dict:
    """Overwrite ``mac_hdd_ng.img`` with the golden snapshot (destructive)."""
    if not VM_ENABLED:
        raise VmError("VM not enabled")
    if is_running():
        raise VmError("VM still running — stop it first")
    if not GOLDEN_HDD.exists():
        raise VmError("No golden image to restore from — bake one first")
    emit("info", "vm", f"Resetting {MAC_HDD.name} from golden snapshot")
    shutil.copy2(GOLDEN_HDD, MAC_HDD)
    return {"status": "reset", "path": str(MAC_HDD)}


def bake_golden() -> dict:
    """Snapshot mac_hdd_ng.img → mac_hdd_golden.img (VM must be stopped)."""
    if not VM_ENABLED:
        raise VmError("VM not enabled")
    if is_running():
        raise VmError("VM still running — stop it first")
    if not MAC_HDD.exists():
        raise VmError("mac_hdd_ng.img not found")

    if GOLDEN_HDD.exists():
        backup = GOLDEN_HDD.with_suffix(GOLDEN_HDD.suffix + ".bak")
        emit("info", "vm", f"Existing golden image backed up to {backup.name}")
        shutil.move(str(GOLDEN_HDD), str(backup))

    emit("info", "vm", f"Baking golden image: {MAC_HDD.name} → {GOLDEN_HDD.name}")
    shutil.copy2(MAC_HDD, GOLDEN_HDD)
    size_gb = GOLDEN_HDD.stat().st_size / (1024 ** 3)
    emit("info", "vm", f"Golden image baked ({size_gb:.1f} GB)")
    return {"status": "baked", "path": str(GOLDEN_HDD), "size_gb": round(size_gb, 2)}


def trigger_key_extraction() -> dict:
    emit("info", "vm", "Key extraction triggered")
    result = sp.run(
        [systemd.SUDO, systemd.SYSTEMCTL, "start", "--no-block", "airtag-extract-keys"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        msg = result.stderr.strip()
        emit("error", "vm", f"Failed to start extraction: {msg}")
        raise VmError(msg)
    emit("info", "vm", "Key extraction service started, VM booting")
    systemd.tail_journal_async("airtag-extract-keys", "vm")
    return {"status": "started", "message": "Key extraction started. This takes a few minutes."}
