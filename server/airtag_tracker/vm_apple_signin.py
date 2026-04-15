"""Drive Apple ID sign-in inside the macOS VM (Ventura 13).

State machine consumed by a worker thread. The browser kicks off the
flow via ``start()``, polls ``status()``, and when the state is
``awaiting_2fa`` supplies the code via ``submit_2fa()``. The worker
then resumes, types the code into the VM, waits for sign-in to
complete, enables Find My Mac, and triggers key extraction.

Driving strategy
----------------
All primitives live in :mod:`vm_ui` — URL-scheme navigation, OCR-bbox
clicks, clipboard paste, authoritative state polling. No hardcoded
pixel coords, no AppleScript (TCC blocks it), no QMP key typing for
passwords (US-layout dependent).
"""

from __future__ import annotations

import threading
import time
from typing import Literal

from . import apple_creds, qmp, vm, vm_ui
from .config import VM_ICLOUD_SIGNED_IN_MARKER
from .events import emit

State = Literal["idle", "running", "awaiting_2fa", "signed_in", "failed"]

_lock = threading.Lock()
_state: State = "idle"
_error: str | None = None
_2fa_code: str | None = None
_2fa_event = threading.Event()
_sms_event = threading.Event()
_sms_phone: str | None = None
_thread: threading.Thread | None = None

# URL schemes that open the Apple ID sign-in pane directly. The first
# one that Ventura honors wins — the scheme name changed across 13.x.
APPLE_ID_URLS = (
    ("com.apple.systempreferences.AppleIDSettings", None),
    ("com.apple.preferences.AppleIDPrefPane", None),
)

SIGNIN_PANE_KEYWORDS = ("one account for everything", "apple id", "sign in")
PASSWORD_PROMPT_KEYWORDS = ("password",)
TWOFA_KEYWORDS = ("verification code", "two-factor", "enter the code", "trust this")
SIGNIN_FAIL_KEYWORDS = (
    "incorrect", "could not sign in", "try again",
    "verification failed", "cannot verify",
)


def status() -> dict:
    with _lock:
        state = _state
        error = _error
        phone = _sms_phone
    return {
        "state": state,
        "error": error,
        "sms_phone": phone,
        "signed_in_cached": VM_ICLOUD_SIGNED_IN_MARKER.exists(),
    }


def request_sms() -> dict:
    """Ask the worker to drive the 'Didn't receive code → SMS' flow."""
    with _lock:
        if _state != "awaiting_2fa":
            raise RuntimeError(f"not awaiting 2fa (state={_state})")
    _sms_event.set()
    return {"requested": True}


def start(email: str | None = None, password: str | None = None) -> dict:
    global _state, _error, _thread, _2fa_code, _sms_phone
    if email and password:
        apple_creds.set_(email, password)
    if not vm.is_running():
        raise RuntimeError("VM is not running")
    # Creds are only required when we actually have to drive a fresh
    # sign-in. If the VM is already signed into iCloud (marker or live
    # check), the worker just runs the post-signin tasks (dismiss
    # prompts, enable Find My Mac, trigger extraction) and doesn't need
    # a password.
    creds = apple_creds.get()
    if not creds:
        already = VM_ICLOUD_SIGNED_IN_MARKER.exists()
        if not already:
            try:
                already = _is_signed_in()
            except Exception:
                already = False
        if not already:
            raise RuntimeError("needs_password")
    with _lock:
        if _state in ("running", "awaiting_2fa"):
            return {"state": _state}
        _state = "running"
        _error = None
        _2fa_code = None
        _sms_phone = None
        _2fa_event.clear()
        _sms_event.clear()
    _thread = threading.Thread(target=_worker, daemon=True, name="apple-signin")
    _thread.start()
    return {"state": _state}


def submit_2fa(code: str) -> dict:
    global _2fa_code
    code = (code or "").strip()
    if not code:
        raise RuntimeError("empty 2fa code")
    with _lock:
        if _state != "awaiting_2fa":
            raise RuntimeError(f"not awaiting 2fa (state={_state})")
        _2fa_code = code
    _2fa_event.set()
    return {"state": "running"}


def _set_state(new: State, error: str | None = None) -> None:
    global _state, _error
    with _lock:
        _state = new
        _error = error


