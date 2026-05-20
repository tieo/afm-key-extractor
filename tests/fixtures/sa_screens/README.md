# SA-screen coordinate fixtures

This directory pairs reference screendumps of macOS Setup Assistant
screens with annotation files that pin down where each hardcoded
coordinate constant in the code is expected to click.

Layout per screen:

    sa_screens/<screen_name>.png        # screendump captured from a real VM
    sa_screens/<screen_name>.json       # annotation: list of coords + bboxes

## Annotation schema

```json
{
  "screen": "sa_create_account",
  "macos": "14",
  "resolution": [1280, 800],
  "captured": "2026-05-20T22:00:00Z",
  "coords": [
    {
      "name": "_FULLNAME_FIELD",
      "xy": [620, 307],
      "bbox": [400, 290, 880, 325],
      "purpose": "Click Full Name input"
    },
    {
      "name": "_PW_FIELD",
      "xy": [550, 390],
      "bbox": [400, 378, 700, 408],
      "purpose": "Click left password sub-field"
    }
  ]
}
```

- `xy`   — the pixel the code clicks (matches the constant in code).
- `bbox` — [x0, y0, x1, y1] of the visible UI element.  Test asserts
           xy lies inside bbox.
- `name` — the source constant name (so a test failure points at the line
           of code to edit).

## Capturing fresh fixtures

Use the debug CLI's `screendump` command (any state where the target screen
is showing) to grab a PNG, then write an annotation by eye or with the
helper at `tools/annotate_fixture.py` (TODO once a real VM run is available).

```
docker exec nix-airtag-tracker-airtag-tracker-1 sh -c \
  "cd /app && PYTHONPATH=server uv run python -m airtag_tracker.debug screendump /data/sa_create_account.ppm"
# then convert PPM → PNG on the host and copy into this directory:
convert ~/airtag-dev/sa_create_account.ppm tests/fixtures/sa_screens/sa_create_account.png
```

## How the test uses these

`tests/test_sa_coords.py` walks every `*.json` in this directory.  For each
entry in `coords`, it asserts `xy` lies inside `bbox` *and* (when the
referenced constant exists in code) that the constant equals the JSON's
xy.  Drift in either direction is a test failure.
