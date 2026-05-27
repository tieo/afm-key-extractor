"""Post-sign-in handlers for the runtime automation flow.

Covers three states:
- DISMISSING_POST_SIGNIN    → clear modal sheets that pop after iCloud sign-in
- RESOLVING_APPLE_ID_UPDATE → handle the "Update Apple ID Settings" badge
- ENABLING_FIND_MY          → navigate iCloud → Find My Mac and toggle it on

All logic is adapted from vm_apple_signin.py.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from ... import qmp, vm_ui
from ...events import emit
from ..context import AutomationContext
from ..states import RuntimeState
from .. import screen
from ._apple_id import APPLE_ID_LANDED_KEYWORDS, APPLE_ID_URLS, open_apple_id_pane

# ---------------------------------------------------------------------------
# Constants specific to this module
# ---------------------------------------------------------------------------

POST_SIGNIN_DISMISSIBLE = (
    # Passcode / "Enter your Mac password" sheet.
    "enter your mac password", "enter the password",
    # iCloud Drive merge sheet.
    "merge", "keep a copy", "don't merge",
    # Freeform/Reminders upgrade nag.
    "upgrade", "later",
    # Messages in iCloud enable nag.
    "messages in icloud",
    # Photos import nag.
    "import", "not now",
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _is_find_my_mac_on() -> bool:
    """Authoritative FMM check via defaults read.

    In MobileMeAccounts plist, 'Enabled = 1;' precedes 'Name = "FIND_MY_MAC";'
    by one line inside each service dict.
    """
    r = vm_ui.ssh(
        "defaults read MobileMeAccounts Accounts 2>/dev/null "
        "| grep -B1 FIND_MY_MAC | grep -c 'Enabled = 1'",
        timeout=10,
    )
    try:
        return int(r.stdout.strip() or "0") > 0
    except ValueError:
        return False


def _check_ls_enabled(ctx: AutomationContext) -> bool:
    """Return True if the Location Services master switch is on in the VM.

    SIP protects the locationd pref file, so we read it via sudo -S.
    """
    pw = ctx.vm_password
    script = f"echo {pw!r} | sudo -S defaults read /var/db/locationd/Library/Preferences/ByHost/com.apple.locationd LocationServicesEnabled 2>/dev/null"
    b64 = base64.b64encode(script.encode()).decode()
    r = vm_ui.ssh(f"echo {b64} | base64 -d | bash", timeout=15)
    out = r.stdout.replace("Password:", "").strip()
    return out == "1"


def _enable_location_services(ctx: AutomationContext) -> None:
    """Enable the Location Services master toggle via System Settings GUI.

    SIP blocks direct writes to locationd prefs even as root, so this
    navigates to Privacy & Security → Location Services, clicks the master
    toggle, and authenticates the change with the VM password.

    In Sonoma 14, clicking the toggle triggers "Privacy & Security is trying to
    modify your system settings. Enter your password to allow this." — this
    function handles that authentication step automatically.

    The toggle widget sits at x≈940 in the 1280×800 golden-image layout; its y
    is found via OCR so minor window placement drift is tolerated.
    """
    if _check_ls_enabled(ctx):
        emit("info", "post_signin", "Location Services already enabled")
        return

    emit("info", "post_signin", "Location Services is OFF — enabling via System Settings GUI")

    LS_URLS = (
        ("com.apple.settings.PrivacySecurity.extension", "Privacy_LocationServices"),
        ("com.apple.preference.security", "Privacy_LocationServices"),
    )
    for bundle, anchor in LS_URLS:
        try:
            vm_ui.open_settings_pane(bundle, anchor, settle_s=4.0)
        except Exception:
            continue
        if vm_ui.wait_for_text(("location services",), deadline_s=10):
            break
    else:
        emit("warning", "post_signin", "Could not open Location Services pane — trying anyway")

    # Find the master toggle row (y > 100 to skip the nav-bar "Location Services"
    # text at y≈83).  The toggle SWITCH is to the right of the label text;
    # in the 1280×800 golden image it is at x≈940.
    p = vm_ui._screendump()
    try:
        words = vm_ui.ocr_words(p)
    finally:
        Path(p).unlink(missing_ok=True)

    toggle_row_y = 134  # pixel fallback for the 1280×800 Sonoma layout
    for t, x, y, w, h in words:
        if t.lower() == "location" and y > 100:
            toggle_row_y = y + h // 2
            break

    vm_ui.click_pixel(940, toggle_row_y, 1280, 800)
    time.sleep(1.5)

    # macOS requires password authentication to modify Location Services.
    text = vm_ui.screen_text()
    if "modify settings" in text or "enter your password" in text:
        emit("info", "post_signin",
             "Location Services auth dialog — entering VM password")
        vm_ui.paste_text(ctx.vm_password)
        time.sleep(0.5)
        with ctx.qmp_lock:
            qmp.send_keys(["ret"])
        time.sleep(2.0)

    if _check_ls_enabled(ctx):
        emit("info", "post_signin", "Location Services enabled")
    else:
        emit("warning", "post_signin",
             "Location Services still appears off — OwnedBeacons sync may not work")


# Backwards-compat alias — older lines call _open_apple_id_pane().
_open_apple_id_pane = open_apple_id_pane


def _handle_icloud_password_prompt(ctx: AutomationContext) -> bool:
    """Detect and dismiss the 'Sign in to iCloud' password sheet.

    macOS sometimes shows this modal after iCloud sign-in completes —
    typically when navigating back into the Apple ID pane.  It asks for
    the Apple ID password and has OK / Cancel buttons.

    Returns True if the prompt was found and handled, False otherwise.
    """
    text = vm_ui.screen_text()
    if "sign in to icloud" not in text and "enter the password for your apple id" not in text:
        return False

    emit("info", "post_signin", "iCloud password prompt detected — entering password")
    if ctx.apple_password:
        vm_ui.paste_text(ctx.apple_password)
        time.sleep(0.4)
    # Click OK (or press Return which defaults to the affirmative button).
    if not vm_ui.click_text("OK", tries=2):
        with ctx.qmp_lock:
            qmp.send_keys(["ret"])
    time.sleep(2.0)
    return True


def _handle_cant_connect_dialog(ctx: AutomationContext) -> bool:
    """Detect and dismiss the 'can't connect to iCloud' notification dialog.

    macOS shows this alert when iCloud can't reach Apple servers during
    the sign-in flow.  It covers System Settings and prevents navigation.
    The primary button ('Apple ID Settings...') just navigates back to the
    Apple ID pane we're already in, so we click 'Later' to dismiss cleanly.

    Returns True if the dialog was found and dismissed.
    """
    text = vm_ui.screen_text()
    if "connect to icloud" not in text and "problem with" not in text:
        return False

    emit("info", "post_signin", "'Can't connect to iCloud' dialog detected — dismissing")
    if not vm_ui.click_text("Later", tries=2):
        with ctx.qmp_lock:
            qmp.send_keys(["esc"])
    time.sleep(1.5)
    return True


def _is_apple_id_update_pending() -> bool:
    """Look for the red-badge 'Update Apple ID Settings' row in sidebar."""
    p = vm_ui._screendump()
    try:
        words = vm_ui.ocr_words(p)
    finally:
        Path(p).unlink(missing_ok=True)
    # Single-line phrase match: the row reads "Update Apple ID Settings".
    # OCR usually splits it; accept any co-located occurrence of
    # "Update" and "Settings" on a sidebar row.
    return (
        vm_ui.find_phrase(words, "Update", "Settings",
                          y_tol=20, screen_h=None, exclude_chrome=True)
        is not None
    )


def _is_keychain_sync_pending() -> bool:
    """Return True if 'Some iCloud Data Isn't Syncing' badge is visible."""
    text = vm_ui.screen_text().lower()
    return "icloud data" in text and "syncing" in text


