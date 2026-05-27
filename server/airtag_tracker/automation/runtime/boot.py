"""Boot-phase handlers for the runtime automation flow.

Covers three states:
- RESTORING_GOLDEN  → copy golden HDD image to working image
- BOOTING           → start the QEMU VM (noVNC is started by vm.start())
- PICKER_SELECTING  → wait for the OpenCore picker and select macOS
"""

from __future__ import annotations

import shutil
import time

from ... import vm, vm_ui
from ...events import emit
from ..context import AutomationContext
from ..states import RuntimeState
from .. import screen
from ..install.opencore import select_macos_entry


def restore_golden(ctx: AutomationContext) -> RuntimeState:
    """Copy the golden HDD image to the working MAC_HDD path.

    If ``ctx.restore_golden`` is False the copy is skipped and we
    proceed directly to booting — useful when re-running the flow on an
    already-customised image without wanting to lose VM state.

    Raises RuntimeError if restore_golden is True but the golden image
    does not exist.
    """
    if not ctx.restore_golden:
        emit("info", "boot", "restore_golden=False — skipping image copy")
        return RuntimeState.BOOTING

    # Stop any lingering QEMU process before overwriting its disk file.
    # Writing to mac_hdd_ng.img while QEMU has it open corrupts the guest
    # filesystem and causes the new run to see a broken macOS state.
    if vm.is_running():
        emit("info", "boot", "VM still running — stopping before golden restore")
        try:
            vm.stop()
            time.sleep(3.0)
        except Exception as e:
            emit("warning", "boot", f"vm.stop() failed ({e}); continuing with restore")

    golden = ctx.adapter.golden_image_path(vm.VM_DIR)
    if not golden.exists():
        raise RuntimeError(
            f"Golden image not found at {golden}. "
            "Run the installation flow first."
        )

    emit("info", "boot", f"Restoring golden image: {golden.name} → {vm.MAC_HDD.name}")
    shutil.copy2(golden, vm.MAC_HDD)
    emit("info", "boot", "Golden image restored")
    return RuntimeState.BOOTING


def start_vm(ctx: AutomationContext) -> RuntimeState:
    """Bring the QEMU VM up.

    OpenCore picker and login are handled by the state machine via OCR —
    `vm.start()` just launches QEMU.
    """
    emit("info", "boot", "Starting VM")
    vm.start()
    emit("info", "boot", "VM started — waiting for OpenCore picker")
    return RuntimeState.PICKER_SELECTING


def select_macos(ctx: AutomationContext) -> RuntimeState:
    """Wait for the OpenCore boot picker and select the macOS entry.

    The golden image's OpenCore may be configured with a short or zero
    auto-boot timeout, meaning the picker is visible for less than one poll
    interval and the VM reaches the login screen (or desktop via autologin)
    before we notice.  Both outcomes are detected and handled:

    - Picker visible → OCR-click the macOS entry (same as install flow).
    - Desktop already up → autologin fired; skip straight to WAITING_DESKTOP.
    - Login screen up → proceed to WAITING_LOGIN_SCREEN normally.

    Polls every 3 s for up to 120 s.
    """
    deadline_s = 120
    poll_s = 3.0
    progress_interval_s = 20
    t0 = time.time()
    last_progress = t0
    emit("info", "boot", "Waiting for OpenCore picker (up to 120 s)")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            emit("info", "boot",
                 f"Still waiting for OpenCore picker… ({elapsed:.0f}s)")
            last_progress = now

        if screen.detect_opencore_picker():
            emit("info", "boot", "OpenCore picker detected — selecting macOS")
            # Retry loop: verify picker disappears after key press.
            # Keys are occasionally dropped (QEMU input timing), leaving the
            # picker stuck and causing a 360s timeout in WAITING_LOGIN_SCREEN.
            for attempt in range(3):
                select_macos_entry(ctx)
                # Poll for up to 12s for the picker to go away.
                for _ in range(6):
                    time.sleep(2.0)
                    if not screen.detect_opencore_picker():
                        return RuntimeState.WAITING_LOGIN_SCREEN
                emit("warning", "boot",
                     f"OpenCore picker still visible after key press (attempt {attempt + 1}) — retrying")
            # Picker persisted through all retries — proceed anyway and let
            # WAITING_LOGIN_SCREEN's login detection sort it out.
            emit("warning", "boot", "OpenCore picker still visible after 3 attempts — proceeding")
            return RuntimeState.WAITING_LOGIN_SCREEN

        # Fast-path: golden image auto-booted past the picker.
        if screen.detect_desktop():
            emit("info", "boot",
                 "Desktop detected — OpenCore auto-booted, skipping to WAITING_DESKTOP")
            return RuntimeState.WAITING_DESKTOP

        if screen.detect_login_screen():
            emit("info", "boot",
                 "Login screen detected — OpenCore auto-booted, proceeding to WAITING_LOGIN_SCREEN")
            return RuntimeState.WAITING_LOGIN_SCREEN

        # The Keyboard Setup Assistant or any foreground app (e.g. System
        # Settings) can hide the Finder menu bar that detect_desktop() looks
        # for.  After a hibernation resume the golden disk's macOS may return
        # directly to the last-used app (e.g. Find My) without showing Finder.
        ocr = vm_ui.screen_text()
        if any(kw in ocr for kw in (
            "keyboard setup assistant",
            "system settings",
            "software update",
            "find my",               # Find My app after hibernation resume
            "enable notifications",  # Find My first-launch dialog
            "not now",               # Common dismiss button on macOS dialogs
            "find your friends",     # Find My friends/items screen
            "lost items",            # Find My items tab
        )):
            emit("info", "boot",
                 "macOS app detected — OpenCore auto-booted, skipping to WAITING_DESKTOP")
            return RuntimeState.WAITING_DESKTOP

        # SSH connectivity is the most reliable "macOS is up" indicator.
        try:
            r = vm_ui.ssh("echo ok", timeout=5)
            if r.returncode == 0:
                emit("info", "boot",
                     "SSH reachable — macOS booted, skipping to WAITING_LOGIN_SCREEN")
                return RuntimeState.WAITING_LOGIN_SCREEN
        except Exception:
            pass

        time.sleep(poll_s)
    raise RuntimeError(f"OpenCore picker not detected within {deadline_s}s")
