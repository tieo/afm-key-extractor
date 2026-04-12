"""Step 2 — Terminal + diskutil eraseDisk."""

from __future__ import annotations

from server.wizard.steps import format_disk


class RecordingDriver:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def key(self, qcode, post_delay=0.0):
        self.events.append(("key", qcode))

    def click(self, x, y, post_delay=0.0):
        self.events.append(("click", (x, y)))

    def type_text(self, text, post_delay=0.0):
        self.events.append(("type", text))

    def wait(self, seconds):
        self.events.append(("wait", seconds))


def test_format_disk_opens_terminal_and_runs_diskutil():
    d = RecordingDriver()
    format_disk.run(d)
    assert d.events == [
        ("click", format_disk.UTILITIES_MENU),
        ("click", format_disk.TERMINAL_ITEM),
        ("type", "diskutil eraseDisk APFS Macintosh-HD disk0"),
        ("key", "ret"),
        ("wait", 8.0),
    ]
