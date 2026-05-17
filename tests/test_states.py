"""Smoke tests for the state machine foundation — no VM required."""

from __future__ import annotations

import threading
import time

from airtag_tracker.automation.context import AutomationContext
from airtag_tracker.automation.states import (
    FlowKind,
    InstallState,
    RuntimeState,
)


def _make_install_ctx() -> AutomationContext:
    return AutomationContext(
        flow_kind=FlowKind.INSTALL,
        vm_password="testpw",
    )


def _make_runtime_ctx() -> AutomationContext:
    return AutomationContext(
        flow_kind=FlowKind.RUNTIME,
        vm_password="testpw",
        apple_email="test@example.com",
        apple_password="secret",
    )


def test_initial_install_state():
    ctx = _make_install_ctx()
    assert ctx.state == InstallState.IDLE
    assert ctx.error is None


def test_initial_runtime_state():
    ctx = _make_runtime_ctx()
    assert ctx.state == RuntimeState.IDLE


def test_set_state_transitions():
    ctx = _make_install_ctx()
    ctx.set_state(InstallState.BOOTING_PICKER)
    assert ctx.state == InstallState.BOOTING_PICKER


def test_set_state_with_error():
    ctx = _make_install_ctx()
    ctx.set_state(InstallState.ERROR, error="disk not found")
    assert ctx.state == InstallState.ERROR
    assert ctx.error == "disk not found"


def test_abort():
    ctx = _make_runtime_ctx()
    assert not ctx.aborted
    ctx.request_abort()
    assert ctx.aborted


def test_2fa_deliver_and_receive():
    ctx = _make_runtime_ctx()

    received = []

    def producer():
        time.sleep(0.05)
        ctx.deliver_2fa("123456")

    t = threading.Thread(target=producer, daemon=True)
    t.start()

    code = ctx.wait_for_2fa(timeout_s=2.0)
    assert code == "123456"
    t.join()


def test_2fa_timeout():
    ctx = _make_runtime_ctx()
    try:
        ctx.wait_for_2fa(timeout_s=0.05)
        assert False, "should have raised"
    except TimeoutError:
        pass


def test_sms_request():
    ctx = _make_runtime_ctx()
    assert not ctx.sms_was_requested()
    ctx.request_sms()
    assert ctx.sms_was_requested()
    assert not ctx.sms_was_requested()  # consumed


def test_broadcast_called_on_set_state():
    ctx = _make_runtime_ctx()
    events = []
    ctx._broadcast = events.append
    ctx.set_state(RuntimeState.BOOTING)
    assert len(events) == 1
    assert events[0]["type"] == "state"
    assert events[0]["state"] == "booting"


def test_stage_labels_coverage():
    from airtag_tracker.automation.states import (
        INSTALL_STAGE_LABELS,
        RUNTIME_STAGE_LABELS,
    )
    for s in InstallState:
        assert s in INSTALL_STAGE_LABELS, f"Missing label for {s}"
    for s in RuntimeState:
        assert s in RUNTIME_STAGE_LABELS, f"Missing label for {s}"
