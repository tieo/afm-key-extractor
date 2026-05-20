"""Tests for the wait_until / wait_for helpers."""

from __future__ import annotations

import time

from airtag_tracker.automation.wait import wait_for, wait_until


def test_wait_until_returns_true_on_immediate_success():
    assert wait_until(lambda: True, deadline_s=1.0, poll_s=0.01) is True


def test_wait_until_returns_false_on_timeout():
    t0 = time.monotonic()
    assert wait_until(lambda: False, deadline_s=0.15, poll_s=0.01) is False
    elapsed = time.monotonic() - t0
    assert 0.10 < elapsed < 0.5


def test_wait_until_polls_until_true():
    counter = {"n": 0}

    def becomes_true():
        counter["n"] += 1
        return counter["n"] >= 3

    assert wait_until(becomes_true, deadline_s=1.0, poll_s=0.01) is True
    assert counter["n"] == 3


def test_wait_until_swallows_exceptions():
    counter = {"n": 0}

    def maybe_raise():
        counter["n"] += 1
        if counter["n"] < 3:
            raise RuntimeError("not yet")
        return True

    assert wait_until(maybe_raise, deadline_s=1.0, poll_s=0.01) is True


def test_wait_for_returns_value():
    counter = {"n": 0}

    def get_value():
        counter["n"] += 1
        return "found" if counter["n"] >= 2 else None

    assert wait_for(get_value, deadline_s=1.0, poll_s=0.01) == "found"


def test_wait_for_returns_none_on_timeout():
    assert wait_for(lambda: None, deadline_s=0.1, poll_s=0.01) is None
