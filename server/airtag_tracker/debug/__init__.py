"""Debug helpers package — VM snapshot + handler replay tools.

Designed for the iter loop where a single SA screen needs many attempts.
Instead of restarting a 90-min install for each tweak::

    1.  Run install once with AIRTAG_AUTO_SNAPSHOT_STATES=sa_create_account
    2.  After the snapshot fires, abort or let the run finish.
    3.  ``python -m airtag_tracker.debug replay sa_create_account``
        → restores the snapshot in seconds, runs only that handler.
    4.  Edit code, re-run step 3.  Repeat until happy.

The same flow is exposed via /api/debug/{snapshot,restore,run-handler}
for UI / curl access.
"""