def _try_resume_keychain_once(ctx: AutomationContext) -> bool:
    """One attempt at the badge → detail page → Resume → Mac password flow.

    Returns True if KEYCHAIN_SYNC is Enabled=1 afterwards.
    """
    # "isn't syncing" is more unique than "iCloud Data" which also matches the
    # regular iCloud sidebar item (navigates to iCloud settings instead of badge).
    clicked = (
        vm_ui.click_text("isn't", "syncing", tries=3)
        or vm_ui.click_text("Syncing", tries=3)
        or vm_ui.click_text("iCloud", "Data", tries=3)
    )
    if not clicked:
        emit("warning", "post_signin", "Could not click iCloud Data badge")
        return False

    time.sleep(2.0)

    # The badge opens a detail page.  "Resume Data Sync" triggers Mac password.
    # OCR occasionally misreads "Sync" as "Syne".
    _ocr_detail = vm_ui.screen_text()
    emit("info", "post_signin", f"Detail page OCR: {_ocr_detail[:400]!r}")
    time.sleep(1.0)

    _found_secondary = (
        vm_ui.click_text("Resume", "Data", tries=3)
        or vm_ui.click_text("Resume", tries=3)
        or vm_ui.click_text("Enter", "Passcode", tries=3)
        or vm_ui.click_text("Verify", tries=3)
    )
    emit("info", "post_signin",
         f"Secondary button: {'found' if _found_secondary else 'NOT found'}")

    # macOS shows "Enter Mac Password" to authorize iCloud Keychain access.
    _ocr_after = vm_ui.screen_text().lower()
    if "mac password" in _ocr_after or "unlock this mac" in _ocr_after or \
            vm_ui.wait_for_text(("mac password", "unlock this mac"), deadline_s=15):
        emit("info", "post_signin", "Mac password dialog — entering VM password")
        with ctx.critical_section():
            vm_ui.paste_text(ctx.vm_password)
            time.sleep(0.4)
            if not vm_ui.click_text("Continue", tries=2):
                if not vm_ui.click_text("OK", tries=2):
                    with ctx.qmp_lock:
                        qmp.send_keys(["ret"])
        time.sleep(3.0)
    else:
        emit("warning", "post_signin",
             f"No Mac password dialog detected; OCR: {_ocr_after[:150]!r}")

    # Some setups additionally prompt for the former iPhone passcode.
    if ctx.iphone_passcode and vm_ui.wait_for_text(("passcode",), deadline_s=10):
        emit("info", "post_signin", "iPhone passcode prompt — entering PIN")
        with ctx.critical_section():
            with ctx.qmp_lock:
                qmp.type_text(ctx.iphone_passcode)
            time.sleep(0.5)
            if not vm_ui.click_text("Continue", tries=2):
                if not vm_ui.click_text("OK", tries=2):
                    with ctx.qmp_lock:
                        qmp.send_keys(["ret"])
        time.sleep(5.0)

    return _check_keychain_enabled()


