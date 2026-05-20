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
    if not screen.has_any_text("sign in with your apple id", "apple id"):
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
    if not screen.has_any_text("terms and conditions"):
        return InstallState.SA_CREATE_ACCOUNT

    emit("info", "setup_assistant", "Screen 7: Terms and Conditions")
    _press_continue()
    _dismiss_terms_agree_popup()
    return InstallState.SA_CREATE_ACCOUNT


def screen_create_account(ctx: AutomationContext) -> InstallState:
    """Screen 8: Create a Computer/Mac Account."""
    emit("info", "setup_assistant", "Screen 8: Create a Computer Account")
    password = vm_password.ensure()

    # Dismiss any error modal from a previous Continue attempt.
    # The "Go Back" button is blue (white text) — OCR-blind.  Return key does NOT
    # reach the modal sheet in QEMU.  click_text("Go","Back") hits the phrase
    # "click Go Back" in the body text, not the button — pixel-only.
    if screen.has_any_text("passwords don't match", "passwords don",
                           "haven't provided", "requested information",
                           "hint can't contain", "hint cannot contain"):
        emit("info", "setup_assistant", "Error dialog on Create Account — clicking Go Back")
        vm_ui.click_pixel(_GO_BACK_X, _GO_BACK_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(3.0)  # wait for sheet dismiss animation

    # Wait for the account screen — may take a moment after T&C Agree.
    # "computer account" / "mac account" is the screen title and is more reliably
    # OCR'd than "Password" (the password row shows a placeholder that garbles OCR
    # when the left sub-field is focused).
    if not screen.has_text("computer account", deadline_s=45, poll_s=2.0):
        if not screen.has_text("mac account", deadline_s=5, poll_s=1.0):
            raise RuntimeError("Account creation screen not reached within 45s")

    # Settle: screen transition from T&C may still be animating.
    time.sleep(2.0)

    # Critical section: the entire field-fill sequence is fragile.  An
    # interleaved popup_watcher click (e.g. trying to dismiss a stale prompt
    # OCR'd in the background) between Cmd+A and Backspace can wipe the wrong
    # field or steal focus mid-typing.  Wrap everything until Continue.
    with ctx.critical_section():
        # Dismiss any macOS character picker that QMP key injection may have
        # left open.  The picker intercepts ALL subsequent key events until
        # dismissed (Escape closes it without inserting a character).
        with qmp.qmp() as c:
            c.send_chord(["esc"])
        time.sleep(0.2)

        # Clear Hint field — Go Back preserves Hint across retries; any stale
        # content matching the password triggers "hint can't contain the
        # password" on re-submit.
        emit("info", "setup_assistant", "Screen 8: clearing Hint field")
        vm_ui.click_pixel(_HINT_FIELD_X, _HINT_FIELD_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(0.3)
        with qmp.qmp() as c:
            c.send_chord(["meta_l", "a"])
            time.sleep(0.1)
            c.send_chord(["backspace"])
        time.sleep(0.3)

        # --- Full Name ---
        # Use Cmd+A → Backspace to clear stale content — more reliable than
        # end+N×backspace because the End key mapping in QEMU/macOS VM is
        # unreliable.
        emit("info", "setup_assistant", "Screen 8: filling Full Name")
        vm_ui.click_pixel(_FULLNAME_FIELD_X, _FULLNAME_FIELD_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(0.5)
        with qmp.qmp() as c:
            c.send_chord(["meta_l", "a"])
            time.sleep(0.1)
            c.send_chord(["backspace"])
            time.sleep(0.1)
            c.type_text("airtag")
        time.sleep(0.5)

        # --- Account Name ---
        # Pixel-click Account Name — Tab from Full Name is ambiguous (macOS
        # sometimes skips Account Name entirely when it auto-fills from Full
        # Name, landing Tab on the password field instead).  Go Back preserves
        # field content; clear before fill.
        emit("info", "setup_assistant", "Screen 8: filling Account Name")
        vm_ui.click_pixel(_ACCOUNT_NAME_FIELD_X, _ACCOUNT_NAME_FIELD_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(0.5)
        with qmp.qmp() as c:
            c.send_chord(["meta_l", "a"])
            time.sleep(0.1)
            c.send_chord(["backspace"])
            time.sleep(0.1)
            c.type_text("airtag")
        time.sleep(0.3)

        # --- Password (left sub-field) ---
        # Pixel-click the left password sub-field.  Tab from Account Name is
        # not used here because Account Name triggers an auto-focus animation
        # that can race.  Do NOT use Cmd+A here — in a compound
        # NSSecureTextField (left+verify halves), Cmd+A selects across both
        # halves and moves the focus anchor to the verify half, causing the
        # subsequent type_text to land in verify instead of left.  Go Back
        # always clears both password sub-fields, so there is nothing to clear.
        emit("info", "setup_assistant", "Screen 8: filling password")
        vm_ui.click_pixel(_PW_FIELD_X, _PW_FIELD_Y, _SCREEN_W, _SCREEN_H)
        time.sleep(0.5)
        with qmp.qmp() as c:
            c.type_text(password, gap_s=0.15)
        # Wait for Requirements Popover to close before Tabbing.  Popover does
        # NOT auto-close — it closes when Tab is pressed.  3.0s gives the UI
        # time to render the filled dots before we Tab.
        time.sleep(3.0)

        # --- Password verify (right sub-field) ---
        # Pixel-clicking verify NEVER works — the compound NSSecureTextField
        # always focuses the LEFT half on any click regardless of x-coord.
        # Tab is the only reliable way to reach verify, and only after the
        # Requirements Popover has closed (open popover intercepts Tab).
        # Do NOT use Cmd+A in verify — it would select across both halves and
        # wipe left.
        emit("info", "setup_assistant", "Screen 8: Tab to verify, then fill")
        with qmp.qmp() as c:
            c.send_chord(["tab"])
        time.sleep(0.8)  # let focus settle on verify
        with qmp.qmp() as c:
            c.type_text(password, gap_s=0.15)
        time.sleep(0.8)

        emit("info", "setup_assistant", "Screen 8: clicking Continue")
        _press_continue()

    # Wait for the screen to advance.  Return the same state on any error dialog
    # so the engine retries (RuntimeError would terminate the engine, not retry).
    # "Haven't provided" means a field was empty (usually Full Name on first
    # attempt due to form not yet settled); returning SA_CREATE_ACCOUNT causes
    # the engine to call this handler again, which starts with Go Back to reset.
    time.sleep(5.0)
    t0 = time.time()
    while time.time() - t0 < 25.0:
        screen_txt = vm_ui.screen_text()
        if "passwords don't match" in screen_txt or "passwords don" in screen_txt:
            emit("warning", "setup_assistant",
                 "Screen 8: passwords don't match after Continue — retrying")
            return InstallState.SA_CREATE_ACCOUNT
        if "hint can't contain" in screen_txt or "hint cannot contain" in screen_txt:
            emit("warning", "setup_assistant",
                 "Screen 8: hint contains password after Continue — retrying")
            return InstallState.SA_CREATE_ACCOUNT
        if "haven't provided" in screen_txt or "requested information" in screen_txt:
            emit("warning", "setup_assistant",
                 "Screen 8: required field(s) missing after Continue — retrying")
            return InstallState.SA_CREATE_ACCOUNT
        if "computer account" not in screen_txt and "mac account" not in screen_txt:
            break  # screen advanced successfully
        time.sleep(2.0)

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
    if screen.has_any_text("choose your look"):
        emit("info", "setup_assistant", "Screen 15: Appearance")
        # Continue is temporarily disabled (grayed) while macOS processes the new
        # account in the background (spinner visible at bottom-left).  Retry until
        # the screen advances.
        for _attempt in range(12):
            _press_continue()
            if not screen.has_any_text("choose your look"):
                break
            emit("info", "setup_assistant",
                 f"Screen 15: Continue still grayed (attempt {_attempt + 1}) — waiting…")
            time.sleep(3.0)

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
