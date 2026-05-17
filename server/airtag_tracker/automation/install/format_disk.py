"""Disk formatting via Disk Utility's Terminal in macOS Recovery.

Opens the Terminal from the Utilities menu bar and runs diskutil to erase
disk0 as a single APFS volume named "Macintosh-HD".  This is the target
disk for the subsequent reinstall step.
"""

from __future__ import annotations

import time

from ... import qmp
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen

_ERASE_CMD = "diskutil eraseDisk APFS Macintosh-HD disk0"


def run(ctx: AutomationContext) -> InstallState:
    """Open Terminal from the Utilities menu bar and run diskutil erase.

    All QMP write commands are serialised via ctx.qmp_lock so the popup
    watcher cannot interleave clicks mid-sequence.
    """
    emit("info", "format_disk", "Opening Utilities → Terminal")
    with ctx.qmp_lock:
        # Click the "Utilities" menu bar item.
        screen.click_text("Utilities")
        time.sleep(0.5)
        # Click "Terminal" in the dropdown.
        screen.click_text("Terminal")
        time.sleep(3.0)
        # Type the erase command and confirm.
        qmp.type_text(_ERASE_CMD)
        qmp.send_keys(["ret"])
    emit("info", "format_disk", f"Issued: {_ERASE_CMD}")
    return InstallState.WAITING_FORMAT_DONE


def wait_done(ctx: AutomationContext) -> InstallState:
    """Poll OCR until diskutil reports that the erase is complete.

    Watches for either "Finished erase" or "erase on disk0" in the Terminal
    output.  Deadline: 90 s, poll every 3 s.
    On success, quits Terminal with cmd+q and advances the flow.
    """
    deadline_s = 90
    poll_s = 3.0
    t0 = time.time()
    emit("info", "format_disk", "Waiting for diskutil to finish…")
    while time.time() - t0 < deadline_s:
        if screen.has_any_text("Finished erase", "erase on disk0"):
            emit("info", "format_disk", "Disk erase complete — quitting Terminal")
            qmp.send_chord(["meta_l", "q"])
            return InstallState.REINSTALL_CLICKING
        time.sleep(poll_s)
    raise RuntimeError(
        f"diskutil erase did not complete within {deadline_s}s"
    )
