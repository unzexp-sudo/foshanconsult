"""Seed the database with the launch event type and a sample conference.

Idempotent: running it twice leaves one row of each.  ``uv run python -m app.seed``.

P1 acceptance: this creates ``consult-30`` and ``GET /healthz`` returns ok.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import init_db, session_scope, utcnow
from app.models import AvailabilityRule, ConferenceEvent, EventType

# D4 (decided): ¥500 / 30 min, Mon–Fri 09:00–18:00 Asia/Shanghai, 4h minimum notice.
CONSULT_ID = "consult-30"
CONSULT_TITLE = "1-1 出海获客诊断"
CONSULT_DESCRIPTION = (
    "30 分钟一对一视频通话，针对你的出海获客现状给出可执行的诊断与下一步建议。"
    "付款成功后日历邀请与确认邮件会立即发出。"
)
CONSULT_PRICE_FEN = 50000  # ¥500
CONSULT_DURATION_MINUTES = 30
CONSULT_MIN_NOTICE_MINUTES = 240
CONSULT_MAX_DAYS_AHEAD = 60
CONSULT_TIMEZONE = "Asia/Shanghai"

# 0 = Monday … 6 = Sunday
WEEKDAYS_MON_FRI = range(0, 5)

# D6 (decided): conferences are ¥50 / seat, up to 100 seats, 1 hour.
CONFERENCE_ID = "conf_sample0001"
CONFERENCE_TITLE = "出海获客公开课（示例场次）"
CONFERENCE_DESCRIPTION = (
    "一小时线上会议，聚焦出海获客的常见问题与实操方法。报名成功后，会议链接会发送至你的邮箱。"
)
CONFERENCE_PRICE_FEN = 5000  # ¥50
CONFERENCE_DURATION_MINUTES = 60
CONFERENCE_CAPACITY = 100
CONFERENCE_TIMEZONE = "Asia/Shanghai"


def seed(db: Session) -> EventType:
    """Create the launch event type if it is absent.  Returns the row."""
    event_type = db.get(EventType, CONSULT_ID)
    if event_type is not None:
        return event_type

    event_type = EventType(
        id=CONSULT_ID,
        title=CONSULT_TITLE,
        description=CONSULT_DESCRIPTION,
        duration_minutes=CONSULT_DURATION_MINUTES,
        price_fen=CONSULT_PRICE_FEN,
        currency="CNY",
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        min_notice_minutes=CONSULT_MIN_NOTICE_MINUTES,
        max_days_ahead=CONSULT_MAX_DAYS_AHEAD,
        timezone=CONSULT_TIMEZONE,
        active=True,
        availability_rules=[
            AvailabilityRule(weekday=weekday, start_local=time(9, 0), end_local=time(18, 0))
            for weekday in WEEKDAYS_MON_FRI
        ],
    )
    db.add(event_type)
    db.flush()
    return event_type


def _sample_start(now: datetime) -> datetime:
    """20:00 Asia/Shanghai, a week from now, as aware UTC.

    Computed relative to *now* rather than hard-coded: a fixed date would quietly
    become a past event and drop off ``/conferences``, which reads as "the feature
    broke" rather than "the seed went stale".
    """
    tz = ZoneInfo(CONFERENCE_TIMEZONE)
    local = now.astimezone(tz) + timedelta(days=7)
    return datetime.combine(local.date(), time(20, 0), tzinfo=tz).astimezone(UTC)


def seed_conference(db: Session, *, now: datetime | None = None) -> ConferenceEvent:
    """Create the sample conference if it is absent.  Returns the row.

    Deliberately created with **no** ``join_url``, which means it is not on sale —
    see :meth:`app.models.ConferenceEvent.is_on_sale`.  A seed row that could
    actually take ¥50 from a real visitor would be a trap, and a placeholder URL
    would be worse: it would be emailed.  So the sample appears on
    ``/conferences`` as "即将开放", the owner pastes their VooV link into it, and it
    opens for sale at that moment.
    """
    event = db.get(ConferenceEvent, CONFERENCE_ID)
    if event is not None:
        return event

    event = ConferenceEvent(
        id=CONFERENCE_ID,
        title=CONFERENCE_TITLE,
        description=CONFERENCE_DESCRIPTION,
        starts_at=_sample_start(now or utcnow()),
        duration_minutes=CONFERENCE_DURATION_MINUTES,
        timezone=CONFERENCE_TIMEZONE,
        price_fen=CONFERENCE_PRICE_FEN,
        currency="CNY",
        capacity=CONFERENCE_CAPACITY,
        join_url="",
        join_note="",
        active=True,
    )
    db.add(event)
    db.flush()
    return event


def main() -> None:
    init_db()
    with session_scope() as db:
        event_type = seed(db)
        conference = seed_conference(db)
        existing = db.scalar(select(EventType).where(EventType.id == CONSULT_ID))
        rules = len(existing.availability_rules) if existing else 0
    print(
        f"seeded event type {event_type.id!r} — {event_type.title!r}, "
        f"{event_type.duration_minutes} min, {event_type.price_fen} 分 "
        f"({event_type.price_fen / 100:.2f} {event_type.currency}), "
        f"{rules} availability rules"
    )
    print(
        f"seeded conference {conference.id!r} — {conference.title!r}, "
        f"{conference.starts_at_utc:%Y-%m-%d %H:%M} UTC, "
        f"{conference.capacity} seats at {conference.price_fen} 分 "
        f"({conference.price_fen / 100:.2f} {conference.currency}), "
        f"on sale: {conference.is_on_sale()} (paste a join_url to open it)"
    )


if __name__ == "__main__":
    main()
