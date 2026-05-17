"""OpenCore picker interaction handlers.

Covers the picker at three distinct moments in the install flow:
1. First boot into Recovery (wait_for_picker + select_installer).
2. Recovery environment loading (wait_for_recovery).
3. Post-install boot into the freshly installed macOS (select_installed).
"""

from __future__ import annotations

import time

from ... import qmp
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


def wait_for_picker(ctx: AutomationContext) -> InstallState:
    """Poll until the OpenCore boot picker is visible.

    Uses template matching as the primary signal, OCR ("EFI") as fallback.
    Deadline: 120 s.  Raises RuntimeError on timeout.
    """
    deadline_s = 120
    poll_s = 3.0
    t0 = time.time()
    emit("info", "opencore", "Waiting for OpenCore picker…")
    while time.time() - t0 < deadline_s:
        if screen.detect_opencore_picker():
            emit("info", "opencore", "OpenCore picker detected")
            return InstallState.PICKER_SELECTING
        time.sleep(poll_s)
    raise RuntimeError(
        f"OpenCore picker not detected within {deadline_s}s"
    )


def select_installer(ctx: AutomationContext) -> InstallState:
    """Navigate the picker to the macOS installer entry and confirm.

    The macOS Installer entry is immediately to the right of the default
    EFI entry.  Send right + ret.
    """
    emit("info", "opencore", "Selecting installer entry (right + ret)")
    qmp.send_keys(["right", "ret"])
    return InstallState.WAITING_RECOVERY


def wait_for_recovery(ctx: AutomationContext) -> InstallState:
    """Poll until the macOS Recovery Utilities screen is visible.

    Looks for both "Reinstall macOS" and "Disk Utility" in the OCR output.
    Deadline: 150 s.  Raises RuntimeError on timeout.
    """
    deadline_s = 150
    poll_s = 4.0
    t0 = time.time()
    emit("info", "opencore", "Waiting for Recovery Utilities screen…")
    while time.time() - t0 < deadline_s:
        if screen.detect_recovery_utilities():
            emit("info", "opencore", "Recovery Utilities screen detected")
            return InstallState.FORMAT_DISK
        time.sleep(poll_s)
    raise RuntimeError(
        f"Recovery Utilities screen not detected within {deadline_s}s"
    )


def select_installed(ctx: AutomationContext) -> InstallState:
    """Navigate the post-install OpenCore picker to the Macintosh HD entry.

    After the installer completes and the VM reboots, OpenCore shows the
    picker again.  The installed macOS (Macintosh HD) is now the third
    entry: EFI (default) → Installer → Macintosh HD.  Send right twice,
    then ret.

    Polls for the picker first (deadline 120 s).
    """
    deadline_s = 120
    poll_s = 3.0
    t0 = time.time()
    emit("info", "opencore", "Waiting for post-install OpenCore picker…")
    while time.time() - t0 < deadline_s:
        if screen.detect_opencore_picker():
            emit("info", "opencore", "Post-install picker detected — selecting Macintosh HD")
            qmp.send_keys(["right", "right", "ret"])
            return InstallState.SETUP_ASSISTANT
        time.sleep(poll_s)
    raise RuntimeError(
        f"Post-install OpenCore picker not detected within {deadline_s}s"
    )
