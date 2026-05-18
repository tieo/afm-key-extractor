"""Post-sign-in handlers for the runtime automation flow.

Covers three states:
- DISMISSING_POST_SIGNIN    → clear modal sheets that pop after iCloud sign-in
- RESOLVING_APPLE_ID_UPDATE → handle the "Update Apple ID Settings" badge
- ENABLING_FIND_MY          → navigate iCloud → Find My Mac and toggle it on

All logic is adapted from vm_apple_signin.py.
"""

from __future__ import annotations

import time

from ... import qmp, vm_ui
from ...events import emit
from ..context import AutomationContext
from ..states import RuntimeState
from .. import screen

# ---------------------------------------------------------------------------
# Constants (mirrors vm_apple_signin.py)
# ---------------------------------------------------------------------------

APPLE_ID_URLS = (
    ("com.apple.systempreferences.AppleIDSettings", None),
    ("com.apple.preferences.AppleIDPrefPane", None),
)

APPLE_ID_LANDED_KEYWORDS = (
    "one account for everything", "apple id", "sign in",
    "icloud", "family sharing", "media & purchases", "sign out",
)

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


def _open_apple_id_pane() -> None:
    """Try each URL scheme in order until the Apple ID pane renders."""
    last = ""
    for bundle, anchor in APPLE_ID_URLS:
        try:
            vm_ui.open_settings_pane(bundle, anchor, settle_s=6.0)
        except Exception as e:
            last = str(e)
            continue
        if vm_ui.wait_for_text(APPLE_ID_LANDED_KEYWORDS, deadline_s=20):
            return
        last = f"{bundle} opened but Apple ID pane never rendered"
    raise RuntimeError(f"could not open Apple ID pane: {last[:200]}")


def _is_apple_id_update_pending() -> bool:
    """Look for the red-badge 'Update Apple ID Settings' row in sidebar."""
    p = vm_ui._screendump()
    words = vm_ui.ocr_words(p)
    # Single-line phrase match: the row reads "Update Apple ID Settings".
    # OCR usually splits it; accept any co-located occurrence of
    # "Update" and "Settings" on a sidebar row.
    return (
        vm_ui.find_phrase(words, "Update", "Settings",
                          y_tol=20, screen_h=None, exclude_chrome=True)
        is not None
    )


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
                with qmp.qmp() as c:
                    c.send_keys(["esc"])
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
        with qmp.qmp() as c:
            c.send_keys(["ret"])
    time.sleep(2.0)

    # Paste password if prompted.
    if vm_ui.wait_for_text(("password",), deadline_s=15):
        vm_ui.paste_text(password)
        time.sleep(0.4)
        with ctx.qmp_lock:
            with qmp.qmp() as c:
                c.send_keys(["ret"])
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
            return RuntimeState.ENABLING_FIND_MY
        time.sleep(3)

    emit("warning", "post_signin", "Apple ID update badge did not clear — continuing")
    return RuntimeState.ENABLING_FIND_MY


def enable_find_my(ctx: AutomationContext) -> RuntimeState:
    """Navigate to iCloud → Find My Mac and enable it.

    Ventura nests Find My Mac inside the iCloud feature list — there is
    no direct URL anchor.  The path is:
        Apple ID pane → click 'iCloud' row → 'Show All' → 'Find My Mac'
        → 'Turn On' → confirm location-permission dialog (Return).

    Returns silently if already enabled (authoritative defaults check).

    Adapted from vm_apple_signin._enable_find_my_mac().

    Raises RuntimeError if Find My Mac never turns on within 60 s.
    """
    deadline_s = 60

    if _is_find_my_mac_on():
        emit("info", "post_signin", "Find My Mac already enabled")
        return RuntimeState.WAITING_ICLOUD_SYNC

    emit("info", "post_signin", "Enabling Find My Mac")

    # The correct Ventura pane is com.apple.systempreferences.AppleIDSettings.
    _open_apple_id_pane()

    if not vm_ui.click_text("iCloud", tries=3):
        raise RuntimeError("Could not locate 'iCloud' row in Apple ID pane")
    time.sleep(1.5)

    if not vm_ui.click_text("Show", "All", tries=3):
        emit("warning", "post_signin",
             "Could not click 'Show All' — Find My row may still be visible")
    time.sleep(1.0)

    if not vm_ui.click_text("Find", "Mac", tries=3):
        raise RuntimeError("Could not locate 'Find My Mac' row in iCloud features list")
    time.sleep(1.5)

    vm_ui.click_text("Turn", "On", tries=2)
    time.sleep(1.0)

    # Location permission prompt — press Return to accept the default button.
    with ctx.qmp_lock:
        with qmp.qmp() as c:
            c.send_keys(["ret"])

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
            return RuntimeState.WAITING_ICLOUD_SYNC
        time.sleep(3)

    raise RuntimeError(f"Find My Mac did not turn on within {deadline_s}s")