def _handle_icloud_keychain_sync(ctx: AutomationContext) -> bool:
    """Handle the 'Some iCloud Data Isn't Syncing' badge in the Apple ID pane.

    Flow (discovered empirically on Sonoma 14):
      1. Click the sidebar badge row ("isn't syncing") → opens a detail page.
      2. Click "Resume Data Sync" on that page → macOS shows "Enter Mac Password".
      3. Paste the VM login password and confirm → KEYCHAIN_SYNC becomes Enabled=1
         and SEARCHPARTY is provisioned, allowing OwnedBeacons to sync.

    Retries up to 3 times if SSH verification shows KEYCHAIN_SYNC still Enabled=0.
    Returns True if the badge was found (regardless of outcome).
    """
    if not _is_keychain_sync_pending():
        return False

    emit("info", "post_signin", "iCloud Keychain not syncing — clicking badge to resume")

    for attempt in range(1, 4):
        ok = _try_resume_keychain_once(ctx)
        if ok:
            emit("info", "post_signin",
                 f"KEYCHAIN_SYNC confirmed Enabled=1 via SSH (attempt {attempt})")
            return True
        emit("warning", "post_signin",
             f"KEYCHAIN_SYNC still Enabled=0 after attempt {attempt}")
        if attempt < 3:
            # Re-open the Apple ID pane and try again.
            try:
                _open_apple_id_pane()
            except Exception:
                pass
            time.sleep(3.0)
            if not _is_keychain_sync_pending():
                emit("info", "post_signin",
                     "iCloud Data badge gone — possibly resolved between attempts")
                return _check_keychain_enabled()

    emit("warning", "post_signin",
         "KEYCHAIN_SYNC still Enabled=0 after 3 attempts — OwnedBeacons may not sync")
    return True


