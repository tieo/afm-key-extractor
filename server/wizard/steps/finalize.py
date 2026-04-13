"""Step 5 — dismiss first-boot modals and shut down for golden promotion.

Entry: desktop reached via ``setup_assistant``. First boot pops up a
``Keyboard Setup Assistant`` dialog that blocks input focus — we
dismiss it with Quit. That logs the user back out to the login screen,
which is the chosen golden-image state (services SSH in with the known
password; no auto-login surface for accidental input).

The ACPI power button is ignored at the macOS login screen without an
active user session, so powerdown requires signing the user in first
so macOS accepts shutdown.

Exit: VM halted cleanly; caller may then
``cp mac_hdd_ng.img mac_hdd_golden.img``.
"""

from __future__ import annotations

from ..driver import Driver

KBD_QUIT = (915, 688)


def run(driver: Driver, password: str = "airtag") -> None:
    driver.click(*KBD_QUIT, post_delay=3.0)
    # Quit drops us to the login screen. Log back in so shutdown works.
    driver.type_text(password)
    driver.key("ret", post_delay=15.0)
