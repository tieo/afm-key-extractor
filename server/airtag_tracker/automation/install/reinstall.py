"""macOS reinstall wizard automation.

click_through walks the seven-click sequence from the Recovery Utilities
picker through to the point where the installer begins copying files.
wait_complete polls for the VM reboot that signals the installer has
finished and the fresh macOS is ready for Setup Assistant.
"""

from __future__ import annotations

import time

from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


def click_through(ctx: AutomationContext) -> InstallState:
    """Drive the reinstall wizard using OCR-targeted clicks only.

    Step sequence
    -------------
    1. "Reinstall macOS" — main row in the Recovery Utilities picker.
    2. "Continue" — bottom-right of the picker confirmation dialog.
    3. "Continue" — "Install macOS Sonoma" splash (can be slow to appear).
    4. "Agree" — software licence agreement.
    5. "Agree" — confirmation sheet for the licence.
    6. "Macintosh" — selects the "Macintosh-HD" destination volume.
    7. "Continue" — begins the installation.
    """
    emit("info", "reinstall", "Starting reinstall wizard click-through")

    # Step 1 — select "Reinstall macOS" from the Recovery picker.
    if not screen.wait_click_text("Reinstall", "macOS", deadline_s=30):
        raise RuntimeError("Could not find 'Reinstall macOS' in Recovery Utilities")

    # Step 2 — Continue (picker dialog).
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not find first 'Continue' button")

    # Step 3 — Continue on the Install macOS splash (slower to appear).
    if not screen.wait_click_text("Continue", deadline_s=15):
        raise RuntimeError("Could not find 'Continue' on the Install macOS splash")

    # Step 4 — Agree to the licence.
    if not screen.wait_click_text("Agree", deadline_s=10):
        raise RuntimeError("Could not find 'Agree' on the licence screen")

    # Step 5 — Agree on the confirmation sheet.
    if not screen.wait_click_text("Agree", deadline_s=10):
        raise RuntimeError("Could not find 'Agree' on the licence confirmation sheet")

    # Step 6 — Select Macintosh-HD as the destination.
    if not screen.wait_click_text("Macintosh", deadline_s=10):
        raise RuntimeError("Could not find 'Macintosh' (destination volume)")

    # Step 7 — Continue to start copying.
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not find final 'Continue' to begin installation")

    emit("info", "reinstall", "Reinstall wizard complete — installation in progress")
    return InstallState.WAITING_INSTALL


def wait_complete(ctx: AutomationContext) -> InstallState:
    """Wait for the installer to finish and the VM to reboot.

    The installer takes 20-45 minutes.  We poll for the OpenCore picker to
    reappear (which happens after the final reboot) every 30 s.
    Progress events are emitted every 5 minutes so the UI shows activity.

    Deadline: 2700 s (45 min).
    """
    deadline_s = 2700
    poll_s = 30.0
    progress_interval_s = 300  # emit a progress event every 5 minutes
    t0 = time.time()
    last_progress = t0

    emit("info", "reinstall", "Waiting for macOS installation to complete (up to 45 min)…")

    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0

        if now - last_progress >= progress_interval_s:
            minutes = int(elapsed // 60)
            emit("info", "reinstall", f"Still installing… ({minutes} min elapsed)")
            last_progress = now

        if screen.detect_opencore_picker():
            emit("info", "reinstall",
                 f"OpenCore picker detected after {int(elapsed // 60)} min "
                 "— installation complete")
            return InstallState.BOOTING_INSTALLED

        time.sleep(poll_s)

    raise RuntimeError(
        f"macOS installation did not complete within {deadline_s}s"
    )
