"""Apple ID sign-in handlers for the runtime automation flow.

Covers six states:
- OPENING_APPLE_ID          → open System Settings → Apple ID pane
- TYPING_CREDENTIALS        → enter email + password
- WAITING_2FA_OR_SIGNED_IN  → wait for Apple to respond
- AWAITING_2FA              → block until the user supplies the 6-digit code
- TYPING_2FA                → type the received code into the VM
- WAITING_SIGNED_IN         → confirm sign-in completed and write marker

All sign-in logic is adapted from vm_apple_signin.py.
"""

from __future__ import annotations

import re
import time

from ... import qmp, vm_ui
from ...config import VM_ICLOUD_SIGNED_IN_MARKER, APPLE_SMS_PHONE_SUFFIX
from ...events import emit
from ..context import AutomationContext
from ..states import RuntimeState
from .. import screen
from ._apple_id import APPLE_ID_LANDED_KEYWORDS, APPLE_ID_URLS, open_apple_id_pane

# ---------------------------------------------------------------------------
# Constants specific to this module
# ---------------------------------------------------------------------------

TWOFA_KEYWORDS = (
    "verification code", "two-factor", "enter the code", "trust this",
)

SIGNIN_FAIL_KEYWORDS = (
    "incorrect", "could not sign in", "try again",
    "verification failed", "cannot verify",
)

