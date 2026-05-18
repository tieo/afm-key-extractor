"""macOS reinstall wizard automation.

click_through walks the seven-click sequence from the Recovery Utilities
picker through to the point where the installer begins copying files.
wait_complete polls for the VM reboot that signals the installer has
finished and the fresh macOS is ready for Setup Assistant.
"""

from __future__ import annotations

import time

from ... import qmp, vm_ui
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


def _press_return_with_log(step: str) -> None:
    emit("info", "reinstall", f"{step}: pressing Return (default button fallback)")
    qmp.send_keys(["ret"])
    time.sleep(2.5)


def _click_popup_agree() -> None:
    """Click Agree on the licence confirmation sheet (modal popup).

    OCR misses the popup's small dark-background buttons, so this uses
    fixed coordinates scaled to the actual framebuffer size.  On a
    1280×800 framebuffer the popup Agree button is consistently at ~(712, 460).
    """
    p = vm_ui._screendump()
    sw, sh = vm_ui._screen_size(p)
    cx = int(712 * sw / 1280)
    cy = int(460 * sh / 800)
    fx = int(640 * sw / 1280)
    fy = int(420 * sh / 800)
    emit("info", "reinstall", f"Step 5: clicking popup Agree at ({cx},{cy})")
    vm_ui.click_pixel(fx, fy, sw, sh)  # focus popup body
    time.sleep(0.3)
    vm_ui.click_pixel(cx, cy, sw, sh)
    time.sleep(1.5)


def click_through(ctx: AutomationContext) -> InstallState:
    """Drive the reinstall wizard.

    OCR-click is the primary method; Return is used as fallback for
    "Continue" screens (the default button in every macOS installer
    dialog). "Agree" requires an explicit click (it is not the default).

    Step sequence
    -------------
    1. "Reinstall macOS" — main row in the Recovery Utilities picker.
    2. "Continue" — picker confirmation dialog (default button → Return).
    3. "Continue" — "Install macOS" splash (default button → Return).
    4. "Agree" — software licence agreement (NOT default; OCR required).
    5. "Agree" — confirmation sheet (NOT default; OCR required).
    6. "Macintosh" — selects the "Macintosh-HD" destination volume (OCR).
    7. "Continue" — begins the installation (default button → Return).
    """
    emit("info", "reinstall", "Starting reinstall wizard click-through")

    # Step 1 — select "Reinstall macOS" from the Recovery picker.
    # After Terminal exits the Recovery Utilities window needs time to re-focus.
    if not screen.wait_click_text("Reinstall", "macOS", deadline_s=60):
        raise RuntimeError("Could not find 'Reinstall macOS' in Recovery Utilities")
    time.sleep(1.5)

    # Step 2 — Continue (picker confirmation dialog, default button).
    if not screen.wait_click_text("Continue", deadline_s=20):
        _press_return_with_log("Step 2")

    # Step 3 — Continue on the Install macOS splash (loads slowly).
    # The splash can take 30-60s to appear; OCR often misreads "Continue".
    if not screen.wait_click_text("Continue", deadline_s=90):
        _press_return_with_log("Step 3")

    # Step 4 — Agree to the licence. "Agree" is NOT the default button.
    # The licence text must be scrolled to the bottom first — macOS disables
    # the Agree button until the user has scrolled to the end.
    # Give up to 90s for the licence screen to load after the connection check.
    if not screen.has_text("Agree", deadline_s=90):
        raise RuntimeError("Could not find 'Agree' on the licence screen")
    emit("info", "reinstall", "Step 4: scrolling licence to bottom")
    qmp.send_keys(["end"])
    time.sleep(1.5)
    if not screen.wait_click_text("Agree", deadline_s=30):
        emit("warning", "reinstall",
             "Step 4: OCR missed 'Agree' after scroll — trying Tab+Space keyboard nav")
        qmp.send_keys(["tab"])
        time.sleep(0.4)
        qmp.send_keys(["spc"])
        time.sleep(2.0)

    # Step 5 — Agree on the confirmation sheet (modal popup).
    # OCR cannot reliably detect the popup's dark-background buttons; use
    # fixed-coordinate click scaled to actual framebuffer dimensions.
    time.sleep(1.0)  # wait for the sheet animation
    _click_popup_agree()

    # Step 6 — Select Macintosh-HD as the destination.
    # Disk scan can take 20-30s before the list appears.
    if not screen.wait_click_text("Macintosh", deadline_s=60):
        raise RuntimeError("Could not find 'Macintosh' (destination volume)")

    # Step 7 — Continue to start copying (default button).
    if not screen.wait_click_text("Continue", deadline_s=30):
        _press_return_with_log("Step 7")

    emit("info", "reinstall", "Reinstall wizard complete — installation in progress")
    return InstallState.WAITING_INSTALL


def wait_complete(ctx: AutomationContext) -> InstallState:
    """Wait for the installer to finish and the VM to reboot.

    The installer takes 20-45 minutes.  We poll for the OpenCore picker to
    reappear (which happens after the final reboot) every 30 s.
    Progress events are emitted every 5 minutes so the UI shows activity.

    Deadline: 2700 s (45 min).
    """
    deadline_s = 14400  # 4 hours — macOS installer overestimates in QEMU
    poll_s = 30.0
    progress_interval_s = 300  # emit a progress event every 5 minutes
    t0 = time.time()
    last_progress = t0

    emit("info", "reinstall", "Waiting for macOS installation to complete (up to 4 h)…")

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
