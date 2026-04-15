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
MAX_WAIT_S = 240.0
START_DELAY_S = 20.0
USERNAME = "airtag"
# Any ≥2 of these visible together = login screen. Picked for OCR
# robustness: all three are short words in default system font.
SIGNATURE_KEYWORDS = ("shut down", "restart", "sleep")


def _ocr_text() -> str:
    """OCR the current framebuffer at 1× and 2×, merged and lowercased."""
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
            rgb = img.convert("RGB")
            out = []
            for variant in (rgb, ImageOps.invert(rgb),
                            rgb.resize((rgb.width * 2, rgb.height * 2), Image.LANCZOS)):
                try:
                    out.append(pytesseract.image_to_string(
                        variant, config="--psm 6"
                    ))
                except Exception:
                    pass
            return "\n".join(out).lower()


def _login_screen_visible() -> bool:
    text = _ocr_text()
    if not text:
        return False
    hits = sum(1 for kw in SIGNATURE_KEYWORDS if kw in text)
    return hits >= 2 and USERNAME in text


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
                # Click the password field first — macOS sometimes lands
                # focus on the Shut Down button instead of the field.
                # We don't know exact coords, but pressing Return on the
                # username tile moves focus to the field reliably.
                qmp.send_keys(["ret"])
                time.sleep(0.6)
                qmp.type_text(password)
                qmp.send_keys(["ret"])
                emit("info", "vm", "Auto-login keystrokes sent")
            except Exception as e:
                emit("error", "vm", f"Auto-login type failed: {e}")
                return
            return
        time.sleep(POLL_INTERVAL_S)
    emit("warning", "vm", f"Auto-login gave up after {MAX_WAIT_S:.0f}s (login window never detected)")


def start() -> None:
    threading.Thread(target=_worker, daemon=True, name="login-autotyper").start()
