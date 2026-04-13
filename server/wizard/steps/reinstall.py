"""Step 3 — Recovery Utilities → Reinstall macOS Ventura → installer runs.

Entry: Recovery Utilities picker (Terminal just quit via Cmd+Q).
Row 1 "Restore from Time Machine" is the default highlighted row, so
clicking Continue without first selecting row 2 lands on the wrong
screen — we click the Ventura icon explicitly.

Action:

1. Click the Ventura icon (row 2) → click Continue.
2. Initial "To set up the installation of macOS Ventura, click
   Continue." → click Continue.
3. License agreement appears → click Agree.
4. Confirmation sheet "I have read and agree …" → click Agree.
5. Disk picker (Macintosh-HD vs macOS Base System) → click Macintosh-HD
   → click Continue.
6. Installer downloads + installs. Progress bar with ETA. Can take
   20–45 min on a warm VM; first run downloads several GB.

Exit: installer finishes, VM reboots from the freshly installed disk.

Known timing issues: the "Continue" button in step 2 can take 10+ s to
actually advance after clicking. A single screendump immediately after
isn't reliable; the caller should poll.
"""

from __future__ import annotations

from ..driver import Driver

REINSTALL_ICON = (466, 277)
PICKER_CONTINUE = (830, 521)
INSTALL_CONTINUE = (640, 643)
LICENSE_AGREE = (693, 638)
CONFIRM_AGREE = (700, 450)
MACINTOSH_HD = (510, 440)
DEST_CONTINUE = (686, 640)


def run(driver: Driver) -> None:
    driver.click(*REINSTALL_ICON, post_delay=0.5)
    driver.click(*PICKER_CONTINUE, post_delay=5.0)
    driver.click(*INSTALL_CONTINUE, post_delay=12.0)
    driver.click(*LICENSE_AGREE, post_delay=4.0)
    driver.click(*CONFIRM_AGREE, post_delay=6.0)
    driver.click(*MACINTOSH_HD, post_delay=2.0)
    driver.click(*DEST_CONTINUE, post_delay=5.0)
