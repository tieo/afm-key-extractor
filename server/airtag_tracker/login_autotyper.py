"""Detect the macOS login window via OCR, then type the stored password.

OCR notes: the 'Enter Password' placeholder tesseract mangles badly
('CbrterPosmwerd'), so matching on 'password' misses the window. The
login screen's unmistakable signature is the trio of bottom-row
buttons (Shut Down / Restart / Sleep) plus the username label. We
match on those instead — all three terms are rendered in plain
fonts that tesseract handles reliably.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from . import qmp, vm_password
from .events import emit


POLL_INTERVAL_S = 3.0
START_DELAY_S = 20.0
USERNAME = "airtag"
# Any ≥2 of these visible together = login screen. Picked for OCR
# robustness: all three are short words in default system font.
SIGNATURE_KEYWORDS = ("shut down", "restart", "sleep")

# Singleton guard: the autotyper is a persistent watchdog. Multiple
# callers (vm.start on boot, app.py autostart on tracker restart) may
# try to start it; only the first succeeds.
_lock = threading.Lock()
_thread: threading.Thread | None = None


def _ocr_text() -> str:
    """OCR the current framebuffer. Grayscale + autocontrast; lowercased merged output.

    Lock screen text (username label, bottom-row buttons) is white over the
    macOS colored wallpaper. RGB-mode tesseract gets defeated by the colored
    gradient and reliably returns "s 4" — even with invert or 2× upscale.
    Grayscale-then-autocontrast collapses the gradient into a high-contrast
    black-and-white frame and recovers all keywords cleanly.
    """
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as e:
        raise RuntimeError(f"OCR dependencies missing: {e}") from e
    with tempfile.TemporaryDirectory() as td:
        ppm = Path(td) / "frame.ppm"
        try:
            qmp.screendump(str(ppm))
        except Exception as e:
            emit("warning", "vm", f"screendump failed: {e}")
            return ""
        if not ppm.exists() or ppm.stat().st_size == 0:
            return ""
        with Image.open(ppm) as img:
            gray = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=5)
            try:
                return pytesseract.image_to_string(gray, config="--psm 6").lower()
            except Exception:
                return ""


def _login_screen_visible() -> bool:
    text = _ocr_text()
    if not text:
        return False
    hits = sum(1 for kw in SIGNATURE_KEYWORDS if kw in text)
    return hits >= 2 and USERNAME in text


def _worker() -> None:
    password = vm_password.get()
    if not password:
        emit("info", "vm", "No VM password stored — autotyper exiting")
        return

    time.sleep(START_DELAY_S)
    attempts = 0
    # Runs for the life of the tracker process. Deploys bring the
    # tracker back up and app.py re-invokes start() on restart.
    while True:
        if _login_screen_visible():
            attempts += 1
            emit("info", "vm", f"Login window detected (attempt {attempts}) — typing password")
            try:
                # With a single user account, macOS focuses the password
                # field on boot — type directly and submit. Sending a
                # leading Return submits an empty password, which triggers
                # the "shake" rejection animation and can race with typing.
                qmp.type_text(password)
                time.sleep(0.3)
                qmp.send_keys(["ret"])
            except Exception as e:
                emit("error", "vm", f"Auto-login type failed: {e}")
                time.sleep(10.0)
                continue
            t_check = time.time() + 15
            unlocked = False
            while time.time() < t_check:
                time.sleep(2.0)
                if not _login_screen_visible():
                    emit("info", "vm", f"Auto-login succeeded after {attempts} attempt(s)")
                    unlocked = True
                    break
            if not unlocked:
                emit("warning", "vm", f"Login screen still visible after attempt {attempts} — retrying")
                time.sleep(3.0)
            continue
        time.sleep(POLL_INTERVAL_S)


def start() -> None:
    """Spawn the watchdog thread. Idempotent — subsequent calls are no-ops."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_worker, daemon=True, name="login-autotyper")
        _thread.start()