# ---------------------------------------------------------------------------
# Readiness / state
# ---------------------------------------------------------------------------

def _wait_ssh(deadline_s: int = 120) -> None:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        r = vm_ui.ssh("echo ok", timeout=8)
        if r.returncode == 0 and "ok" in r.stdout:
            return
        time.sleep(3)
    raise RuntimeError("VM SSH never came up")


def _wait_desktop(deadline_s: int = 300) -> None:
    """`open` requires a GUI session — poll for Dock.app, not just sshd."""
    emit("info", "vm", "Apple ID sign-in: waiting for desktop (post-login)")
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        r = vm_ui.ssh("pgrep -x Dock >/dev/null && echo up", timeout=8)
        if r.returncode == 0 and "up" in r.stdout:
            return
        time.sleep(4)
    raise RuntimeError("desktop never came up")


def _is_signed_in() -> bool:
    r = vm_ui.ssh(
        "defaults read MobileMeAccounts Accounts 2>/dev/null | grep -c AccountID",
        timeout=10,
    )
    try:
        return r.returncode == 0 and int(r.stdout.strip() or "0") > 0
    except ValueError:
        return False


def _is_find_my_mac_on() -> bool:
    """Authoritative FMM check via `defaults`. Avoids OCR entirely."""
    # In MobileMeAccounts plist, `Enabled = 1;` precedes `Name = "FIND_MY_MAC";`
    # by one line inside each service dict.
    r = vm_ui.ssh(
        "defaults read MobileMeAccounts Accounts 2>/dev/null "
        "| grep -B1 FIND_MY_MAC | grep -c 'Enabled = 1'",
        timeout=10,
    )
    try:
        return int(r.stdout.strip() or "0") > 0
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Sign-in flow
# ---------------------------------------------------------------------------

APPLE_ID_LANDED_KEYWORDS = SIGNIN_PANE_KEYWORDS + (
    # Post-signin account view shows these instead of the sign-in sheet.
    "icloud", "family sharing", "media & purchases", "sign out",
)


def _open_apple_id_pane() -> None:
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


def _focus_email_field() -> None:
    """URL-scheme nav leaves focus in sidebar search.

    cmd-a + delete clears stray chars, tab advances into the sheet
    where Ventura lands on the first text field (email).
    """
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "a"])
        time.sleep(0.2)
        c.send_keys(["delete"])
        time.sleep(0.2)
        c.send_keys(["tab"])
        time.sleep(0.5)


def _screen_has_fail() -> bool:
    text = vm_ui.screen_text()
    return any(kw in text for kw in SIGNIN_FAIL_KEYWORDS)


