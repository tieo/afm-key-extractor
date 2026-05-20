"""Shared Apple ID pane navigation helpers.

`open_apple_id_pane()` was duplicated byte-for-byte in apple_signin and
post_signin; the URL/keyword constants were duplicated too.  Both modules
now import from here.
"""

from __future__ import annotations

from ... import vm_ui

# Ventura+ deep-links: try the modern bundle first, fall back to the legacy one.
APPLE_ID_URLS = (
    ("com.apple.systempreferences.AppleIDSettings", None),
    ("com.apple.preferences.AppleIDPrefPane", None),
)

# Distinctive text on the Apple ID Settings pane — used both to confirm the
# pane rendered and to detect "already signed in" (sign out is only visible
# when an account is active).
APPLE_ID_LANDED_KEYWORDS = (
    "one account for everything", "apple id", "sign in",
    "icloud", "family sharing", "media & purchases", "sign out",
)


def open_apple_id_pane(settle_s: float = 6.0, landed_deadline_s: int = 20) -> None:
    """Open System Settings → Apple ID. Tries each URL until the pane renders.

    Raises RuntimeError if all URLs fail or the pane never confirms via OCR.
    """
    last = ""
    for bundle, anchor in APPLE_ID_URLS:
        try:
            vm_ui.open_settings_pane(bundle, anchor, settle_s=settle_s)
        except Exception as e:
            last = str(e)
            continue
        if vm_ui.wait_for_text(APPLE_ID_LANDED_KEYWORDS, deadline_s=landed_deadline_s):
            return
        last = f"{bundle} opened but Apple ID pane never rendered"
    raise RuntimeError(f"could not open Apple ID pane: {last[:200]}")
