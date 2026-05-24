"""Post-Setup-Assistant finalisation steps.

Three handlers:
1. dismiss_first_boot — close the Keyboard Setup Assistant modal that
   appears the first time a user desktop loads, then re-authenticate.
2. shutdown — gracefully power down the VM via QMP system_powerdown.
3. bake_golden — snapshot the configured disk to the golden image path.
"""

from __future__ import annotations

import time

from ... import qmp, vm, vm_password
from ...config import VM_DIR
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


def dismiss_first_boot(ctx: AutomationContext) -> InstallState:
    """Dismiss first-boot dialogs, enable SSH, and configure autologin.

    After SA completes, macOS shows: Welcome splash → Keyboard Setup
    Assistant.  We dismiss both, then enable SSH Remote Login via
    launchctl (systemsetup -setremotelogin requires Full Disk Access on
    Sequoia and is therefore not usable here).

    Autologin is configured so that future boots do not require keyboard
    input at the login window — QMP key injection is blocked by macOS
    Sequoia's loginwindow security policy.
    """
    password = vm_password.ensure()

    # If we arrive here from the lock screen (e.g. resumed after QEMU restart
    # mid-SA), log in so Spotlight and SSH are accessible.
    if screen.detect_login_screen():
        emit("info", "finalize", "Lock screen detected — logging in before finalising")
        _login_at_lock_screen(password)

    # Dismiss Keyboard Setup Assistant if present.
    emit("info", "finalize", "Waiting for Keyboard Setup Assistant modal…")
    if screen.has_text("Keyboard", "Setup", deadline_s=30, poll_s=2.0):
        emit("info", "finalize", "Keyboard Setup Assistant detected — clicking Quit")
        if not screen.click_text("Quit", tries=3):
            emit("warning", "finalize", "OCR Quit not found — using pixel fallback")
            from ... import vm_ui
            vm_ui.click_pixel(905, 684, 1280, 800)
        time.sleep(1.5)

    # Enable SSH Remote Login via Spotlight → Terminal.
    _enable_ssh(password)

    # Configure autologin so runtime boots skip the login window.
    _configure_autologin(password)

    return InstallState.SHUTTING_DOWN


def _login_at_lock_screen(password: str) -> None:
    """Click the lock-screen password field, type the password, wait for desktop.

    Tries multiple strategies because macOS Sonoma's loginwindow can be picky
    about which input events it honours depending on how QEMU was started.
    """
    from ... import vm_ui

    for attempt in range(3):
        # Pixel analysis of 1280×800 Sonoma lock screen shows the password
        # input box at y≈625-645; user icon at y≈547.  Try both focal points.
        y_targets = [632, 547, 590]
        y = y_targets[attempt % len(y_targets)]
        vm_ui.click_pixel(640, y, 1280, 800)
        time.sleep(0.8)
        qmp.type_text(password)
        time.sleep(0.3)
        qmp.send_keys(["ret"])
        if screen.has_text("Finder", deadline_s=25, poll_s=3.0):
            emit("info", "finalize", "Logged in — desktop reached")
            time.sleep(2.0)
            return
        emit("info", "finalize",
             f"Lock-screen login attempt {attempt + 1}/3 did not reach desktop — retrying")

    raise RuntimeError("Desktop (Finder) not reached within 3 lock-screen login attempts")


