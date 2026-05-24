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
from . import _sa_fields


_SCREEN_W, _SCREEN_H = 1280, 800

# Bottom-right Continue button pixel fallback.
_CONTINUE_X, _CONTINUE_Y = 987, 675

# Screen 8 field centres.
_FULLNAME_FIELD_X, _FULLNAME_FIELD_Y = 620, 307
_ACCOUNT_NAME_FIELD_X, _ACCOUNT_NAME_FIELD_Y = 620, 337
_PW_FIELD_X, _PW_FIELD_Y = 550, 390
_PW_VERIFY_FIELD_X, _PW_VERIFY_FIELD_Y = 740, 390
_HINT_FIELD_X, _HINT_FIELD_Y = 660, 421

# Screen 5 pixel fallbacks.
_NOT_NOW_X, _NOT_NOW_Y = 287, 670
_MIGRATION_ALERT_OK_X, _MIGRATION_ALERT_OK_Y = 640, 486

# Screen 8 error dialog: "Go Back" blue button (white text — OCR-blind; pixel only).
# Modal sheet covers y≈285-515; button centered at (640, 492). Confirmed interactively.
_GO_BACK_X, _GO_BACK_Y = 640, 492

# Screen 9: "Don't Use" in location confirmation sheet (blue button, top of dialog).
_DONT_USE_X, _DONT_USE_Y = 640, 476

# Apple ID skip confirmation dialog: blue "Skip" button (right of "Don't Skip").
# White text on blue — OCR misses it; pixel fallback only.
_APPLE_ID_SKIP_X, _APPLE_ID_SKIP_Y = 699, 493

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
    time.sleep(3.0)  # Wait for slide animation before screen_languages runs
    return InstallState.SA_LANGUAGES


def screen_languages(ctx: AutomationContext) -> InstallState:
    """Screen 2: Written and Spoken Languages."""
    if screen.has_text("written", deadline_s=8, poll_s=1.0):
        emit("info", "setup_assistant", "Screen 2: Written and Spoken Languages")
        _press_continue()
    return InstallState.SA_ACCESSIBILITY


def screen_accessibility(ctx: AutomationContext) -> InstallState:
    """Screen 3: Accessibility.

    Uses a deadline wait (not single-shot) because the previous screen's
    Continue pixel-fallback can take >2 s to animate — a single-shot check
    runs before the screen renders, returns False, and the engine silently
    skips screen 3 onward, leaving SA stuck on Accessibility.
    """
    if not screen.has_text("accessibility", deadline_s=10, poll_s=2.0):
        return InstallState.SA_DATA_PRIVACY
    emit("info", "setup_assistant", "Screen 3: Accessibility")
    # Button is "Not Now" (no "Continue" label) — try OCR then pixel fallback.
    if not vm_ui.click_text("Not", "Now", include_menubar=True, tries=3):
        emit("info", "setup_assistant", "Continue not found by OCR — using pixel fallback")
        vm_ui.click_pixel(_CONTINUE_X, _CONTINUE_Y, _SCREEN_W, _SCREEN_H)
    time.sleep(2.0)
    return InstallState.SA_DATA_PRIVACY


def screen_data_privacy(ctx: AutomationContext) -> InstallState:
    """Screen 4: Data & Privacy."""
    if not screen.has_text("privacy", deadline_s=8, poll_s=2.0):
        return InstallState.SA_MIGRATION
    emit("info", "setup_assistant", "Screen 4: Data & Privacy")
    _press_continue()
    return InstallState.SA_MIGRATION


def screen_migration(ctx: AutomationContext) -> InstallState:
    """Screen 5: Migration Assistant.

    A case-sensitive-filesystem alert may appear automatically when macOS
    detects an incompatible source disk.  Dismiss it before clicking Not Now.
    """
    if not screen.has_text("migration", deadline_s=8, poll_s=2.0):
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


