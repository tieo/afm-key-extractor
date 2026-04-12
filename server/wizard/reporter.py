"""Structured progress reporting.

The wizard emits ``info`` / ``warning`` / ``error`` messages and a
coarse phase label.  The tracker hooks these to its existing
``emit(level, 'vm', msg)`` and ``_set_phase(phase, msg)`` helpers.
Tests pass a :class:`CapturingReporter` that records the sequence for
assertions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class Reporter(Protocol):
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...
    def phase(self, name: str, msg: str | None = None) -> None: ...


@dataclass
class CallbackReporter:
    """Reporter that delegates to the tracker's ``emit`` / ``_set_phase``."""
    emit: Callable[[str, str, str], None]
    set_phase: Callable[[str, str | None], None]

    def info(self, msg: str) -> None:
        self.emit("info", "vm", msg)

    def warning(self, msg: str) -> None:
        self.emit("warning", "vm", msg)

    def error(self, msg: str) -> None:
        self.emit("error", "vm", msg)

    def phase(self, name: str, msg: str | None = None) -> None:
        self.set_phase(name, msg)


@dataclass
class CapturingReporter:
    """In-memory reporter for tests."""
    events: list[tuple[str, str]] = field(default_factory=list)
    phases: list[tuple[str, str | None]] = field(default_factory=list)

    def info(self, msg: str) -> None:
        self.events.append(("info", msg))

    def warning(self, msg: str) -> None:
        self.events.append(("warning", msg))

    def error(self, msg: str) -> None:
        self.events.append(("error", msg))

    def phase(self, name: str, msg: str | None = None) -> None:
        self.phases.append((name, msg))

    def messages(self, level: str | None = None) -> list[str]:
        if level is None:
            return [m for _, m in self.events]
        return [m for lvl, m in self.events if lvl == level]

    def phase_names(self) -> list[str]:
        return [n for n, _ in self.phases]
