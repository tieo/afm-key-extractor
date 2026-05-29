"""QEMU process lifecycle: start / stop / is_running / status."""

from __future__ import annotations

import os
import socket
import subprocess as sp
from pathlib import Path

from .. import systemd
from ..config import (
    QMP_SOCK,
    VM_ENABLED,
    VM_PASSWORD_PATH,
    VM_PID_FILE,
    VNC_WS_PORT,
)
from ..events import emit
from . import _qemu
from ._qemu import MAC_HDD


class VmError(Exception):
    """Raised on any failure starting, stopping, or interacting with the VM."""


def is_running() -> bool:
    """True if QEMU is reachable on QMP socket.

    QMP-reachability is the only signal that survives a container restart with
    a stale socket file or a PID that collided in a new namespace.
    """
    qmp_path = Path(QMP_SOCK)
    if not qmp_path.exists():
        VM_PID_FILE.unlink(missing_ok=True)
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(QMP_SOCK)
        return True
    except OSError:
        qmp_path.unlink(missing_ok=True)
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


_OVMF_VARS_BLOAT_THRESHOLD_BYTES = 100 * 1024 * 1024  # 100 MB - empty is ~400 KB


def _check_ovmf_bloat() -> None:
    """Warn loudly if OVMF_VARS has grown past 100 MB.

    OVMF_VARS-1920x1080.qcow2 is attached as pflash without snapshot=on, so
    every QEMU `savevm` writes its full RAM-state delta into this file as
    well as into MacHDD. Healthy is ~400 KB; previously seen in the wild
    at 214 GB after many auto-snapshot + failure-capture cycles. The on-disk
    GC (failure_capture.gc_orphan_snapshots) clears the named entries but
    qcow2 only reclaims that space on a subsequent `qemu-img convert` pass,
    so the size growth is the canary - flag it as soon as it crosses an
    obviously-wrong threshold so the operator can compact/reset it.
    """
    try:
        size = _qemu.OVMF_VARS.stat().st_size
    except FileNotFoundError:
        return
    if size > _OVMF_VARS_BLOAT_THRESHOLD_BYTES:
        gb = size / (1024 ** 3)
        emit("warning", "vm",
             f"OVMF_VARS is {gb:.1f} GB - probably stale savevm data. "
             "Stop the VM, then `qemu-img convert -O qcow2 OVMF_VARS-1920x1080.qcow2 new && mv new OVMF_VARS-1920x1080.qcow2` "
             "to compact it.")


def _launch_qemu(install_mode: bool = False, base_system: Path | None = None) -> None:
    _check_ovmf_bloat()
    if install_mode:
        if base_system is None:
            raise VmError("base_system is required when install_mode=True")
        args = _qemu.install_args(base_system)
    else:
        args = _qemu.runtime_args()
    # Force TMPDIR=/tmp so qemu's snapshot=on overlay doesn't land in a
    # nix-shell temp dir that gets cleaned up before QEMU exits.
    env = os.environ.copy()
    env["TMPDIR"] = "/tmp"
    result = sp.run(args, capture_output=True, text=True, timeout=30, env=env)
    if result.returncode != 0:
        emit("error", "vm", f"QEMU failed to start: {result.stderr}")
        raise VmError(f"Failed to start VM: {result.stderr}")
    systemd.ctl("start", "airtag-novnc")
    emit("info", "vm", f"VM started, noVNC proxy active on port {VNC_WS_PORT}")


def start() -> dict:
    """Boot the existing VM disk with no automation.

    The state machine handles OpenCore picker selection and login explicitly
    via OCR — this function just brings QEMU up.  The caller (engine handler)
    must restore a golden image first if the disk is not yet provisioned.
    """
    if not VM_ENABLED:
        raise VmError("VM not enabled")
    if is_running():
        return {"status": "already_running", "vnc_ws_port": VNC_WS_PORT}

    if not MAC_HDD.exists():
        raise VmError(
            f"VM disk not present at {MAC_HDD}. "
            "Restore from a versioned golden image (ctx.adapter.golden_image_path) first."
        )
    emit("info", "vm", "Starting VM")
    try:
        _launch_qemu()
    except VmError:
        raise
    except Exception as e:
        emit("error", "vm", f"VM start error: {e}")
        raise VmError(str(e))
    return {"status": "started", "vnc_ws_port": VNC_WS_PORT}


def start_for_install(base_system: Path) -> dict:
    """Boot in install mode with *base_system* attached as the recovery installer.

    Pass `ctx.adapter.base_system_path(VM_DIR)` from automation handlers.
    """
    if not VM_ENABLED:
        raise VmError("VM not enabled")
    if is_running():
        return {"status": "already_running", "vnc_ws_port": VNC_WS_PORT}
    if not base_system.exists():
        raise VmError(
            f"BaseSystem image not found at {base_system}. "
            "Run the VM provisioning step first."
        )
    # Always recreate mac_hdd_ng.img as a blank disk before install.  If a
    # prior install left a bootable macOS EFI partition on the disk, OVMF
    # probes it during POST and can spend minutes trying to boot it before
    # falling through to OpenCore — causing spurious picker timeouts.  A
    # blank qcow2 has no EFI partition; OVMF skips it instantly.
    qemu_img = str(Path(_qemu.find_qemu()).parent / "qemu-img")
    emit("info", "vm", "Creating blank mac_hdd_ng.img (80 GB)")
    result = sp.run(
        [qemu_img, "create", "-f", "qcow2", str(MAC_HDD), "80G"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise VmError(f"qemu-img create failed: {result.stderr}")
    emit("info", "vm", f"Starting VM in install mode ({base_system.name} attached)")
    try:
        _launch_qemu(install_mode=True, base_system=base_system)
    except VmError:
        raise
    except Exception as e:
        emit("error", "vm", f"VM install-mode start error: {e}")
        raise VmError(str(e))
    return {"status": "started", "vnc_ws_port": VNC_WS_PORT}


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
