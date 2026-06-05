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


_OVMF_VARS_BLOAT_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB - empty is ~400 KB


def _check_ovmf_bloat() -> None:
    """Warn (and let the next vm.stop auto-compact) if OVMF_VARS is oversized.

    OVMF_VARS-1920x1080.qcow2 is attached as pflash without snapshot=on, so
    every QEMU `savevm` writes its full RAM-state delta into this file as
    well as into MacHDD. Healthy is ~400 KB; previously seen in the wild at
    214 GB after many auto-snapshot + failure-capture cycles. _compact_
    ovmf_vars_if_oversize() in vm.stop() reclaims it on the next clean stop;
    this canary just makes the leak visible in the activity log if a stop
    was missed or the container was killed while a VM held the lock.
    """
    try:
        size = _qemu.OVMF_VARS.stat().st_size
    except FileNotFoundError:
        return
    if size > _OVMF_VARS_BLOAT_THRESHOLD_BYTES:
        gb = size / (1024 ** 3)
        emit("warning", "vm",
             f"OVMF_VARS is {gb:.1f} GB - savevm leftover. "
             "The next clean vm.stop will compact it automatically; "
             "or run `qemu-img convert -O qcow2 OVMF_VARS-1920x1080.qcow2 new "
             "&& mv new OVMF_VARS-1920x1080.qcow2` manually while QEMU is down.")


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


_OVMF_VARS_COMPACT_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB


def _compact_ovmf_vars_if_oversize() -> None:
    """Rewrite OVMF_VARS via qemu-img convert when it's bloated.

    QEMU's `savevm` writes the full guest RAM delta into every writable qcow2
    pflash too, not just MacHDD - and we never `loadvm` against OVMF_VARS, so
    that data is pure waste. Without active GC it climbs ~6-10 GB per
    snapshot, easily filling the host disk after a few dozen flows. Sweeping
    here on stop, while OVMF_VARS isn't locked, is the cheap and bounded
    fix: a convert with no `-s` flag drops every internal snapshot, leaving
    just the live NVRAM cluster (~400 KB). MacHDD is left alone because the
    debug-iteration harness deliberately keeps its named snapshots.
    """
    try:
        size = _qemu.OVMF_VARS.stat().st_size
    except FileNotFoundError:
        return
    if size <= _OVMF_VARS_COMPACT_THRESHOLD_BYTES:
        return
    qemu_img = str(Path(_qemu.find_qemu()).parent / "qemu-img")
    tmp = _qemu.OVMF_VARS.with_suffix(_qemu.OVMF_VARS.suffix + ".compact")
    try:
        result = sp.run(
            [qemu_img, "convert", "-O", "qcow2", str(_qemu.OVMF_VARS), str(tmp)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            emit("warning", "vm",
                 f"OVMF_VARS compact failed: {result.stderr.strip()[:200]}")
            return
        tmp.replace(_qemu.OVMF_VARS)
        new_size = _qemu.OVMF_VARS.stat().st_size
        emit("info", "vm",
             f"OVMF_VARS compacted {size / 1e9:.1f} GB -> {new_size / 1024:.0f} KB")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        emit("warning", "vm", f"OVMF_VARS compact errored: {e}")


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
    # Wait for QEMU to release the OVMF_VARS write lock, then compact if it's
    # accumulated savevm waste. Bounded by the threshold so the common no-op
    # case is just a stat().
    _wait_for_qemu_exit(deadline_s=10)
    _compact_ovmf_vars_if_oversize()
    return {"status": "stopped"}


def _wait_for_qemu_exit(deadline_s: float = 10) -> None:
    import time as _t
    t0 = _t.monotonic()
    while _t.monotonic() - t0 < deadline_s:
        if not is_running():
            return
        _t.sleep(0.2)
