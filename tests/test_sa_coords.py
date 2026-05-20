"""Verify hardcoded SA-screen coordinates against fixture annotations.

The architecture intent is OCR-only clicks, but macOS Setup Assistant
ships several white-on-blue buttons that OCR can't read.  For those we
hardcode pixel coordinates.  This test prevents silent drift:

- Each fixture JSON in ``tests/fixtures/sa_screens/`` declares a list of
  named coordinates with their expected (x, y) and a bounding box for the
  visible UI element.
- For each entry: the (x, y) MUST lie inside its bbox.
- When the named constant exists in code (e.g. ``_FULLNAME_FIELD_X``,
  ``_FULLNAME_FIELD_Y``), the test cross-checks the JSON xy matches code.

A coordinate failure here means either:
1. The code constant drifted away from the actual button position (fix
   the constant), or
2. macOS moved the button (re-capture the fixture).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sa_screens"


def _load_fixtures() -> list[tuple[str, dict]]:
    """Return [(name, annotation), ...] for every JSON in the fixtures dir."""
    out: list[tuple[str, dict]] = []
    if not _FIXTURES_DIR.exists():
        return out
    for path in sorted(_FIXTURES_DIR.glob("*.json")):
        with path.open() as f:
            out.append((path.stem, json.load(f)))
    return out


_FIXTURES = _load_fixtures()


def _coord_test_ids() -> list[str]:
    """Generate test IDs of the form `<screen>.<coord_name>`."""
    ids: list[str] = []
    for screen_name, ann in _FIXTURES:
        for c in ann.get("coords", []):
            ids.append(f"{screen_name}.{c['name']}")
    return ids


def _all_coord_entries() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for screen_name, ann in _FIXTURES:
        for c in ann.get("coords", []):
            out.append((screen_name, c))
    return out


_COORD_ENTRIES = _all_coord_entries()


def _read_setup_assistant_constants() -> dict[str, tuple[int, int]]:
    """Parse `_FOO_X = ...` / `_FOO_Y = ...` pairs from setup_assistant.py.

    Returns a dict keyed by the base name (e.g. ``_FULLNAME_FIELD``) mapping
    to its (x, y).  Constants without both X and Y are skipped.
    """
    src = (
        Path(__file__).parent.parent
        / "server"
        / "airtag_tracker"
        / "automation"
        / "install"
        / "setup_assistant.py"
    )
    text = src.read_text()
    pat = re.compile(r"^(_[A-Z][A-Z0-9_]*?)_([XY])\s*=\s*(\d+)", re.MULTILINE)
    coords: dict[str, dict[str, int]] = {}
    for base, axis, value in pat.findall(text):
        coords.setdefault(base, {})[axis] = int(value)
    # Cross-form: also catch `_FOO_X, _FOO_Y = a, b`
    pair_pat = re.compile(
        r"^(_[A-Z][A-Z0-9_]*?)_X\s*,\s*\1_Y\s*=\s*(\d+)\s*,\s*(\d+)",
        re.MULTILINE,
    )
    for base, x, y in pair_pat.findall(text):
        coords.setdefault(base, {})["X"] = int(x)
        coords.setdefault(base, {})["Y"] = int(y)
    return {
        name: (xy["X"], xy["Y"])
        for name, xy in coords.items()
        if "X" in xy and "Y" in xy
    }


_CODE_CONSTANTS = _read_setup_assistant_constants()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_setup_assistant_constants_parsed():
    """Sanity: at least one named constant was found in the source.

    If this fails, the regex broke (likely due to a code style change in
    setup_assistant.py) and all per-coord checks would be vacuously passing.
    """
    assert _CODE_CONSTANTS, "No _FOO_X/_FOO_Y constants parsed from setup_assistant.py"
    # The Full Name and Password sub-field constants are the core SA-8 anchors.
    assert "_FULLNAME_FIELD" in _CODE_CONSTANTS
    assert "_PW_FIELD" in _CODE_CONSTANTS


@pytest.mark.skipif(not _COORD_ENTRIES, reason="No SA-screen fixtures captured yet")
@pytest.mark.parametrize("entry", _COORD_ENTRIES, ids=_coord_test_ids() or None)
def test_coord_inside_bbox(entry):
    screen, c = entry
    x, y = c["xy"]
    x0, y0, x1, y1 = c["bbox"]
    assert x0 <= x <= x1, f"{screen}.{c['name']}: x={x} outside [{x0}, {x1}]"
    assert y0 <= y <= y1, f"{screen}.{c['name']}: y={y} outside [{y0}, {y1}]"


@pytest.mark.skipif(not _COORD_ENTRIES, reason="No SA-screen fixtures captured yet")
@pytest.mark.parametrize("entry", _COORD_ENTRIES, ids=_coord_test_ids() or None)
def test_coord_matches_code_constant(entry):
    """If a constant by the fixture's name exists in source, JSON xy must match."""
    screen, c = entry
    name = c["name"]
    if name not in _CODE_CONSTANTS:
        pytest.skip(f"No matching constant {name} in setup_assistant.py")
    expected = _CODE_CONSTANTS[name]
    assert tuple(c["xy"]) == expected, (
        f"{screen}.{name}: fixture xy={c['xy']} but code has {expected}"
    )
