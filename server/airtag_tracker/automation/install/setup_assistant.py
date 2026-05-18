"""macOS Setup Assistant automation.

Walks all 13 screens of the first-boot Setup Assistant that appear after
a fresh macOS installation, creating the local ``airtag`` account and
dismissing every optional service screen.

Text input uses ``vm_ui.paste_text()`` (clipboard via SSH + cmd-v) rather
than QMP keystroke sequences so that the password survives any keyboard-
layout difference.
"""

from __future__ import annotations

import time

from ... import qmp, vm_ui, vm_password
from ...events import emit
from ..context import AutomationContext
from ..states import InstallState
from .. import screen


def run(ctx: AutomationContext) -> InstallState:
    """Walk all 13 Setup Assistant screens and land on the desktop."""
    password = vm_password.ensure()
    emit("info", "setup_assistant", "Starting Setup Assistant automation")

    # ------------------------------------------------------------------
    # 1. Country or Region
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 1: Country or Region")
    if not screen.has_text("Country", "Region", deadline_s=120, poll_s=3.0):
        raise RuntimeError("Setup Assistant 'Country or Region' screen not reached within 120s")
    qmp.type_text("united sta")
    time.sleep(1.0)
    qmp.send_keys(["ret"])
    time.sleep(1.0)
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Country screen")

    # ------------------------------------------------------------------
    # 2. Written and Spoken Languages
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 2: Written and Spoken Languages")
    if not screen.wait_click_text("Continue", deadline_s=15):
        raise RuntimeError("Could not click Continue on Written and Spoken Languages screen")

    # ------------------------------------------------------------------
    # 3. Accessibility
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 3: Accessibility")
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Accessibility screen")

    # ------------------------------------------------------------------
    # 4. Data & Privacy
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 4: Data & Privacy")
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Data & Privacy screen")

    # ------------------------------------------------------------------
    # 5. Migration Assistant
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 5: Migration Assistant")
    if not screen.wait_click_text("Not", "Now", deadline_s=10):
        raise RuntimeError("Could not click 'Not Now' on Migration Assistant screen")

    # ------------------------------------------------------------------
    # 6. Apple ID sign-in
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 6: Apple ID")
    if not screen.wait_click_text("Set", "Up", deadline_s=10):
        # Fallback: the button may read "Set Up Later" as a single span.
        if not screen.wait_click_text("Later", deadline_s=5):
            raise RuntimeError("Could not click 'Set Up Later' on Apple ID screen")
    # Confirmation sheet: "Skip"
    if not screen.wait_click_text("Skip", deadline_s=5):
        raise RuntimeError("Could not click 'Skip' on Apple ID confirmation sheet")

    # ------------------------------------------------------------------
    # 7. Terms and Conditions
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 7: Terms and Conditions")
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Terms and Conditions screen")
    if not screen.wait_click_text("Agree", deadline_s=5):
        raise RuntimeError("Could not click 'Agree' on Terms confirmation sheet")

    # ------------------------------------------------------------------
    # 8. Create a Computer Account
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 8: Create a Computer Account")
    if not screen.has_text("Computer Account", deadline_s=30, poll_s=2.0):
        raise RuntimeError("'Create a Computer Account' screen not reached within 30s")

    # Full name field is focused by default.
    # SSH is not available during Setup Assistant (no user account yet), so
    # use QMP type_text.  vm_password.ensure() generates only URL-safe chars
    # (A-Za-z0-9 + '-' + '_') which _ascii_to_chord handles correctly.
    qmp.type_text("airtag")
    # Tab past the account name field (auto-filled from full name) and into password.
    qmp.send_keys(["tab", "tab"])
    time.sleep(0.3)
    qmp.type_text(password)
    # Tab into the password-verify field.
    qmp.send_keys(["tab"])
    time.sleep(0.3)
    qmp.type_text(password)

    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Create Account screen")

    # Account creation can take a moment — wait up to 30 s for the screen to advance.
    time.sleep(5.0)
    t0 = time.time()
    while time.time() - t0 < 25.0:
        if not screen.has_any_text("Computer Account"):
            break
        time.sleep(2.0)

    # ------------------------------------------------------------------
    # 9. Location Services
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 9: Location Services")
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Location Services screen")
    if not screen.wait_click_text("Don't", "Use", deadline_s=5):
        raise RuntimeError("Could not click \"Don't Use\" on Location Services sheet")

    # ------------------------------------------------------------------
    # 10. Time Zone
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 10: Time Zone")
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Time Zone screen")

    # ------------------------------------------------------------------
    # 11. Analytics / Share with Apple
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 11: Analytics")
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Analytics screen")

    # ------------------------------------------------------------------
    # 12. Screen Time
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 12: Screen Time")
    if not screen.wait_click_text("Set", "Up", deadline_s=10):
        # Fallback for "Set Up Later" as a single OCR token.
        if not screen.wait_click_text("Later", deadline_s=5):
            raise RuntimeError("Could not click 'Set Up Later' on Screen Time screen")

    # ------------------------------------------------------------------
    # 13. Appearance / Choose Your Look
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Screen 13: Appearance")
    if not screen.wait_click_text("Continue", deadline_s=10):
        raise RuntimeError("Could not click Continue on Appearance screen")

    # ------------------------------------------------------------------
    # Wait for desktop (Finder menu bar)
    # ------------------------------------------------------------------
    emit("info", "setup_assistant", "Waiting for desktop (Finder)…")
    if not screen.has_text("Finder", deadline_s=300, poll_s=3.0):
        raise RuntimeError("Desktop (Finder) not detected within 300s after Setup Assistant")

    emit("info", "setup_assistant", "Setup Assistant complete — desktop reached")
    return InstallState.DISMISS_FIRST_BOOT
