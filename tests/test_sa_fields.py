"""Tests for the Setup Assistant Create Account field primitives.

The primitives are thin wrappers over `qmp` and `vm_ui`; tests verify
the sequencing and the strategy switch in fill_password_compound, with
the underlying hardware (QMP socket, screendump) mocked out.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from airtag_tracker.automation.install import _sa_fields


def _patch_qmp():
    """Patch the qmp.qmp() context manager so chord/type_text are observed."""
    cm = MagicMock()
    qmp_client = MagicMock()
    cm.__enter__ = MagicMock(return_value=qmp_client)
    cm.__exit__ = MagicMock(return_value=False)
    return patch("airtag_tracker.qmp.qmp", return_value=cm), qmp_client


# ---------------------------------------------------------------------------
# click_field
# ---------------------------------------------------------------------------

def test_click_field_calls_click_pixel():
    with patch("airtag_tracker.vm_ui.click_pixel") as click:
        with patch("time.sleep"):
            _sa_fields.click_field(100, 200)
    click.assert_called_once_with(100, 200, 1280, 800)


# ---------------------------------------------------------------------------
# clear_focused
# ---------------------------------------------------------------------------

def test_clear_focused_sends_cmd_a_then_backspace():
    p, qmp_client = _patch_qmp()
    with p, patch("time.sleep"):
        _sa_fields.clear_focused()
    chords = [args[0][0] for args in qmp_client.send_chord.call_args_list]
    assert chords[0] == ["meta_l", "a"]
    assert chords[1] == ["backspace"]


# ---------------------------------------------------------------------------
# fill_field
# ---------------------------------------------------------------------------

def test_fill_field_click_clear_type():
    p, qmp_client = _patch_qmp()
    with p, \
         patch("airtag_tracker.vm_ui.click_pixel") as click, \
         patch("time.sleep"):
        _sa_fields.fill_field(50, 60, "airtag", label="Full Name")
    click.assert_called_once_with(50, 60, 1280, 800)
    # Cmd+A + Backspace then type
    chords = [args[0][0] for args in qmp_client.send_chord.call_args_list]
    assert chords[:2] == [["meta_l", "a"], ["backspace"]]
    qmp_client.type_text.assert_called_once_with("airtag", gap_s=0.15)


def test_fill_field_skip_clear():
    p, qmp_client = _patch_qmp()
    with p, patch("airtag_tracker.vm_ui.click_pixel"), patch("time.sleep"):
        _sa_fields.fill_field(50, 60, "v", clear=False)
    qmp_client.send_chord.assert_not_called()
    qmp_client.type_text.assert_called_once_with("v", gap_s=0.15)


def test_fill_field_gap_is_slow_enough_to_avoid_char_drop():
    """Regression: at gap_s=0.04 only "ai" of "airtag" registered, dropping
    4/6 chars on SA Create Account.  Lock in the slower default."""
    import inspect
    sig = inspect.signature(_sa_fields.fill_field)
    assert sig.parameters["gap_s"].default >= 0.10, (
        "fill_field gap_s must stay at the empirically-validated slow rate"
    )


# ---------------------------------------------------------------------------
# dismiss_character_picker
# ---------------------------------------------------------------------------

def test_dismiss_character_picker_sends_esc():
    p, qmp_client = _patch_qmp()
    with p, patch("time.sleep"):
        _sa_fields.dismiss_character_picker()
    qmp_client.send_chord.assert_called_once_with(["esc"])


# ---------------------------------------------------------------------------
# fill_password_compound — strategy switch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("strategy,gap", [
    ("qmp_slow", 0.15),
    ("qmp_fast", 0.04),
])
def test_fill_password_compound_qmp_strategies(monkeypatch, strategy, gap):
    monkeypatch.setenv("AIRTAG_SA8_PW_STRATEGY", strategy)
    p, qmp_client = _patch_qmp()
    with p, patch("airtag_tracker.vm_ui.click_pixel"), patch("time.sleep"):
        _sa_fields.fill_password_compound(550, 390, "abc123")
    # password typed twice (left + verify), with expected gap_s.
    type_calls = qmp_client.type_text.call_args_list
    assert len(type_calls) == 2
    for call in type_calls:
        assert call[0] == ("abc123",)
        assert call[1]["gap_s"] == gap
    # Tab pressed between left and verify.
    chord_calls = [args[0][0] for args in qmp_client.send_chord.call_args_list]
    assert ["tab"] in chord_calls


def test_fill_password_compound_paste_strategy(monkeypatch):
    monkeypatch.setenv("AIRTAG_SA8_PW_STRATEGY", "paste")
    p, qmp_client = _patch_qmp()
    with p, \
         patch("airtag_tracker.vm_ui.click_pixel"), \
         patch("airtag_tracker.vm_ui.paste_text") as paste, \
         patch("time.sleep"):
        _sa_fields.fill_password_compound(550, 390, "abc123")
    # Pasted twice, never typed.
    assert paste.call_count == 2
    assert paste.call_args_list[0][0] == ("abc123",)
    qmp_client.type_text.assert_not_called()


# ---------------------------------------------------------------------------
# Error modal + post-Continue classifier
# ---------------------------------------------------------------------------

def test_dismiss_error_modal_when_absent_returns_false():
    with patch("airtag_tracker.automation.screen.has_any_text", return_value=False):
        assert _sa_fields.dismiss_error_modal_if_present() is False


def test_dismiss_error_modal_when_present_clicks_go_back():
    with patch("airtag_tracker.automation.screen.has_any_text", return_value=True), \
         patch("airtag_tracker.vm_ui.click_pixel") as click, \
         patch("time.sleep"):
        assert _sa_fields.dismiss_error_modal_if_present() is True
    click.assert_called_once_with(640, 492, 1280, 800)


@pytest.mark.parametrize("screen_text,expected", [
    ("computer account", None),  # still on screen — would normally retry
    ("welcome to mac", None),    # advanced past create-account
    ("passwords don't match", "passwords_mismatch"),
    ("hint can't contain", "hint_contains_password"),
    ("you haven't provided", "missing_field"),
])
def test_verify_advanced_or_classify_error(screen_text, expected):
    with patch("airtag_tracker.vm_ui.screen_text", return_value=screen_text), \
         patch("time.sleep"):
        # Short deadlines so the loop exits in the "still on screen" case.
        result = _sa_fields.verify_advanced_or_classify_error(
            settle_s=0.0, deadline_s=0.05, poll_s=0.01,
        )
    if expected is None and screen_text == "computer account":
        # Stuck on screen with no error keyword → classifier falls through to
        # "missing_field" on timeout (handler then re-runs to recover).
        assert result == "missing_field"
    else:
        assert result == expected
