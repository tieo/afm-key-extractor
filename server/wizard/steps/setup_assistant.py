"""Step 4 — drive Setup Assistant to a working desktop.

Entry: freshly installed Ventura rebooted into Setup Assistant, first
screen = Country picker. This is the boot we get after step 3's
installer finishes and OpenCore auto-selects Macintosh-HD.

We create a local admin user ``airtag`` / ``airtag`` inside Setup
Assistant rather than planting it offline via ``dscl`` — SA is the
reliable path, and scripted click-through is simpler to maintain than
working around Ventura's sealed system volume.

Screens walked (all native 1280×800):

1. Select Your Country or Region — type "united sta" then click the
   primary Continue button. List filters as you type; typing advances
   the highlighted row to United States.
2. Written and Spoken Languages — accept English (US) default. Click
   Continue.
3. Accessibility — Continue (no features enabled).
4. Data & Privacy — Continue.
5. Migration Assistant — click "Not Now" (blue text, bottom-left at
   roughly (285, 672)). Migration is irrelevant for a fresh build.
6. Sign In with Your Apple ID — click "Set Up Later" (blue text,
   around (300, 671)); confirm with "Skip" via Return (Skip is the
   default button).
7. Terms and Conditions — click Agree (primary button), then Agree
   again on the confirmation sheet at roughly (743, 480). The
   keyboard default in the sheet is *Disagree*, so the click is not
   optional.
8. Create a Computer Account — Full Name field is auto-focused. Type
   ``airtag``, Tab twice (skip Account name), type password twice
   (Tab between), Continue.
9. Enable Location Services — Continue, then Return on the "Are you
   sure" confirmation (Don't Use is the default).
10. Select Your Time Zone — accept Pacific default. Continue.
11. Analytics — Continue.
12. Screen Time — "Set Up Later" at the same (300, 672).
13. Choose Your Look — Continue (Light default).

Exit: macOS desktop with user ``airtag`` logged in. A Keyboard Setup
Assistant dialog pops up on first boot asking to identify the keyboard
type; we dismiss it with Quit. An "Upgrade to macOS Tahoe"
notification also appears — we ignore it (the wizard's job is done).

Primary Continue button: the same position in most screens, roughly
(985, 660) — measured once on the Languages screen and reused.
"""

from __future__ import annotations

from ..driver import Driver

CONTINUE = (985, 660)
BLUE_LINK_LEFT = (300, 672)      # "Not Now" / "Set Up Later" bottom-left link
CONFIRM_AGREE = (743, 480)       # Agree inside T&C confirmation sheet


def run(driver: Driver, username: str = "airtag", password: str = "airtag") -> None:
    # 1. Country — type to filter then Continue.
    driver.type_text("united sta", post_delay=0.5)
    driver.key("ret", post_delay=1.0)
    driver.click(*CONTINUE, post_delay=5.0)
    # 2. Written and Spoken Languages
    driver.click(*CONTINUE, post_delay=4.0)
    # 3. Accessibility
    driver.click(*CONTINUE, post_delay=4.0)
    # 4. Data & Privacy
    driver.click(*CONTINUE, post_delay=4.0)
    # 5. Migration Assistant — Not Now
    driver.click(*BLUE_LINK_LEFT, post_delay=4.0)
    # 6. Apple ID — Set Up Later then confirm Skip with Return
    driver.click(*BLUE_LINK_LEFT, post_delay=2.0)
    driver.key("ret", post_delay=4.0)
    # 7. Terms — Agree, then Agree on sheet
    driver.click(*CONTINUE, post_delay=2.0)
    driver.click(*CONFIRM_AGREE, post_delay=4.0)
    # 8. Create a Computer Account
    driver.type_text(username)
    driver.key("tab")
    driver.key("tab")
    driver.type_text(password)
    driver.key("tab")
    driver.type_text(password)
    driver.click(*CONTINUE, post_delay=15.0)
    # 9. Location Services — skip, confirm with Return
    driver.click(*CONTINUE, post_delay=2.0)
    driver.key("ret", post_delay=4.0)
    # 10. Time Zone
    driver.click(*CONTINUE, post_delay=4.0)
    # 11. Analytics
    driver.click(*CONTINUE, post_delay=4.0)
    # 12. Screen Time — Set Up Later
    driver.click(*BLUE_LINK_LEFT, post_delay=4.0)
    # 13. Choose Your Look — Continue
    driver.click(*CONTINUE, post_delay=15.0)
