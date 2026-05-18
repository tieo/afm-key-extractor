"""Post-Setup-Assistant finalisation steps.

Three handlers:
1. dismiss_first_boot — close the Keyboard Setup Assistant modal that
   appears the first time a user desktop loads, then re-authenticate.
2. shutdown — gracefully power down the VM via QMP system_powerdown.
3. bake_golden — snapshot the configured disk to the golden image path.
"""

from __future__ import annotations

import time

from ... import qmp, vm, vm_password
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


def dismiss_first_boot(ctx: AutomationContext) -> InstallState:
    """Dismiss first-boot dialogs and enable SSH.

    After SA completes, macOS shows: Welcome splash → Keyboard Setup
    Assistant.  We dismiss both, then enable SSH Remote Login via
    launchctl (systemsetup -setremotelogin requires Full Disk Access on
    Sequoia and is therefore not usable here).
    """
    password = vm_password.ensure()

    # Dismiss Keyboard Setup Assistant if present.
    emit("info", "finalize", "Waiting for Keyboard Setup Assistant modal…")
    if screen.has_text("Keyboard", "Setup", deadline_s=30, poll_s=2.0):
        emit("info", "finalize", "Keyboard Setup Assistant detected — clicking Quit")
        if not screen.click_text("Quit", tries=3):
            emit("warning", "finalize", "OCR Quit not found — using pixel fallback")
            from ... import vm_ui
            vm_ui.click_pixel(905, 684, 1280, 800)
        time.sleep(1.5)

    # Enable SSH Remote Login via Spotlight → Terminal.
    _enable_ssh(password)

    return InstallState.SHUTTING_DOWN


def _enable_ssh(password: str) -> None:
    """Open Terminal via Spotlight and enable SSH Remote Login.

    Uses launchctl rather than systemsetup because macOS Sequoia requires
    Full Disk Access for systemsetup -setremotelogin, which a GUI session
    doesn't hold.
    """
    emit("info", "finalize", "Enabling SSH Remote Login via launchctl")
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "spc"])
    time.sleep(1.5)
    qmp.type_text("Terminal")
    time.sleep(0.5)
    qmp.send_keys(["ret"])
    time.sleep(6.0)
    # Enable SSH: launchctl load -w works without Full Disk Access.
    cmd = "sudo launchctl load -w /System/Library/LaunchDaemons/ssh.plist"
    qmp.type_text(cmd)
    qmp.send_keys(["ret"])
    time.sleep(1.5)
    qmp.type_text(password)
    qmp.send_keys(["ret"])
    time.sleep(4.0)
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "q"])
    time.sleep(1.0)
    emit("info", "finalize", "SSH Remote Login enabled")


def shutdown(ctx: AutomationContext) -> InstallState:
    """Issue a graceful ACPI shutdown via QMP and wait for the VM to stop.

    Polls vm.is_running() every 2 s for up to 60 s.  Raises RuntimeError
    if the VM has not stopped by then.
    """
    emit("info", "finalize", "Sending system_powerdown via QMP")
    qmp.system_powerdown()

    deadline_s = 60
    poll_s = 2.0
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if not vm.is_running():
            emit("info", "finalize", "VM stopped cleanly")
            return InstallState.BAKING_GOLDEN
        time.sleep(poll_s)

    raise RuntimeError(
        f"VM still running {deadline_s}s after system_powerdown was issued"
    )


def bake_golden(ctx: AutomationContext) -> InstallState:
    """Snapshot mac_hdd_ng.img → mac_hdd_golden.img.

    Delegates to vm.bake_golden() which handles the file copy and emits
    its own events.  We emit one additional info event here for the SSE
    log so the progress bar advances to DONE.
    """
    emit("info", "finalize", "Baking golden image from installed disk…")
    vm.bake_golden()
    emit("info", "finalize", "Golden image saved — installation complete")
    return InstallState.DONE