def _dismiss_apple_id_skip_dialog() -> None:
    """Dismiss the 'Are you sure you want to skip signing in with Apple ID?' sheet.

    The dialog has two buttons:
    - Gray  "Don't Skip" (left)  — go back to Apple ID sign-in.
    - Blue  "Skip"       (right) — proceed without Apple ID.

    OCR cannot read the blue "Skip" button (white text on blue background).
    The gray "Don't Skip" button OCRs as separate words 'Don't' and 'Skip'
    which confuses click_text('Skip') into clicking the wrong button.

    Strategy: detect the dialog by its body text, then pixel-click the blue
    "Skip" button at its confirmed coordinates (verified from screenshot).
    """
    time.sleep(2.0)  # wait for the sheet animation to complete
    if screen.has_any_text("don't skip", "are you sure"):
        emit("info", "setup_assistant",
             "Apple ID skip confirmation — clicking blue Skip (pixel fallback)")
        vm_ui.click_pixel(_APPLE_ID_SKIP_X, _APPLE_ID_SKIP_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(1.5)
    else:
        emit("info", "setup_assistant",
             "Apple ID skip dialog not detected — dialog may have auto-dismissed")


def screen_apple_id(ctx: AutomationContext) -> InstallState:
    """Screen 6: Apple ID sign-in — skip."""
    if not screen.has_text("apple id", deadline_s=8, poll_s=2.0):
        return InstallState.SA_TERMS

    emit("info", "setup_assistant", "Screen 6: Apple ID")
    if not vm_ui.click_text("Set", "Up", include_menubar=True, tries=2):
        if not vm_ui.click_text("Later", include_menubar=True, tries=2):
            emit("warning", "setup_assistant", "Screen 6: 'Set Up Later' not found by OCR")
    _dismiss_apple_id_skip_dialog()
    return InstallState.SA_TERMS


def _dismiss_terms_agree_popup() -> None:
    """Dismiss the 'I have read and agree…' confirmation popup on the T&C screen.

    The popup has two buttons at y≈474:
    - Gray  "Disagree" (left,  center ≈ 580, 474)
    - Gray  "Agree"    (right, center ≈ 699, 474)

    click_text("Agree") finds the word "agree" in the popup body text
    ("I have read and agree to the macOS Software License Agreement.")
    at y≈420 instead of the button at y≈474, so it clicks the wrong spot.
    Use pixel fallback directly — coordinates confirmed from screenshot.
    """
    _AGREE_X, _AGREE_Y = 699, 474
    if screen.has_any_text("disagree", "i have read"):
        emit("info", "setup_assistant", "Terms Agree popup — clicking Agree (pixel fallback)")
        vm_ui.click_pixel(_AGREE_X, _AGREE_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(1.5)
    else:
        emit("info", "setup_assistant",
             "Terms Agree popup not detected — may have already advanced")


def screen_terms(ctx: AutomationContext) -> InstallState:
    """Screen 7: Terms and Conditions."""
    if not screen.has_text("terms and conditions", deadline_s=8, poll_s=2.0):
        return InstallState.SA_CREATE_ACCOUNT

    emit("info", "setup_assistant", "Screen 7: Terms and Conditions")
    _press_continue()
    _dismiss_terms_agree_popup()
    return InstallState.SA_CREATE_ACCOUNT


def screen_create_account(ctx: AutomationContext) -> InstallState:
    """Screen 8: Create a Computer/Mac Account.

    Composition of small primitives from ``_sa_fields``.  Each
    primitive owns its own quirks (Cmd+A pitfalls, focus rules,
    timing).  When a step fails, the fix lives in one primitive,
    not in this orchestration function.
    """
    emit("info", "setup_assistant", "Screen 8: Create a Computer Account")
    password = vm_password.ensure()

    # Dismiss any leftover error modal from a prior attempt.
    _sa_fields.dismiss_error_modal_if_present()

    # Wait for the account screen — "computer account" / "mac account" OCRs
    # more reliably than "Password" (the placeholder dots garble OCR when
    # the left sub-field is focused).
    if not screen.has_text("computer account", deadline_s=45, poll_s=2.0):
        if not screen.has_text("mac account", deadline_s=5, poll_s=1.0):
            raise RuntimeError("Account creation screen not reached within 45s")

    time.sleep(2.0)  # settle: T&C → SA-8 slide animation

    # Critical section: an interleaved popup_watcher click between any
    # Cmd+A and the next keystroke can wipe the wrong field or steal focus
    # mid-typing.  Hold from first click through Continue.
    with ctx.critical_section():
        _sa_fields.dismiss_character_picker()

        # Hint must be cleared every attempt — Go Back preserves it across
        # retries; stale Hint matching the password triggers a re-error.
        _sa_fields.fill_field(
            _HINT_FIELD_X, _HINT_FIELD_Y, "",
            clear=True, label="Hint (clearing)",
        )

        # clear=False — Cmd+A on an EMPTY text field puts macOS into a
        # "pending-replacement" state.  Subsequent typing is provisional;
        # when focus shifts (we click Account Name next) macOS reverts the
        # last char(s) of the previously-focused field.  Empirical: with
        # Cmd+A first, Full Name "airtag" → "airt" (last 2 chars dropped).
        # Without Cmd+A, Full Name keeps all 6.  Go Back already clears
        # Full Name / Account Name across retries — only Hint persists —
        # so we never need Cmd+A on these two.
        _sa_fields.fill_field(
            _FULLNAME_FIELD_X, _FULLNAME_FIELD_Y, "airtag",
            clear=False, label="Full Name",
        )
        _sa_fields.fill_field(
            _ACCOUNT_NAME_FIELD_X, _ACCOUNT_NAME_FIELD_Y, "airtag",
            clear=False, label="Account Name",
        )

        # Password compound (left half + tab + verify half) — see primitive
        # for the strategy env var that lets us swap typing approaches via
        # the snapshot/replay harness without code edits.
        _sa_fields.fill_password_compound(_PW_FIELD_X, _PW_FIELD_Y, password)

        emit("info", "setup_assistant", "Screen 8: clicking Continue")
        _press_continue()

    # Wait for the screen to advance or classify the error so the engine
    # can retry (returning SA_CREATE_ACCOUNT triggers a fresh attempt).
    err = _sa_fields.verify_advanced_or_classify_error()
    if err == "passwords_mismatch":
        emit("warning", "setup_assistant",
             "Screen 8: passwords don't match after Continue — retrying")
        return InstallState.SA_CREATE_ACCOUNT
    if err == "hint_contains_password":
        emit("warning", "setup_assistant",
             "Screen 8: hint contains password after Continue — retrying")
        return InstallState.SA_CREATE_ACCOUNT
    if err == "missing_field":
        emit("warning", "setup_assistant",
             "Screen 8: required field(s) missing after Continue — retrying")
        return InstallState.SA_CREATE_ACCOUNT
    return InstallState.SA_APPLE_ID_2


def screen_apple_id_2(ctx: AutomationContext) -> InstallState:
    """Screen 9 (macOS Sequoia): Apple ID sign-in appears again after Create Account."""
    if not screen.has_any_text("sign in with your apple id", "apple id", "set up later"):
        return InstallState.SA_TERMS_2

    emit("info", "setup_assistant", "Screen 9: Post-account Apple ID — skipping")
    if not vm_ui.click_text("Set", "Up", include_menubar=True, tries=2):
        if not vm_ui.click_text("Later", include_menubar=True, tries=2):
            emit("warning", "setup_assistant", "Screen 9: 'Set Up Later' not found by OCR")
    _dismiss_apple_id_skip_dialog()
    return InstallState.SA_TERMS_2


def screen_terms_2(ctx: AutomationContext) -> InstallState:
    """Screen 10 (macOS Sequoia): Terms and Conditions appear again after the post-account Apple ID."""
    if not screen.has_any_text("terms and conditions"):
        return InstallState.SA_LOCATION

    emit("info", "setup_assistant", "Screen 10: Terms and Conditions (post-account)")
    _press_continue()
    _dismiss_terms_agree_popup()
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
    # Use a short deadline so a transient OCR miss (screen animating in) doesn't
    # cause us to skip the Continue click and get stuck on this screen forever.
    if screen.has_text("choose your look", deadline_s=8, poll_s=2.0):
        emit("info", "setup_assistant", "Screen 15: Appearance")
        # Continue is temporarily disabled (grayed) while macOS processes the new
        # account in the background (spinner visible at bottom-left).  Retry until
        # the screen advances.
        # Use has_text with a short deadline (not single-shot has_any_text) so a
        # transient OCR miss doesn't falsely conclude the screen has advanced.
        for _attempt in range(12):
            _press_continue()
            if not screen.has_text("choose your look", deadline_s=4, poll_s=1.0):
                break
            emit("info", "setup_assistant",
                 f"Screen 15: Continue still grayed (attempt {_attempt + 1}) — waiting…")
            time.sleep(2.0)

    # macOS Sequoia: Update Mac Automatically screen.
    if screen.has_text("Update", "Automatically", deadline_s=10, poll_s=2.0):
        emit("info", "setup_assistant", "Screen 16: Update Mac Automatically")
        _press_continue()

    # Welcome to Mac splash.
    if screen.has_text("Welcome", deadline_s=15, poll_s=2.0):
        emit("info", "setup_assistant", "Welcome splash — clicking Continue")
        _click_blue_pill("Continue", 640, 722)

    # Wait for Finder/desktop.  Periodically re-press Continue if the Appearance
    # screen is still visible (handles the case where the advancement check above
    # got a false-negative and exited the loop too early).
    emit("info", "setup_assistant", "Waiting for desktop (Finder)…")
    deadline = time.time() + 300
    while time.time() < deadline:
        if screen.has_any_text("choose your look"):
            emit("info", "setup_assistant",
                 "Appearance screen still visible in Finder wait — re-pressing Continue")
            _press_continue()
            time.sleep(2.0)
            continue
        if screen.has_any_text("Finder"):
            emit("info", "setup_assistant", "Setup Assistant complete — desktop reached")
            return InstallState.DISMISS_FIRST_BOOT
        time.sleep(3.0)
    raise RuntimeError("Desktop (Finder) not detected within 300s after Setup Assistant")
