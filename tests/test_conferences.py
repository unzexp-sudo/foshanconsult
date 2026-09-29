"""Ticketed conferences — capacity, money, the notify route, and the 1-1 conflict.

Four properties carry most of the weight here, and each has a test that fails
loudly if it regresses:

1. **Capacity is a database invariant, not a check.**  ``uq_live_seat`` makes the
   101st seat impossible rather than merely unlikely, and
   :func:`test_seat_counts_match_what_the_reservation_path_enforces` pins the
   display/enforcement parity the whole design rests on — a page advertising a seat
   the write path then refuses is a defect this project has hit twice.

2. **A verified payment is never dropped.**  The notify handler used to look an
   order up in the ``bookings`` table only, so a ``TK…`` ticket payment was
   "unknown" and the money went nowhere.
   :func:`test_a_ticket_payment_settles_through_the_notify_endpoint` is what would
   have caught that.

3. **The join link is a secret.**  It is delivered after a verified payment and
   appears on no surface before one.  The page and the API are separate code paths,
   so both are checked.

4. **A conference blocks 1-1 time in both directions.**  The grid hides it and the
   write path refuses it; the two must agree, which is the same rule
   ``effective_step_minutes`` exists to enforce.

Time is passed explicitly (``now=``) wherever the test is *about* time, and left
alone otherwise — a hold created at a fixed past instant is already expired by the
time the HTTP client reads it, which is a trap rather than a test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app import deps
from app.config import settings
from app.db import utcnow
from app.models import BookingStatus
from app.routers.payments import NOTIFY_PATH
from app.services import conference as conference_service
from app.services.availability import generate_slots
from app.services.booking import SlotNotBookable, create_booking
from app.services.conference import create_event

# 2026-09-21 is a Monday, so the seeded Mon–Fri rule applies.
CONSULT_DATE = date(2026, 9, 21)
NOW = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)  # 09:00 Shanghai
# 14:00–15:00 Shanghai on that local date.  Both 14:00 and 14:30 are on the
# 30-minute grid, so a one-hour conference must remove two slots.
BLOCKED_START = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
BLOCKED_NEXT = datetime(2026, 9, 21, 6, 30, tzinfo=UTC)

LINK = "https://meeting.tencent.com/dm/TESTROOM"


@pytest.fixture
def wired_deps(monkeypatch, fake_calendar, fake_payments, recording_email):
    """Point ``app.deps`` at the shared fakes.

    ``app.tasks`` resolves its gateways through ``app.deps.get_*`` at call time —
    *not* through FastAPI's ``dependency_overrides``, because a background task runs
    after the request is gone.  Swapping the module attributes is the only thing
    that makes the fakes observable there.
    """
    monkeypatch.setattr(deps, "get_calendar_gateway", lambda: fake_calendar)
    monkeypatch.setattr(deps, "get_payment_gateway", lambda: fake_payments)
    monkeypatch.setattr(deps, "get_email_sender", lambda: recording_email)


def reserve(db: Session, event, payments, *, now=None, email: str = "buyer@example.com"):
    """Hold the next free seat.  ``now=None`` means "the real clock"."""
    return conference_service.reserve_seat(
        db,
        conference_event_id=event.id,
        customer_name="Buyer",
        customer_email=email,
        payments=payments,
        now=now,
    )


def small_event(db: Session, *, capacity: int, **overrides):
    """A cheap event to fill up, so a capacity test does not sell 100 seats."""
    fields = {
        "title": "小场",
        "starts_at": utcnow() + timedelta(days=3),
        "price_fen": 5000,
        "capacity": capacity,
        "join_url": LINK,
    }
    fields.update(overrides)
    return create_event(db, **fields)


def pay(db: Session, seat, payments, *, transaction_id: str = "tx-1"):
    """Settle a seat directly, without going through HTTP."""
    conference_service.mark_seat_paid(db, seat, transaction_id=transaction_id, paid_at=utcnow())
    db.commit()
    return seat


def notify(client, payments, seat):
    headers, body = payments.build_notify(
        out_trade_no=seat.out_trade_no, amount_fen=seat.amount_fen
    )
    return client.post(NOTIFY_PATH, content=body, headers=headers)


# ---------------------------------------------------------------------------
# Seats and capacity
# ---------------------------------------------------------------------------


def test_seats_are_allocated_lowest_free_number_first(db_session, conference, fake_payments):
    first = reserve(db_session, conference, fake_payments)
    second = reserve(db_session, conference, fake_payments)
    assert (first.seat_no, second.seat_no) == (1, 2)

    # Cancelling the first frees number 1: a cancelled row falls out of the
    # partial index's predicate, so the number is genuinely allocatable again.
    conference_service.cancel_seat(db_session, first.reference)
    third = reserve(db_session, conference, fake_payments)
    assert third.seat_no == 1
    assert third.id != first.id


def test_the_seat_after_the_last_one_is_refused(db_session, fake_payments):
    event = small_event(db_session, capacity=3)
    for _ in range(3):
        reserve(db_session, event, fake_payments)

    counts = conference_service.seat_counts(db_session, event)
    assert counts.taken == 3
    assert counts.available == 0
    assert counts.sold_out is True

    with pytest.raises(conference_service.SoldOut):
        reserve(db_session, event, fake_payments)


def test_seat_counts_match_what_the_reservation_path_enforces(db_session, fake_payments):
    """``available`` must hit zero at exactly the moment the next seat is refused.

    ``seat_counts`` and ``reserve_seat`` share ``_taken_seat_numbers`` precisely so
    the number a visitor reads and the number the write path enforces cannot drift.
    This is that shared primitive's contract, stated as a test.
    """
    event = small_event(db_session, capacity=2)
    assert conference_service.seat_counts(db_session, event).available == 2

    reserve(db_session, event, fake_payments)
    assert conference_service.seat_counts(db_session, event).available == 1

    reserve(db_session, event, fake_payments)
    assert conference_service.seat_counts(db_session, event).available == 0

    with pytest.raises(conference_service.SoldOut):
        reserve(db_session, event, fake_payments)


def test_an_expired_hold_gives_its_seat_number_back(db_session, conference, fake_payments):
    first = reserve(db_session, conference, fake_payments, now=NOW)
    assert first.seat_no == 1

    # Past the hold window.  Lazy expiry already stops the dead hold *counting*,
    # but the partial index keys on `status`, so the number only becomes
    # allocatable once the transactional pre-insert sweep flips the row.  Both
    # halves matter and only the sweep frees the number.
    later = NOW + timedelta(minutes=settings.hold_minutes + 1)
    second = reserve(db_session, conference, fake_payments, now=later)

    assert second.seat_no == 1
    assert second.id != first.id
    assert conference_service.seat_counts(db_session, conference, now=later).taken == 1


def test_the_price_is_snapshotted_when_the_seat_is_reserved(
    db_session, conference, fake_payments
):
    seat = reserve(db_session, conference, fake_payments)
    assert seat.amount_fen == 5000
    assert fake_payments.charges[-1].amount_fen == 5000

    conference_service.update_event(db_session, conference, price_fen=9900)
    db_session.refresh(seat)

    # The price the customer was shown is the price they are charged.
    assert seat.amount_fen == 5000


def test_an_event_without_a_meeting_link_cannot_be_sold(db_session, fake_payments):
    """The link is the product.  Selling a seat without one is unfixable later."""
    event = small_event(db_session, capacity=10, join_url="")
    assert event.is_on_sale() is False

    with pytest.raises(conference_service.SeatsClosed):
        reserve(db_session, event, fake_payments)


# ---------------------------------------------------------------------------
# The notify route
# ---------------------------------------------------------------------------


def test_a_ticket_payment_settles_through_the_notify_endpoint(
    client, db_session, conference, fake_payments, recording_email, wired_deps
):
    """A ``TK…`` order must route to `conference_seats`, not to `bookings`."""
    seat = reserve(db_session, conference, fake_payments)
    assert seat.reference.startswith("TK")

    response = notify(client, fake_payments, seat)
    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"

    db_session.expire_all()
    settled = conference_service.get_seat(db_session, seat.reference)
    assert settled.status is BookingStatus.PAID
    assert settled.ticket_sent_at is not None

    assert len(recording_email.sent) == 1
    message = recording_email.sent[0]
    assert message["to"] == "buyer@example.com"
    assert seat.reference in message["subject"]
    assert "第 1 号座位" in message["body"]
    # The join link is the entire point of the email.
    assert LINK in message["body"]
    assert f"{settings.public_base_url}/ticket/{seat.reference}" in message["body"]


def test_a_replayed_ticket_notification_does_not_email_twice(
    client, db_session, conference, fake_payments, recording_email, wired_deps
):
    seat = reserve(db_session, conference, fake_payments)
    headers, body = fake_payments.build_notify(
        out_trade_no=seat.out_trade_no, amount_fen=seat.amount_fen
    )

    first = client.post(NOTIFY_PATH, content=body, headers=headers)
    second = client.post(NOTIFY_PATH, content=body, headers=headers)

    assert first.json()["code"] == "SUCCESS"
    assert second.json()["code"] == "SUCCESS"
    # A second email is a second copy of a secret.
    assert len(recording_email.sent) == 1


def test_a_ticket_payment_with_the_wrong_amount_does_not_settle(
    client, db_session, conference, fake_payments, recording_email, wired_deps
):
    seat = reserve(db_session, conference, fake_payments)
    headers, body = fake_payments.build_notify(
        out_trade_no=seat.out_trade_no,
        amount_fen=seat.amount_fen - 1,  # underpaid by one 分
    )

    response = client.post(NOTIFY_PATH, content=body, headers=headers)
    assert response.json()["code"] == "FAIL"

    db_session.expire_all()
    assert (
        conference_service.get_seat(db_session, seat.reference).status
        is BookingStatus.PENDING_PAYMENT
    )
    assert recording_email.sent == []


def test_a_payment_after_the_event_was_cancelled_needs_a_refund(
    client, db_session, conference, fake_payments, recording_email, wired_deps
):
    """Never ack SUCCESS for money we cannot honour."""
    seat = reserve(db_session, conference, fake_payments)
    conference_service.cancel_event(db_session, conference)

    response = notify(client, fake_payments, seat)
    assert response.json()["code"] == "FAIL"

    db_session.expire_all()
    assert (
        conference_service.get_seat(db_session, seat.reference).status
        is BookingStatus.CANCELLED
    )
    assert recording_email.sent == []


# ---------------------------------------------------------------------------
# The join link is a secret
# ---------------------------------------------------------------------------


def test_the_join_link_is_absent_before_payment(client, db_session, conference, fake_payments):
    seat = reserve(db_session, conference, fake_payments)

    # The public list has no join_url field at all.
    listing = client.get("/api/conferences").json()
    assert listing
    assert all("join_url" not in item for item in listing)

    # The seat status carries the field but must leave it empty.
    status = client.get(f"/api/seats/{seat.reference}").json()
    assert status["status"] == "pending_payment"
    assert status["join_url"] is None
    assert status["join_note"] is None

    # And the page a buyer lands on before paying must not leak it either.
    page = client.get(f"/ticket/{seat.reference}")
    assert page.status_code == 200
    assert LINK not in page.text


def test_the_join_link_appears_once_the_seat_is_paid(
    client, db_session, conference, fake_payments, recording_email, wired_deps
):
    seat = reserve(db_session, conference, fake_payments)
    assert notify(client, fake_payments, seat).json()["code"] == "SUCCESS"

    status = client.get(f"/api/seats/{seat.reference}").json()
    assert status["status"] == "paid"
    assert status["join_url"] == LINK
    assert status["join_note"] == "会议号 123 456 789"

    page = client.get(f"/ticket/{seat.reference}")
    assert LINK in page.text


def test_the_public_page_shows_seats_taken_and_remaining(
    client, db_session, conference, fake_payments
):
    reserve(db_session, conference, fake_payments)
    reserve(db_session, conference, fake_payments)

    page = client.get("/conferences")
    assert page.status_code == 200
    assert "已报名" in page.text
    assert "98" in page.text  # 100 - 2 remaining
    assert LINK not in page.text


# ---------------------------------------------------------------------------
# Cancelling an event
# ---------------------------------------------------------------------------


def test_cancelling_an_event_closes_every_seat_and_names_the_paid_ones(
    db_session, conference, fake_payments
):
    paid = pay(db_session, reserve(db_session, conference, fake_payments), fake_payments)
    held = reserve(db_session, conference, fake_payments)

    needing_refund = conference_service.cancel_event(db_session, conference)

    # v1 issues no automatic refunds, so this list *is* the follow-up work.
    assert needing_refund == [paid.reference]
    assert conference.cancelled_at is not None
    assert conference.is_on_sale() is False

    for reference in (paid.reference, held.reference):
        db_session.expire_all()
        assert (
            conference_service.get_seat(db_session, reference).status
            is BookingStatus.CANCELLED
        )


def test_a_cancelled_event_is_no_longer_bookable(db_session, conference, fake_payments):
    conference_service.cancel_event(db_session, conference)
    with pytest.raises(conference_service.SeatsClosed):
        reserve(db_session, conference, fake_payments)


# ---------------------------------------------------------------------------
# The 1-1 conflict, in both directions
# ---------------------------------------------------------------------------


def test_a_conference_hides_the_slots_it_covers(db_session, event_type, fake_calendar):
    # `now=NOW` on both calls: the grid is bounded by `min_notice_minutes`, so
    # without an injected clock this test would silently depend on what time of day
    # the suite happens to run — and a stale `CONSULT_DATE` would empty the grid
    # entirely rather than fail honestly.
    baseline = generate_slots(
        db_session, event_type, CONSULT_DATE, calendar=fake_calendar, now=NOW
    )
    starts = [slot.start for slot in baseline]
    assert BLOCKED_START in starts
    assert BLOCKED_NEXT in starts

    create_event(
        db_session,
        title="公开课",
        starts_at=BLOCKED_START,
        duration_minutes=60,
        join_url=LINK,
    )

    blocked = [
        slot.start
        for slot in generate_slots(
            db_session, event_type, CONSULT_DATE, calendar=fake_calendar, now=NOW
        )
    ]
    # A one-hour conference removes two 30-minute starts, not just the top of the
    # hour — the whole window is the owner's time.
    assert BLOCKED_START not in blocked
    assert BLOCKED_NEXT not in blocked
    assert len(blocked) == len(baseline) - 2


def test_a_booking_overlapping_a_conference_is_refused(
    db_session, event_type, fake_calendar, fake_payments
):
    """The grid is advice; this is the rule.  A crafted POST must be refused too."""
    create_event(
        db_session,
        title="公开课",
        starts_at=BLOCKED_START,
        duration_minutes=60,
        join_url=LINK,
    )

    with pytest.raises(SlotNotBookable):
        create_booking(
            db_session,
            event_type_id=event_type.id,
            slot_start=BLOCKED_START,
            customer_name="Alice",
            customer_email="alice@example.com",
            calendar=fake_calendar,
            payments=fake_payments,
            now=NOW,
        )


def test_a_cancelled_conference_stops_blocking_slots(db_session, event_type, fake_calendar):
    event = create_event(
        db_session,
        title="公开课",
        starts_at=BLOCKED_START,
        duration_minutes=60,
        join_url=LINK,
    )
    conference_service.cancel_event(db_session, event)

    starts = [
        slot.start
        for slot in generate_slots(
            db_session, event_type, CONSULT_DATE, calendar=fake_calendar, now=NOW
        )
    ]
    assert BLOCKED_START in starts


def test_scheduling_over_a_live_booking_is_reported(
    db_session, event_type, fake_calendar, fake_payments
):
    """Reported, not refused: the conference is the commitment that cannot move."""
    booking = create_booking(
        db_session,
        event_type_id=event_type.id,
        slot_start=BLOCKED_START,
        customer_name="Alice",
        customer_email="alice@example.com",
        calendar=fake_calendar,
        payments=fake_payments,
        now=NOW,
    )

    event = create_event(
        db_session,
        title="公开课",
        starts_at=BLOCKED_START,
        duration_minutes=60,
        join_url=LINK,
    )
    # `now=NOW` matters here too: "live" applies lazy expiry, and a hold created at
    # a fixed past instant is already dead by the time the real clock reads it.
    assert conference_service.clashing_bookings(db_session, event, now=NOW) == [
        booking.reference
    ]


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def test_the_admin_conference_endpoints_require_the_token(client):
    assert client.get("/api/admin/conferences").status_code == 401
    assert client.post("/api/admin/conferences", json={"title": "x"}).status_code == 401


def test_the_admin_can_schedule_edit_and_cancel_a_conference(client, db_session):
    token = {"X-Admin-Token": settings.secret_key}
    starts_at = (utcnow() + timedelta(days=10)).isoformat()

    created = client.post(
        "/api/admin/conferences",
        headers=token,
        json={
            "title": "出海获客公开课",
            "starts_at": starts_at,
            "price_fen": 5000,
            "capacity": 100,
            "join_url": LINK,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    conference_id = body["conference"]["id"]
    assert body["conference"]["capacity"] == 100
    assert body["conference"]["seats_available"] == 100
    assert body["clashing_bookings"] == []

    # A PATCH only touches what it names.
    edited = client.patch(
        f"/api/admin/conferences/{conference_id}",
        headers=token,
        json={"title": "改期后的公开课", "capacity": 120},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["conference"]["title"] == "改期后的公开课"
    assert edited.json()["conference"]["capacity"] == 120
    assert edited.json()["conference"]["join_url"] == LINK  # untouched

    seats = client.get(f"/api/admin/conferences/{conference_id}/seats", headers=token)
    assert seats.status_code == 200
    assert seats.json() == []

    cancelled = client.post(f"/api/admin/conferences/{conference_id}/cancel", headers=token)
    assert cancelled.status_code == 200
    assert cancelled.json()["paid_seats_needing_refund"] == []


def test_scheduling_without_an_offset_is_refused(client):
    token = {"X-Admin-Token": settings.secret_key}
    response = client.post(
        "/api/admin/conferences",
        headers=token,
        json={"title": "无时区", "starts_at": "2026-10-01T20:00:00"},
    )
    assert response.status_code == 422


def test_capacity_cannot_be_lowered_below_the_seats_already_taken(
    db_session, conference, fake_payments
):
    reserve(db_session, conference, fake_payments)
    reserve(db_session, conference, fake_payments)

    with pytest.raises(conference_service.CapacityBelowSeats):
        conference_service.update_event(db_session, conference, capacity=1)

    # Exactly the number taken is allowed — that is simply sold out, not invalid.
    conference_service.update_event(db_session, conference, capacity=2)
    assert conference.capacity == 2
    assert conference_service.seat_counts(db_session, conference).sold_out is True


def test_editing_an_unknown_field_is_refused(db_session, conference):
    with pytest.raises(ValueError):
        conference_service.update_event(db_session, conference, seat_no=5)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def test_the_ticket_page_404s_for_an_unknown_reference(client):
    assert client.get("/ticket/TKZZZZZZ").status_code == 404


def test_a_cancelled_conference_page_404s(client, db_session, conference):
    conference_service.cancel_event(db_session, conference)
    assert client.get(f"/conference/{conference.id}").status_code == 404


def test_the_conference_page_renders_the_sign_up_form(client, conference):
    page = client.get(f"/conference/{conference.id}")
    assert page.status_code == 200
    assert conference.title in page.text
    assert "¥50" in page.text
    # The join link must not be on the sign-up page either.
    assert LINK not in page.text
