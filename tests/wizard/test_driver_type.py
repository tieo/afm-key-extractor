"""Type-text primitive — ASCII → qcode sequences."""

from __future__ import annotations

import json

from server.wizard.driver import Driver


class FakeTransport:
    def __init__(self, canned=None):
        self.sent = []
        self._canned = list(canned or [])

    def send(self, data):
        self.sent.append(data)

    def recv(self, n):
        return self._canned.pop(0) if self._canned else b""

    def close(self):
        pass


def _driver():
    qmp = FakeTransport(
        canned=[b'{"QMP":{}}\n', b'{"return":{}}\n'] + [b'{"return":{}}\n'] * 50
    )
    return Driver(qmp, FakeTransport(), sleep=lambda _: None), qmp


def _send_key_payloads(qmp):
    out = []
    for s in qmp.sent:
        if not s.strip():
            continue
        p = json.loads(s.strip())
        if p.get("execute") == "send-key":
            out.append([k["data"] for k in p["arguments"]["keys"]])
    return out


def test_type_text_lowercase_letters():
    d, qmp = _driver()
    d.type_text("abc")
    assert _send_key_payloads(qmp) == [["a"], ["b"], ["c"]]


def test_type_text_uppercase_uses_shift():
    d, qmp = _driver()
    d.type_text("Ab")
    assert _send_key_payloads(qmp) == [["shift", "a"], ["b"]]


def test_type_text_space_and_hyphen():
    d, qmp = _driver()
    d.type_text("a-b c")
    assert _send_key_payloads(qmp) == [["a"], ["minus"], ["b"], ["spc"], ["c"]]


def test_type_text_diskutil_command():
    d, qmp = _driver()
    d.type_text("diskutil eraseDisk APFS Macintosh-HD disk0")
    # Sanity: 42 characters → 42 send-key commands.
    assert len(_send_key_payloads(qmp)) == 42
