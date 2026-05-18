"""macOS Setup Assistant automation — one handler per screen.

Each screen is a distinct InstallState so the engine's retry/resume logic
applies at screen granularity.  Handlers that don't see their expected screen
(because we're resuming past it) skip silently and advance to the next state.
"""

from __future__ import annotations

import time

from ... import qmp, vm_ui, vm_password
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


_SCREEN_W, _SCREEN_H = 1280, 800

# Bottom-right Continue button pixel fallback.
_CONTINUE_X, _CONTINUE_Y = 987, 675

# Screen 8 field centres.
_FULLNAME_FIELD_X, _FULLNAME_FIELD_Y = 620, 307
_PW_FIELD_X, _PW_FIELD_Y = 550, 390

# Screen 5 pixel fallbacks.
_NOT_NOW_X, _NOT_NOW_Y = 287, 670
_MIGRATION_ALERT_OK_X, _MIGRATION_ALERT_OK_Y = 640, 486

# Screen 9: "Don't Use" in location confirmation sheet (blue button, top of dialog).
_DONT_USE_X, _DONT_USE_Y = 640, 476

# Screen 12: "Set Up Later" button.
_SCREEN_TIME_LATER_X, _SCREEN_TIME_LATER_Y = 905, 670


def _press_continue() -> None:
    if not vm_ui.click_text("Continue", include_menubar=True, tries=3):
        emit("info", "setup_assistant", "Continue not found by OCR — using pixel fallback")
        vm_ui.click_pixel(_CONTINUE_X, _CONTINUE_Y, _SCREEN_W, _SCREEN_H)
    time.sleep(2.0)


def _click_blue_pill(first: str, x: int, y: int, last: str | None = None) -> bool:
    label = first if last is None else f"{first} {last}"
    if vm_ui.click_text(first, last, include_menubar=True, tries=3):
        return True
    emit("info", "setup_assistant",
         f"'{label}' not found by OCR — using pixel fallback at ({x},{y})")
    vm_ui.click_pixel(x, y, _SCREEN_W, _SCREEN_H)
    time.sleep(1.5)
    return False


def screen_country(ctx: AutomationContext) -> InstallState:
    """Screen 1: Country or Region.  Waits up to 120 s for SA to appear."""
    if not screen.has_text("Country", "Region", deadline_s=120, poll_s=3.0):
        raise RuntimeError("Setup Assistant 'Country or Region' screen not reached within 120s")
    emit("info", "setup_assistant", "Screen 1: Country or Region")
    qmp.type_text("united sta")
    time.sleep(1.0)
    qmp.send_keys(["ret"])
    time.sleep(1.0)
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Country screen")
    return InstallState.SA_LANGUAGES


def screen_languages(ctx: AutomationContext) -> InstallState:
    """Screen 2: Written and Spoken Languages."""
    if screen.has_any_text("written and spoken", "spoken languages"):
        emit("info", "setup_assistant", "Screen 2: Written and Spoken Languages")
        _press_continue()
    return InstallState.SA_ACCESSIBILITY


def screen_accessibility(ctx: AutomationContext) -> InstallState:
    """Screen 3: Accessibility."""
    if screen.has_any_text("accessibility"):
        emit("info", "setup_assistant", "Screen 3: Accessibility")
        _press_continue()
    return InstallState.SA_DATA_PRIVACY


def screen_data_privacy(ctx: AutomationContext) -> InstallState:
    """Screen 4: Data & Privacy."""
    if screen.has_any_text("data & privacy", "data and privacy"):
        emit("info", "setup_assistant", "Screen 4: Data & Privacy")
        _press_continue()
    return InstallState.SA_MIGRATION


