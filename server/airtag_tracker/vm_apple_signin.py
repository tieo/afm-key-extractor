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
SIGNIN_FAIL_KEYWORDS = ("incorrect", "could not sign in", "try again")


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
    """Navigate System Settings to the Apple ID sign-in pane."""
    # Close anything stuck on the screen, then fire the URL scheme.
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "q"]); time.sleep(0.5)
    for url in APPLE_ID_URLS:
        r = _ssh(f"open {url!r}", timeout=15)
        if r.returncode == 0:
            break
    else:
        raise RuntimeError("could not open Apple ID pane via URL scheme")
    # Give System Settings time to actually render the sheet.
    time.sleep(6.0)


def _type_credentials(email: str, password: str) -> None:
    """Type the email into the focused field, submit, then the password.

    Ventura's Apple ID sign-in sheet focuses the email field on open.
    If focus is wrong, Cmd-Tab-ing back and typing still works because
    Return submits the email form, which then autofocuses the password
    field.
    """
    with qmp.qmp() as c:
        c.type_text(email, gap_s=0.08)
        time.sleep(0.4)
        c.send_keys(["ret"]); time.sleep(4.0)
        c.type_text(password, gap_s=0.08)
        time.sleep(0.4)
        c.send_keys(["ret"])


def _wait_for_2fa_or_signed_in(deadline_s: int = 90) -> str:
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
