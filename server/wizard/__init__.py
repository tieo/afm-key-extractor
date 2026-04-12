"""Golden-image build automation.

Drives a macOS Ventura installer VM from the OpenCore boot picker all
the way to a fully-configured system, then shuts it down so the caller
can snapshot the disk as `mac_hdd_golden.img`.

Each step is a standalone handler in its own module. A step's entry
state is the previous step's exit state; steps compose linearly.

Currently implemented:
  1. boot_installer — OpenCore picker → Recovery Utilities picker
"""

from .driver import Driver
from .steps import boot_installer

__all__ = ["Driver", "boot_installer"]
