"""QEMU binary discovery and command-line construction.

Pure data — no side effects beyond `_find_qemu()` calling `find` to locate
the binary.  Anyone needing to launch QEMU goes through `_lifecycle`, not
this module directly.
"""

from __future__ import annotations

import shutil
import subprocess as sp
from pathlib import Path

from ..config import (
    MONITOR_SOCK,
    QEMU_BINARY,
    QMP_SOCK,
    VM_DIR,
    VM_PID_FILE,
)

MAC_HDD = VM_DIR / "mac_hdd_ng.img"
OVMF_CODE = VM_DIR / "OVMF_CODE_4M.fd"
# OVMF_VARS is qcow2 (not raw) so QEMU's `savevm` writes its snapshot
# state into the same file and that state survives QEMU process restart.
# A raw pflash with `snapshot=on` is snapshottable for the duration of
# the running QEMU but discards snapshots on shutdown — useless for
# the iter-loop where we restart the container between code edits.
OVMF_VARS = VM_DIR / "OVMF_VARS-1920x1080.qcow2"
OPENCORE_QCOW = VM_DIR / "OpenCore" / "OpenCore.qcow2"

# NOTE: +invtsc is intentionally NOT set even though OSX-KVM's reference config
# includes it.  invtsc makes the CPU device non-migratable, which causes QEMU's
# `savevm` to fail with "State blocked by non-migratable CPU device (invtsc flag)"
# — even for same-host snapshots.  Disabling invtsc trades a small amount of
# guest TSC accuracy for the snapshot/replay harness becoming usable.  macOS
# Sonoma still boots fine without it.
_CPU = (
    "Skylake-Client,-hle,-rtm,kvm=on,vendor=GenuineIntel,"
    "vmware-cpuid-freq=on,+ssse3,+sse4.2,+popcnt,+avx,+aes,+xsave,+xsaveopt,check"
)
_OSK = "ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc"


def find_qemu() -> str:
    """Return path to qemu-system-x86_64.

    Resolution order:
    1. AIRTAG_QEMU_BINARY env var (explicit override)
    2. shutil.which (PATH lookup)
    3. `find /nix/store -maxdepth 3` — avoids slow full-store glob
    """
    if QEMU_BINARY != "qemu-system-x86_64":
        return QEMU_BINARY
    if found := shutil.which("qemu-system-x86_64"):
        return found
    try:
        r = sp.run(
            ["find", "/nix/store", "-maxdepth", "3", "-name", "qemu-system-x86_64",
             "!", "-name", "*.drv"],
            capture_output=True, text=True, timeout=10,
        )
        # Prefer full qemu (not host-cpu-only) for macOS CPU model flag support.
        lines = [l for l in r.stdout.splitlines() if "host-cpu-only" not in l]
        if lines:
            return lines[0]
        if r.stdout.strip():
            return r.stdout.splitlines()[0]
    except Exception:
        pass
    return "qemu-system-x86_64"  # will fail with a clear FileNotFoundError


def base_args() -> list[str]:
    """Args common to install and runtime modes."""
    return [
        find_qemu(),
        "-enable-kvm",
        "-m", "8192",
        "-cpu", _CPU,
        "-machine", "q35",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd,bus=xhci.0",
        "-device", "usb-tablet,bus=xhci.0",
        "-smp", "4,cores=2",
        "-device", f"isa-applesmc,osk={_OSK}",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        # OVMF_VARS is qcow2 (see comment above); no snapshot=on so the
        # savevm state persists across QEMU process restart.
        "-drive", f"if=pflash,format=qcow2,file={OVMF_VARS}",
        "-smbios", "type=2",
        "-device", "ich9-intel-hda",
        "-device", "hda-duplex",
        "-device", "ich9-ahci,id=sata",
        # OpenCore must NOT have snapshot=on: savevm writes its per-disk
        # delta into the device's qcow2, and snapshot=on routes those
        # writes to a temp overlay that's discarded when QEMU exits — so
        # any saved snapshot becomes "non-loadable on MacHDD's peer
        # device" on the next QEMU launch.  Dropping snapshot=on means
        # OpenCore's NVRAM emulation writes (macOS sets EFI vars during
        # configure phase) now persist; the existing OVMF-failure
        # recovery in install/opencore.py handles any resulting boot
        # issues via a qemu_restarts loop.
        "-drive", f"id=OpenCoreBoot,if=none,format=qcow2,file={OPENCORE_QCOW}",
        "-device", "ide-hd,bus=sata.2,drive=OpenCoreBoot",
        "-drive", f"id=MacHDD,if=none,file={MAC_HDD},format=qcow2",
        "-device", "ide-hd,bus=sata.4,drive=MacHDD",
        "-netdev", "user,id=net0,hostfwd=tcp::2222-:22",
        "-device", "virtio-net-pci,netdev=net0,id=net0,mac=52:54:00:c9:18:27",
        "-device", "vmware-svga",
        "-vnc", "127.0.0.1:1",
        "-monitor", f"unix:{MONITOR_SOCK},server,nowait",
        "-qmp", f"unix:{QMP_SOCK},server,nowait",
        "-daemonize",
        "-pidfile", str(VM_PID_FILE),
    ]


def runtime_args() -> list[str]:
    """Runtime mode: no installer media attached."""
    return base_args()


def install_args(base_system: Path) -> list[str]:
    """Install mode: base_system image attached as sata.3 (recovery installer).

    Raw format: savevm will fail (no place to store snapshot delta) but
    _try_snapshot / failure_capture already handle that gracefully.
    """
    args = base_args()
    oc_dev_idx = args.index("ide-hd,bus=sata.2,drive=OpenCoreBoot")
    insert_at = oc_dev_idx + 1
    args[insert_at:insert_at] = [
        "-drive", f"id=InstallMedia,if=none,file={base_system},format=raw",
        "-device", "ide-hd,bus=sata.3,drive=InstallMedia",
    ]
    return args
