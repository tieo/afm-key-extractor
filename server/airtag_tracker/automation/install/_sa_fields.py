"""Field primitives for the Setup Assistant Create Account screen.

The big handler in ``setup_assistant.screen_create_account`` is built
from a small number of recurring patterns:

- click a field, clear its existing content, type a value, settle
- handle the compound NSSecureTextField (left + verify halves) which
  has wildly different focus/clear semantics from a normal text field
- dismiss the error modal that appears on bad input

Each primitive here is small enough to be unit-tested against a mocked
QMP transport, and (via the snapshot/replay harness) re-runnable
against a paused VM in seconds.

Strategy: when something breaks, change ONE function here, not the
whole 130-line handler.  Comments at each function explain *why* the
non-obvious sequencing exists — most of it is purely empirical.
"""

from __future__ import annotations

import os
import time

from ... import qmp, vm_ui
from ...events import emit


_SCREEN_W, _SCREEN_H = 1280, 800


def click_field(x: int, y: int, *, settle_s: float = 0.5) -> None:
    """Pixel-click a field's centre and wait for focus to land."""
    vm_ui.click_pixel(x, y, _SCREEN_W, _SCREEN_H)
    time.sleep(settle_s)


def clear_focused(*, post_clear_s: float = 0.1) -> None:
    """Cmd+A + Backspace on the currently-focused regular text field.

    NEVER call this on a password verify sub-field: Cmd+A in a compound
    NSSecureTextField selects across both halves and wipes the just-typed
    left password.
    """
    with qmp.qmp() as c:
        c.send_chord(["meta_l", "a"])
        time.sleep(0.1)
        c.send_chord(["backspace"])
        time.sleep(post_clear_s)


def fill_field(
    x: int,
    y: int,
    value: str,
    *,
    clear: bool = True,
    label: str = "",
    settle_s: float = 0.5,
    gap_s: float = 0.15,
) -> None:
    """Click → optionally clear → type into a regular text field.

    *label* is only for logging.  Use ``fill_password_compound`` for the
    password row instead of this; the compound widget has different rules.

    gap_s=0.15 (not the QMP default 0.04) because faster typing drops chars:
    empirically the SA-8 Full Name field captured only "ai" out of "airtag"
    at gap_s=0.04 — macOS autocomplete/animation steals focus mid-typing.
    0.15s gives each keystroke time to register before the next one fires.
    """
    if label:
        emit("info", "sa_fields", f"filling {label}")
    click_field(x, y, settle_s=settle_s)
    if clear:
        clear_focused()
    with qmp.qmp() as c:
        c.type_text(value, gap_s=gap_s)
    time.sleep(settle_s)


def dismiss_character_picker() -> None:
    """Send Escape to close any macOS character picker QMP may have opened.

    The picker intercepts ALL subsequent key events until dismissed, so this
    is the first thing to do when entering a typing-heavy screen.
    """
    with qmp.qmp() as c:
        c.send_chord(["esc"])
    time.sleep(0.2)


def fill_password_compound(
    left_x: int,
    left_y: int,
    password: str,
    *,
    settle_after_left_s: float = 3.0,
    settle_after_tab_s: float = 0.8,
    settle_after_verify_s: float = 0.8,
) -> None:
    """Fill both halves of a compound NSSecureTextField.

    Sequence (empirically derived — see comments for the *why* of each):

    1. Pixel-click left half.  Tab from prev field is unreliable.
    2. Type password into left.  NEVER Cmd+A clear — that wipes both halves.
    3. Wait for the Requirements popover to close (popover steals Tab).
    4. Tab to verify (the only reliable way to reach the verify half;
       pixel-click always returns focus to the left half regardless of x).
    5. Type password into verify.  Again no Cmd+A.

    Typing strategy is governed by the ``AIRTAG_SA8_PW_STRATEGY`` env var:

    - ``qmp_slow`` (default): ``qmp.type_text(gap_s=0.15)`` for both halves.
      Known good for 12-char hex; FAILS for 32-char hex (root cause unknown).
    - ``qmp_fast``: ``gap_s=0.04`` — does the verify mismatch go away when
      typing is faster (theory: macOS verify-equality check is rate-limited)?
    - ``paste``: ``vm_ui.paste_text(password)`` via clipboard for both halves
      — bypasses QMP scancode mapping entirely.  Best candidate for fixing
      the 32-char failure.

    The strategy switch is the SINGLE PLACE to edit when iterating on the
    password problem via the snapshot harness — restore snapshot, change env,
    restart, replay.
    """
    strategy = os.environ.get("AIRTAG_SA8_PW_STRATEGY", "qmp_slow").lower()
    emit("info", "sa_fields", f"filling password (strategy={strategy})")

    # --- Left half ---
    click_field(left_x, left_y)
    _type_password(password, strategy)
    time.sleep(settle_after_left_s)

    # --- Tab to verify ---
    with qmp.qmp() as c:
        c.send_chord(["tab"])
    time.sleep(settle_after_tab_s)

    # --- Verify half ---
    _type_password(password, strategy)
    time.sleep(settle_after_verify_s)


