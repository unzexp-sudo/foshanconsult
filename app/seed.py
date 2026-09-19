"""Seed the database with the launch event type.

Idempotent: running it twice leaves one row.  ``uv run python -m app.seed``.

P1 acceptance: this creates ``consult-30`` and ``GET /healthz`` returns ok.
"""

from __future__ import annotations

from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import init_db, session_scope
from app.models import AvailabilityRule, EventType

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


def main() -> None:
    init_db()
    with session_scope() as db:
        event_type = seed(db)
        existing = db.scalar(select(EventType).where(EventType.id == CONSULT_ID))
        rules = len(existing.availability_rules) if existing else 0
    print(
        f"seeded event type {event_type.id!r} — {event_type.title!r}, "
        f"{event_type.duration_minutes} min, {event_type.price_fen} 分 "
        f"({event_type.price_fen / 100:.2f} {event_type.currency}), "
        f"{rules} availability rules"
    )


if __name__ == "__main__":
    main()
