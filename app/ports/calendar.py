"""Calendar port.  Frozen by docs/MODULE_CONTRACT.md §8."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class BusyInterval:
    start: datetime  # aware UTC
    end: datetime


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    html_link: str | None = None


class CalendarGatewayError(Exception):
    """Any failure talking to the calendar, however it is reached."""


@runtime_checkable
class CalendarGateway(Protocol):
    def freebusy(self, time_min: datetime, time_max: datetime) -> list[BusyInterval]:
        """Busy intervals overlapping [time_min, time_max)."""
        ...

    def create_hold(
        self,
        *,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        reference: str,
    ) -> CalendarEvent:
        """Create a tentative event that blocks the slot immediately."""
        ...

    def confirm(self, event_id: str, *, summary: str, description: str) -> None:
        """Rewrite a hold into the real, confirmed event."""
        ...

    def release(self, event_id: str) -> None:
        """Delete a hold.  Must be idempotent: an unknown event is not an error."""
        ...