def _check_keychain_enabled() -> bool:
    """Return True if KEYCHAIN_SYNC is Enabled=1 in MobileMeAccounts."""
    try:
        r = vm_ui.ssh(
            "defaults read MobileMeAccounts Accounts 2>/dev/null "
            "| grep -B1 KEYCHAIN_SYNC | grep -c 'Enabled = 1'",
            timeout=10,
        )
        return int(r.stdout.strip() or "0") > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# State handlers
# ---------------------------------------------------------------------------

def dismiss_prompts(ctx: AutomationContext) -> RuntimeState:
    """Dismiss modal sheets that pop after a fresh iCloud sign-in.

    Strategy: for up to 45 s, poll OCR; when a known dismissible-sheet
    keyword appears, prefer clicking 'Later' / 'Not Now' / 'Cancel'
    (non-destructive), else press Escape.  Exit once two consecutive
    polls see no matches.

    Adapted from vm_apple_signin._dismiss_post_signin_prompts().
    """
    emit("info", "post_signin", "Dismissing post-signin dialogs")
    deadline_s = 45
    progress_interval_s = 15
    clean_rounds = 0
    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            emit("info", "post_signin",
                 f"Still dismissing post-signin dialogs… ({elapsed:.0f}s)")
            last_progress = now
        text = vm_ui.screen_text()

        # The "Sign in to iCloud" password sheet must be handled by entering
        # the password and clicking OK — clicking Cancel would abort iCloud
        # activation and leave the Apple ID pane stuck on the sign-in form.
        if "sign in to icloud" in text or "enter the password for your apple id" in text:
            _handle_icloud_password_prompt(ctx)
            clean_rounds = 0
            continue

        matched = [kw for kw in POST_SIGNIN_DISMISSIBLE if kw in text]
        if not matched:
            clean_rounds += 1
            if clean_rounds >= 2:
                emit("info", "post_signin", "Post-signin dialogs cleared")
                return RuntimeState.RESOLVING_APPLE_ID_UPDATE
            time.sleep(2)
            continue
        clean_rounds = 0
        # Prefer a dedicated dismiss button (non-destructive) before Escape.
        clicked = False
        for label_pair in (("Later",), ("Not", "Now"), ("Cancel",), ("Don't", "Merge")):
            if vm_ui.click_text(*label_pair, tries=1):
                clicked = True
                break
        if not clicked:
            with ctx.qmp_lock:
                qmp.send_keys(["esc"])
            time.sleep(1.0)
        time.sleep(1.5)

    emit("warning", "post_signin",
         "Post-signin dialogs not fully cleared within deadline — continuing anyway")
    return RuntimeState.RESOLVING_APPLE_ID_UPDATE


