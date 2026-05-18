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
    """Poll OCR until diskutil reports that the erase is complete.

    Watches for either "Finished erase" or "erase on disk0" in the Terminal
    output.  Deadline: 90 s, poll every 3 s.
    On success, quits Terminal with cmd+q and advances the flow.
    """
    deadline_s = 90
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
        if screen.has_any_text("Finished erase", "erase on disk0"):
            emit("info", "format_disk", "Disk erase complete — quitting Terminal")
            qmp.send_chord(["meta_l", "q"])
            return InstallState.REINSTALL_CLICKING
        time.sleep(poll_s)
    raise RuntimeError(
        f"diskutil erase did not complete within {deadline_s}s"
    )
