"""API router package."""
from .automation import router as automation
from .events import router as events
from .keys import router as keys
from .twofa import router as twofa

__all__ = ["automation", "events", "keys", "twofa"]
