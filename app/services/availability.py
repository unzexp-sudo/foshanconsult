"""Slot-grid generation — contract §14.1.

Availability rules are LOCAL wall-clock windows in ``event_type.timezone``.
Everything that leaves this module is timezone-aware UTC.

Two ideas do all the work:

* the grid is ``slot_step_minutes`` aligned to each rule's own ``start_local``,
  and a slot must fit *entirely* inside the rule window;
* a slot is offered only if it overlaps neither calendar busy time (expanded by
  the buffers) nor a *live* booking — where "live" applies lazy expiry, so an
  expired ``pending_payment`` hold never occupies its slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import utcnow
from app.models import AvailabilityRule, Booking, BookingStatus, EventType
from app.ports.calendar import CalendarGateway

__all__ = [
    "Slot",
    "generate_slots",
    "is_slot_on_grid",
    "local_date_bounds",
    "owned_intervals",
]


@dataclass(frozen=True)
class Slot:
    start: datetime  # aware UTC
    end: datetime  # aware UTC


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _zone(event_type: EventType) -> ZoneInfo:
    return ZoneInfo(event_type.timezone)


def local_date_bounds(event_type: EventType, local_date: date) -> tuple[datetime, datetime]:
    """The ``[start, end)`` UTC window covering ``local_date`` in the event timezone."""
    tz = _zone(event_type)
    start_local = datetime.combine(local_date, time(0, 0), tzinfo=tz)
    end_local = datetime.combine(local_date + timedelta(days=1), time(0, 0), tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def owned_intervals(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    event_type_id: str,
    now: datetime | None = None,
) -> set[tuple[datetime, datetime]]:
    """Intervals in ``[start, end)`` that must **not** be treated as calendar-busy.

    The calendar is the source of truth for busy time, but an interval *we* put
    there is a record we already own and the database has the final say on it.
    Three cases, and getting them apart is the whole point:

    * **same event type** — the partial unique index and lazy expiry adjudicate it,
      so the calendar must not pre-empt them.  This is what lets an expired hold
      free its slot while the sweeper is dead (contract §7).
    * **different event type, still live** — a real commitment by the same person.
      Two 1-1 calls cannot overlap, so it keeps blocking.  Excluding these was a
      latent double-booking hole.
    * **already dead** (expired or cancelled) — never blocks anything, whoever it
      belonged to.

    Matching is on the RAW interval bounds, because the hold we created is exactly
    ``[slot_start, slot_end)``; buffers are applied by the caller afterwards.
    """
    moment = _as_utc(now or utcnow())
    rows = db.execute(
        select(
            Booking.slot_start,
            Booking.slot_end,
            Booking.event_type_id,
            Booking.status,
            Booking.expires_at,
        ).where(
            Booking.calendar_event_id.is_not(None),
            Booking.slot_start < end,
            Booking.slot_end > start,
        )
    ).all()

    ignored: set[tuple[datetime, datetime]] = set()
    for slot_start, slot_end, owner_type, status, expires_at in rows:
        is_live = status == BookingStatus.PAID or (
            status == BookingStatus.PENDING_PAYMENT and _as_utc(expires_at) > moment
        )
        if not is_live or owner_type == event_type_id:
            ignored.add((_as_utc(slot_start), _as_utc(slot_end)))
    return ignored


def _rule_grid(
    event_type: EventType, rule: AvailabilityRule, local_date: date
) -> list[datetime]:
    """Aware-local candidate starts for one rule on one local date."""
    tz = _zone(event_type)
    duration = timedelta(minutes=event_type.duration_minutes)
    step = timedelta(minutes=settings.slot_step_minutes)
    if duration <= timedelta(0) or step <= timedelta(0):
        return []

    rule_start = datetime.combine(local_date, rule.start_local, tzinfo=tz)
    rule_end = datetime.combine(local_date, rule.end_local, tzinfo=tz)
    if rule_end <= rule_start:
        return []

    starts: list[datetime] = []
    cursor = rule_start
    while cursor + duration <= rule_end:
        starts.append(cursor)
        cursor += step
    return starts


def is_slot_on_grid(
    db: Session,
    event_type: EventType,
    slot_start: datetime,
    *,
    now: datetime | None = None,
) -> bool:
    """Grid + notice + window only.  Consults neither the calendar nor the DB."""
    moment = _as_utc(now or utcnow())
    start = _as_utc(slot_start)

    if start < moment + timedelta(minutes=event_type.min_notice_minutes):
        return False
    if start > moment + timedelta(days=event_type.max_days_ahead):
        return False

    step_seconds = settings.slot_step_minutes * 60
    if step_seconds <= 0:
        return False

    tz = _zone(event_type)
    local = start.astimezone(tz).replace(tzinfo=None)  # naive local wall clock
    duration = timedelta(minutes=event_type.duration_minutes)

    for rule in event_type.availability_rules:
        if rule.weekday != local.weekday():
            continue
        rule_start = datetime.combine(local.date(), rule.start_local)
        rule_end = datetime.combine(local.date(), rule.end_local)
        if rule_end <= rule_start:
            continue
        if local < rule_start or local + duration > rule_end:
            continue
        offset = local - rule_start
        if int(offset.total_seconds()) % step_seconds != 0:
            continue
        return True
    return False


def generate_slots(
    db: Session,
    event_type: EventType,
    local_date: date,
    *,
    calendar: CalendarGateway,
    now: datetime | None = None,
) -> list[Slot]:
    """The bookable grid for one local date, ascending by start (aware UTC)."""
    moment = _as_utc(now or utcnow())
    duration = timedelta(minutes=event_type.duration_minutes)
    day_start, day_end = local_date_bounds(event_type, local_date)

    # Busy time is expanded by the buffers.  Query a padded window so a busy
    # interval that starts/ends just outside the local day is still seen.
    pad = timedelta(
        minutes=(
            event_type.buffer_before_minutes
            + event_type.buffer_after_minutes
            + event_type.duration_minutes
            + settings.slot_step_minutes
        )
    )
    # Our own holds are filtered out on their RAW bounds, before the buffer is
    # applied — see `owned_intervals` for which ones and why.
    owned = owned_intervals(
        db, day_start - pad, day_end + pad, event_type_id=event_type.id, now=moment
    )
    expanded_busy = [
        (
            _as_utc(interval.start) - timedelta(minutes=event_type.buffer_before_minutes),
            _as_utc(interval.end) + timedelta(minutes=event_type.buffer_after_minutes),
        )
        for interval in calendar.freebusy(day_start - pad, day_end + pad)
        if (_as_utc(interval.start), _as_utc(interval.end)) not in owned
    ]

    # Live = paid, or a pending hold that has not expired yet (lazy expiry §7.1).
    live = (Booking.status == BookingStatus.PAID) | (
        (Booking.status == BookingStatus.PENDING_PAYMENT) & (Booking.expires_at > moment)
    )
    rows = db.execute(
        select(Booking.slot_start, Booking.slot_end).where(
            Booking.event_type_id == event_type.id,
            Booking.slot_start < day_end,
            Booking.slot_end > day_start,
            live,
        )
    ).all()
    taken = [(_as_utc(row[0]), _as_utc(row[1])) for row in rows]

    starts: set[datetime] = set()
    for rule in event_type.availability_rules:
        if rule.weekday != local_date.weekday():
            continue
        for candidate_local in _rule_grid(event_type, rule, local_date):
            candidate = candidate_local.astimezone(UTC)
            if not is_slot_on_grid(db, event_type, candidate, now=moment):
                continue
            end = candidate + duration
            overlaps_busy = any(
                candidate < busy_end and end > busy_start
                for busy_start, busy_end in expanded_busy
            )
            if overlaps_busy:
                continue
            if any(candidate < taken_end and end > taken_start for taken_start, taken_end in taken):
                continue
            starts.add(candidate)

    return [Slot(start=start, end=start + duration) for start in sorted(starts)]
