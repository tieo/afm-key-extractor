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
    """Dismiss the Keyboard Setup Assistant and re-authenticate.

    macOS shows a "Keyboard Setup Assistant" modal on the first desktop
    session.  We wait up to 30 s for it, click Quit (with retries), then
    type the VM password and press Return to unlock the session in case
    the dismiss triggers a re-lock.
    """
    emit("info", "finalize", "Waiting for Keyboard Setup Assistant modal…")
    if screen.has_text("Keyboard", "Setup", deadline_s=30, poll_s=2.0):
        emit("info", "finalize", "Keyboard Setup Assistant detected — clicking Quit")
        screen.click_text("Quit", tries=3)
        time.sleep(1.0)

    password = vm_password.ensure()
    emit("info", "finalize", "Typing VM password to re-authenticate")
    qmp.type_text(password)
    qmp.send_keys(["ret"])
    time.sleep(5.0)

    return InstallState.SHUTTING_DOWN


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
