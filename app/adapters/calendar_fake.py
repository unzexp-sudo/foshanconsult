"""In-memory calendar gateway for local dev and tests.

Instantiated by ``app.deps`` when ``CALENDAR_GATEWAY=fake`` (the default), so it
must satisfy :class:`~app.ports.calendar.CalendarGateway` and import nothing but
the port.  It is deliberately *not* the test double from ``tests/fakes.py``:
that one records ``released``/``confirmed`` history for assertions, which is a
test-only concern.  This one keeps only what production semantics require, and
is deterministic — no clock, no randomness, no I/O.

Holds are tracked by event id rather than by interval, so releasing one hold can
never accidentally free a different hold that happens to share the same slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.ports.calendar import BusyInterval, CalendarEvent, CalendarGatewayError


@dataclass
class _Hold:
    summary: str
    description: str
    start: datetime
    end: datetime
    reference: str
    status: str = "tentative"


class FakeCalendarGateway:
    """In-memory busy intervals and events."""

    def __init__(self) -> None:
        self._busy: list[BusyInterval] = []
        self._holds: dict[str, _Hold] = {}
        self._counter = 0

    # -- local-dev seeding --------------------------------------------------

    def add_busy(self, start: datetime, end: datetime) -> None:
        """Seed pre-existing busy time so availability has something to exclude."""
        self._busy.append(BusyInterval(start=start, end=end))

    # -- CalendarGateway ----------------------------------------------------

    def freebusy(self, time_min: datetime, time_max: datetime) -> list[BusyInterval]:
        intervals = list(self._busy)
        intervals.extend(
            BusyInterval(start=hold.start, end=hold.end) for hold in self._holds.values()
        )
        return [
            interval
            for interval in intervals
            if interval.start < time_max and interval.end > time_min
        ]

    def create_hold(
        self,
        *,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        reference: str,
    ) -> CalendarEvent:
        self._counter += 1
        event_id = f"fake-{self._counter:04d}"
        self._holds[event_id] = _Hold(
            summary=summary,
            description=description,
            start=start,
            end=end,
            reference=reference,
        )
        return CalendarEvent(
            event_id=event_id, html_link=f"https://calendar.fake/{event_id}"
        )

    def confirm(self, event_id: str, *, summary: str, description: str) -> None:
        hold = self._holds.get(event_id)
        if hold is None:
            raise CalendarGatewayError(f"unknown event {event_id!r}")
        hold.summary = summary
        hold.description = description
        hold.status = "confirmed"

    def release(self, event_id: str) -> None:
        # Idempotent on purpose: releasing an unknown event is not an error,
        # because release is retried by the sweeper.
        self._holds.pop(event_id, None)
