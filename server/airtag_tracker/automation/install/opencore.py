"""OpenCore picker interaction handlers.

Covers the picker at three distinct moments in the install flow:
1. First boot into Recovery (wait_for_picker + select_installer).
2. Recovery environment loading (wait_for_recovery).
3. Post-install boot into the freshly installed macOS (select_installed).
"""

from __future__ import annotations

import time

from ... import qmp, vm
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


def wait_for_picker(ctx: AutomationContext) -> InstallState:
    """Poll until the OpenCore boot picker is visible.

    Starts the VM in install mode if it is not already running.
    Uses template matching as the primary signal, OCR ("EFI") as fallback.
    Deadline: 180 s after VM start.  Raises RuntimeError on timeout.
    """
    if not vm.is_running():
        emit("info", "opencore", "VM not running — starting in install mode")
        vm.start_for_install()
        time.sleep(5.0)  # give QEMU a moment to initialise before polling

    deadline_s = 180
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

    The macOS Installer (BaseSystem) entry is immediately to the right of
    the default EFI entry.  Send right + ret under qmp_lock to prevent
    the popup watcher from injecting a keypress mid-sequence.
    """
    emit("info", "opencore", "Selecting installer entry (right + ret)")
    with ctx.qmp_lock:
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

    macOS installation involves two reboot+configure phases, each preceded
    by an OpenCore picker.  This handler loops: whenever the picker appears
    it sends right+right+ret (EFI→Installer→MacHDD), then resumes watching.
    It advances to SETUP_ASSISTANT only when the Setup Assistant
    "Country or Region" screen is detected.

    TianoCore BIOS recovery: macOS sets volatile EFI boot priority variables
    during configure phases.  If OVMF fails all boot entries and drops into
    the Boot Maintenance Manager, we issue system_reset so OVMF re-reads the
    unchanged OVMF_VARS file and boots OpenCore again.

    Deadline: 1800 s (30 min) from first call to cover both configure phases.
    Raises RuntimeError on timeout.
    """
    deadline_s = 1800
    poll_s = 5.0
    t0 = time.time()
    picker_seen = 0
    resets_done = 0
    MAX_RESETS = 5
    emit("info", "opencore", "Waiting for post-install boot sequence…")
    while time.time() - t0 < deadline_s:
        if screen.detect_opencore_picker():
            picker_seen += 1
            emit("info", "opencore",
                 f"Post-install picker #{picker_seen} — selecting Macintosh HD")
            with ctx.qmp_lock:
                qmp.send_keys(["right", "right", "ret"])
            time.sleep(10.0)  # let macOS start booting before next poll
            continue

        if screen.detect_setup_assistant():
            emit("info", "opencore",
                 "Setup Assistant detected — advancing flow")
            return InstallState.SETUP_ASSISTANT

        if screen.detect_tiano_bios() and resets_done < MAX_RESETS:
            resets_done += 1
            emit("info", "opencore",
                 f"TianoCore BIOS detected — issuing system_reset #{resets_done}")
            try:
                qmp.system_reset()
            except Exception as e:
                emit("warning", "opencore", f"system_reset failed: {e}")
            time.sleep(15.0)  # wait for POST + OpenCore to appear
            continue

        time.sleep(poll_s)
    raise RuntimeError(
        f"Setup Assistant not reached within {deadline_s}s after install"
    )
