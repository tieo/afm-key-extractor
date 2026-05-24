#!/usr/bin/env bash
# entrypoint.sh — Docker container entrypoint.
#
# 1. Provision the VM directory (idempotent).
# 2. Start the FastAPI server.

set -euo pipefail

/app/scripts/provision.sh

exec uv run python server/tracker.py
