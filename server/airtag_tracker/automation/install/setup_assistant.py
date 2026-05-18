"""macOS Setup Assistant automation.

Walks all 13 screens of the first-boot Setup Assistant that appear after
a fresh macOS installation, creating the local ``airtag`` account and
dismissing every optional service screen.

Text input uses ``vm_ui.paste_text()`` (clipboard via SSH + cmd-v) rather
than QMP keystroke sequences so that the password survives any keyboard-
layout difference.
"""

from __future__ import annotations

import time

from ... import qmp, vm_ui, vm_password
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


# The Setup Assistant Continue button is a blue pill with white text that
# tesseract consistently misreads (conf ~19, below the 30 threshold).
# Pixel position is stable across all SA screens at 1280×800 VM resolution.
_CONTINUE_X, _CONTINUE_Y = 987, 675
_SCREEN_W, _SCREEN_H = 1280, 800

# Screen 8: Create a Computer Account — field coordinates (1280×800)
# The password and verify fields sit side-by-side on one row at y≈390.
_PW_FIELD_X, _PW_FIELD_Y = 550, 390    # "new password" (left)
_VERIFY_FIELD_X, _VERIFY_FIELD_Y = 770, 390  # "verify" (right)
# "passwords don't match" modal Go Back button (centred in dialog)
_GOBACK_X, _GOBACK_Y = 640, 485

# Screen 9: Location Services — "Don't Use" in the confirmation sheet.
# The button is a blue pill and is frequently undetectable by OCR.
_DONT_USE_X, _DONT_USE_Y = 880, 485

# Screen 12: Screen Time — "Set Up Later" secondary button (left of Continue).
_SCREEN_TIME_LATER_X, _SCREEN_TIME_LATER_Y = 905, 670


def _press_continue() -> None:
    """Click the Continue button, falling back to pixel coords if OCR fails."""
    if not screen.wait_click_text("Continue", deadline_s=8):
        emit("info", "setup_assistant",
             "Continue not found by OCR — using pixel fallback")
        vm_ui.click_pixel(_CONTINUE_X, _CONTINUE_Y, _SCREEN_W, _SCREEN_H)
    time.sleep(1.5)


def _current_screen() -> int:
    """Return the index (1-13) of the Setup Assistant screen currently visible.

    Returns 0 if the current screen cannot be identified (e.g. still loading).
    Used to skip past screens that have already been completed on resumption.
    """
    text = screen.has_any_text  # brevity alias
    # Check in reverse order so the highest matching screen wins.
    if text("choose your look"):
        return 13
    if text("screen time"):
        return 12
    if text("time zone"):
        return 10
    if text("location services"):
        return 9
    if text("computer account", "mac account"):
        return 8
    if text("terms and conditions"):
        return 7
    if text("sign in with your apple id"):
        return 6
    if text("migration assistant", "transfer your information"):
        return 5
    if text("data & privacy"):
        return 4
    if text("accessibility"):
        return 3
    if text("written and spoken", "spoken languages"):
        return 2
    if text("country or region", "choose your country"):
        return 1
    return 0


