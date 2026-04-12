"""Step 5 — Keyboard Assistant dismissal + re-login."""

from __future__ import annotations

from server.wizard.steps import finalize


class RecordingDriver:
    def __init__(self):
        self.events = []

    def click(self, x, y, post_delay=0.0):
        self.events.append(("click", (x, y)))

    def key(self, qcode, post_delay=0.0):
        self.events.append(("key", qcode))

    def type_text(self, text, post_delay=0.0):
        self.events.append(("type", text))


def test_finalize_quits_keyboard_assistant_then_logs_in():
    d = RecordingDriver()
    finalize.run(d, password="pw")
    assert d.events == [
        ("click", finalize.KBD_QUIT),
        ("type", "pw"),
        ("key", "ret"),
    ]