def resolve_update(ctx: AutomationContext) -> RuntimeState:
    """Handle the 'Update Apple ID Settings' red-badge prompt if present.

    Opens the Apple ID pane, looks for the badge row via OCR, clicks it,
    accepts any Continue dialog, and pastes the Apple ID password if a
    password prompt appears.  Waits up to 60 s for the badge to clear.

    Adapted from vm_apple_signin._resolve_apple_id_update().
    """
    emit("info", "post_signin", "Checking for Apple ID update prompt")
    deadline_s = 60
    try:
        _open_apple_id_pane()
    except Exception as e:
        emit("warning", "post_signin", f"Could not open Apple ID pane for update check: {e}")
        return RuntimeState.ENABLING_FIND_MY

    if not _is_apple_id_update_pending():
        emit("info", "post_signin", "No Apple ID update pending")
        _handle_icloud_keychain_sync(ctx)
        return RuntimeState.ENABLING_FIND_MY

    emit("info", "post_signin", "Apple ID update pending — driving prompt")
    password = ctx.apple_password
    if not password:
        emit("warning", "post_signin",
             "Apple ID update prompt present but no password in context — skipping")
        return RuntimeState.ENABLING_FIND_MY

    if not vm_ui.click_text("Update", "Settings", tries=3):
        emit("warning", "post_signin", "Could not click 'Update Apple ID Settings' row")
        return RuntimeState.ENABLING_FIND_MY

    time.sleep(2.0)
    # Accept any onboarding/Continue dialog that overlaps the button.
    with ctx.qmp_lock:
        qmp.send_keys(["ret"])
    time.sleep(2.0)

    # Paste password if prompted.
    if vm_ui.wait_for_text(("password",), deadline_s=15):
        vm_ui.paste_text(password)
        time.sleep(0.4)
        with ctx.qmp_lock:
            qmp.send_keys(["ret"])
        emit("info", "post_signin", "Apple ID update: password submitted")
    else:
        emit("info", "post_signin",
             "No password prompt — update may have resolved on Continue")

    # Wait for the badge to clear.
    progress_interval_s = 20
    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            emit("info", "post_signin",
                 f"Still waiting for Apple ID update to clear… ({elapsed:.0f}s)")
            last_progress = now
        if not _is_apple_id_update_pending():
            emit("info", "post_signin", "Apple ID settings up to date")
            _handle_icloud_keychain_sync(ctx)
            return RuntimeState.ENABLING_FIND_MY
        time.sleep(3)

    emit("warning", "post_signin", "Apple ID update badge did not clear — continuing")
    _handle_icloud_keychain_sync(ctx)
    return RuntimeState.ENABLING_FIND_MY


def _open_find_my_and_complete_onboarding(ctx: AutomationContext) -> None:
    """Open the Find My app and dismiss all first-launch onboarding dialogs.

    On the first open after iCloud sign-in, Find My shows up to three dialogs:
    1. TCC location permission: "Find My would like to use your current location"
       Don't Allow | Allow  — click Allow (right button, ~x=730 in 1280×800).
    2. "What's New in Find My" feature overview — click Continue.
    3. "Find Your Friends & Lost Items" notification prompt — click Not Now.

    Completing these dialogs grants Find My's TCC location permission and
    activates its CloudKit zone, both of which searchpartyuseragent needs
    before it will populate OwnedBeacons.

    Safe to call even when Find My is already set up — the main interface
    (People/Devices/Items tabs) is detected immediately and the function returns.
    """
    emit("info", "post_signin", "Opening Find My app to complete onboarding")

    vm_ui.ssh("pkill 'System Settings' 2>/dev/null; true", timeout=5)
    time.sleep(1)
    vm_ui.ssh("open -a FindMy", timeout=10)
    time.sleep(5.0)  # cold launch

    deadline_s = 60
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        text = vm_ui.screen_text().lower()

        if "would like to use your current location" in text:
            emit("info", "post_signin",
                 "Find My location permission dialog — clicking Allow")
            # Native macOS buttons are invisible to OCR; use a pixel click for the
            # right-side Allow button.  Dialog is centered on the 1280×800 display
            # with body text ending at ~y=337; buttons sit ~40px below at y≈375.
            # Allow (right) is at ~x=730; Don't Allow (left) is at ~x=570.
            vm_ui.click_pixel(730, 375, 1280, 800)
            time.sleep(2.0)
            continue

        if "what's new in find my" in text:
            emit("info", "post_signin", "Find My 'What's New' screen — clicking Continue")
            if not vm_ui.click_text("Continue", tries=2):
                with ctx.qmp_lock:
                    qmp.send_keys(["ret"])
            time.sleep(2.0)
            continue

        if "find your friends" in text or ("lost items" in text and "not now" in text):
            emit("info", "post_signin",
                 "Find My notifications prompt — clicking Not Now")
            if not vm_ui.click_text("Not", "Now", tries=2):
                with ctx.qmp_lock:
                    qmp.send_keys(["esc"])
            time.sleep(2.0)
            continue

        if "people" in text and "devices" in text and "items" in text:
            emit("info", "post_signin", "Find My app onboarding complete")
            return

        time.sleep(2.0)

    emit("warning", "post_signin",
         "Find My onboarding timed out — continuing; OwnedBeacons sync may be delayed")


