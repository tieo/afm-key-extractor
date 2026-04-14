"""Drive Apple ID sign-in inside the macOS VM.

State machine consumed by a worker thread. The browser kicks off the
flow via ``start()``, polls ``status()``, and when the state is
``awaiting_2fa`` supplies the code via ``submit_2fa()``. The worker
then resumes, types the code into the VM, waits for sign-in to
complete, and (on success) triggers key extraction automatically.

We drive macOS via ``ssh -p 2222`` + ``osascript``. SSH is already set
up for the extract-keys flow, so no new plumbing is needed. osascript
UI scripting is more stable than OCR+clicks for the Apple ID pane —
System Events gives us accessibility-tree access to the real buttons
and fields, not pixels.
"""

from __future__ import annotations

import shlex
import subprocess as sp
import threading
import time
from typing import Literal

from . import apple_creds, vm, vm_password
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


def status() -> dict:
    with _lock:
        return {"state": _state, "error": _error}


def start() -> dict:
    """Kick off the sign-in worker. No-op if one is already running."""
    global _state, _error, _thread, _2fa_code
    creds = apple_creds.get()
    if not creds:
        raise RuntimeError(
            "No Apple credentials cached. Sign in via the web form first."
        )
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


def _ssh(cmd: str, timeout: int = 30) -> sp.CompletedProcess:
    """Run ``cmd`` on the VM via sshpass. Returns the CompletedProcess."""
    pw = vm_password.get() or ""
    full = [
        "sshpass", "-p", pw,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        "-p", str(VM_PORT),
        f"{VM_USER}@{VM_HOST}",
        cmd,
    ]
    return sp.run(full, capture_output=True, text=True, timeout=timeout)


def _osascript(script: str, timeout: int = 30) -> str:
    """Run ``script`` on the VM via ``osascript -e`` and return stdout."""
    # osascript -e takes one line per -e; we base64-encode and pipe to
    # sidestep quoting headaches for multi-line scripts.
    import base64
    b64 = base64.b64encode(script.encode()).decode()
    cmd = f"echo {shlex.quote(b64)} | base64 -D | osascript -"
    r = _ssh(cmd, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"osascript failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _worker() -> None:
    try:
        creds = apple_creds.get()
        if not creds:
            raise RuntimeError("credentials vanished")
        email, password = creds

        emit("info", "vm", "Apple ID sign-in: waiting for VM SSH")
        _wait_ssh()

        emit("info", "vm", "Apple ID sign-in: opening System Settings")
        _open_apple_id_pane()

        emit("info", "vm", "Apple ID sign-in: typing credentials")
        _type_credentials(email, password)

        emit("info", "vm", "Apple ID sign-in: waiting for 2FA prompt")
        if _wait_for_2fa_prompt():
            _set_state("awaiting_2fa")
            emit("info", "vm", "Apple ID sign-in: awaiting 2FA code from browser")
            if not _2fa_event.wait(timeout=300):
                raise RuntimeError("2FA code not supplied within 5 min")
            _set_state("running")
            emit("info", "vm", "Apple ID sign-in: typing 2FA code")
            _type_2fa(_2fa_code or "")

        emit("info", "vm", "Apple ID sign-in: waiting for signed-in state")
        _wait_signed_in()

        _set_state("signed_in")
        apple_creds.clear()
        emit("info", "vm", "Apple ID sign-in complete — triggering key extraction")
        try:
            vm.trigger_key_extraction()
        except Exception as e:
            emit("warning", "vm", f"Auto key extraction failed to start: {e}")
    except Exception as e:
        _set_state("failed", str(e))
        emit("error", "vm", f"Apple ID sign-in failed: {e}")


# ---------------------------------------------------------------------------
# Driving macOS — the actual osascript state machine.
#
# These are intentionally small and separate so we can iterate each
# against a live VM. The exact keystroke/UI-script sequence for
# Ventura's Apple ID pane is fragile; getting it right needs real-VM
# testing, not staring at docs.
# ---------------------------------------------------------------------------

def _wait_ssh(deadline_s: int = 120) -> None:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        r = _ssh("echo ok", timeout=8)
        if r.returncode == 0 and "ok" in r.stdout:
            return
        time.sleep(3)
    raise RuntimeError("VM SSH never came up")


def _open_apple_id_pane() -> None:
    # TODO: replace with the exact Ventura System Settings AppleScript.
    # Placeholder: open System Settings and click the Apple ID row.
    _osascript(
        'tell application "System Settings" to activate\n'
        'delay 2\n'
        'tell application "System Events"\n'
        '  tell process "System Settings"\n'
        '    click button "Sign In" of window 1\n'
        '  end tell\n'
        'end tell\n'
    )


def _type_credentials(email: str, password: str) -> None:
    # TODO: real implementation — click the email field, set value, click
    # Continue, wait for password field, set value, click Continue.
    _osascript(
        f'tell application "System Events"\n'
        f'  keystroke "{email}"\n'
        f'  key code 36\n'  # Return
        f'  delay 2\n'
        f'  keystroke "{password}"\n'
        f'  key code 36\n'
        f'end tell\n'
    )


def _wait_for_2fa_prompt(deadline_s: int = 60) -> bool:
    # TODO: poll for the "Enter Verification Code" sheet via
    # System Events. Return True if it appeared, False if sign-in
    # completed without 2FA.
    time.sleep(5)
    return True


def _type_2fa(code: str) -> None:
    _osascript(
        f'tell application "System Events"\n'
        f'  keystroke "{code}"\n'
        f'  key code 36\n'
        f'end tell\n'
    )


def _wait_signed_in(deadline_s: int = 90) -> None:
    # TODO: poll for the signed-in state (e.g. the Apple ID pane header
    # shows the user's name). For now, just wait a bit.
    time.sleep(15)
