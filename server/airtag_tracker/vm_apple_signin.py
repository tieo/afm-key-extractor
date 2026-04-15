"""Drive Apple ID sign-in inside the macOS VM (Ventura 13).

State machine consumed by a worker thread. The browser kicks off the
flow via ``start()``, polls ``status()``, and when the state is
``awaiting_2fa`` supplies the code via ``submit_2fa()``. The worker
then resumes, types the code into the VM, waits for sign-in to
complete, and (on success) triggers key extraction automatically.

Driving strategy
----------------
System Settings' accessibility tree on Ventura is unreliable for
scripted clicks (many fields are SwiftUI and don't expose stable AX
roles). So we use the same pattern that already works for
``key_extraction._enable_remote_login``:

* SSH runs an `open "x-apple.systempreferences:..."` URL to navigate
  directly to the Apple ID sign-in pane — no hunting through menus.
* QMP send-key types into whatever field has focus (Ventura focuses
  the email field automatically).
* SSH polls ``defaults read MobileMeAccounts`` for the signed-in
  state — that's the authoritative signal.
* OCR on a screendump detects the 2FA sheet so we know when to stop
  and wait for a code from the browser.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import Literal

from . import apple_creds, qmp, vm, vm_password
from .config import VM_ICLOUD_SIGNED_IN_MARKER
from .events import emit

State = Literal["idle", "running", "awaiting_2fa", "signed_in", "failed"]

_lock = threading.Lock()
_state: State = "idle"
_error: str | None = None
_2fa_code: str | None = None
_2fa_event = threading.Event()
_thread: threading.Thread | None = None

VM_USER = "airtag"
VM_HOST = "localhost"
VM_PORT = 2222

# URL schemes that open the Apple ID sign-in pane directly. The first
# one that Ventura honors wins — the exact scheme name changed across
# 13.0/13.x point releases.
APPLE_ID_URLS = (
    "x-apple.systempreferences:com.apple.systempreferences.AppleIDSettings",
    "x-apple.systempreferences:com.apple.preferences.AppleIDPrefPane",
)

TWOFA_KEYWORDS = ("verification code", "two-factor", "enter the code", "trust this")
SIGNIN_FAIL_KEYWORDS = (
    "incorrect", "could not sign in", "try again",
    "verification failed", "cannot verify",
)


def status() -> dict:
    with _lock:
        state = _state
        error = _error
    return {
        "state": state,
        "error": error,
        "signed_in_cached": VM_ICLOUD_SIGNED_IN_MARKER.exists(),
    }


def start(email: str | None = None, password: str | None = None) -> dict:
    """Kick off the sign-in worker.

    If ``email`` and ``password`` are provided, they're stashed into
    ``apple_creds`` first. Otherwise uses whatever is already cached
    (populated by the web login flow)."""
    global _state, _error, _thread, _2fa_code
    if email and password:
        apple_creds.set_(email, password)
    creds = apple_creds.get()
    if not creds:
        raise RuntimeError("needs_password")
    if not vm.is_running():
        raise RuntimeError("VM is not running")
    with _lock:
        if _state in ("running", "awaiting_2fa"):
            return {"state": _state}
        _state = "running"
        _error = None
        _2fa_code = None
        _2fa_event.clear()
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
# SSH + OCR helpers
# ---------------------------------------------------------------------------

def _ssh(cmd: str, timeout: int = 30):
    import subprocess as sp
    pw = vm_password.get() or ""
    return sp.run(
        [
            "sshpass", "-p", pw,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            "-p", str(VM_PORT),
            f"{VM_USER}@{VM_HOST}",
            cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def _wait_ssh(deadline_s: int = 120) -> None:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        r = _ssh("echo ok", timeout=8)
        if r.returncode == 0 and "ok" in r.stdout:
            return
        time.sleep(3)
    raise RuntimeError("VM SSH never came up")


def _wait_desktop(deadline_s: int = 300) -> None:
    """Wait until a user is logged in to the desktop.

    `open` (and System Settings) require an active GUI session. sshd
    runs before login, so SSH reachability isn't enough — we need
    Dock.app to be running, which only happens post-login.
    """
    emit("info", "vm", "Apple ID sign-in: waiting for desktop (post-login)")
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        r = _ssh("pgrep -x Dock >/dev/null && echo up", timeout=8)
        if r.returncode == 0 and "up" in r.stdout:
            return
        time.sleep(4)
    raise RuntimeError("desktop never came up (login auto-type may have failed)")


def _is_signed_in() -> bool:
    r = _ssh(
        "defaults read MobileMeAccounts Accounts 2>/dev/null "
        "| grep -c AccountID",
        timeout=10,
    )
    try:
        return r.returncode == 0 and int(r.stdout.strip() or "0") > 0
    except ValueError:
        return False


def _ocr_screen() -> str:
    """Screendump the VM and OCR the result. Returns '' on failure."""
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError:
        return ""
    with tempfile.TemporaryDirectory() as td:
        shot = Path(td) / "frame.ppm"
        try:
            qmp.screendump(str(shot))
        except Exception as e:
            emit("warning", "vm", f"signin screendump failed: {e}")
            return ""
        if not shot.exists() or shot.stat().st_size == 0:
            return ""
        try:
            with Image.open(shot) as img:
                inverted = ImageOps.invert(img.convert("L"))
                return pytesseract.image_to_string(
                    inverted, config="--psm 6"
                ).lower()
        except Exception as e:
            emit("warning", "vm", f"signin OCR failed: {e}")
            return ""


def _screen_matches(keywords: tuple[str, ...]) -> bool:
    text = _ocr_screen()
    return any(kw in text for kw in keywords)


# ---------------------------------------------------------------------------
# Driving macOS
# ---------------------------------------------------------------------------

def _open_apple_id_pane() -> None:
    """Navigate System Settings to the Apple ID sign-in pane.

    Uses `killall` instead of osascript-quit because TCC blocks any
    AppleScript automation with a consent prompt. After opening we
    verify via OCR that the sign-in sheet actually appeared."""
    _ssh("killall 'System Settings' 2>/dev/null; true", timeout=10)
    time.sleep(1.5)
    last_err = ""
    for url in APPLE_ID_URLS:
        r = _ssh(f"open {url!r} 2>&1", timeout=15)
        if r.returncode != 0:
            last_err = (r.stdout + r.stderr).strip()
            continue
        # Wait up to 20s for the sign-in sheet to render.
        if _wait_for_keywords(SIGNIN_PANE_KEYWORDS, deadline_s=20):
            return
        last_err = f"URL {url} opened but sign-in sheet never rendered"
    raise RuntimeError(f"could not open Apple ID pane: {last_err[:200]}")


SIGNIN_PANE_KEYWORDS = ("one account for everything", "apple id", "sign in")
PASSWORD_PROMPT_KEYWORDS = ("password",)


def _wait_for_keywords(
    keywords: tuple[str, ...],
    deadline_s: int,
    poll_s: float = 2.0,
) -> bool:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if _screen_matches(keywords):
            return True
        if _screen_matches(SIGNIN_FAIL_KEYWORDS):
            raise RuntimeError("Apple rejected credentials (check password)")
        time.sleep(poll_s)
    return False


def _focus_email_field() -> None:
    """Move keyboard focus from sidebar search → email field.

    URL-scheme navigation lands focus in the sidebar search. AppleScript
    UI-scripting to re-focus is blocked by macOS TCC (unattended consent
    prompts stall osascript indefinitely), so this is keyboard-only:
    cmd-a + delete clears any stray characters in the search field,
    then tab advances focus into the main sheet where Ventura
    auto-lands on the first text field (email).
    """
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "a"])
        time.sleep(0.2)
        c.send_keys(["delete"])
        time.sleep(0.2)
        c.send_keys(["tab"])
        time.sleep(0.5)


def _type_credentials(email: str, password: str) -> None:
    """Focus email field, type email → Return → verify password sheet → type password → Return.

    Each step verifies its effect via OCR; on mismatch we retry once
    before giving up with a diagnostic.
    """
    for attempt in (1, 2):
        _focus_email_field()
        with qmp.qmp() as c:
            c.type_text(email, gap_s=0.08)
            time.sleep(0.6)
            c.send_keys(["ret"])
        emit("info", "vm", "Apple ID sign-in: email submitted, waiting for password prompt")
        if _wait_for_keywords(PASSWORD_PROMPT_KEYWORDS, deadline_s=20):
            break
        if attempt == 2:
            raise RuntimeError("password prompt never appeared after typing email")
        emit("warning", "vm", "Password prompt missing — retrying email entry")
        # Re-open the pane so the form is in a known state.
        _open_apple_id_pane()

    time.sleep(0.4)
    with qmp.qmp() as c:
        c.type_text(password, gap_s=0.08)
        time.sleep(0.5)
        c.send_keys(["ret"])


def _wait_for_2fa_or_signed_in(deadline_s: int = 180) -> str:
    """Return ``'2fa'`` if the 2FA sheet appeared, ``'signed_in'`` if
    the account is active, or raise if neither happened in time."""
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if _is_signed_in():
            return "signed_in"
        if _screen_matches(TWOFA_KEYWORDS):
            return "2fa"
        if _screen_matches(SIGNIN_FAIL_KEYWORDS):
            raise RuntimeError("Apple rejected credentials (check password)")
        time.sleep(4)
    raise RuntimeError("timed out waiting for 2FA or signed-in state")


def _type_2fa(code: str) -> None:
    # The 2FA sheet focuses the first digit field; typing the digits
    # advances through the six boxes automatically.
    with qmp.qmp() as c:
        c.type_text(code, gap_s=0.15)
        time.sleep(0.5)
        c.send_keys(["ret"])


def _wait_signed_in(deadline_s: int = 180) -> None:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if _is_signed_in():
            return
        if _screen_matches(SIGNIN_FAIL_KEYWORDS):
            raise RuntimeError("Apple rejected 2FA code")
        time.sleep(4)
    raise RuntimeError("sign-in never completed")


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker() -> None:
    try:
        creds = apple_creds.get()
        if not creds:
            raise RuntimeError("credentials vanished")
        email, password = creds

        emit("info", "vm", "Apple ID sign-in: waiting for VM SSH")
        _wait_ssh()
        _wait_desktop()

        if _is_signed_in():
            emit("info", "vm", "VM already signed into iCloud — skipping")
            _set_state("signed_in")
            apple_creds.clear()
            try:
                vm.trigger_key_extraction()
            except Exception as e:
                emit("warning", "vm", f"Key extraction trigger failed: {e}")
            return

        emit("info", "vm", "Apple ID sign-in: opening System Settings pane")
        _open_apple_id_pane()

        emit("info", "vm", "Apple ID sign-in: typing credentials")
        _type_credentials(email, password)

        emit("info", "vm", "Apple ID sign-in: waiting for 2FA or signed-in")
        outcome = _wait_for_2fa_or_signed_in()

        if outcome == "2fa":
            _set_state("awaiting_2fa")
            emit("info", "vm", "Apple ID sign-in: awaiting 2FA code from browser")
            if not _2fa_event.wait(timeout=300):
                raise RuntimeError("2FA code not supplied within 5 min")
            _set_state("running")
            emit("info", "vm", "Apple ID sign-in: typing 2FA code")
            _type_2fa(_2fa_code or "")
            _wait_signed_in()

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