def enable_find_my(ctx: AutomationContext) -> RuntimeState:
    """Navigate to iCloud → Find My Mac and enable it, then complete Find My onboarding.

    Ventura nests Find My Mac inside the iCloud feature list — there is
    no direct URL anchor.  The path is:
        Apple ID pane → click 'iCloud' row → 'Show All' → 'Find My Mac'
        → 'Turn On' → confirm location-permission dialog (Return).

    After enabling, opens the Find My app to dismiss its first-launch
    onboarding dialogs (location permission, What's New, notifications),
    which is required for searchpartyuseragent to sync OwnedBeacons.

    Returns silently if already enabled (authoritative defaults check).

    Adapted from vm_apple_signin._enable_find_my_mac().

    Raises RuntimeError if Find My Mac never turns on within 60 s.
    """
    deadline_s = 60

    # Location Services must be on for Find My Mac to work AND for
    # searchpartyuseragent to sync OwnedBeacons.  Enable it first so that
    # clicking "Turn On" for Find My Mac shows the location-permission dialog
    # (not a "Location Services is off" alert that our Return press can't resolve).
    _enable_location_services(ctx)

    if _is_find_my_mac_on():
        emit("info", "post_signin", "Find My Mac already enabled")
        _open_find_my_and_complete_onboarding(ctx)
        return RuntimeState.WAITING_ICLOUD_SYNC

    emit("info", "post_signin", "Enabling Find My Mac")

    # Indicators that only appear in the logged-in Apple ID view, not the
    # sign-in form.  "sign out" is the gold standard but may be off-screen;
    # "family sharing" and "media & purchases" appear as sidebar rows that
    # the sign-in form body never contains.  Any one suffices.
    LOGGED_IN_KEYWORDS = (
        "sign out",
        "family sharing",
        "media & purchases",
        "icloud data",        # sync-status badge, only visible when signed in
    )

    def _wait_for_logged_in_pane(deadline_s: int) -> bool:
        _open_apple_id_pane()
        _handle_icloud_password_prompt(ctx)
        _handle_cant_connect_dialog(ctx)
        return vm_ui.wait_for_text(LOGGED_IN_KEYWORDS, deadline_s=deadline_s)

    if not _wait_for_logged_in_pane(30):
        for attempt in range(1, 4):
            emit("warning", "post_signin",
                 f"Apple ID pane still in sign-in state (attempt {attempt}) — "
                 f"restarting System Settings; OCR: {vm_ui.screen_text()[:120]!r}")
            vm_ui.ssh("pkill 'System Settings'", timeout=5)
            time.sleep(5.0)
            if _wait_for_logged_in_pane(45):
                break
        else:
            raise RuntimeError(
                "Apple ID pane not in logged-in state after 3 System Settings restarts"
            )

    # Navigation to the Find My Mac toggle is adapter-specific so future macOS
    # versions can override the path without touching this handler.
    ctx.adapter.navigate_to_find_my_mac(ctx)

    # Location permission prompt — press Return to accept the default button.
    with ctx.qmp_lock:
        qmp.send_keys(["ret"])

    progress_interval_s = 20
    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            emit("info", "post_signin",
                 f"Still waiting for Find My Mac to enable… ({elapsed:.0f}s)")
            last_progress = now
        if _is_find_my_mac_on():
            emit("info", "post_signin", "Find My Mac enabled")
            _open_find_my_and_complete_onboarding(ctx)
            return RuntimeState.WAITING_ICLOUD_SYNC
        time.sleep(3)

    raise RuntimeError(f"Find My Mac did not turn on within {deadline_s}s")
