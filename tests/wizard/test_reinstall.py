"""Step 3 — Reinstall macOS Ventura from Recovery picker."""

from __future__ import annotations

from server.wizard.steps import reinstall


class RecordingDriver:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def click(self, x, y, post_delay=0.0):
        self.clicks.append((x, y))


def test_reinstall_clicks_through_installer_wizard():
    d = RecordingDriver()
    reinstall.run(d)
    assert d.clicks == [
        reinstall.REINSTALL_ICON,
        reinstall.PICKER_CONTINUE,
        reinstall.INSTALL_CONTINUE,
        reinstall.LICENSE_AGREE,
        reinstall.CONFIRM_AGREE,
        reinstall.MACINTOSH_HD,
        reinstall.DEST_CONTINUE,
    ]