def screen_migration(ctx: AutomationContext) -> InstallState:
    """Screen 5: Migration Assistant.

    A case-sensitive-filesystem alert may appear automatically when macOS
    detects an incompatible source disk.  Dismiss it before clicking Not Now.
    """
    if not screen.has_any_text("migration assistant", "transfer your information",
                               "transfer information"):
        return InstallState.SA_APPLE_ID

    emit("info", "setup_assistant", "Screen 5: Migration Assistant")

    # Dismiss incompatibility alert if present.
    if screen.has_any_text("cannot be used for migration", "case sensitive"):
        emit("info", "setup_assistant",
             "Migration incompatibility alert detected — dismissing")
        if not vm_ui.click_text("OK", include_menubar=True, tries=3):
            vm_ui.click_pixel(_MIGRATION_ALERT_OK_X, _MIGRATION_ALERT_OK_Y,
                              _SCREEN_W, _SCREEN_H)
        time.sleep(1.5)

    if not vm_ui.click_text("Not", "Now", include_menubar=True, tries=3):
        emit("info", "setup_assistant", "'Not Now' not found — using pixel fallback")
        vm_ui.click_pixel(_NOT_NOW_X, _NOT_NOW_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(1.5)

    return InstallState.SA_APPLE_ID


def screen_apple_id(ctx: AutomationContext) -> InstallState:
    """Screen 6: Apple ID sign-in — skip."""
    if not screen.has_any_text("sign in with your apple id", "apple id"):
        return InstallState.SA_TERMS

    emit("info", "setup_assistant", "Screen 6: Apple ID")
    if not vm_ui.click_text("Set", "Up", include_menubar=True, tries=2):
        if not vm_ui.click_text("Later", include_menubar=True, tries=2):
            _press_continue()
    # Dismiss skip confirmation dialog.
    if not vm_ui.click_text("Skip", include_menubar=True, tries=6):
        if not vm_ui.click_text("Don't", "Use", include_menubar=True, tries=3):
            if not screen.has_any_text("terms and conditions", "computer account",
                                       "mac account"):
                qmp.send_keys(["ret"])
                time.sleep(1.0)
    return InstallState.SA_TERMS


def screen_terms(ctx: AutomationContext) -> InstallState:
    """Screen 7: Terms and Conditions."""
    if not screen.has_any_text("terms and conditions"):
        return InstallState.SA_CREATE_ACCOUNT

    emit("info", "setup_assistant", "Screen 7: Terms and Conditions")
    _press_continue()
    if not vm_ui.click_text("Agree", include_menubar=True, tries=4):
        _AGREE_X, _AGREE_Y = 699, 474
        if screen.has_any_text("agree"):
            emit("info", "setup_assistant", "Agree detected — using pixel fallback")
            vm_ui.click_pixel(_AGREE_X, _AGREE_Y, _SCREEN_W, _SCREEN_H)
            time.sleep(1.5)
        else:
            emit("info", "setup_assistant",
                 "Terms Agree sheet not found — may have already advanced")
    return InstallState.SA_CREATE_ACCOUNT


def screen_create_account(ctx: AutomationContext) -> InstallState:
    """Screen 8: Create a Computer/Mac Account."""
    emit("info", "setup_assistant", "Screen 8: Create a Computer Account")
    password = vm_password.ensure()

    # Dismiss passwords-don't-match modal FIRST — its backdrop dims the form,
    # so OCR won't find "Password" while the modal is visible.
    if screen.has_any_text("passwords don't match", "passwords don"):
        emit("info", "setup_assistant", "Password mismatch dialog — pressing Return (Go Back)")
        qmp.send_keys(["ret"])
        time.sleep(2.0)

    # Wait for the account screen — may take a moment after T&C Agree.
    if not screen.has_text("Password", deadline_s=45, poll_s=2.0):
        raise RuntimeError("Account creation screen not reached within 45s")

    # Always clear and re-enter Full Name — Cmd+A overwrites any stale content.
    vm_ui.click_pixel(_FULLNAME_FIELD_X, _FULLNAME_FIELD_Y, _SCREEN_W, _SCREEN_H)
    time.sleep(0.3)
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "a"])
        c.send_keys(["delete"], gap_s=0.1)
        c.type_text("airtag")
    time.sleep(0.5)
    # Tab: Full Name → Account Name (auto-filled) → Password left sub-field.
    qmp.send_keys(["tab"])
    time.sleep(0.5)
    qmp.send_keys(["tab"])
    time.sleep(0.3)

    # Enter password in left sub-field (focus is here after the two Tabs).
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "a"])
        c.send_keys(["delete"], gap_s=0.1)
        c.type_text(password, gap_s=0.08)

    # Wait for the requirements popover to close before pressing Tab.
    # Pressing Tab while the popover is open sends Tab to the popover, not the form.
    time.sleep(2.0)

    # Tab to verify (right) sub-field.
    # Pixel-clicking the verify sub-field positions the cursor there but does NOT
    # transfer keyboard focus within the compound control — Tab is the only reliable way.
    qmp.send_keys(["tab"])
    time.sleep(0.5)

    with qmp.qmp() as c:
        c.type_text(password, gap_s=0.08)

    _press_continue()

    # Wait for the screen to advance; error if password mismatch persists.
    time.sleep(5.0)
    t0 = time.time()
    while time.time() - t0 < 25.0:
        if not screen.has_any_text("Computer Account", "Mac Account"):
            break
        time.sleep(2.0)
    if screen.has_any_text("passwords don't match", "passwords don"):
        raise RuntimeError("Screen 8: passwords still don't match after Continue")

    return InstallState.SA_APPLE_ID_2


