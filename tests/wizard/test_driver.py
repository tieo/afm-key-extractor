"""Unit tests for the QMP/monitor driver using fake transports."""

from __future__ import annotations

import json

from server.wizard.driver import ABS_MAX, FB_HEIGHT, FB_WIDTH, Driver


class FakeTransport:
    def __init__(self, canned: list[bytes] | None = None) -> None:
        self.sent: list[bytes] = []
        self._canned = list(canned or [])

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        return self._canned.pop(0) if self._canned else b""

    def close(self) -> None:
        pass


def _make_driver() -> tuple[Driver, FakeTransport, FakeTransport]:
    # QMP responds to greeting + capabilities + each send-key.
    qmp = FakeTransport(
        canned=[
            b'{"QMP":{"version":{}}}\n',  # greeting
            b'{"return":{}}\n',  # capabilities ack
        ]
        + [b'{"return":{}}\n'] * 20
    )
    mon = FakeTransport()
    return Driver(qmp, mon, sleep=lambda _: None), qmp, mon


def test_key_sends_qmp_send_key_command():
    d, qmp, _ = _make_driver()
    d.key("right")
    payloads = [json.loads(s.strip()) for s in qmp.sent if s.strip()]
    cap = [p for p in payloads if p.get("execute") == "qmp_capabilities"]
    send_key = [p for p in payloads if p.get("execute") == "send-key"]
    assert len(cap) == 1
    assert len(send_key) == 1
    assert send_key[0]["arguments"]["keys"] == [{"type": "qcode", "data": "right"}]


def test_click_maps_pixel_to_absolute_axis():
    d, qmp, _ = _make_driver()
    d.click(640, 400)
    payloads = [json.loads(s.strip()) for s in qmp.sent if s.strip()]
    abs_event = next(
        p
        for p in payloads
        if p.get("execute") == "input-send-event"
        and any(e["type"] == "abs" for e in p["arguments"]["events"])
    )
    events = abs_event["arguments"]["events"]
    # Single event with both axes — fixes the "axes in separate events"
    # bug that caused the previous attempt's off-by-15px clicks.
    assert len(events) == 2
    axes = {e["data"]["axis"]: e["data"]["value"] for e in events}
    assert axes["x"] == int(640 / FB_WIDTH * ABS_MAX)
    assert axes["y"] == int(400 / FB_HEIGHT * ABS_MAX)


def test_click_sends_btn_down_and_up():
    d, qmp, _ = _make_driver()
    d.click(100, 100)
    payloads = [json.loads(s.strip()) for s in qmp.sent if s.strip()]
    btn_events = [
        e
        for p in payloads
        if p.get("execute") == "input-send-event"
        for e in p["arguments"]["events"]
        if e["type"] == "btn"
    ]
    assert [e["data"]["down"] for e in btn_events] == [True, False]


def test_capabilities_negotiated_only_once():
    d, qmp, _ = _make_driver()
    d.key("a")
    d.key("b")
    cap_count = sum(
        1
        for s in qmp.sent
        if s.strip() and json.loads(s.strip()).get("execute") == "qmp_capabilities"
    )
    assert cap_count == 1


def test_screenshot_writes_file_and_reads_back(tmp_path):
    dest = tmp_path / "shot.ppm"
    expected = b"P6\n1 1\n255\n\xff\x00\x00"

    class WritingMonitor(FakeTransport):
        def send(self, data: bytes) -> None:
            super().send(data)
            if b"screendump" in data:
                dest.write_bytes(expected)

    qmp = FakeTransport(
        canned=[b'{"QMP":{}}\n', b'{"return":{}}\n'] + [b'{"return":{}}\n'] * 5
    )
    mon = WritingMonitor()
    d = Driver(qmp, mon, sleep=lambda _: None)

    data = d.screenshot(dest)
    assert data == expected
    assert any(b"screendump" in s for s in mon.sent)
