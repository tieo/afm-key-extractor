"""Sentinel parsing is the only place we reinterpret OCR.  Cover it
directly — the rest of the Recovery flow depends on it."""

from server.wizard.recovery import _parse_sentinel


def test_parse_sentinel_with_value():
    t = "some noise\nWIZARD_SENTINEL DVOL=/Volumes/Macintosh HD - Data\nmore"
    assert _parse_sentinel(t, "DVOL") == "/Volumes/Macintosh HD - Data"


def test_parse_sentinel_boolean_form():
    # bare sentinel — no '=' — returns empty string (present but valueless)
    t = "WIZARD_SENTINEL USER_PLIST_OK\n"
    assert _parse_sentinel(t, "USER_PLIST_OK") == ""


def test_parse_sentinel_missing_returns_none():
    assert _parse_sentinel("nothing here", "DVOL") is None


def test_parse_sentinel_ignores_other_keys():
    t = "WIZARD_SENTINEL DVOL_RW=1\nWIZARD_SENTINEL DVOL=/x\n"
    assert _parse_sentinel(t, "DVOL_RW") == "1"
    assert _parse_sentinel(t, "DVOL") == "/x"
