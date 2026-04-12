"""Step 4 — Setup Assistant click-through + account creation."""

from __future__ import annotations

from server.wizard.steps import setup_assistant


class RecordingDriver:
    def __init__(self):
        self.events = []

    def click(self, x, y, post_delay=0.0):
        self.events.append(("click", (x, y)))

    def key(self, qcode, post_delay=0.0):
        self.events.append(("key", qcode))

    def type_text(self, text, post_delay=0.0):
        self.events.append(("type", text))


def test_setup_assistant_creates_account_and_skips_optional_screens():
    d = RecordingDriver()
    setup_assistant.run(d, username="tester", password="pw")

    types = [e for e in d.events if e[0] == "type"]
    assert ("type", "tester") in types
    # Password is typed twice (new + verify).
    assert sum(1 for e in types if e == ("type", "pw")) == 2

    clicks = [e[1] for e in d.events if e[0] == "click"]
    assert setup_assistant.BLUE_LINK_LEFT in clicks  # Not Now + Set Up Later + Screen Time
    assert clicks.count(setup_assistant.BLUE_LINK_LEFT) == 3
    assert setup_assistant.CONFIRM_AGREE in clicks
