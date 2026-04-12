"""Step 1 — OpenCore boot picker → Recovery Utilities picker.

Entry: QEMU just started with BaseSystem + blank MacHDD attached.
OpenCore shows two entries: EFI (default, left) and macOS Base System
(right). Default selection auto-boots after a short timeout.

Action: send ``right`` then ``ret`` to select and boot the installer.

Exit: Recovery Utilities picker (dark grey dialog, four rows, Continue
button). Arriving there takes ~60–90 s while the installer kernel
boots.
"""

from __future__ import annotations

from ..driver import Driver


def run(driver: Driver) -> None:
    """Select macOS Base System and press Enter."""
    driver.key("right", post_delay=0.5)
    driver.key("ret", post_delay=0.5)