def _type_password(password: str, strategy: str) -> None:
    """Single point of variance for the password typing experiment."""
    if strategy == "paste":
        # Clipboard paste — keymap-agnostic and instant.
        vm_ui.paste_text(password)
        return
    gap_s = 0.04 if strategy == "qmp_fast" else 0.15
    with qmp.qmp() as c:
        c.type_text(password, gap_s=gap_s)


# ---------------------------------------------------------------------------
# Error modal
# ---------------------------------------------------------------------------

# "Go Back" button is white-on-blue — OCR-blind.  Return key does NOT
# reach the modal in QEMU.  click_text("Go", "Back") hits the body text
# "click Go Back" instead of the button.  Pixel-only.
_GO_BACK_X, _GO_BACK_Y = 640, 492

_ERROR_KEYWORDS = (
    "passwords don't match", "passwords don",
    "haven't provided", "requested information",
    "hint can't contain", "hint cannot contain",
)


def dismiss_error_modal_if_present() -> bool:
    """If an error modal is showing, click Go Back and wait for it to dismiss.

    Returns True if a modal was found and dismissed.
    """
    from .. import screen

    if not screen.has_any_text(*_ERROR_KEYWORDS):
        return False
    emit("info", "sa_fields", "Error dialog on Create Account — clicking Go Back")
    vm_ui.click_pixel(_GO_BACK_X, _GO_BACK_Y, _SCREEN_W, _SCREEN_H)
    time.sleep(3.0)  # settle: sheet dismiss animation
    return True


def verify_advanced_or_classify_error(
    *,
    settle_s: float = 5.0,
    deadline_s: float = 90.0,
    poll_s: float = 2.0,
) -> str | None:
    """After Continue, wait for the screen to advance or return a known error.

    Returns:
    - ``None`` if the screen advanced (success).
    - ``"passwords_mismatch"`` — password halves disagreed.
    - ``"hint_contains_password"`` — Hint field contained the password.
    - ``"missing_field"`` — at least one required field was left empty.

    Caller maps these to the appropriate retry signal.

    Deadline is 90 s because account creation shows a "Creating account..."
    spinner state where the SA-8 title text "computer account" is still on
    screen for 30-60 s after Continue while macOS finalises the local user.
    Treating that as "still on SA-8" would otherwise trigger a false-
    positive missing_field retry — empirically the account IS created
    successfully but the next screen (Location Services etc.) hasn't
    rendered yet.  On timeout we now return None (assume advanced) so the
    engine moves forward; subsequent SA handlers detect what they actually
    see.
    """
    time.sleep(settle_s)
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        screen_txt = vm_ui.screen_text()
        if "passwords don't match" in screen_txt or "passwords don" in screen_txt:
            return "passwords_mismatch"
        if "hint can't contain" in screen_txt or "hint cannot contain" in screen_txt:
            return "hint_contains_password"
        if "haven't provided" in screen_txt or "requested information" in screen_txt:
            return "missing_field"
        if "computer account" not in screen_txt and "mac account" not in screen_txt:
            return None  # advanced past the create-account screen
        time.sleep(poll_s)
    # Timed out without seeing the screen change — most likely the account
    # creation spinner is just taking longer than expected.  Assume success;
    # the next handler will detect what's actually on screen.
    return None