def _configure_autologin(password: str) -> None:
    """Configure macOS autologin so runtime boots skip the login window.

    macOS Sequoia blocks QMP keyboard injection at the loginwindow
    (both from QMP send-key and VNC key events), so we must configure
    autologin rather than trying to type the password.

    Two writes required:
    1. ``defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser``
    2. ``/etc/kcpassword`` — XOR-encoded password file (macOS's autologin
       credential store, obfuscated with a fixed 11-byte key).
    """
    from ... import vm_ui

    emit("info", "finalize", "Configuring autologin via SSH")

    # Build kcpassword bytes: XOR password with repeating 11-byte key, then
    # null-terminate and pad to a multiple of 11 bytes.
    key = [0x7D, 0x89, 0x52, 0x23, 0xD2, 0xBC, 0xDD, 0xEA, 0xA3, 0xB9, 0x1F]
    pw_bytes = password.encode("utf-8") + b"\x00"
    while len(pw_bytes) % 11 != 0:
        pw_bytes += b"\x00"
    kcp = bytes(b ^ key[i % 11] for i, b in enumerate(pw_bytes))
    kcp_hex = kcp.hex()

    # Build a shell script that elevates once via sudo -S then does all root
    # work inside a single bash -c.  Sending via base64 avoids quoting hazards
    # with special characters in the password or hex payload.
    import base64
    script = (
        f"PW={password!r}\n"
        # One sudo invocation handles both the loginwindow pref and kcpassword.
        # printf inside the bash -c doesn't fight sudo -S for stdin because
        # sudo already consumed the password line before exec'ing bash.
        f"echo \"$PW\" | sudo -S bash -c '"
        "defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser airtag; "
        f"printf \"{kcp_hex}\" | xxd -r -p > /etc/kcpassword; "
        "chmod 600 /etc/kcpassword'\n"
    )
    # Use a Python-side base64 encode so no shell quoting is needed.
    b64 = base64.b64encode(script.encode()).decode()
    # SSH may not be ready immediately after launchctl load — retry for up to 30 s.
    for attempt in range(1, 7):
        r = vm_ui.ssh(f"echo {b64} | base64 -d | bash", timeout=30)
        if r.returncode == 0:
            break
        emit("info", "finalize",
             f"SSH attempt {attempt}/6 failed (rc={r.returncode}) — retrying in 5 s…")
        time.sleep(5.0)
    if r.returncode != 0:
        raise RuntimeError(
            f"Autologin configuration failed after 6 attempts: {(r.stderr or r.stdout).strip()[:300]}"
        )
    emit("info", "finalize", "Autologin configured — next boot will skip login window")


def _enable_ssh(password: str) -> None:
    """Open Terminal via Spotlight and enable SSH Remote Login.

    Uses launchctl rather than systemsetup because macOS Sequoia requires
    Full Disk Access for systemsetup -setremotelogin, which a GUI session
    doesn't hold.
    """
    emit("info", "finalize", "Enabling SSH Remote Login via launchctl")
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "spc"])
    time.sleep(1.5)
    qmp.type_text("Terminal")
    time.sleep(0.5)
    qmp.send_keys(["ret"])
    time.sleep(6.0)
    # Enable SSH: launchctl load -w works without Full Disk Access.
    cmd = "sudo launchctl load -w /System/Library/LaunchDaemons/ssh.plist"
    qmp.type_text(cmd)
    qmp.send_keys(["ret"])
    time.sleep(1.5)
    qmp.type_text(password)
    qmp.send_keys(["ret"])
    time.sleep(4.0)
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "q"])
    time.sleep(1.0)
    emit("info", "finalize", "SSH Remote Login enabled")


def shutdown(ctx: AutomationContext) -> InstallState:
    """Issue a graceful ACPI shutdown via QMP; force-stop on timeout.

    Waits up to 120 s for a clean shutdown.  If macOS hasn't stopped by
    then (e.g. blocked by first-boot notification dialogs), falls back to
    vm.stop() (SIGTERM) and proceeds to bake_golden regardless — the APFS
    filesystem survives an unclean poweroff.
    """
    emit("info", "finalize", "Sending system_powerdown via QMP")
    qmp.system_powerdown()

    deadline_s = 120
    poll_s = 2.0
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if not vm.is_running():
            emit("info", "finalize", "VM stopped cleanly")
            return InstallState.BAKING_GOLDEN
        time.sleep(poll_s)

    emit("warning", "finalize",
         f"VM still running after {deadline_s}s — forcing stop via SIGTERM")
    vm.stop()
    # Give QEMU a moment to write and close its image files.
    time.sleep(5.0)
    return InstallState.BAKING_GOLDEN


def bake_golden(ctx: AutomationContext) -> InstallState:
    """Snapshot mac_hdd_ng.img → mac_hdd_golden.img.

    Delegates to vm.bake_golden() which handles the file copy and emits
    its own events.  We emit one additional info event here for the SSE
    log so the progress bar advances to DONE.
    """
    golden = ctx.adapter.golden_image_path(VM_DIR)
    emit("info", "finalize",
         f"Baking golden image from installed disk → {golden.name}…")
    vm.bake_golden(golden_path=golden)
    emit("info", "finalize", f"Golden image saved ({ctx.adapter.display_name}) — installation complete")
    return InstallState.DONE
