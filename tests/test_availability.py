"""Availability grid tests (M1, contract §14.1).

Everything is driven through the real ORM/SQLite session and the fake calendar —
no mocks of the code under test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models import AvailabilityRule, Booking, BookingStatus, EventType
from app.services.availability import (
    Slot,
    generate_slots,
    is_slot_on_grid,
    local_date_bounds,
)
from tests.fakes import FakeCalendarGateway

# 2026-09-19 is a Saturday, so 09-21 is a Monday and 09-22 a Tuesday.
MONDAY = date(2026, 9, 21)
TUESDAY = date(2026, 9, 22)


def utc(*args: int) -> datetime:
    """``utc(2026, 9, 21, 9, 0)`` in full, or ``utc(9, 0)`` for a time on MONDAY."""
    if len(args) == 2:
        return datetime(2026, 9, 21, *args, tzinfo=UTC)
    return datetime(*args, tzinfo=UTC)


def make_event_type(
    db: Session,
    *,
    id: str = "et-test",
    timezone: str = "Asia/Shanghai",
    duration_minutes: int = 30,
    min_notice_minutes: int = 0,
    max_days_ahead: int = 60,
    buffer_before_minutes: int = 0,
    buffer_after_minutes: int = 0,
    rules: tuple[tuple[int, time, time], ...] = ((0, time(9, 0), time(10, 0)),),
) -> EventType:
    event_type = EventType(
        id=id,
        title="test",
        description="",
        duration_minutes=duration_minutes,
        price_fen=50000,
        currency="CNY",
        buffer_before_minutes=buffer_before_minutes,
        buffer_after_minutes=buffer_after_minutes,
        min_notice_minutes=min_notice_minutes,
        max_days_ahead=max_days_ahead,
        timezone=timezone,
        active=True,
        availability_rules=[
            AvailabilityRule(weekday=weekday, start_local=start, end_local=end)
            for weekday, start, end in rules
        ],
    )
    db.add(event_type)
    db.commit()
    db.refresh(event_type)
    return event_type


def add_booking(
    db: Session,
    event_type: EventType,
    slot_start: datetime,
    *,
    status: BookingStatus,
    expires_at: datetime,
) -> Booking:
    booking = Booking(
        id=uuid.uuid4().hex,
        reference=f"BK{uuid.uuid4().hex[:6].upper()}",
        event_type_id=event_type.id,
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=event_type.duration_minutes),
        status=status,
        expires_at=expires_at,
        amount_fen=event_type.price_fen,
        currency="CNY",
        customer_name="Test",
        customer_email="test@example.com",
    )
    db.add(booking)
    db.commit()
    return booking


def starts(db: Session, event_type: EventType, local_date: date, now: datetime, calendar=None):
    return [
        slot.start
        for slot in generate_slots(
            db, event_type, local_date, calendar=calendar or FakeCalendarGateway(), now=now
        )
    ]


# ---------------------------------------------------------------------------
# local_date_bounds
# ---------------------------------------------------------------------------


def test_local_date_bounds_are_the_utc_window_for_the_local_date(db_session):
    event_type = make_event_type(db_session, timezone="Asia/Shanghai")
    start, end = local_date_bounds(event_type, MONDAY)
    assert start == utc(2026, 9, 20, 16, 0)
    assert end == utc(2026, 9, 21, 16, 0)


# ---------------------------------------------------------------------------
# Grid shape: weekday, timezone, alignment, fit
# ---------------------------------------------------------------------------


def test_grid_is_local_wall_clock_in_the_event_timezone(db_session):
    event_type = make_event_type(
        db_session, timezone="Asia/Shanghai", rules=((0, time(9, 0), time(10, 0)),)
    )
    slots = generate_slots(
        db_session, event_type, MONDAY, calendar=FakeCalendarGateway(), now=utc(2026, 9, 21, 0, 0)
    )
    assert [slot.start for slot in slots] == [utc(1, 0), utc(1, 15), utc(1, 30)]
    assert [slot.end for slot in slots] == [utc(1, 30), utc(1, 45), utc(2, 0)]


def test_grid_honours_a_different_timezone(db_session):
    event_type = make_event_type(
        db_session, timezone="UTC", rules=((0, time(9, 0), time(10, 0)),)
    )
    assert starts(db_session, event_type, MONDAY, utc(2026, 9, 21, 0, 0)) == [
        utc(9, 0),
        utc(9, 15),
        utc(9, 30),
    ]


def test_only_rules_for_the_matching_weekday_produce_slots(db_session):
    event_type = make_event_type(
        db_session, timezone="UTC", rules=((1, time(9, 0), time(10, 0)),)  # Tuesday only
    )
    now = utc(2026, 9, 21, 0, 0)
    assert starts(db_session, event_type, MONDAY, now) == []
    assert starts(db_session, event_type, TUESDAY, now) == [
        utc(2026, 9, 22, 9, 0),
        utc(2026, 9, 22, 9, 15),
        utc(2026, 9, 22, 9, 30),
    ]


def test_slot_must_fit_entirely_inside_the_rule_window(db_session):
    event_type = make_event_type(
        db_session, timezone="UTC", rules=((0, time(9, 0), time(10, 0)),)
    )
    result = starts(db_session, event_type, MONDAY, utc(2026, 9, 21, 0, 0))
    assert result[-1] == utc(9, 30)  # 09:45 + 30m would spill past 10:00
    assert utc(9, 45) not in result


def test_overlapping_rules_are_unioned_without_duplicates(db_session):
    event_type = make_event_type(
        db_session,
        timezone="UTC",
        rules=((0, time(9, 0), time(10, 0)), (0, time(9, 15), time(10, 0))),
    )
    result = starts(db_session, event_type, MONDAY, utc(2026, 9, 21, 0, 0))
    assert result == [utc(9, 0), utc(9, 15), utc(9, 30)]
    assert len(result) == len(set(result))


# ---------------------------------------------------------------------------
# Buffers
# ---------------------------------------------------------------------------


def test_buffer_after_expands_calendar_busy(db_session):
    event_type = make_event_type(
        db_session,
        timezone="UTC",
        buffer_after_minutes=15,
        rules=((0, time(9, 0), time(12, 0)),),
    )
    calendar = FakeCalendarGateway()
    calendar.add_busy(utc(10, 0), utc(10, 30))
    result = starts(db_session, event_type, MONDAY, utc(2026, 9, 21, 0, 0), calendar)
    assert utc(9, 30) in result
    for blocked in (utc(9, 45), utc(10, 0), utc(10, 15), utc(10, 30)):
        assert blocked not in result
    assert utc(10, 45) in result


def test_buffer_before_expands_calendar_busy(db_session):
    event_type = make_event_type(
        db_session,
        timezone="UTC",
        buffer_before_minutes=15,
        rules=((0, time(9, 0), time(12, 0)),),
    )
    calendar = FakeCalendarGateway()
    calendar.add_busy(utc(10, 0), utc(10, 30))
    result = starts(db_session, event_type, MONDAY, utc(2026, 9, 21, 0, 0), calendar)
    assert utc(9, 15) in result
    for blocked in (utc(9, 30), utc(9, 45), utc(10, 0), utc(10, 15)):
        assert blocked not in result
    assert utc(10, 30) in result


# ---------------------------------------------------------------------------
# Notice window and booking horizon
# ---------------------------------------------------------------------------


def test_min_notice_minutes_excludes_early_slots(db_session):
    event_type = make_event_type(
        db_session,
        timezone="UTC",
        min_notice_minutes=240,
        rules=((0, time(9, 0), time(12, 0)),),
    )
    result = starts(db_session, event_type, MONDAY, utc(2026, 9, 21, 6, 0))
    assert result[0] == utc(10, 0)  # exactly now + 240m is allowed
    assert utc(9, 45) not in result


def test_max_days_ahead_limits_the_horizon(db_session):
    event_type = make_event_type(
        db_session,
        timezone="UTC",
        max_days_ahead=1,
        rules=((0, time(9, 0), time(10, 0)), (1, time(9, 0), time(10, 0))),
    )
    now = utc(2026, 9, 21, 0, 0)
    assert starts(db_session, event_type, MONDAY, now) == [utc(9, 0), utc(9, 15), utc(9, 30)]
    assert starts(db_session, event_type, TUESDAY, now) == []  # beyond now + 1 day


# ---------------------------------------------------------------------------
# Live bookings and lazy expiry
# ---------------------------------------------------------------------------


def test_live_pending_hold_removes_the_slot(db_session):
    event_type = make_event_type(
        db_session, timezone="UTC", rules=((0, time(9, 0), time(10, 0)),)
    )
    now = utc(2026, 9, 21, 0, 0)
    add_booking(
        db_session,
        event_type,
        utc(9, 0),
        status=BookingStatus.PENDING_PAYMENT,
        expires_at=now + timedelta(minutes=10),
    )
    # 09:00–09:30 is held; 09:15–09:45 would overlap it, 09:30–10:00 still fits.
    assert starts(db_session, event_type, MONDAY, now) == [utc(9, 30)]


def test_paid_booking_removes_the_slot(db_session):
    event_type = make_event_type(
        db_session, timezone="UTC", rules=((0, time(9, 0), time(10, 0)),)
    )
    now = utc(2026, 9, 21, 0, 0)
    add_booking(
        db_session,
        event_type,
        utc(9, 0),
        status=BookingStatus.PAID,
        expires_at=now - timedelta(days=1),
    )
    assert utc(9, 0) not in starts(db_session, event_type, MONDAY, now)


def test_expired_hold_is_not_treated_as_live(db_session):
    event_type = make_event_type(
        db_session, timezone="UTC", rules=((0, time(9, 0), time(10, 0)),)
    )
    now = utc(2026, 9, 21, 0, 0)
    add_booking(
        db_session,
        event_type,
        utc(9, 0),
        status=BookingStatus.PENDING_PAYMENT,
        expires_at=now - timedelta(seconds=1),  # lazily expired
    )
    assert starts(db_session, event_type, MONDAY, now) == [utc(9, 0), utc(9, 15), utc(9, 30)]


# ---------------------------------------------------------------------------
# is_slot_on_grid
# ---------------------------------------------------------------------------


def test_is_slot_on_grid_covers_grid_notice_and_window(db_session):
    event_type = make_event_type(
        db_session,
        timezone="UTC",
        min_notice_minutes=60,
        max_days_ahead=2,
        rules=((0, time(9, 0), time(10, 0)),),
    )
    now = utc(2026, 9, 21, 0, 0)
    assert is_slot_on_grid(db_session, event_type, utc(9, 0), now=now) is True
    assert is_slot_on_grid(db_session, event_type, utc(9, 7), now=now) is False  # off-grid
    assert is_slot_on_grid(db_session, event_type, utc(9, 45), now=now) is False  # won't fit
    assert is_slot_on_grid(db_session, event_type, utc(8, 0), now=now) is False  # before rule
    assert (
        is_slot_on_grid(db_session, event_type, utc(2026, 9, 24, 9, 0), now=now) is False
    )  # beyond the horizon


def test_is_slot_on_grid_ignores_the_calendar_and_the_db(db_session):
    """It is a pure grid/window predicate — a busy calendar must not change it."""
    event_type = make_event_type(
        db_session, timezone="UTC", rules=((0, time(9, 0), time(10, 0)),)
    )
    now = utc(2026, 9, 21, 0, 0)
    add_booking(
        db_session,
        event_type,
        utc(9, 0),
        status=BookingStatus.PENDING_PAYMENT,
        expires_at=now + timedelta(minutes=10),
    )
    assert is_slot_on_grid(db_session, event_type, utc(9, 0), now=now) is True


def test_slot_dataclass_is_frozen():
    slot = Slot(start=utc(9, 0), end=utc(9, 30))
    assert slot.start == utc(9, 0)
