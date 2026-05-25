"""Disk formatting via Disk Utility's Terminal in macOS Recovery.

Opens the Terminal from the Utilities menu bar and runs diskutil to
partition and format the target disk as a single APFS volume.

QEMU SATA disk mapping inside macOS Recovery (install mode) is NOT stable:
  disk0 = OpenCore.qcow2  (bootloader, ~402 MB)   OR   BaseSystem.img
  disk1 = mac_hdd_ng.img  (80 GB blank target)    OR   OpenCore.qcow2
  disk2 = BaseSystem.img  (3.2 GB, boot/recovery) OR   mac_hdd_ng.img

The disk number assigned to mac_hdd_ng.img varies between boots.  The
target is always identified by its size (≥50 GB) — OpenCore is ~400 MB
and BaseSystem is ~3.2 GB, so the 85.9 GB disk is always the install
target regardless of its disk number.
"""

from __future__ import annotations

import time

from ... import qmp, vm_ui
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen

# The QEMU SATA bus assigns disk numbers non-deterministically across boots
# (disk0=OpenCore/disk1=target/disk2=BaseSystem OR disk0=BaseSystem/disk1=OpenCore/disk2=target).
# Find the target disk reliably by its size: mac_hdd_ng.img is always created
# as "80G" qcow2 which macOS reports as 85.9 GB — the only disk ≥50 GB in
# the QEMU set (OpenCore ~402 MB, BaseSystem ~3.2 GB).
_ERASE_CMD = (
    "TARGET=$(diskutil list internal physical"
    r" | awk '/[5-9][0-9]\.[0-9].*GB/{print $NF; exit}');"
    " diskutil eraseDisk APFS Macintosh-HD $TARGET"
)


def run(ctx: AutomationContext) -> InstallState:
    """Open Terminal from the Utilities menu bar and run diskutil erase.

    All QMP write commands are serialised via ctx.qmp_lock so the popup
    watcher cannot interleave clicks mid-sequence.
    """
    emit("info", "format_disk", "Opening Utilities → Terminal")
    with ctx.qmp_lock:
        # "Utilities" sits in the macOS menu bar, which is outside the normal
        # content area filter — pass include_menubar=True so the click lands.
        clicked = screen.click_text("Utilities", include_menubar=True, tries=5)
        if not clicked:
            # Fallback: keyboard menu bar navigation (ctrl+F2 → focus menu bar).
            emit("warning", "format_disk", "OCR missed Utilities — trying ctrl+F2 navigation")
            qmp.send_chord(["ctrl", "f2"])
            time.sleep(0.5)
            # Type "u" to jump to Utilities, then Enter to open it.
            qmp.type_text("u")
            time.sleep(0.3)
        time.sleep(0.8)
        # Click "Terminal" in the dropdown.
        clicked_term = screen.click_text("Terminal", tries=5)
        if not clicked_term:
            emit("warning", "format_disk", "OCR missed Terminal dropdown — trying keyboard nav")
            qmp.type_text("t")
        time.sleep(3.0)
        # Only type the command if Terminal appears to be open.
        if not screen.has_any_text("Terminal", "bash", "zsh", "Last login"):
            raise RuntimeError("Terminal did not open in Recovery — cannot run diskutil")
        # Type the erase command and confirm.
        qmp.type_text(_ERASE_CMD)
        qmp.send_keys(["ret"])
    emit("info", "format_disk", f"Issued: {_ERASE_CMD}")
    return InstallState.WAITING_FORMAT_DONE


def wait_done(ctx: AutomationContext) -> InstallState:
    """Poll OCR until diskutil partitionDisk reports success.

    Watches for "finished partitioning" (partitionDisk completion) or
    "finished erase" (eraseDisk completion — kept as fallback).
    Does NOT match "Macintosh-HD" which appears in the typed command text
    and would cause an immediate false-positive.
    Deadline: 120 s, poll every 3 s.
    On success, quits Terminal with cmd+q and advances the flow.
    """
    # Wait past the command line so OCR doesn't pick up keywords from the
    # typed command text itself (e.g. "Macintosh-HD" in the command).
    time.sleep(5.0)

    deadline_s = 180
    poll_s = 3.0
    progress_interval_s = 20
    t0 = time.time()
    last_progress = t0
    emit("info", "format_disk", "Waiting for diskutil to finish…")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            emit("info", "format_disk",
                 f"Still waiting for disk erase… ({elapsed:.0f}s)")
            last_progress = now
        text = vm_ui.screen_text()
        if now - last_progress >= progress_interval_s:
            emit("info", "format_disk",
                 f"Still waiting for disk erase… ({elapsed:.0f}s) — screen: {text[:120]!r}")
            last_progress = now
        if any(kw in text for kw in ("finished partitioning", "finished erase")):
            emit("info", "format_disk", "Disk erase complete")
            ctx.adapter.pre_reboot_recovery_setup(ctx)
            emit("info", "format_disk", "Quitting Terminal")
            qmp.send_chord(["meta_l", "q"])
            return InstallState.REINSTALL_CLICKING
        # "[Process completed]" appears in Terminal when the shell session exits —
        # the diskutil command finished and the non-interactive shell closed.
        # Treat this as successful completion (diskutil errors produce an error
        # message before the shell exits, not a clean process-completed banner).
        if "process completed" in text:
            emit("info", "format_disk",
                 "Terminal shell exited ([Process completed]) — assuming diskutil succeeded")
            ctx.adapter.pre_reboot_recovery_setup(ctx)
            emit("info", "format_disk", "Quitting Terminal")
            qmp.send_chord(["meta_l", "q"])
            return InstallState.REINSTALL_CLICKING
        time.sleep(poll_s)
    screen_text = vm_ui.screen_text()
    raise RuntimeError(
        f"diskutil erase did not complete within {deadline_s}s — screen: {screen_text[:200]!r}"
    )
