"""Screen detection utilities — OCR and template matching.

Both methods operate on a QMP screendump (PPM file from the QEMU
monitor socket).  Neither requires SSH or an active macOS user session,
so they work in every stage from the OpenCore picker onward.

OCR path
--------
Delegates to :mod:`vm_ui` which already implements the proven
four-variant Tesseract approach (1×/2× × normal/inverted).

Template path
-------------
OpenCV ``matchTemplate`` with ``TM_CCOEFF_NORMED``.  Templates are
small PNG crops stored in ``automation/templates/``.  If a template
file is missing, the function returns ``None`` gracefully — callers
should fall back to OCR rather than crashing.

QMP serialisation
-----------------
Screendump commands are read-only (they don't affect VM state) and are
safe to call from any thread without holding the qmp_lock.  Only
``click_at`` requires the caller to hold the lock if it's part of a
sequence.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .. import vm_ui
from ..events import emit

TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def has_text(*keywords: str, deadline_s: int = 0, poll_s: float = 2.0) -> bool:
    """Return True as soon as ALL keywords appear on screen.

    With ``deadline_s=0`` (default) this is a single non-blocking check.
    With a positive deadline it polls until they appear or time expires.
    """
    if deadline_s <= 0:
        text = vm_ui.screen_text()
        return all(kw.lower() in text for kw in keywords)
    return vm_ui.wait_for_text(
        tuple(kw.lower() for kw in keywords),
        deadline_s=deadline_s,
        poll_s=poll_s,
    )


def has_any_text(*keywords: str) -> bool:
    """Return True if ANY of the keywords appear on screen (single check)."""
    text = vm_ui.screen_text()
    return any(kw.lower() in text for kw in keywords)


def click_text(
    first: str,
    last: str | None = None,
    tries: int = 3,
    include_menubar: bool = False,
) -> bool:
    """Click a UI element identified by OCR text. Coordinate-free."""
    return vm_ui.click_text(first, last, tries=tries, include_menubar=include_menubar)


def wait_click_text(
    first: str,
    last: str | None = None,
    deadline_s: int = 30,
    tries_per_poll: int = 1,
    poll_s: float = 2.0,
) -> bool:
    """Poll until the text appears, then click it. Returns False on timeout."""
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        if vm_ui.click_text(first, last, tries=tries_per_poll):
            return True
        time.sleep(poll_s)
    emit("warning", "screen", f"wait_click_text timed out: {first!r}/{last!r}")
    return False


# ---------------------------------------------------------------------------
# Template matching
# ---------------------------------------------------------------------------

def _load_template(name: str):
    """Load a template PNG via OpenCV. Returns None if file missing or cv2 absent."""
    try:
        import cv2
    except ImportError:
        return None
    path = TEMPLATES_DIR / f"{name}.png"
    if not path.exists():
        return None
    tpl = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return tpl


def find_template(
    name: str,
    threshold: float = 0.80,
    scale_range: tuple[float, float] = (0.8, 1.2),
    scale_steps: int = 5,
) -> tuple[int, int] | None:
    """Locate a template image on the current screen.

    Tries multiple scales within *scale_range* to handle slight
    resolution differences.  Returns the (x, y) click centre in native
    VM pixel coordinates, or None if not found above threshold.

    Falls back to None gracefully when OpenCV is not installed or the
    template file does not exist — callers should use OCR as fallback.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    tpl = _load_template(name)
    if tpl is None:
        return None

    ppm = tempfile.mktemp(suffix=".ppm")
    try:
        vm_ui._screendump(ppm)
        frame = cv2.imread(ppm, cv2.IMREAD_COLOR)
    except Exception:
        return None
    finally:
        Path(ppm).unlink(missing_ok=True)

    if frame is None:
        return None

    best_val = 0.0
    best_loc = (0, 0)
    th, tw = tpl.shape[:2]

    import numpy as np
    scales = np.linspace(scale_range[0], scale_range[1], scale_steps)
    for scale in scales:
        if scale == 1.0:
            t = tpl
        else:
            new_w = max(1, int(tw * scale))
            new_h = max(1, int(th * scale))
            t = cv2.resize(tpl, (new_w, new_h), interpolation=cv2.INTER_AREA)
        if t.shape[0] > frame.shape[0] or t.shape[1] > frame.shape[1]:
            continue
        result = cv2.matchTemplate(frame, t, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val > best_val:
            best_val = max_val
            rh, rw = t.shape[:2]
            best_loc = (max_loc[0] + rw // 2, max_loc[1] + rh // 2)

    if best_val >= threshold:
        return best_loc
    return None


def wait_template(
    name: str,
    deadline_s: int = 60,
    threshold: float = 0.80,
    poll_s: float = 3.0,
) -> tuple[int, int] | None:
    """Poll until template found or deadline exceeded."""
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        loc = find_template(name, threshold=threshold)
        if loc is not None:
            return loc
        time.sleep(poll_s)
    return None


# ---------------------------------------------------------------------------
# Screen state detection
# ---------------------------------------------------------------------------

def detect_opencore_picker() -> bool:
    """True if the OpenCore boot picker is visible.

    Uses OCR for strings that only appear on the picker:
      - "EFI" (default first entry label)
      - "macOS Base System" / "Base System" (recovery installer entry)
      - "REL-" prefix from the OpenCore version string shown below icons

    Template matching was removed because the dark background of the picker
    template produced false positives on the macOS installer progress screen.
    The 4-variant OCR (including autocontrast 2×) reliably detects these
    strings even against the picker's dark background.
    """
    return has_any_text("EFI", "Base System", "REL-")


def detect_recovery_utilities() -> bool:
    """True if the macOS Recovery Utilities picker is visible."""
    return has_text("Reinstall macOS", "Disk Utility")


def detect_setup_assistant() -> bool:
    """True if Setup Assistant is running (any screen).

    Checks for distinctive text from each of the 13 Setup Assistant screens
    so that mid-flow resumption can be detected when we're past screen 1.
    """
    return has_any_text(
        # screen 1
        "country or region", "choose your country",
        # screen 2
        "written and spoken", "spoken languages",
        # screen 3 — single word too broad, pair it
        # screen 4
        "data & privacy",
        # screen 5
        "migration assistant", "transfer your information",
        # screen 6
        "sign in with your apple id",
        # screen 7
        "terms and conditions",
        # screen 8
        "computer account", "mac account",
        # screen 9
        "location services",
        # screen 10
        "time zone",
        # screen 11 — "analytics" alone is too broad, skip
        # screen 12
        "screen time",
        # screen 13
        "choose your look",
    )


def detect_tiano_bios() -> bool:
    """True if the TianoCore BIOS Boot Maintenance Manager is visible.

    When macOS sets volatile EFI boot priority variables during configure
    phases, OVMF may fail all entries and fall into the Boot Maintenance
    Manager.  Both strings appear together only on that screen.
    """
    return has_text("Boot Manager", "Device Manager")


def detect_login_screen() -> bool:
    """True if the macOS login window is visible."""
    text = vm_ui.screen_text()
    hits = sum(1 for kw in ("shut down", "restart", "sleep") if kw in text)
    return hits >= 2 and "airtag" in text


def detect_desktop() -> bool:
    """True if a user desktop is visible (Finder menu bar present)."""
    return has_text("Finder")
