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
    """Confirm the licence agreement confirmation sheet (modal popup).

    The popup's [Disagree] [Agree] pill buttons are invisible to all OCR
    variants (tiny dark-on-dark).  The popup body text contains the word
    "agree" at y<430 (the EULA Agree button OCRs as "Adge"/"A%e" at y≈637,
    so y<430 reliably discriminates popup body from EULA button).

    The popup is keyboard-deaf in QEMU Recovery mode: Return and Tab events
    go to the underlying EULA window, not the modal sheet.  The only reliable
    method is a direct pixel click on the Agree pill button.

    Button geometry (1280×800 VM): Disagree ≈x526-635, Agree ≈x644-753,
    both at y≈440-465.  Agree center ≈ (sw//2 + 60, popup_body_bottom + 29).
    """
    emit("info", "reinstall", "Step 5: waiting for popup sheet animation…")
    time.sleep(3.0)

    for attempt in range(1, 4):
        p = vm_ui._screendump()
        sw, sh = vm_ui._screen_size(p)
        words = vm_ui.ocr_words(p)

        # Popup body text has "agree"/"agreement" at y<430.
        # EULA Agree button OCRs as "Adge"/"A%e" at y≈637 — never matches.
        popup_words = [
            w for w in words
            if w[0].lower() in ("agree", "agreement", "agreement.")
            and w[2] < 430
        ]
        if not popup_words:
            emit("info", "reinstall", f"Step 5 attempt {attempt}: popup gone — proceeding")
            return

        body_bottom = max(w[2] + w[4] for w in popup_words)
        # Agree pill button: right button of the pair, ~60px right of centre,
        # ~29px below the bottom of the popup body text.
        click_x = sw // 2 + 60
        click_y = body_bottom + 29
        emit("info", "reinstall",
             f"Step 5 attempt {attempt}: clicking popup Agree at ({click_x},{click_y})")
        vm_ui.click_pixel(click_x, click_y, sw, sh)
        time.sleep(3.0)

    emit("warning", "reinstall", "Step 5: all direct-click attempts failed — popup may still be open")


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
    # The splash body text contains the word "continue" (e.g. "click Continue"),
    # so any OCR search for "Continue" hits the body text instead of the button
    # and clicks the wrong place.  The Continue button IS the default action,
    # so pressing Return after the splash appears is both correct and reliable.
    emit("info", "reinstall", "Step 3: waiting for installer splash…")
    if screen.has_text("install macos", deadline_s=60):
        emit("info", "reinstall", "Step 3: installer splash detected — pressing Return")
    else:
        emit("warning", "reinstall",
             "Step 3: splash not detected in 60s — pressing Return anyway")
    _press_return_with_log("Step 3")

    # Step 4 — Agree to the licence. "Agree" is NOT the default button.
    # The licence text must be scrolled to the bottom first — macOS disables
    # the Agree button until the user has scrolled to the end.
    # Allow 3 min: the installer does a server-side connection check before
    # showing the licence, which can take 60-120 s in QEMU.
    # The "Agree" button OCRs unreliably ("Ag&e", "Agge") due to font size.
    # "Disagree" always OCRs correctly; click the button to its right instead.
    if not screen.has_text("Disagree", deadline_s=180):
        raise RuntimeError("Could not find EULA screen (no 'Disagree' button)")
    # Scroll the EULA text view to the bottom using the mouse scroll wheel.
    # Keyboard Page Down requires focus on the text view, which is unreliable
    # (focus may be on a button).  Scroll wheel events go to the element under
    # the mouse pointer regardless of keyboard focus, so click a word in the
    # EULA body first to position the pointer inside the scroll view, then
    # scroll wheel the rest of the way down.
    emit("info", "reinstall", "Step 4: scrolling licence to bottom via mouse wheel")
    vm_ui.click_text("CAREFULLY", tries=2)   # "PLEASE READ CAREFULLY" — top of EULA body
    time.sleep(0.3)
    # 30 clicks × ~3 lines/click = ~90 lines — more than any EULA length.
    vm_ui.scroll_down(clicks=30, gap_s=0.05)
    time.sleep(1.5)
    emit("info", "reinstall", "Step 4: clicking Agree (right of Disagree)")
    if not vm_ui.click_right_of("Disagree"):
        emit("warning", "reinstall",
             "Step 4: click_right_of failed — trying Tab+Space keyboard nav")
        qmp.send_keys(["tab"])
        time.sleep(0.4)
        qmp.send_keys(["spc"])
        time.sleep(2.0)

    # Step 5 — Agree on the confirmation sheet (modal popup).
    # Same problem: "Agree" OCRs poorly; "Disagree" is reliable.
    time.sleep(1.0)  # wait for the sheet animation
    _click_popup_agree()

    # Step 6 — Select Macintosh-HD as the destination.
    # Disk scan can take 20-30s before the list appears.
    # "Macintosh-HD" OCRs as one token "macintoshhd"; _prefix_extend handles
    # the match since "macintosh" is ≥8 chars and "macintoshhd".startswith it.
    if not screen.wait_click_text("Macintosh", deadline_s=120):
        # Emit a debug log with the full OCR word list so we can diagnose
        import tempfile
        _p = vm_ui._screendump()
        _words = vm_ui.ocr_words(_p)
        emit("error", "reinstall",
             f"Step 6: disk not found. OCR words: {sorted(set(w[0] for w in _words))}")
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
