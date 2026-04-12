"""Step 2 — Recovery Utilities → Terminal → format blank disk as APFS.

Entry: Recovery Utilities picker (menu bar reads
``Recovery  File  Edit  Utilities  Window``).

Action: open Utilities menu, pick Terminal, then run
``diskutil eraseDisk APFS Macintosh-HD disk0``. The blank 128 GiB qcow2
appears as ``disk0`` (physical, ~137.4 GB) — BaseSystem is on ``disk1``
and OpenCore on ``disk2``.

Exit: APFS volume ``Macintosh-HD`` mounted at ``/Volumes/Macintosh-HD``.
Terminal stays open for the next step (reinstall via ``startosinstall``).

The volume name uses a hyphen rather than a space so the command can be
typed without shell quoting. Later steps must match.
"""

from __future__ import annotations

from ..driver import Driver

UTILITIES_MENU = (243, 12)
TERMINAL_ITEM = (240, 64)


def run(driver: Driver) -> None:
    driver.click(*UTILITIES_MENU, post_delay=0.5)
    driver.click(*TERMINAL_ITEM, post_delay=3.0)
    driver.type_text("diskutil eraseDisk APFS Macintosh-HD disk0")
    driver.key("ret")
    driver.wait(8.0)
