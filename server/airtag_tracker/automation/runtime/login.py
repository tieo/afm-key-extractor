"""Login-phase handlers for the runtime automation flow.

Covers four states:
- WAITING_LOGIN_SCREEN  → poll until the macOS login window appears
- LOGGING_IN            → paste VM password and press Return
- WAITING_DESKTOP       → wait for Dock to come up (SSH check + OCR guard)
- DISABLING_SLEEP       → disable screensaver / sleep so the screen stays on
"""

from __future__ import annotations

import time

from ... import qmp, vm_ui
from ...events import emit
from ..context import AutomationContext
from ..states import RuntimeState
from .. import screen


def wait_for_login_screen(ctx: AutomationContext) -> RuntimeState:
    """Poll until the macOS login window is visible — or autologin fires.

    When autologin is configured the desktop (Dock) comes up directly
    without a login window.  We check for that first each poll so we
    don't time-out waiting for a screen that will never appear.

    Polls every 3 s for up to 360 s.  Raises RuntimeError on timeout.
    """
    deadline_s = 360
    poll_s = 3.0
    progress_interval_s = 30
    t0 = time.time()
    last_progress = t0
    emit("info", "login", "Waiting for macOS login screen or autologin (up to 360 s)")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            screen_snippet = vm_ui.screen_text()[:80] if hasattr(vm_ui, 'screen_text') else ''
            emit("info", "login",
                 f"Still waiting for login screen… ({elapsed:.0f}s) screen: {repr(screen_snippet)}")
            last_progress = now
        # Fast path: autologin booted straight to desktop — skip login entirely.
        # SSH may accept connections but hang during early boot; catch and retry.
        try:
            r = vm_ui.ssh("pgrep -x Dock", timeout=8)
            if r.returncode == 0:
                emit("info", "login", "Dock already running — autologin succeeded, skipping login")
                return RuntimeState.WAITING_DESKTOP
        except Exception:
            pass
        if screen.detect_login_screen():
            emit("info", "login", "Login screen detected")
            return RuntimeState.LOGGING_IN
        time.sleep(poll_s)
    raise RuntimeError(f"Login screen not detected within {deadline_s}s")


def log_in(ctx: AutomationContext) -> RuntimeState:
    """Paste the VM password and press Return to log in.

    Uses clipboard paste so the password is never sent as QMP key
    events (which are US-layout-dependent and would mangle special chars).
    Waits 2 s after submitting for the session to begin loading.
    """
    emit("info", "login", "Typing VM password")
    vm_ui.paste_text(ctx.vm_password)
    with ctx.qmp_lock:
        qmp.send_keys(["ret"])
    time.sleep(2.0)
    return RuntimeState.WAITING_DESKTOP


def wait_for_desktop(ctx: AutomationContext) -> RuntimeState:
    """Wait for the user desktop to be fully up.

    Primary signal: ``pgrep -x Dock`` returns 0 over SSH, meaning the
    Dock (and therefore the full GUI session) is running.

    Guard: if the login screen re-appears between polls (e.g. the
    session was rejected or the screensaver locked before sleep was
    disabled), we return LOGGING_IN so the engine retries the password.

    Polls every 4 s for up to 300 s.  Raises RuntimeError on timeout.
    """
    deadline_s = 300
    poll_s = 4.0
    progress_interval_s = 30
    t0 = time.time()
    last_progress = t0
    emit("info", "login", "Waiting for desktop (Dock) to come up (up to 300 s)")
    while time.time() - t0 < deadline_s:
        now = time.time()
        elapsed = now - t0
        if now - last_progress >= progress_interval_s:
            screen_snippet = vm_ui.screen_text()[:80] if hasattr(vm_ui, 'screen_text') else ''
            emit("info", "login",
                 f"Still waiting for desktop… ({elapsed:.0f}s) screen: {repr(screen_snippet)}")
            last_progress = now
        # Check for a re-appeared login/lock screen first — if so, retry login.
        if screen.detect_login_screen():
            emit("info", "login", "Login screen re-appeared — retrying password")
            return RuntimeState.LOGGING_IN

        try:
            r = vm_ui.ssh("pgrep -x Dock", timeout=8)
            if r.returncode == 0:
                emit("info", "login", "Dock is running — desktop is up")
                return RuntimeState.DISABLING_SLEEP
        except Exception:
            pass

        time.sleep(poll_s)
    raise RuntimeError(f"Desktop (Dock) did not come up within {deadline_s}s")


def disable_sleep(ctx: AutomationContext) -> RuntimeState:
    """Disable display sleep, system sleep, and the screensaver.

    Without this a locked screen renders every
    ``open x-apple.systempreferences:...`` call a no-op (the pane opens
    behind the loginwindow overlay and OCR never sees the expected text).

    Also disables the password-on-wake requirement so a transient blank
    screen does not require another login.
    """
    from ... import vm_password as _vm_password
    import base64 as _base64

    emit("info", "login", "Disabling sleep and screensaver")
    pw = _vm_password.get() or ctx.vm_password
    script = (
        f"PW={pw!r}\n"
        "echo \"$PW\" | sudo -S pmset -a displaysleep 0 sleep 0 disksleep 0 hibernatemode 0\n"
        "sudo rm -f /var/vm/sleepimage\n"
        "defaults -currentHost write com.apple.screensaver idleTime -int 0\n"
        "defaults write com.apple.screensaver askForPassword -int 0\n"
    )
    b64 = _base64.b64encode(script.encode()).decode()
    vm_ui.ssh(f"echo {b64} | base64 -d | bash", timeout=30)
    emit("info", "login", "Sleep, screensaver, and hibernation disabled")
    return RuntimeState.OPENING_APPLE_ID