# Text that ONLY appears in the logged-in Apple ID view, never in the sign-in
# form or any transitional state.  Any one match (+ plist confirmation) is
# sufficient to declare the account signed in.
# "sign out"        — the gold standard, but may be off-screen (requires scroll)
# "family sharing"  — sidebar row in logged-in view
# "media & purchases" — sidebar row in logged-in view
# "icloud data"     — iCloud sync badge visible only when signed in
LOGGEDIN_INDICATORS = (
    "sign out",
    "family sharing",
    "media & purchases",
    "icloud data",
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _is_signed_in() -> bool:
    """Authoritative iCloud sign-in check via defaults read."""
    r = vm_ui.ssh(
        "defaults read MobileMeAccounts Accounts 2>/dev/null | grep -c AccountID",
        timeout=10,
    )
    try:
        return r.returncode == 0 and int(r.stdout.strip() or "0") > 0
    except ValueError:
        return False


def _screen_has_fail() -> bool:
    text = vm_ui.screen_text()
    return any(kw in text for kw in SIGNIN_FAIL_KEYWORDS)


def _extract_masked_phone() -> str | None:
    """Scan OCR text for an Apple-style masked phone number.

    Apple shows things like '+49 •••• ••12 34' on the SMS-sent sheet.
    OCR often turns •/● into '.', '-', '*', '+' or drops them entirely,
    so we match the tail digits with any noise in between.
    """
    text = vm_ui.screen_text()
    for pat in (
        r"\+\d[\d\s\-\.\*\+•●x]{3,}\d{2,4}",
        r"[\*•●\.\+x]{2,}[\s\-]?\d{2,4}",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()
    return None


def _request_sms_code() -> str | None:
    """Ensure an SMS verification code is dispatched to a phone number.

    Two paths:
    1. Fast path — Apple already sent the code (screen shows "resend code" or
       "can't use this number?").  Extract and return the masked phone number.
    2. Navigation path — screen shows the trusted-device prompt ("Didn't receive
       a code?").  Navigate: didn't-receive → can't-get-to-devices → [select
       number] → Send Code.

    If AIRTAG_SMS_PHONE_SUFFIX is set and a number-selection sheet appears,
    clicks the row containing that suffix before clicking Send Code.
    Returns the masked phone number, or None on failure.
    """
    emit("info", "apple_signin", "Requesting SMS verification code")

    # Fast path: Apple already dispatched the code (SMS-sent state).
    # Poll up to 10 s for the screen to settle — the dialog may still be
    # animating when this function is called.
    for _ in range(5):
        text = vm_ui.screen_text()
        if "resend code" in text or "can't use this number" in text:
            phone = _extract_masked_phone()
            if phone:
                emit("info", "apple_signin", f"SMS already sent by Apple to {phone}")
            else:
                emit("info", "apple_signin", "SMS already sent by Apple (phone not OCR'd)")
            return phone
        time.sleep(2.0)

    # Navigation path: trusted-device prompt → request SMS.
    if not vm_ui.click_text("receive", "code", tries=3):
        emit("warning", "apple_signin",
             "SMS flow: 'Didn't receive a verification code?' not found")
        return None
    time.sleep(1.5)
    if not vm_ui.click_text("get", "devices", tries=3):
        emit("warning", "apple_signin",
             "SMS flow: 'Can't get to your trusted devices?' not found")
        return None
    time.sleep(1.5)

    # When the Apple ID has multiple trusted numbers, Apple shows a selection
    # list before the Send Code button.  Click the configured number if present.
    if APPLE_SMS_PHONE_SUFFIX:
        suffix_digits = "".join(c for c in APPLE_SMS_PHONE_SUFFIX if c.isdigit())
        if suffix_digits:
            if vm_ui.click_text(suffix_digits, tries=2):
                emit("info", "apple_signin",
                     f"Selected phone number ending in {suffix_digits}")
                time.sleep(1.0)
            else:
                emit("warning", "apple_signin",
                     f"Phone number suffix '{suffix_digits}' not found on screen — "
                     "proceeding with Apple's default selection")

    if not vm_ui.click_text("Send", "Code", tries=3):
        emit("warning", "apple_signin", "SMS flow: 'Send Code' button not found")
        return None
    time.sleep(2.5)
    phone = _extract_masked_phone()
    if phone:
        emit("info", "apple_signin", f"SMS sent to {phone}")
    return phone


# Backwards-compat alias — older lines call _open_apple_id_pane().
_open_apple_id_pane = open_apple_id_pane


# ---------------------------------------------------------------------------
# State handlers
# ---------------------------------------------------------------------------

def open_apple_id(ctx: AutomationContext) -> RuntimeState:
    """Open System Settings and navigate to the Apple ID sign-in pane.

    If the VM is already signed in (authoritative SSH check) we skip
    straight to post-signin dismissal.  Otherwise we try each known URL
    scheme until the pane renders.

    Raises RuntimeError if the pane never opens.
    """
    emit("info", "apple_signin", "Opening Apple ID settings pane")

    last_err = ""
    for bundle, anchor in APPLE_ID_URLS:
        try:
            vm_ui.open_settings_pane(bundle, anchor, settle_s=6.0)
        except Exception as e:
            last_err = str(e)
            continue

        if vm_ui.wait_for_text(APPLE_ID_LANDED_KEYWORDS, deadline_s=20):
            # Pane is open — check if already signed in.
            r = vm_ui.ssh(
                "defaults read MobileMeAccounts Accounts 2>/dev/null | grep -c AccountID",
                timeout=10,
            )
            try:
                already = r.returncode == 0 and int(r.stdout.strip() or "0") > 0
            except ValueError:
                already = False

            if already:
                # Also verify the UI shows the logged-in view.  MobileMeAccounts can
                # have a stale AccountID from a prior run whose session has since expired;
                # System Settings correctly shows the sign-in form in that case.
                # "Sign Out" only appears in the logged-in view, never in the sign-in form.
                ui_logged_in = vm_ui.wait_for_text(LOGGEDIN_INDICATORS, deadline_s=8)
                if ui_logged_in:
                    emit("info", "apple_signin", "Already signed into iCloud — skipping credentials")
                    return RuntimeState.DISMISSING_POST_SIGNIN
                emit("info", "apple_signin",
                     "MobileMeAccounts has AccountID but UI shows sign-in form "
                     "(stale session) — re-entering credentials")

            emit("info", "apple_signin", "Apple ID pane open — ready to enter credentials")
            return RuntimeState.TYPING_CREDENTIALS

        last_err = f"{bundle} opened but Apple ID pane never rendered"

    raise RuntimeError(f"could not open Apple ID pane: {last_err[:200]}")


def type_credentials(ctx: AutomationContext) -> RuntimeState:
    """Focus the email field, enter Apple ID email and password.

    Uses clipboard paste for both values so special characters are not
    mangled by QMP keystroke layout mapping.

    Sonoma's sign-in sheet is a single-page form (Email or Phone Number +
    Password fields visible simultaneously) - Tab advances email → password.
    The older two-page wizard used Return to advance; that left focus on the
    email field on the one-page form and the password got pasted into it,
    producing "<email><password>" and an empty password field.
    """
    emit("info", "apple_signin", "Entering Apple ID credentials")

    # Focus email field via adapter — uses a pixel click so sidebar search
    # never captures the paste.
    with ctx.qmp_lock:
        ctx.adapter.focus_apple_id_email_field(ctx)

    vm_ui.paste_text(ctx.apple_email)
    time.sleep(0.4)
    with ctx.qmp_lock:
        qmp.send_keys(["tab"])
    time.sleep(0.6)

    vm_ui.paste_text(ctx.apple_password)
    time.sleep(0.4)
    with ctx.qmp_lock:
        qmp.send_keys(["ret"])
    vm_ui.wipe_clipboard()

    return RuntimeState.WAITING_2FA_OR_SIGNED_IN


def wait_2fa_or_signed_in(ctx: AutomationContext) -> RuntimeState:
    """Poll until Apple either completes sign-in or asks for a 2FA code.

    Checks authoritative SSH state, OCR for 2FA keywords, and OCR for
    failure keywords on each poll.

    Polls every 4 s for up to 180 s.  Raises RuntimeError on failure
    keywords or timeout.
    """
    deadline_s = 180
    poll_s = 4.0
    progress_interval_s = 30
    t0 = time.time()
    last_progress = t0
    emit("info", "apple_signin", "Waiting for Apple response (2FA or signed-in)")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            screen_snippet = vm_ui.screen_text()[:80] if hasattr(vm_ui, 'screen_text') else ''
            emit("info", "apple_signin",
                 f"Still waiting for Apple response… ({elapsed:.0f}s) screen: {repr(screen_snippet)}")
            last_progress = now

        text = vm_ui.screen_text()

        # macOS may show "Enter Mac Password" before showing 2FA, to set up
        # iCloud Keychain.  Enter the VM password and continue waiting.
        if "enter mac password" in text or "enter your mac password" in text:
            emit("info", "apple_signin",
                 "Enter Mac Password prompt (pre-2FA) — entering VM password")
            vm_ui.paste_text(ctx.vm_password)
            time.sleep(0.4)
            with ctx.qmp_lock:
                qmp.send_keys(["ret"])
            time.sleep(2.0)
            continue

        if any(kw in text for kw in TWOFA_KEYWORDS):
            emit("info", "apple_signin", "2FA prompt detected")
            return RuntimeState.AWAITING_2FA

        if any(kw in text for kw in SIGNIN_FAIL_KEYWORDS):
            raise RuntimeError(
                "Apple rejected credentials — check Apple ID email and password"
            )

        # LOGGEDIN_INDICATORS only appear in the logged-in Apple ID view —
        # never in the sign-in form or any transitional state.  Require one
        # alongside the plist check to prevent stale-AccountID false positives.
        if any(kw in text for kw in LOGGEDIN_INDICATORS) and _is_signed_in():
            emit("info", "apple_signin", "Signed in — logged-in Apple ID view detected")
            return RuntimeState.DISMISSING_POST_SIGNIN

        time.sleep(poll_s)

    raise RuntimeError(
        f"Timed out waiting for 2FA prompt or signed-in state after {deadline_s}s"
    )


def await_2fa_input(ctx: AutomationContext) -> RuntimeState:
    """Block until the user submits their 2FA code via the API.

    Broadcasts a ``2fa_required`` SSE event so the browser can surface
    the code-entry UI.  While waiting, checks every 2 s whether an SMS
    code was requested; if so, drives the in-VM SMS flow and records the
    masked phone number.

    Raises TimeoutError after 600 s (propagated by ctx.wait_for_2fa).
    """
    emit("info", "apple_signin", "Waiting for 2FA code from user (up to 10 min)")

    if ctx._broadcast:
        try:
            from datetime import UTC, datetime
            ctx._broadcast({
                "type": "2fa_required",
                "ts": datetime.now(UTC).isoformat(),
            })
        except Exception:
            pass

    # Automatically request an SMS code so Tasker can relay it — no manual
    # intervention needed.  Wait 5 s first for the VM's 2FA dialog to settle.
    time.sleep(5.0)
    with ctx._lock:
        code_ready = ctx._2fa_code is not None
    if not code_ready:
        phone = _request_sms_code()
        ctx.set_sms_phone(phone)

    progress_interval_s = 60
    t0 = time.time()
    last_progress = t0
    deadline = t0 + 600.0
    while time.time() < deadline:
        ctx._2fa_event.wait(timeout=2.0)

        with ctx._lock:
            code_ready = ctx._2fa_code is not None

        if code_ready:
            break

        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            emit("info", "apple_signin",
                 f"Waiting for 2FA code from Tasker relay… ({elapsed:.0f}s)")
            last_progress = now
    else:
        raise TimeoutError("2FA code not supplied within 600 s")

    return RuntimeState.TYPING_2FA


def type_2fa(ctx: AutomationContext) -> RuntimeState:
    """Type the 2FA code into the VM and clear it from context.

    Uses ``qmp.type_text`` with a small per-character gap so the
    digit-entry boxes register each keystroke separately.
    """
    code = ctx._2fa_code or ""
    emit("info", "apple_signin", "Typing 2FA code")
    with ctx.qmp_lock:
        qmp.type_text(code, gap_s=0.15)
        time.sleep(0.5)
        qmp.send_keys(["ret"])
    ctx.clear_2fa()
    return RuntimeState.WAITING_SIGNED_IN


def wait_signed_in(ctx: AutomationContext) -> RuntimeState:
    """Wait for the iCloud sign-in to complete after 2FA.

    Polls the authoritative SSH check every 4 s.  Also checks for
    failure keywords each poll.  On success, writes the signed-in marker
    file so subsequent runs can skip credentials.

    Polls for up to 180 s.  Raises RuntimeError on failure or timeout.
    """
    deadline_s = 180
    poll_s = 4.0
    progress_interval_s = 30
    t0 = time.time()
    last_progress = t0
    emit("info", "apple_signin", "Waiting for sign-in to complete")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            screen_snippet = vm_ui.screen_text()[:80] if hasattr(vm_ui, 'screen_text') else ''
            emit("info", "apple_signin",
                 f"Still waiting for sign-in… ({elapsed:.0f}s) screen: {repr(screen_snippet)}")
            last_progress = now

        text = vm_ui.screen_text()

        if _screen_has_fail():
            raise RuntimeError("Apple rejected the 2FA code — sign-in failed")

        # After sign-in, macOS may show "Enter Mac Password" to set up iCloud
        # Keychain.  Enter the VM password and press Return.
        if "enter mac password" in text or "enter your mac password" in text:
            emit("info", "apple_signin",
                 "Enter Mac Password prompt — entering VM password")
            vm_ui.paste_text(ctx.vm_password)
            time.sleep(0.4)
            with ctx.qmp_lock:
                qmp.send_keys(["ret"])
            time.sleep(2.0)
            continue

        # macOS may also show "Enter iPhone Passcode" to import iCloud Keychain
        # from the paired phone.  We can't provide the phone passcode here, so
        # click "Don't Know Passcode?" / "Cancel" to skip Keychain import.
        if ("iphone" in text or "unlock" in text) and (
            "passcode" in text or "some icloud data" in text
        ):
            emit("info", "apple_signin",
                 "iPhone passcode prompt detected — cancelling Keychain import")
            if not vm_ui.click_text("don't", "know", tries=2):
                if not vm_ui.click_text("Cancel", tries=2):
                    with ctx.qmp_lock:
                        qmp.send_keys(["esc"])
            time.sleep(2.0)
            continue

        # LOGGEDIN_INDICATORS only appear in the logged-in Apple ID view —
        # require one alongside the plist check to avoid false positives.
        if any(kw in text for kw in LOGGEDIN_INDICATORS) and _is_signed_in():
            emit("info", "apple_signin", "iCloud sign-in confirmed")
            try:
                VM_ICLOUD_SIGNED_IN_MARKER.parent.mkdir(parents=True, exist_ok=True)
                VM_ICLOUD_SIGNED_IN_MARKER.write_text("1")
            except Exception:
                pass
            return RuntimeState.DISMISSING_POST_SIGNIN

        time.sleep(poll_s)

    raise RuntimeError(f"Sign-in did not complete within {deadline_s}s")