def screen_apple_id_2(ctx: AutomationContext) -> InstallState:
    """Screen 9 (macOS Sequoia): Apple ID sign-in appears again after Create Account."""
    if not screen.has_any_text("sign in with your apple id", "apple id", "set up later"):
        return InstallState.SA_TERMS_2

    emit("info", "setup_assistant", "Screen 9: Post-account Apple ID — skipping")
    if not vm_ui.click_text("Set", "Up", include_menubar=True, tries=2):
        if not vm_ui.click_text("Later", include_menubar=True, tries=2):
            _press_continue()
    time.sleep(2.0)
    # Dismiss "Are you sure you want to skip?" confirmation — "Skip" is the blue/default button.
    if not vm_ui.click_text("Skip", include_menubar=True, tries=4):
        if not screen.has_any_text("terms and conditions", "location services"):
            qmp.send_keys(["ret"])
            time.sleep(1.0)
    time.sleep(1.5)
    return InstallState.SA_TERMS_2


def screen_terms_2(ctx: AutomationContext) -> InstallState:
    """Screen 10 (macOS Sequoia): Terms and Conditions appear again after the post-account Apple ID."""
    if not screen.has_any_text("terms and conditions"):
        return InstallState.SA_LOCATION

    emit("info", "setup_assistant", "Screen 10: Terms and Conditions (post-account)")
    _press_continue()
    if not vm_ui.click_text("Agree", include_menubar=True, tries=4):
        if screen.has_any_text("agree"):
            emit("info", "setup_assistant", "Agree detected — using pixel fallback")
            vm_ui.click_pixel(699, 474, _SCREEN_W, _SCREEN_H)
            time.sleep(1.5)
    return InstallState.SA_LOCATION


def screen_location(ctx: AutomationContext) -> InstallState:
    """Screen 11: Location Services."""
    if screen.has_any_text("location services"):
        emit("info", "setup_assistant", "Screen 11: Location Services")
        _press_continue()
        # Confirmation sheet: "Don't Use" is the blue/default button at centre-top.
        if not vm_ui.click_text("Don't", "Use", include_menubar=True, tries=3):
            emit("info", "setup_assistant", "'Don't Use' not found — using pixel fallback")
            vm_ui.click_pixel(_DONT_USE_X, _DONT_USE_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(1.5)
    return InstallState.SA_TIMEZONE


def screen_timezone(ctx: AutomationContext) -> InstallState:
    """Screen 12: Time Zone."""
    if screen.has_any_text("time zone"):
        emit("info", "setup_assistant", "Screen 12: Time Zone")
        _press_continue()
    return InstallState.SA_ANALYTICS


def screen_analytics(ctx: AutomationContext) -> InstallState:
    """Screen 13: Analytics / Share with Apple."""
    emit("info", "setup_assistant", "Screen 13: Analytics")
    _press_continue()
    return InstallState.SA_SCREEN_TIME


def screen_screen_time(ctx: AutomationContext) -> InstallState:
    """Screen 14: Screen Time."""
    if screen.has_any_text("screen time"):
        emit("info", "setup_assistant", "Screen 14: Screen Time")
        _click_blue_pill("Set", _SCREEN_TIME_LATER_X, _SCREEN_TIME_LATER_Y, last="Up")
    return InstallState.SA_APPEARANCE


def screen_appearance(ctx: AutomationContext) -> InstallState:
    """Screen 15: Appearance + optional extra screens, then wait for desktop."""
    if screen.has_any_text("choose your look"):
        emit("info", "setup_assistant", "Screen 15: Appearance")
        _press_continue()

    # macOS Sequoia: Update Mac Automatically screen.
    if screen.has_text("Update", "Automatically", deadline_s=10, poll_s=2.0):
        emit("info", "setup_assistant", "Screen 16: Update Mac Automatically")
        _press_continue()

    # Welcome to Mac splash.
    if screen.has_text("Welcome", deadline_s=15, poll_s=2.0):
        emit("info", "setup_assistant", "Welcome splash — clicking Continue")
        _click_blue_pill("Continue", 640, 722)

    emit("info", "setup_assistant", "Waiting for desktop (Finder)…")
    if not screen.has_text("Finder", deadline_s=300, poll_s=3.0):
        raise RuntimeError("Desktop (Finder) not detected within 300s after Setup Assistant")

    emit("info", "setup_assistant", "Setup Assistant complete — desktop reached")
    return InstallState.DISMISS_FIRST_BOOT