def run(ctx: AutomationContext) -> InstallState:
    """Walk all 13 Setup Assistant screens and land on the desktop."""
    password = vm_password.ensure()
    emit("info", "setup_assistant", "Starting Setup Assistant automation")

    # Detect mid-flow resumption: if we're already past screen 1, skip ahead.
    resume_from = _current_screen()
    if resume_from > 1:
        emit("info", "setup_assistant",
             f"Resuming Setup Assistant from screen {resume_from}")
    elif resume_from == 0:
        # Screen not identified — check for the "passwords don't match" dialog
        # which covers the Screen 8 title text and fools OCR.
        if screen.has_any_text("passwords don't match", "passwords don"):
            resume_from = 8
            emit("info", "setup_assistant",
                 "Mismatch dialog detected — resuming at screen 8")
        else:
            resume_from = 1

    # ------------------------------------------------------------------
    # 1. Country or Region
    # ------------------------------------------------------------------
    if resume_from <= 1:
        emit("info", "setup_assistant", "Screen 1: Country or Region")
        if not screen.has_text("Country", "Region", deadline_s=120, poll_s=3.0):
            raise RuntimeError("Setup Assistant 'Country or Region' screen not reached within 120s")
        qmp.type_text("united sta")
        time.sleep(1.0)
        qmp.send_keys(["ret"])
        time.sleep(1.0)
        if not screen.wait_click_text("Continue", deadline_s=10):
            raise RuntimeError("Could not click Continue on Country screen")

    # ------------------------------------------------------------------
    # 2. Written and Spoken Languages
    # ------------------------------------------------------------------
    if resume_from <= 2:
        emit("info", "setup_assistant", "Screen 2: Written and Spoken Languages")
        _press_continue()

    # ------------------------------------------------------------------
    # 3. Accessibility
    # ------------------------------------------------------------------
    if resume_from <= 3:
        emit("info", "setup_assistant", "Screen 3: Accessibility")
        _press_continue()

    # ------------------------------------------------------------------
    # 4. Data & Privacy
    # ------------------------------------------------------------------
    if resume_from <= 4:
        emit("info", "setup_assistant", "Screen 4: Data & Privacy")
        _press_continue()

    # ------------------------------------------------------------------
    # 5. Migration Assistant
    # ------------------------------------------------------------------
    if resume_from <= 5:
        emit("info", "setup_assistant", "Screen 5: Migration Assistant")
        # Older macOS shows "Not Now" button; newer shows radio buttons + Continue.
        if not screen.wait_click_text("Not", "Now", deadline_s=10):
            # Select "Set up as new Mac" radio button (if shown).
            screen.wait_click_text("new", deadline_s=5)
            time.sleep(0.5)
            _press_continue()

    # ------------------------------------------------------------------
    # 6. Apple ID sign-in
    # ------------------------------------------------------------------
    if resume_from <= 6:
        emit("info", "setup_assistant", "Screen 6: Apple ID")
        # In newer macOS there is no "Set Up Later" button.
        # Click Continue with empty field to trigger the skip confirmation.
        if not screen.wait_click_text("Set", "Up", deadline_s=5):
            if not screen.wait_click_text("Later", deadline_s=5):
                _press_continue()  # Continue with empty field → skip dialog
        # Skip confirmation dialog or auto-advance past Apple ID.
        if not screen.wait_click_text("Skip", deadline_s=15):
            if not screen.wait_click_text("Don't", "Use", deadline_s=5):
                if not screen.has_any_text("terms and conditions", "computer account"):
                    # Dismiss any error dialog that appeared (validation error).
                    qmp.send_keys(["ret"])
                    time.sleep(1.0)

    # ------------------------------------------------------------------
    # 7. Terms and Conditions
    # ------------------------------------------------------------------
    if resume_from <= 7:
        emit("info", "setup_assistant", "Screen 7: Terms and Conditions")
        _press_continue()
        if not screen.wait_click_text("Agree", deadline_s=10):
            emit("warning", "setup_assistant",
                 "Terms Agree sheet not found — may have already advanced")

    # ------------------------------------------------------------------
    # 8. Create a Computer Account
    # ------------------------------------------------------------------
    if resume_from <= 8:
        emit("info", "setup_assistant", "Screen 8: Create a Computer Account")
        # "Password:" label is present on both "Create a Computer Account" and
        # "Create a Mac Account" (the newer title) — use it as the reliable signal.
        if not screen.has_text("Password", deadline_s=30, poll_s=2.0):
            raise RuntimeError("Account creation screen not reached within 30s")

        # Dismiss "passwords don't match" modal via Return (the dialog's default
        # button).  click_pixel is silently blocked by macOS modal dispatch; the
        # Return key is always routed to the focused dialog button.
        if screen.has_any_text("passwords don't match", "passwords don"):
            emit("info", "setup_assistant",
                 "Password mismatch dialog detected — pressing Return to dismiss")
            qmp.send_keys(["ret"])
            time.sleep(1.5)

        # Type full name only if the field is empty (not pre-filled from a prior run).
        if not screen.has_any_text("airtag"):
            qmp.type_text("airtag")
            time.sleep(0.5)

        # Fill both sub-fields of the compound password control.
        # The requirements popover absorbs Tab when sent within the same QMP
        # connection as the preceding type_text — so we use three separate
        # connections: fill LEFT, wait for popover to settle, Tab, fill RIGHT.
        vm_ui.click_pixel(_PW_FIELD_X, _PW_FIELD_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(0.3)
        with qmp.qmp() as c:
            c.send_chord(["meta_l", "a"])
            c.send_keys(["delete"], gap_s=0.1)
            c.type_text(password, gap_s=0.04)
        time.sleep(1.5)   # let requirements popover settle before Tab
        qmp.send_keys(["tab"])
        time.sleep(0.3)
        with qmp.qmp() as c:
            c.send_chord(["meta_l", "a"])
            c.send_keys(["delete"], gap_s=0.1)
            c.type_text(password, gap_s=0.04)

        _press_continue()

        # Wait up to 30 s for the screen to advance.  If the mismatch dialog
        # reappears, raise now rather than silently proceeding past Screen 8.
        time.sleep(5.0)
        t0 = time.time()
        while time.time() - t0 < 25.0:
            if not screen.has_any_text("Computer Account", "Mac Account"):
                break
            time.sleep(2.0)
        if screen.has_any_text("passwords don't match", "passwords don"):
            raise RuntimeError(
                "Screen 8: passwords still don't match after Continue — aborting"
            )

    # ------------------------------------------------------------------
    # 9. Location Services
    # ------------------------------------------------------------------
    if resume_from <= 9:
        emit("info", "setup_assistant", "Screen 9: Location Services")
        _press_continue()
        if not screen.wait_click_text("Don't", "Use", deadline_s=10):
            emit("info", "setup_assistant",
                 "\"Don't Use\" not found by OCR — using pixel fallback")
            vm_ui.click_pixel(_DONT_USE_X, _DONT_USE_Y, _SCREEN_W, _SCREEN_H)
            time.sleep(1.0)

    # ------------------------------------------------------------------
    # 10. Time Zone
    # ------------------------------------------------------------------
    if resume_from <= 10:
        emit("info", "setup_assistant", "Screen 10: Time Zone")
        _press_continue()

    # ------------------------------------------------------------------
    # 11. Analytics / Share with Apple
    # ------------------------------------------------------------------
    if resume_from <= 11:
        emit("info", "setup_assistant", "Screen 11: Analytics")
        _press_continue()

    # ------------------------------------------------------------------
    # 12. Screen Time
    # ------------------------------------------------------------------
    if resume_from <= 12:
        emit("info", "setup_assistant", "Screen 12: Screen Time")
        if not screen.wait_click_text("Set", "Up", deadline_s=20):
            if not screen.wait_click_text("Later", deadline_s=10):
                emit("info", "setup_assistant",
                     "'Set Up Later' not found by OCR — using pixel fallback")
                vm_ui.click_pixel(_SCREEN_TIME_LATER_X, _SCREEN_TIME_LATER_Y,
                                  _SCREEN_W, _SCREEN_H)
                time.sleep(1.0)

    # ------------------------------------------------------------------
    # 13. Appearance / Choose Your Look
    # ------------------------------------------------------------------
    if resume_from <= 13:
        emit("info", "setup_assistant", "Screen 13: Appearance")
        _press_continue()

    # ------------------------------------------------------------------
    # 14. Update Mac Automatically (macOS Sequoia extra screen)
    # Appears after Choose Your Look before the Welcome splash.
    # ------------------------------------------------------------------
    if screen.has_text("Update", "Automatically", deadline_s=10, poll_s=2.0):
        emit("info", "setup_assistant", "Screen 14: Update Mac Automatically")
        _press_continue()

    # ------------------------------------------------------------------
    # Welcome to Mac splash — click Continue to reach the desktop.
    # ------------------------------------------------------------------
    if screen.has_text("Welcome", deadline_s=15, poll_s=2.0):
        emit("info", "setup_assistant", "Welcome to Mac splash — clicking Continue")
        if not screen.wait_click_text("Continue", deadline_s=10):
            vm_ui.click_pixel(640, 722, _SCREEN_W, _SCREEN_H)
            time.sleep(1.0)

    # ------------------------------------------------------------------
    # Wait for desktop (Finder menu bar)
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Waiting for desktop (Finder)…")
    if not screen.has_text("Finder", deadline_s=300, poll_s=3.0):
        raise RuntimeError("Desktop (Finder) not detected within 300s after Setup Assistant")

    emit("info", "setup_assistant", "Setup Assistant complete — desktop reached")
    return InstallState.DISMISS_FIRST_BOOT
