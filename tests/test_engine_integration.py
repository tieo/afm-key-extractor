"""Integration smoke tests for the state machine engine and FastAPI layer.

No real VM, QMP socket, or SSH required — all hardware interactions are
patched out.  These tests verify that:

- The engine transitions through states in the right order.
- Errors land in the ERROR state.
- Abort stops the engine cleanly.
- The retry counter escalates to ERROR after MAX_RETRIES same-state returns.
- FastAPI endpoints return sensible shapes when no flow is running.
- The 2FA endpoint rejects requests when no context exists.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from airtag_tracker.automation import engine as eng
from airtag_tracker.automation.context import AutomationContext
from airtag_tracker.automation.states import FlowKind, InstallState, RuntimeState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_ctx() -> AutomationContext:
    return AutomationContext(flow_kind=FlowKind.INSTALL, vm_password="pw")


def _runtime_ctx() -> AutomationContext:
    return AutomationContext(
        flow_kind=FlowKind.RUNTIME,
        vm_password="pw",
        apple_email="a@b.com",
        apple_password="secret",
    )


def _reset_engine() -> None:
    """Reset the module-level engine singleton between tests."""
    eng._engine._thread = None
    eng._ctx = None


def _noop_broadcast(event: dict) -> None:
    pass


# ---------------------------------------------------------------------------
# Engine: install flow
# ---------------------------------------------------------------------------

def test_engine_install_happy_path():
    """Engine drives through a short fake install flow and reaches DONE."""
    _reset_engine()
    ctx = _install_ctx()

    # Map every InstallState to a handler that immediately advances.
    advance = {
        InstallState.IDLE:               lambda c: InstallState.SETUP_ASSISTANT,
        InstallState.SETUP_ASSISTANT:    lambda c: InstallState.DONE,
        InstallState.DONE:               lambda c: InstallState.DONE,  # terminal
        InstallState.ERROR:              lambda c: InstallState.ERROR,  # terminal
    }

    with patch.object(eng, "_get_handler", side_effect=lambda s: advance.get(s, lambda c: InstallState.DONE)):
        eng.start_flow(ctx, _noop_broadcast)
        # Give the background thread up to 2 s to finish.
        deadline = time.time() + 2
        while time.time() < deadline and eng._engine.is_running:
            time.sleep(0.05)

    assert ctx.state == InstallState.DONE, f"Expected DONE, got {ctx.state}"
    assert ctx.error is None


def test_engine_handler_raises_goes_to_error():
    """A RuntimeError in a handler transitions to ERROR with the message."""
    _reset_engine()
    ctx = _install_ctx()

    def _boom(c):
        raise RuntimeError("disk not found")

    advance = {
        InstallState.IDLE:  _boom,
        InstallState.ERROR: lambda c: InstallState.ERROR,
    }
    with patch.object(eng, "_get_handler", side_effect=lambda s: advance.get(s, _boom)):
        eng.start_flow(ctx, _noop_broadcast)
        deadline = time.time() + 2
        while time.time() < deadline and eng._engine.is_running:
            time.sleep(0.05)

    assert ctx.state == InstallState.ERROR
    assert "disk not found" in (ctx.error or "")


def test_engine_retry_exhaustion_goes_to_error():
    """Same-state returns exceed MAX_RETRIES → ERROR."""
    _reset_engine()
    ctx = _install_ctx()

    # Always return IDLE from IDLE — never advance.
    stuck = lambda c: InstallState.IDLE

    advance = {
        InstallState.IDLE:  stuck,
        InstallState.ERROR: lambda c: InstallState.ERROR,
    }
    # Zero retry delay so the test finishes in milliseconds.
    with patch.object(eng, "RETRY_DELAY_S", 0.0), \
         patch.object(eng, "_get_handler", side_effect=lambda s: advance.get(s, stuck)):
        eng.start_flow(ctx, _noop_broadcast)
        deadline = time.time() + 2
        while time.time() < deadline and eng._engine.is_running:
            time.sleep(0.05)

    assert ctx.state == InstallState.ERROR


def test_engine_abort_stops_cleanly():
    """Abort flag stops the engine before ERROR is reached."""
    _reset_engine()
    ctx = _install_ctx()

    def _abort_me(c: AutomationContext):
        c.request_abort()
        return InstallState.IDLE

    advance = {
        InstallState.IDLE:  _abort_me,
        InstallState.ERROR: lambda c: InstallState.ERROR,
    }
    # Zero retry delay so we don't wait 3s per retry while abort propagates.
    with patch.object(eng, "RETRY_DELAY_S", 0.0), \
         patch.object(eng, "_get_handler", side_effect=lambda s: advance.get(s, _abort_me)):
        eng.start_flow(ctx, _noop_broadcast)
        deadline = time.time() + 2
        while time.time() < deadline and eng._engine.is_running:
            time.sleep(0.05)

    assert not eng._engine.is_running
    assert ctx.state != InstallState.ERROR


# ---------------------------------------------------------------------------
# Engine: runtime flow + 2FA
# ---------------------------------------------------------------------------

def test_engine_runtime_2fa_deliver():
    """Engine pauses at AWAITING_2FA until a code is delivered."""
    _reset_engine()
    ctx = _runtime_ctx()
    delivered = threading.Event()

    def _await_2fa(c: AutomationContext):
        code = c.wait_for_2fa(timeout_s=3.0)
        assert code == "123456"
        delivered.set()
        return RuntimeState.DONE

    advance = {
        RuntimeState.IDLE:        lambda c: RuntimeState.AWAITING_2FA,
        RuntimeState.AWAITING_2FA: _await_2fa,
        RuntimeState.DONE:        lambda c: RuntimeState.DONE,
        RuntimeState.ERROR:       lambda c: RuntimeState.ERROR,
    }

    # suppress popup_watcher for this test
    with patch("airtag_tracker.automation.popup_watcher.start"), \
         patch("airtag_tracker.automation.popup_watcher.stop"), \
         patch.object(eng, "_get_handler", side_effect=lambda s: advance.get(s, lambda c: RuntimeState.DONE)):
        eng.start_flow(ctx, _noop_broadcast)
        time.sleep(0.1)  # let the engine reach AWAITING_2FA
        ctx.deliver_2fa("123456")
        delivered.wait(timeout=3)

    assert delivered.is_set(), "2FA was never consumed by engine"


# ---------------------------------------------------------------------------
# FastAPI: basic endpoints
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """TestClient with all heavy VM/QMP imports mocked out."""
    # Reset engine singleton so no stale context from earlier engine tests leaks in.
    _reset_engine()
    with patch("airtag_tracker.vm.is_running", return_value=False), \
         patch("airtag_tracker.vm.status", return_value={"enabled": False}):
        from airtag_tracker.api.app import create_app
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def test_api_status_idle(client):
    resp = client.get("/api/automation/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "idle"
    assert data["running"] is False
    assert data["flow"] is None


def test_api_vm_status(client):
    resp = client.get("/api/vm/status")
    assert resp.status_code == 200
    assert "enabled" in resp.json()


def test_api_log_empty(client):
    resp = client.get("/api/log")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_api_2fa_no_context(client):
    """Submitting 2FA with no active context returns 400."""
    # Reset engine state so there's no context.
    eng._ctx = None
    resp = client.post("/api/vm/apple-signin/2fa", json={"code": "123456"})
    assert resp.status_code == 400


def test_api_request_sms_no_context(client):
    eng._ctx = None
    resp = client.post("/api/vm/apple-signin/request-sms")
    assert resp.status_code == 400


def test_api_keys_list(client):
    resp = client.get("/api/keys/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_api_root_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
