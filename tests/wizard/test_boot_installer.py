"""Step 1 — OpenCore boot picker → installer boot."""

from __future__ import annotations

from server.wizard.steps import boot_installer


class RecordingDriver:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def key(self, qcode, post_delay=0.0):
        self.events.append(("key", qcode))

    def click(self, x, y, post_delay=0.0):
        self.events.append(("click", (x, y)))

    def screenshot(self, path):
        self.events.append(("screenshot", str(path)))
        return b""


def test_boot_installer_sends_right_then_return():
    d = RecordingDriver()
    boot_installer.run(d)
    assert d.events == [("key", "right"), ("key", "ret")]
