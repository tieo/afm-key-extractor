"""macOS reinstall wizard automation.

click_through walks the seven-click sequence from the Recovery Utilities
picker through to the point where the installer begins copying files.
wait_complete polls for the VM reboot that signals the installer has
finished and the fresh macOS is ready for Setup Assistant.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ... import qmp, vm_ui
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


def _press_return_with_log(step: str) -> None:
    emit("info", "reinstall", f"{step}: pressing Return (default button fallback)")
    qmp.send_keys(["ret"])
    time.sleep(2.5)


def _click_popup_agree() -> None:
    """Confirm the licence agreement confirmation sheet (modal popup).

    The popup's [Disagree] [Agree] pill buttons are invisible to all OCR
    variants (tiny dark-on-dark).  The popup body text contains the word
    "agree" at y<430 (the EULA Agree button OCRs as "Adge"/"A%e" at y≈637,
    so y<430 reliably discriminates popup body from EULA button).

    The popup is keyboard-deaf in QEMU Recovery mode: Return and Tab events
    go to the underlying EULA window, not the modal sheet.  The only reliable
    method is a direct pixel click on the Agree pill button.

    Button geometry (1280×800 VM): Agree center ≈ (sw//2 + 60, popup_body_bottom + 29).
    Background EULA words bleed into OCR — x-band filter (35%-75% of sw) isolates
    popup body text from background ("agreement is unavailable." at x≈441).
    """
    emit("info", "reinstall", "Step 5: waiting for popup sheet animation…")
    time.sleep(3.0)

    for attempt in range(1, 4):
        p = vm_ui._screendump()
        try:
            sw, sh = vm_ui._screen_size(p)
            words = vm_ui.ocr_words(p)
        finally:
            Path(p).unlink(missing_ok=True)

        # Popup body text has "agree"/"agreement" in the centre x-band.
        # The background EULA screen bleeds through OCR at x<450 and x>850
        # (e.g. "The license agreement is unavailable." at x≈441) — those are
        # excluded by the x filter.  The Agree/Disagree pill buttons at y≈637
        # OCR as "Adge"/"A%e" or are missed entirely; detect gone via no words.
        popup_words = [
            w for w in words
            if w[0].lower() in ("agree", "agreement", "agreement.")
            and w[2] < 500                          # above button row
            and sw * 0.35 < w[1] < sw * 0.75       # popup x band (not background bleed)
        ]
        if not popup_words:
            emit("info", "reinstall", f"Step 5 attempt {attempt}: popup gone — proceeding")
            return

        body_bottom = max(w[2] + w[4] for w in popup_words)
        # Agree pill: right of centre by ~60px, ~29px below last body text line.
        click_x = sw // 2 + 60
        click_y = body_bottom + 29
        emit("info", "reinstall",
             f"Step 5 attempt {attempt}: clicking popup Agree at ({click_x},{click_y})")
        vm_ui.click_pixel(click_x, click_y, sw, sh)
        time.sleep(3.0)

    emit("warning", "reinstall", "Step 5: all direct-click attempts failed — popup may still be open")


def click_through(ctx: AutomationContext) -> InstallState:
    """Drive the reinstall wizard.

    OCR-click is the primary method; Return is used as fallback for
    "Continue" screens (the default button in every macOS installer
    dialog). "Agree" requires an explicit click (it is not the default).

    Step sequence
    -------------
    1. "Reinstall macOS" — main row in the Recovery Utilities picker.
    2. "Continue" — picker confirmation dialog (default button → Return).
    3. "Continue" — "Install macOS" splash (default button → Return).
    4. "Agree" — software licence agreement (NOT default; OCR required).
    5. "Agree" — confirmation sheet (NOT default; OCR required).
    6. "Macintosh" — selects the "Macintosh-HD" destination volume (OCR).
    7. "Continue" — begins the installation (default button → Return).
    """
    emit("info", "reinstall", "Starting reinstall wizard click-through")

    # Step 1 — select "Reinstall macOS" from the Recovery picker.
    # After Terminal exits the Recovery Utilities window needs time to re-focus.
    if not screen.wait_click_text("Reinstall", "macOS", deadline_s=60):
        raise RuntimeError("Could not find 'Reinstall macOS' in Recovery Utilities")
    time.sleep(1.5)

    # Step 2 — Continue (picker confirmation dialog, default button).
    if not screen.wait_click_text("Continue", deadline_s=20):
        _press_return_with_log("Step 2")

    # Step 3 — Continue on the Install macOS splash (loads slowly).
    # The splash body text contains "click Continue", so OCR for "Continue"
    # hits the body text instead of the button.  Return is the default action
    # but requires the window to have keyboard focus.  Strategy: press Return,
    # then if EULA hasn't appeared within 10 s, pixel-click the Continue
    # button directly (center of screen, y≈370 in the 1280×800 layout).
    emit("info", "reinstall", "Step 3: waiting for installer splash…")
    if screen.has_text("install macos", deadline_s=60):
        emit("info", "reinstall", "Step 3: installer splash detected — pressing Return")
    else:
        emit("warning", "reinstall",
             "Step 3: splash not detected in 60s — pressing Return anyway")
    _press_return_with_log("Step 3")
    if not screen.has_text("Disagree", deadline_s=10):
        emit("info", "reinstall",
             "Step 3: Return may not have registered — clicking Continue (pixel fallback)")
        vm_ui.click_pixel(640, 370, 1280, 800)
        time.sleep(1.5)

    # Step 4 — Agree to the licence. "Agree" is NOT the default button.
    # The licence text must be scrolled to the bottom first — macOS disables
    # the Agree button until the user has scrolled to the end.
    # Allow 3 min: the installer does a server-side connection check before
    # showing the licence, which can take 60-120 s in QEMU.
    # The "Agree" button OCRs unreliably ("Ag&e", "Agge") due to font size.
    # "Disagree" always OCRs correctly; click the button to its right instead.
    if not screen.has_text("Disagree", deadline_s=170):
        raise RuntimeError("Could not find EULA screen (no 'Disagree' button)")
    # Scroll the EULA text view to the bottom using the mouse scroll wheel.
    # Keyboard Page Down requires focus on the text view, which is unreliable
    # (focus may be on a button).  Scroll wheel events go to the element under
    # the mouse pointer regardless of keyboard focus, so click a word in the
    # EULA body first to position the pointer inside the scroll view, then
    # scroll wheel the rest of the way down.
    emit("info", "reinstall", "Step 4: scrolling licence to bottom via mouse wheel")
    vm_ui.click_text("CAREFULLY", tries=2)   # "PLEASE READ CAREFULLY" — top of EULA body
    time.sleep(0.3)
    # 30 clicks × ~3 lines/click = ~90 lines — more than any EULA length.
    vm_ui.scroll_down(clicks=30, gap_s=0.05)
    time.sleep(1.5)
    emit("info", "reinstall", "Step 4: clicking Agree (right of Disagree)")
    if not vm_ui.click_right_of("Disagree"):
        emit("warning", "reinstall",
             "Step 4: click_right_of failed — trying Tab+Space keyboard nav")
        qmp.send_keys(["tab"])
        time.sleep(0.4)
        qmp.send_keys(["spc"])
        time.sleep(2.0)

    # Step 5 — Agree on the confirmation sheet (modal popup).
    # Same problem: "Agree" OCRs poorly; "Disagree" is reliable.
    time.sleep(1.0)  # wait for the sheet animation
    _click_popup_agree()

    # Step 6 — Select Macintosh-HD as the destination.
    # The Sonoma installer shows the disk label ("Macintosh-HD") in small text
    # below the disk icon on a dark background — Tesseract OCR cannot read it
    # reliably.  We try OCR first; on failure we pixel-click the HDD icon
    # directly.  The HDD icon is always the LEFT of the two icons shown
    # (the right icon is the "macOS Base System" recovery disk which must NOT
    # be selected).  Coordinates verified against a live 1280×800 screenshot.
    _DISK_ICON_X, _DISK_ICON_Y = 580, 480  # Macintosh-HD HDD icon centre
    if not screen.wait_click_text("Macintosh", deadline_s=15):
        emit("info", "reinstall",
             "Step 6: OCR missed disk label — pixel-clicking Macintosh-HD icon")
        vm_ui.click_pixel(_DISK_ICON_X, _DISK_ICON_Y, 1280, 800)
        time.sleep(2.0)  # wait for Continue to un-gray after disk selection

    # Step 7 — Continue to start copying (default button).
    if not screen.wait_click_text("Continue", deadline_s=30):
        _press_return_with_log("Step 7")

    emit("info", "reinstall", "Reinstall wizard complete — installation in progress")
    return InstallState.WAITING_INSTALL


def _extract_remaining(text: str) -> str | None:
    """Pull 'about X hours … remaining' or 'less than a minute remaining' from OCR text."""
    m = re.search(r'((?:about|less than)[\w\s]+remaining)', text)
    return m.group(1).strip() if m else None


def _read_progress_bar(ppm: str) -> float | None:
    """Read installer progress bar fill (0.0–1.0) via pixel analysis.

    Handles two visual styles seen on 1280×800 QEMU framebuffer:

    Phase 1 (installer UI, gray background):
    - Bar spans y=548-555, x≈490-789
    - Fill colour: (23, 105, 231) — macOS blue
    - Empty track: (62, 62, 62) — mid-gray

    Phase 2 (configure, black background / Apple logo screen):
    - Bar spans y=715-720, x≈523-756
    - Fill colour: ~(211, 211, 211) — neutral light gray
    - Empty track: ~(38, 38, 38) — dark gray

    Does NOT delete the PPM — caller is responsible for cleanup.
    Returns None if neither bar style is detectable.
    """
    try:
        from PIL import Image
        with Image.open(ppm) as im:
            pix = im.convert("RGB").load()
            w, _ = im.size

            # --- Phase 1: blue-fill bar ---
            blue = gray1 = 0
            for y in range(548, 556):
                for x in range(0, w):
                    r, g, b = pix[x, y]
                    if b > 150 and b > r * 4 and b > g * 1.5:
                        blue += 1
                    elif 52 <= r <= 75 and abs(r - g) < 8 and abs(g - b) < 8:
                        gray1 += 1
            total1 = blue + gray1
            if total1 >= 10:
                return round(blue / total1, 2)

            # --- Phase 2: light-gray fill on dark track (Apple logo / configure screen) ---
            filled = empty = 0
            for y in range(715, 721):
                for x in range(480, 800):
                    r, g, b = pix[x, y]
                    # Filled: all channels > 180, all equal (neutral gray)
                    if r > 180 and abs(r - g) < 20 and abs(g - b) < 20:
                        filled += 1
                    # Empty track: all channels < 80, all equal (dark gray)
                    elif r < 80 and abs(r - g) < 20 and abs(g - b) < 20:
                        empty += 1
            total2 = filled + empty
            if total2 >= 10:
                return round(filled / total2, 2)

            return None
    except Exception as e:
        emit("warning", "reinstall", f"progress bar read failed: {e}")
        return None


def wait_complete(ctx: AutomationContext) -> InstallState:
    """Wait for the installer to finish and the VM to reboot.

    Takes one screendump per poll and reuses it for:
    - Pixel-based progress bar fill ratio
    - OCR for 'X minutes remaining' text
    - OpenCore picker detection (install complete)
    - Setup Assistant detection (install + configure complete, SA already running)

    Deadline: 4 h (macOS installer often overestimates in QEMU).
    """
    from .. import screen as _screen

    deadline_s = 14400
    poll_s = 30.0
    t0 = time.time()
    last_remaining: str | None = None
    last_bar_pct: int | None = None
    screendump_fails = 0

    emit("info", "reinstall", "Waiting for macOS installation to complete (up to 4 h)…")

    while time.time() - t0 < deadline_s:
        if ctx.aborted:
            return InstallState.WAITING_INSTALL  # engine abort check will catch this

        elapsed = time.time() - t0
        minutes = int(elapsed // 60)

        try:
            ppm = vm_ui._screendump()
            if screendump_fails:
                emit("info", "reinstall",
                     f"Screendump recovered after {screendump_fails} failure(s)")
                screendump_fails = 0
        except Exception as exc:
            screendump_fails += 1
            if screendump_fails == 1 or screendump_fails % 5 == 0:
                from ... import vm as _vm
                running = _vm.is_running()
                emit("warning", "reinstall",
                     f"Screendump failed ({screendump_fails}×, {minutes} min elapsed,"
                     f" vm_running={running}): {exc}")
                if not running:
                    raise RuntimeError(
                        f"QEMU process died during installation after {minutes} min"
                    )
            time.sleep(poll_s)
            continue

        try:
            bar = _read_progress_bar(ppm)
            text = vm_ui.screen_text(ppm)
        finally:
            Path(ppm).unlink(missing_ok=True)

        remaining = _extract_remaining(text)
        if remaining and remaining != last_remaining:
            emit("info", "reinstall", f"Installer ({minutes} min elapsed): {remaining}")
            last_remaining = remaining

        if bar is not None:
            bar_pct = int(bar * 100)
            if last_bar_pct is None or abs(bar_pct - last_bar_pct) >= 1:
                emit("info", "reinstall", f"Progress bar: ~{bar_pct}%")
                last_bar_pct = bar_pct
        elif last_bar_pct is not None:
            emit("warning", "reinstall", "Progress bar: no longer detectable")
            last_bar_pct = None  # only warn once per transition, not every poll

        # OpenCore picker markers — base system / REL- version string.
        if "base system" in text or "rel-" in text:
            emit("info", "reinstall",
                 f"OpenCore picker detected after {minutes} min — installation complete")
            return InstallState.BOOTING_INSTALLED

        # SA already running: install + both configure phases completed while we
        # were polling (30 s gap missed the picker).  Skip directly to SA_COUNTRY.
        if _screen.detect_setup_assistant():
            emit("info", "reinstall",
                 f"Setup Assistant detected after {minutes} min — skipping BOOTING_INSTALLED")
            return InstallState.SA_COUNTRY

        time.sleep(poll_s)

    raise RuntimeError(
        f"macOS installation did not complete within {deadline_s}s"
    )
