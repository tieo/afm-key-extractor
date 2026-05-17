"""Entry point — start the FastAPI/Uvicorn server."""

from __future__ import annotations

import logging

import uvicorn

from airtag_tracker.api.app import create_app
from airtag_tracker.config import PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