def _wait_for_keywords(keywords: tuple[str, ...], deadline_s: int, poll_s: float = 2.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if vm_ui.wait_for_text(keywords, deadline_s=int(poll_s) + 1, poll_s=poll_s):
            return True
        if _screen_has_fail():
            raise RuntimeError("Apple rejected credentials (check password)")
        if time.time() - t0 >= deadline_s:
            break
    return False


def _type_credentials(email: str, password: str) -> None:
    for attempt in (1, 2):
        _focus_email_field()
        vm_ui.paste_text(email)
        time.sleep(0.4)
        with qmp.qmp() as c:
            c.send_keys(["ret"])
        emit("info", "vm", "Apple ID sign-in: email submitted, waiting for password prompt")
        if _wait_for_keywords(PASSWORD_PROMPT_KEYWORDS, deadline_s=20):
            break
        if attempt == 2:
            raise RuntimeError("password prompt never appeared after typing email")
        emit("warning", "vm", "Password prompt missing — retrying email entry")
        _open_apple_id_pane()

    time.sleep(0.4)
    vm_ui.paste_text(password)
    time.sleep(0.4)
    with qmp.qmp() as c:
        c.send_keys(["ret"])
    vm_ui.wipe_clipboard()


def _wait_for_2fa_or_signed_in(deadline_s: int = 180) -> str:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if _is_signed_in():
            return "signed_in"
        text = vm_ui.screen_text()
        if any(kw in text for kw in TWOFA_KEYWORDS):
            return "2fa"
        if any(kw in text for kw in SIGNIN_FAIL_KEYWORDS):
            raise RuntimeError("Apple rejected credentials (check password)")
        time.sleep(4)
    raise RuntimeError("timed out waiting for 2FA or signed-in state")


def _extract_masked_phone() -> str | None:
    """Scan OCR text for an Apple-style masked phone number.

    Apple shows things like '+49 •••• ••12 34' or '(•••) •••-1234'
    on the SMS-sent sheet. OCR often turns •/● into '.', '-', '*', or
    just drops them, so match the *tail digits* with any garbage in
    between a leading '+' or digit.
    """
    import re
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

    Returns the masked phone OCR'd from the 'code sent' sheet, or None
    if any step failed (caller logs a warning but keeps waiting for a code)."""
    emit("info", "vm", "Apple ID sign-in: requesting SMS code")
    if not vm_ui.click_text("receive", "code", tries=3):
        emit("warning", "vm", "SMS flow: 'Didn't receive a verification code?' not found")
        return None
    time.sleep(1.5)
    if not vm_ui.click_text("get", "devices", tries=3):
        emit("warning", "vm", "SMS flow: 'Can't get to your trusted devices?' not found")
        return None
    time.sleep(1.5)
    if not vm_ui.click_text("Send", "Code", tries=3):
        emit("warning", "vm", "SMS flow: 'Send Code' button not found")
        return None
    time.sleep(2.5)
    phone = _extract_masked_phone()
    if phone:
        emit("info", "vm", f"Apple ID sign-in: SMS sent to {phone}")
    return phone


def _type_2fa(code: str) -> None:
    with qmp.qmp() as c:
        c.type_text(code, gap_s=0.15)
        time.sleep(0.5)
        c.send_keys(["ret"])


def _wait_signed_in(deadline_s: int = 180) -> None:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if _is_signed_in():
            return
        if _screen_has_fail():
            raise RuntimeError("Apple rejected 2FA code")
        time.sleep(4)
    raise RuntimeError("sign-in never completed")


# ---------------------------------------------------------------------------
# Find My Mac
# ---------------------------------------------------------------------------

POST_SIGNIN_DISMISSIBLE = (
    # Passcode/"Enter your Mac password" sheet — can always be Later/Cancelled.
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


def _dismiss_post_signin_prompts(deadline_s: int = 45) -> None:
    """Dismiss modal sheets that pop after a fresh iCloud sign-in.

    Strategy: for up to ``deadline_s``, poll OCR; when a known
    dismissible-sheet keyword appears, prefer clicking 'Later' /
    'Not Now' / 'Cancel' (non-destructive), else press Escape. Exit
    once two consecutive polls see no matches.
    """
    emit("info", "vm", "Dismissing post-signin dialogs")
    clean_rounds = 0
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        text = vm_ui.screen_text()
        matched = [kw for kw in POST_SIGNIN_DISMISSIBLE if kw in text]
        if not matched:
            clean_rounds += 1
            if clean_rounds >= 2:
                return
            time.sleep(2)
            continue
        clean_rounds = 0
        # Prefer a dedicated dismiss button (non-destructive) before Escape.
        clicked = False
        for label_pair in (("Later",), ("Not", "Now"), ("Cancel",), ("Don't", "Merge")):
            if vm_ui.click_text(*label_pair, tries=1, settle_s=1.0):
                clicked = True
                break
        if not clicked:
            with qmp.qmp() as c:
                c.send_keys(["esc"])
            time.sleep(1.0)
        time.sleep(1.5)
    emit("warning", "vm", "Post-signin dialogs not fully cleared within deadline")


def _enable_find_my_mac(deadline_s: int = 60) -> None:
    """Navigate to iCloud → Find My Mac and toggle it on.

    Ventura nests Find My Mac inside the iCloud feature list. No URL
    anchor reaches it directly (we verified ?FindMyMac / ?FindMy don't
    drill down), so this is URL to iCloud pane + two OCR-bbox clicks.
    Returns silently if already on (authoritative check via `defaults`).
    """
    if _is_find_my_mac_on():
        emit("info", "vm", "Find My Mac already enabled")
        return
    emit("info", "vm", "Enabling Find My Mac")
    # The correct Ventura pane is com.apple.systempreferences.AppleIDSettings;
    # the old com.apple.preferences.AppleIDPrefPane bundle doesn't navigate
    # and leaves Settings parked on whatever it was last on.
    _open_apple_id_pane()
    # Open iCloud subpane by clicking the row in the account overview.
    if not vm_ui.click_text("iCloud", tries=3):
        raise RuntimeError("could not locate 'iCloud' row in Apple ID pane")
    time.sleep(1.5)
    if not vm_ui.click_text("Show", "All", tries=3):
        emit("warning", "vm", "Could not click 'Show All' — Find My row may still be visible")
    time.sleep(1.0)
    if not vm_ui.click_text("Find", "Mac", tries=3):
        raise RuntimeError("could not locate 'Find My Mac' row")
    time.sleep(1.5)
    vm_ui.click_text("Turn", "On", tries=2)
    time.sleep(1.0)
    # Location permission prompt — default button is "Allow"/"OK".
    with qmp.qmp() as c:
        c.send_keys(["ret"])
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if _is_find_my_mac_on():
            emit("info", "vm", "Find My Mac enabled")
            return
        time.sleep(3)
    raise RuntimeError("Find My Mac never turned on")


def _close_settings_and_findmy() -> None:
    """Close System Settings and FindMy.app windows left behind.

    FindMy.app may have been launched by a misdirected OCR click on the
    Dock tooltip before chrome-exclusion was in place. killall is the
    blunt but reliable option — neither app has unsaved state."""
    vm_ui.ssh(
        "killall 'System Settings' 2>/dev/null; "
        "killall 'FindMy' 2>/dev/null; "
        "killall 'Find My' 2>/dev/null; true",
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker() -> None:
    try:
        creds = apple_creds.get()

        emit("info", "vm", "Apple ID sign-in: waiting for VM SSH")
        _wait_ssh()
        _wait_desktop()

        if not _is_signed_in():
            if not creds:
                raise RuntimeError("credentials vanished")
            email, password = creds
            emit("info", "vm", "Apple ID sign-in: opening System Settings pane")
            _open_apple_id_pane()

            emit("info", "vm", "Apple ID sign-in: typing credentials")
            _type_credentials(email, password)

            emit("info", "vm", "Apple ID sign-in: waiting for 2FA or signed-in")
            outcome = _wait_for_2fa_or_signed_in()

            if outcome == "2fa":
                global _sms_phone
                _set_state("awaiting_2fa")
                emit("info", "vm", "Apple ID sign-in: awaiting 2FA code from browser")
                deadline = time.time() + 600
                while time.time() < deadline:
                    if _2fa_event.wait(timeout=1.0):
                        break
                    if _sms_event.is_set():
                        _sms_event.clear()
                        phone = _request_sms_code()
                        with _lock:
                            _sms_phone = phone
                else:
                    raise RuntimeError("2FA code not supplied within 10 min")
                _set_state("running")
                emit("info", "vm", "Apple ID sign-in: typing 2FA code")
                _type_2fa(_2fa_code or "")
                _wait_signed_in()
        else:
            emit("info", "vm", "VM already signed into iCloud")

        try:
            _dismiss_post_signin_prompts()
        except Exception as e:
            emit("warning", "vm", f"post-signin dismiss failed: {e}")

        try:
            _enable_find_my_mac()
        except Exception as e:
            emit("warning", "vm", f"Find My Mac enable failed: {e}")

        # Close any app windows the worker opened or that were left over
        # from a bad earlier run (System Settings, and FindMy.app if a
        # misdirected OCR click launched it from the Dock). Leaves
        # Finder/Dock alone.
        try:
            _close_settings_and_findmy()
        except Exception as e:
            emit("warning", "vm", f"post-signin cleanup failed: {e}")

        _set_state("signed_in")
        try:
            VM_ICLOUD_SIGNED_IN_MARKER.parent.mkdir(parents=True, exist_ok=True)
            VM_ICLOUD_SIGNED_IN_MARKER.write_text("1")
        except Exception:
            pass
        apple_creds.clear()
        emit("info", "vm", "Apple ID sign-in complete — triggering key extraction")
        try:
            vm.trigger_key_extraction()
        except Exception as e:
            emit("warning", "vm", f"Auto key extraction failed to start: {e}")
    except Exception as e:
        _set_state("failed", str(e))
        emit("error", "vm", f"Apple ID sign-in failed: {e}")
