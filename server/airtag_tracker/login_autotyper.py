"""Detect the macOS login window via OCR on QMP screendumps, then type
the stored password.

Why OCR and not a timer: the login window appears 20–60 s after the
OpenCore picker depending on kernel cache state, FileVault, and disk
speed. Blindly typing after a sleep either misses the window or types
into the wrong focus. OCR gives us a deterministic "the password field
is visible right now" signal.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from . import qmp, vm_password
from .config import VM_SSH_ENABLED_MARKER
from .events import emit


POLL_INTERVAL_S = 3.0
MAX_WAIT_S = 180.0
START_DELAY_S = 20.0      # no point screen-dumping before macOS is close to login
MATCH_KEYWORDS = ("password", "enter password")


def _ocr(ppm_path: Path) -> str:
    """Read text from a QEMU framebuffer dump.

    macOS login is white text on a dark desktop; tesseract needs the
    image inverted to a dark-on-light page, and PSM 11 ('sparse text')
    handles the scattered labels much better than the default layout
    analysis.
    """
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as e:
        raise RuntimeError(f"OCR dependencies missing: {e}") from e
    with Image.open(ppm_path) as img:
        inverted = ImageOps.invert(img.convert("L"))
        return pytesseract.image_to_string(inverted, config="--psm 11").lower()


def _login_screen_visible() -> bool:
    with tempfile.TemporaryDirectory() as td:
        shot = Path(td) / "frame.ppm"
        try:
            qmp.screendump(str(shot))
        except Exception as e:
            emit("warning", "vm", f"screendump failed: {e}")
            return False
        if not shot.exists() or shot.stat().st_size == 0:
            return False
        try:
            text = _ocr(shot)
        except Exception as e:
            emit("warning", "vm", f"OCR failed: {e}")
            return False
        return any(kw in text for kw in MATCH_KEYWORDS)


def _enable_ssh(password: str) -> None:
    """One-time: open Terminal via Spotlight, run `sudo systemsetup
    -setremotelogin on`, answer the password prompt. Marker file makes
    this idempotent across reboots."""
    if VM_SSH_ENABLED_MARKER.exists():
        return
    emit("info", "vm", "Enabling Remote Login in macOS (one-time)")
    try:
        time.sleep(25)  # let the desktop settle after login
        # Spotlight → Terminal.
        qmp.send_chord(["meta_l", "spc"])
        time.sleep(1.2)
        qmp.type_text("Terminal")
        time.sleep(0.8)
        qmp.send_keys(["ret"])
        time.sleep(3.5)
        # Run the command. A trailing newline is sent via Return so the
        # shell line-edits cleanly even if auto-complete chimes in.
        qmp.type_text("sudo systemsetup -setremotelogin on")
        qmp.send_keys(["ret"])
        time.sleep(1.5)
        # sudo now prompts "Password:" — type it.
        qmp.type_text(password)
        qmp.send_keys(["ret"])
        time.sleep(3.0)
        # Close the Terminal window.
        qmp.send_chord(["meta_l", "q"])
        VM_SSH_ENABLED_MARKER.parent.mkdir(parents=True, exist_ok=True)
        VM_SSH_ENABLED_MARKER.write_text("1")
        emit("info", "vm", "Remote Login enable sequence sent")
    except Exception as e:
        emit("warning", "vm", f"Enable-SSH sequence failed: {e}")


def _worker() -> None:
    password = vm_password.get()
    if not password:
        emit("info", "vm", "No VM password stored — skipping auto-login")
        return

    time.sleep(START_DELAY_S)
    deadline = time.time() + MAX_WAIT_S
    while time.time() < deadline:
        if _login_screen_visible():
            emit("info", "vm", "Login window detected via OCR — typing password")
            try:
                qmp.type_text(password)
                qmp.send_keys(["ret"])
                emit("info", "vm", "Auto-login keystrokes sent")
            except Exception as e:
                emit("error", "vm", f"Auto-login type failed: {e}")
                return
            _enable_ssh(password)
            return
        time.sleep(POLL_INTERVAL_S)
    emit("warning", "vm", f"Auto-login gave up after {MAX_WAIT_S:.0f}s (login window never detected)")


def start() -> None:
    threading.Thread(target=_worker, daemon=True, name="login-autotyper").start()
