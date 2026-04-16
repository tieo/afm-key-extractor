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
# Boot can take 5-10 min on a cold VM; give the autotyper plenty of
# runway so a slow first boot doesn't leave the VM stranded at the
# login screen until something else triggers a retry.
MAX_WAIT_S = 1800.0
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
    attempts = 0
    while time.time() < deadline:
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
                return
            # Wait for login screen to disappear (= success). If it stays
            # visible, macOS rejected the password — retry after cooldown.
            t_check = time.time() + 15
            while time.time() < t_check:
                time.sleep(2.0)
                if not _login_screen_visible():
                    emit("info", "vm", f"Auto-login succeeded after {attempts} attempt(s)")
                    return
            emit("warning", "vm", f"Login screen still visible after attempt {attempts} — retrying")
            time.sleep(3.0)
            continue
        time.sleep(POLL_INTERVAL_S)
    emit("warning", "vm", f"Auto-login gave up after {MAX_WAIT_S:.0f}s ({attempts} attempts)")


def start() -> None:
    threading.Thread(target=_worker, daemon=True, name="login-autotyper").start()
