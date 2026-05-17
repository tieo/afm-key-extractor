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
from ...config import VM_ICLOUD_SIGNED_IN_MARKER
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

PASSWORD_PROMPT_KEYWORDS = ("password",)

TWOFA_KEYWORDS = (
    "verification code", "two-factor", "enter the code", "trust this",
)

SIGNIN_FAIL_KEYWORDS = (
    "incorrect", "could not sign in", "try again",
    "verification failed", "cannot verify",
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
    OCR often turns •/● into '.', '-', '*' or drops them, so we match
    the tail digits with any noise in between.
    """
    text = vm_ui.screen_text()
    for pat in (
        r"\+\d[\d\s\-\.\*•●x]{3,}\d{2,4}",
        r"[\*•●\.x]{2,}[\s\-]?\d{2,4}",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()
    return None


def _request_sms_code() -> str | None:
    """Drive the three-click 'didn't receive → trusted devices → Send Code' path.

    Returns the masked phone number OCR'd from the confirmation sheet,
    or None if any click failed.
    """
    emit("info", "apple_signin", "Requesting SMS verification code")
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
    if not vm_ui.click_text("Send", "Code", tries=3):
        emit("warning", "apple_signin", "SMS flow: 'Send Code' button not found")
        return None
    time.sleep(2.5)
    phone = _extract_masked_phone()
    if phone:
        emit("info", "apple_signin", f"SMS sent to {phone}")
    return phone


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
                emit("info", "apple_signin", "Already signed into iCloud — skipping credentials")
                return RuntimeState.DISMISSING_POST_SIGNIN

            emit("info", "apple_signin", "Apple ID pane open — ready to enter credentials")
            return RuntimeState.TYPING_CREDENTIALS

        last_err = f"{bundle} opened but Apple ID pane never rendered"

    raise RuntimeError(f"could not open Apple ID pane: {last_err[:200]}")


def type_credentials(ctx: AutomationContext) -> RuntimeState:
    """Focus the email field, enter Apple ID email and password.

    Uses clipboard paste for both values so special characters are not
    mangled by QMP keystroke layout mapping.  Retries once if the
    password prompt does not appear after the email is submitted.

    Raises RuntimeError if the password prompt never appears after two
    attempts.
    """
    emit("info", "apple_signin", "Entering Apple ID credentials")

    for attempt in (1, 2):
        # Focus email field: cmd-a clears sidebar search, tab advances into
        # the sign-in sheet where Ventura lands on the first text field.
        with ctx.qmp_lock:
            with qmp.qmp() as c:
                c.send_chord(["meta_l", "a"])
            time.sleep(0.2)
            with qmp.qmp() as c:
                c.send_keys(["delete"])
            time.sleep(0.2)
            with qmp.qmp() as c:
                c.send_keys(["tab"])
            time.sleep(0.5)

        vm_ui.paste_text(ctx.apple_email)
        time.sleep(0.4)
        with ctx.qmp_lock:
            with qmp.qmp() as c:
                c.send_keys(["ret"])

        emit("info", "apple_signin", "Email submitted — waiting for password prompt")
        if vm_ui.wait_for_text(PASSWORD_PROMPT_KEYWORDS, deadline_s=20):
            break
        if attempt == 2:
            raise RuntimeError("Password prompt never appeared after typing Apple ID email")
        emit("warning", "apple_signin",
             "Password prompt missing after attempt 1 — retrying")
        _open_apple_id_pane()

    time.sleep(0.4)
    vm_ui.paste_text(ctx.apple_password)
    time.sleep(0.4)
    with ctx.qmp_lock:
        with qmp.qmp() as c:
            c.send_keys(["ret"])
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
    t0 = time.time()
    emit("info", "apple_signin", "Waiting for Apple response (2FA or signed-in)")
    while time.time() - t0 < deadline_s:
        if _is_signed_in():
            emit("info", "apple_signin", "Signed in without 2FA")
            return RuntimeState.DISMISSING_POST_SIGNIN

        text = vm_ui.screen_text()
        if any(kw in text for kw in TWOFA_KEYWORDS):
            emit("info", "apple_signin", "2FA prompt detected")
            return RuntimeState.AWAITING_2FA

        if any(kw in text for kw in SIGNIN_FAIL_KEYWORDS):
            raise RuntimeError(
                "Apple rejected credentials — check Apple ID email and password"
            )

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

    # Poll for SMS request while waiting for the 2FA event.
    # ctx.wait_for_2fa blocks internally on a threading.Event; we want to
    # interleave SMS checks, so we use short waits in a loop instead.
    deadline = time.time() + 600.0
    while time.time() < deadline:
        # Short wait — lets the event fire quickly when code is delivered.
        ctx._2fa_event.wait(timeout=2.0)

        # If code is already in, stop polling.
        with ctx._lock:
            code_ready = ctx._2fa_code is not None

        if code_ready:
            break

        # Check whether the user requested an SMS code while we waited.
        if ctx.sms_was_requested():
            phone = _request_sms_code()
            ctx.set_sms_phone(phone)
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
        with qmp.qmp() as c:
            c.type_text(code, gap_s=0.15)
        time.sleep(0.5)
        with qmp.qmp() as c:
            c.send_keys(["ret"])
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
    t0 = time.time()
    emit("info", "apple_signin", "Waiting for sign-in to complete")
    while time.time() - t0 < deadline_s:
        if _is_signed_in():
            emit("info", "apple_signin", "iCloud sign-in confirmed")
            try:
                VM_ICLOUD_SIGNED_IN_MARKER.parent.mkdir(parents=True, exist_ok=True)
                VM_ICLOUD_SIGNED_IN_MARKER.write_text("1")
            except Exception:
                pass
            return RuntimeState.DISMISSING_POST_SIGNIN

        if _screen_has_fail():
            raise RuntimeError("Apple rejected the 2FA code — sign-in failed")

        time.sleep(poll_s)

    raise RuntimeError(f"Sign-in did not complete within {deadline_s}s")
