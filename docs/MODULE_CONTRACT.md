# MODULE_CONTRACT.md

Frozen interfaces for the paid 1-1 booking service. **Read this before writing code.**
If reality diverges from this document, the document is wrong — report the divergence, do
not silently invent a third shape.

---

## 1. Purpose

A public booking page where an individual pays by WeChat Pay (Native / 扫码) for a 1-1 call
slot. **A booking is confirmed only after WeChat Pay's signed notification verifies the
payment.** Slots come from the owner's Google Calendar, which is the source of truth for
busy time.

## 2. Topology (decided — do not redesign)

Two deployables, one repository.

| Deployable | Where | Why |
|---|---|---|
| `app/` (booking app) | Railway now → **Tencent Cloud + ICP-filed domain** later | WeChat Pay Native on a PC website requires an ICP-filed domain, which requires mainland hosting |
| `relay/` (calendar relay) | **Railway** | `googleapis.com` is unreachable from mainland servers; the relay holds the Google credentials and is the only thing that talks to Google |

Outbound `mainland → Railway` is fine. `Railway → Google` is fine. Neither box does both.
The booking app **never** holds a Google credential in production; it calls the relay.

**Corollary:** the booking app must work with `CALENDAR_GATEWAY=fake` and
`PAYMENT_GATEWAY=fake`, with zero external services, for all local dev and tests.

## 3. Stack and non-obvious conventions

- Python 3.13, FastAPI, **SQLAlchemy 2.0 in synchronous mode** (sync ORM so the web app and
  the Celery worker share one engine and one sessionmaker — do not introduce async sessions).
- Pydantic v2 + `pydantic-settings` for config. Jinja2 for server-rendered pages.
- Celery + Redis in prod. **Correctness must not depend on Celery** — see §7.
- SQLite for dev/tests, Postgres in prod. Anything you write must run on both.
- `httpx` for outbound HTTP. `cryptography` for RSA/AES.
- pytest. No network access in tests, ever.

### Money
**All amounts are integer 分 (fen), currency `CNY`.** `¥500` is `50000`. No floats, no
`Decimal` in the DB, no yuan anywhere in code. Format only at the edge, for display.

### Time
All datetimes in the DB and in every function signature are **timezone-aware UTC**.
Naive datetimes are a bug. The display timezone is `SETTINGS.default_timezone`
(`Asia/Shanghai`) and is applied only in templates and in slot-grid generation.
Event types carry their own `timezone`; slot grids are generated in that timezone, then
converted to UTC at the boundary.

## 4. Directory layout and ownership

```
booking-service/
  docs/MODULE_CONTRACT.md      FROZEN — owner: integrator
  pyproject.toml               FROZEN after foundation
  .env.example                 FROZEN after foundation
  README.md                    owner: M7
  Dockerfile                   owner: M7   (booking image — MUST keep the plain name)
  Dockerfile.relay             owner: M7
  railway.json                 owner: M7   (booking service config; Railway auto-reads this)
  railway.relay.json           owner: M7   (relay service config; must be selected explicitly)

  app/
    main.py                    FOUNDATION (routers registered in try/except ImportError)
    config.py                  FOUNDATION — owner: integrator
    db.py                      FOUNDATION — owner: integrator
    models.py                  FOUNDATION — owner: integrator  (AUTHORITATIVE)
    schemas.py                 FOUNDATION — owner: integrator
    tasks.py                   owner: M6
    seed.py                    FOUNDATION — owner: integrator
    ports/
      calendar.py              FOUNDATION — owner: integrator
      payments.py              FOUNDATION — owner: integrator
      email.py                 FOUNDATION — owner: integrator
    adapters/
      calendar_relay.py        owner: M4
      calendar_google.py       owner: M4
      calendar_fake.py         owner: M4
      payments_wechat.py       owner: M2
      payments_fake.py         owner: M2
      email_smtp.py            owner: M6
      email_console.py         owner: M6
    services/
      availability.py          owner: M1
      booking.py               owner: M1
      sweeper.py               owner: M6
    routers/
      pages.py                 owner: M5
      booking.py               owner: M1
      payments.py              owner: M2
      admin.py                 owner: M1
    templates/                 owner: M5
    static/                    owner: M5

  relay/
    main.py                    owner: M3
    config.py                  owner: M3
    auth.py                    owner: M3
    google.py                  owner: M3
    schemas.py                 owner: M3

  tests/
    conftest.py                FOUNDATION — owner: integrator. OFF-LIMITS to module agents.
    fakes.py                   FOUNDATION — owner: integrator. OFF-LIMITS to module agents.
    test_foundation.py         FOUNDATION — owner: integrator
    test_availability.py       owner: M1
    test_booking.py            owner: M1
    test_wechat_signature.py   owner: M2
    test_notify_idempotency.py owner: M2
    test_relay.py              owner: M3
    test_calendar_adapters.py  owner: M4
    test_expiry.py             owner: M6
    test_e2e.py                owner: M7
```

**Do not touch `main.py`, `pyproject.toml`, `config.py`, `db.py`, `models.py`, `ports/**`,
`tests/conftest.py`, `tests/fakes.py`, or any sibling module's files.** If you need a change
in a frozen file, implement against the frozen shape and report the needed change in your
summary.

## 5. Settings — frozen field names

`app/config.py` exposes `settings` (a module-level singleton) and `get_settings()`.

```
app_env: str = "dev"                    # "dev" | "prod"
database_url: str = "sqlite:///./booking.db"
secret_key: str = "dev-only-change-me"
public_base_url: str = "http://localhost:8000"
default_timezone: str = "Asia/Shanghai"

calendar_gateway: str = "fake"          # "relay" | "google" | "fake"
calendar_relay_url: str = ""
calendar_relay_secret: str = ""
google_client_id: str = ""
google_client_secret: str = ""
google_refresh_token: str = ""
google_calendar_id: str = "primary"

payment_gateway: str = "fake"           # "wechat" | "fake"
wechat_app_id: str = ""
wechat_mch_id: str = ""
wechat_api_v3_key: str = ""
wechat_cert_serial_no: str = ""
wechat_private_key_path: str = ""
wechat_public_key_id: str = ""
wechat_public_key_path: str = ""

hold_minutes: int = 10
slot_step_minutes: int = 15
sweeper_interval_seconds: int = 60

email_backend: str = "console"          # "console" | "smtp"
smtp_host: str = ""
smtp_port: int = 465
smtp_user: str = ""
smtp_password: str = ""
email_from: str = ""
```

`wechat_notify_url` is **derived**, not configured: `{public_base_url}/api/payments/wechat/notify`.

## 6. Models — frozen names and fields

`app/models.py` is authoritative. `datetime` columns are `DateTime(timezone=True)`.

### `BookingStatus` (str Enum)
`PENDING_PAYMENT="pending_payment"`, `PAID="paid"`, `EXPIRED="expired"`,
`CANCELLED="cancelled"`.

### `EventType`
| field | type | notes |
|---|---|---|
| `id` | `str` PK | slug, e.g. `"consult-30"` |
| `title` | `str` | `"1-1 consultation"` |
| `description` | `str` | shown on the page |
| `duration_minutes` | `int` | 30 |
| `price_fen` | `int` | `50000` = ¥500 |
| `currency` | `str` | `"CNY"` |
| `buffer_before_minutes` | `int` | default 0 |
| `buffer_after_minutes` | `int` | default 0 |
| `min_notice_minutes` | `int` | cannot book sooner than this; default 240 |
| `max_days_ahead` | `int` | booking window; default 60 |
| `timezone` | `str` | `"Asia/Shanghai"` |
| `active` | `bool` | default True |

### `AvailabilityRule`
`id` int PK · `event_type_id` FK → `EventType.id` · `weekday` int (0=Mon … 6=Sun) ·
`start_local` `Time` · `end_local` `Time`. Multiple rules per event type per weekday are
allowed and are unioned.

### `Booking`
| field | type | notes |
|---|---|---|
| `id` | `str` PK | uuid4 hex |
| `reference` | `str` unique | short human code, e.g. `BK7Q2M4X` |
| `event_type_id` | FK | |
| `slot_start` / `slot_end` | aware UTC datetime | |
| `status` | `BookingStatus` | |
| `expires_at` | aware UTC datetime | hold deadline |
| `amount_fen` | `int` | **snapshot at creation** — never re-read `EventType.price_fen` |
| `currency` | `str` | `"CNY"` |
| `customer_name` / `customer_email` | `str` | required |
| `customer_phone` / `customer_note` | `str \| None` | optional |
| `calendar_event_id` | `str \| None` | |
| `out_trade_no` | `str \| None` unique | WeChat merchant order number |
| `provider_transaction_id` | `str \| None` | WeChat `transaction_id` |
| `paid_at` | aware UTC datetime \| None | |
| `created_at` / `updated_at` | aware UTC datetime | |

**Partial unique index** — the double-booking guard:

```
uq_active_slot ON bookings (event_type_id, slot_start)
  WHERE status IN ('pending_payment', 'paid')
```

Implemented with `postgresql_where` **and** `sqlite_where` so it holds in both engines.

### `PaymentEvent` (append-only audit + idempotency)
`id` int PK · `out_trade_no` str index · `transaction_id` str unique nullable ·
`kind` str (`"notify"` \| `"refund_notify"`) · `raw_headers` JSON · `raw_body` Text ·
`outcome` str · `received_at` aware UTC.

`transaction_id` unique is the replay guard: WeChat resends a notification up to 15 times.

## 7. Expiry — correctness does not depend on the worker

1. **Lazy expiry (authoritative).** Any read that can be affected by a hold calls
   `booking.is_expired(now)` and treats a `PENDING_PAYMENT` booking past `expires_at` as
   expired. Availability always excludes expired holds.
2. **Pre-insert sweep (transactional).** Before inserting a new booking, the same transaction
   runs a targeted `UPDATE bookings SET status='expired' WHERE status='pending_payment' AND
   expires_at <= :now AND event_type_id = :et AND slot_start = :slot`. This is what lets the
   partial unique index in §6 actually free the slot.
3. **Sweeper (hygiene only).** A periodic task releases the calendar hold and closes the
   WeChat order for rows already flipped to expired. If the sweeper is down, nothing breaks —
   slots still free correctly and stale calendar holds are the only residue.

## 8. Ports — frozen signatures

### `app/ports/calendar.py`

```python
@dataclass(frozen=True)
class BusyInterval:
    start: datetime   # aware UTC
    end: datetime

@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    html_link: str | None = None

class CalendarGateway(Protocol):
    def freebusy(self, time_min: datetime, time_max: datetime) -> list[BusyInterval]: ...
    def create_hold(self, *, summary: str, description: str,
                    start: datetime, end: datetime, reference: str) -> CalendarEvent: ...
    def confirm(self, event_id: str, *, summary: str, description: str) -> None: ...
    def release(self, event_id: str) -> None: ...

class CalendarGatewayError(Exception): ...
```

A hold is created as a **tentative / opaque** event titled `HOLD · <customer> · <reference>`
so it blocks the slot immediately. `confirm()` rewrites it to the real title and description.

### `app/ports/payments.py`

```python
@dataclass(frozen=True)
class ChargeRequest:
    out_trade_no: str
    amount_fen: int
    description: str
    expires_at: datetime   # aware UTC

@dataclass(frozen=True)
class ChargeResult:
    code_url: str
    provider_order_id: str | None = None

@dataclass(frozen=True)
class PaymentNotification:
    out_trade_no: str
    transaction_id: str
    amount_fen: int
    trade_state: str            # "SUCCESS" | "CLOSED" | ...
    success_time: datetime | None

class PaymentGateway(Protocol):
    def create_charge(self, req: ChargeRequest) -> ChargeResult: ...
    def parse_notification(self, headers: Mapping[str, str], body: bytes) -> PaymentNotification: ...
    def query_order(self, out_trade_no: str) -> PaymentNotification | None: ...
    def close_order(self, out_trade_no: str) -> None: ...

class PaymentSignatureError(Exception): ...
class PaymentAmountMismatch(Exception): ...
```

`parse_notification` **must** verify the signature before decrypting, and **must** raise
`PaymentSignatureError` on a bad signature — never return unverified data.

### `app/ports/email.py`

```python
class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...
```

## 9. Relay HTTP contract — frozen

All bodies JSON. All datetimes ISO-8601 with an explicit UTC offset.

Auth on every request except `GET /healthz`:

```
X-Relay-Timestamp: <unix seconds>
X-Relay-Signature: hex(hmac_sha256(secret, f"{timestamp}.{raw_body}"))
```

Reject if `|now - timestamp| > 300`. Compare with `hmac.compare_digest`.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/healthz` | — | `{"ok": true}` |
| POST | `/freebusy` | `{"time_min": str, "time_max": str}` | `{"busy": [{"start": str, "end": str}]}` |
| POST | `/events` | `{"summary","description","start","end","reference","transparent": bool}` | `{"event_id": str, "html_link": str\|null}` |
| PATCH | `/events/{event_id}` | `{"summary","description"}` | `{"event_id": str}` |
| DELETE | `/events/{event_id}` | — | `{"deleted": true}` |

`transparent: true` → the event does not block time (`transparency: "transparent"`).
Holds are created with `transparent: false`.

Errors: `4xx/5xx` with `{"detail": str}`. `404` from `PATCH`/`DELETE` on an unknown event is
**not** an error for the caller — `CalendarGateway.release` treats it as already released.

## 10. Celery task names — frozen

| Task | Args | Does |
|---|---|---|
| `app.tasks.release_expired_holds` | — | Sweep expired holds: release calendar, close WeChat order |
| `app.tasks.finalize_paid_booking` | `booking_id: str` | Confirm calendar event, send confirmation email |
| `app.tasks.release_booking_hold` | `booking_id: str` | Release one hold (cancel path) |

In dev (`app_env == "dev"`) the app runs these inline via `BackgroundTasks`; the task
functions must therefore be plain importable functions that Celery wraps, and must be safe to
call synchronously.

## 11. HTTP surface — booking app

| Method | Path | Notes |
|---|---|---|
| GET | `/` | Booking page (HTML) |
| GET | `/api/event-types` | List active event types |
| GET | `/api/slots?event_type_id=&date=` | Available slots for a local date |
| POST | `/api/bookings` | Create hold + WeChat order. Returns `{reference, code_url, amount_fen, expires_at, status}` |
| GET | `/api/bookings/{reference}` | Status for polling. Returns `{reference, status, amount_fen, expires_at, slot_start, event_type_id}` |
| POST | `/api/bookings/{reference}/cancel` | Customer cancel (only while `pending_payment`) |
| POST | `/api/payments/wechat/notify` | WeChat callback. **No auth header** — authenticity comes from the signature |
| GET | `/book/{reference}` | Status page (HTML) |
| GET | `/healthz` | `{"ok": true}` |
| GET | `/api/admin/bookings` | Read-only list, requires `X-Admin-Token: settings.secret_key` |

Ticketed conferences — §14.5. Same token for every `/api/admin/*` row.

| Method | Path | Notes |
|---|---|---|
| GET | `/conferences` | Conference list (HTML), with seats taken / remaining |
| GET | `/conference/{conference_id}` | Sign-up page (HTML). 404s when cancelled or inactive |
| GET | `/ticket/{reference}` | Pay, then read the join link (HTML) |
| GET | `/api/conferences` | Upcoming events + seat counts. **Never carries `join_url`** |
| POST | `/api/conferences/{conference_id}/seats` | Hold a seat + WeChat order. `201` / `404` unknown / `409` sold out / `422` not on sale |
| GET | `/api/seats/{reference}` | Ticket status. `join_url` and `join_note` are `null` until `paid` |
| POST | `/api/seats/{reference}/cancel` | Give up a pending hold; frees the seat number |
| GET | `/api/admin/conferences` | Every event, cancelled and past included |
| POST | `/api/admin/conferences` | Schedule one. Returns `clashing_bookings` |
| PATCH | `/api/admin/conferences/{conference_id}` | Reschedule / re-price / edit the link / change capacity |
| POST | `/api/admin/conferences/{conference_id}/cancel` | Cancel; returns `paid_seats_needing_refund` |
| GET | `/api/admin/conferences/{conference_id}/seats` | Attendee list, by seat number |

`POST /api/conferences/{id}/seats` body:
`{"customer_name","customer_email","customer_phone?"}` — **no amount field**, ever. The
price is snapshotted from the event row.

**The join link is a secret.** `join_url` is delivered in the ticket email after a
verified payment and appears on no response reachable before one. That rule is enforced
in the routers, not in the templates: `GET /api/seats/{reference}` and `/ticket/{reference}`
both gate it on `status == "paid"`.

`POST /api/bookings` body:
`{"event_type_id","slot_start","customer_name","customer_email","customer_phone?","customer_note?"}`
`slot_start` is ISO-8601 **with offset**. Reject `422` if the slot is not on the grid, is
inside `min_notice_minutes`, is beyond `max_days_ahead`, or is already taken → `409`.

Notify endpoint contract:
- Verify signature → decrypt → persist `PaymentEvent` → mark paid **only if**
  `notification.amount_fen == booking.amount_fen` and `trade_state == "SUCCESS"`.
- Idempotent: a repeat `transaction_id` returns success without side effects.
- Respond `{"code":"SUCCESS","message":"成功"}` within 5 s. Any real work goes to a
  background task.
- On amount mismatch: log loudly, leave the booking `pending_payment`, respond
  `{"code":"FAIL","message":"amount mismatch"}`.

## 12. Local dev and test rules

- `uv run pytest` must pass with **no network and no external services**.
- Tests use SQLite in a temp file, `CALENDAR_GATEWAY=fake`, `PAYMENT_GATEWAY=fake`,
  `EMAIL_BACKEND=console`.
- `tests/conftest.py` provides: `db_session`, `client`, `event_type` fixtures, and a
  `freeze_now` helper. `tests/fakes.py` provides `FakeCalendarGateway`, `FakePaymentGateway`,
  `RecordingEmailSender`, and `make_wechat_notify(...)` which builds a genuinely signed,
  genuinely AES-GCM-encrypted WeChat notification body from a throwaway keypair.
  **These two files are owned by the integrator and are off-limits.**
- A test that needs a new fixture must build it locally in its own file, or request the
  change in its report.

## 13. Out of scope for v1 — do not build

Refunds, 发票/invoicing, coupons, multi-staff round-robin, mini-program, admin CRUD UI,
i18n, Stripe, recurring availability exceptions, Google Calendar push notifications
(the sweeper plus freebusy is enough). Flag any of these as a follow-up, don't implement.

---

# 14. Internal service API — frozen by the integrator

§5–§13 freeze the *external* surface. These freeze the *internal* one, because three
modules call into `app/services/**` and would otherwise invent three shapes.

Rules that make the rest of this section work:

- **Routers resolve gateways with `Depends(app.deps.get_*)` and pass them into services
  as arguments.** Services never import `app.deps` and never build a gateway. This is
  what lets `tests/conftest.py` swap in the fakes via `dependency_overrides`.
- **Every service function takes an explicit `now: datetime | None = None`** and resolves
  it with `now or utcnow()`. Tests pass `now` instead of freezing the clock.
- All datetimes crossing these boundaries are **aware UTC**.

## 14.1 `app/services/availability.py` (owner M1)

```python
@dataclass(frozen=True)
class Slot:
    start: datetime   # aware UTC
    end: datetime     # aware UTC

def local_date_bounds(event_type: EventType, local_date: date) -> tuple[datetime, datetime]
    """The [start, end) UTC window covering `local_date` in event_type.timezone."""

def effective_step_minutes(event_type: EventType) -> int
    """The grid step actually used: max(slot_step_minutes, duration_minutes).

    A step finer than the duration puts two overlapping starts in the grid
    (09:00-09:30 *and* 09:15-09:45 for a 30-minute service), so the step is
    clamped per event type. `is_slot_on_grid` uses the same value, which is what
    keeps the grid and the booking path agreeing about which starts exist.
    """

def generate_slots(
    db: Session,
    event_type: EventType,
    local_date: date,
    *,
    calendar: CalendarGateway,
    now: datetime | None = None,
) -> list[Slot]
    """The bookable grid for one local date.

    On the `effective_step_minutes` grid; inside an AvailabilityRule window for
    that weekday; inside [now + min_notice_minutes, now + max_days_ahead];
    not overlapping calendar busy time; not overlapping a live booking.
    Ascending by start, and pairwise non-overlapping.
    """

def is_slot_on_grid(
    db: Session,
    event_type: EventType,
    slot_start: datetime,
    *,
    now: datetime | None = None,
) -> bool
    """Grid + notice + window only. Does NOT consult the calendar or the DB."""
```

## 14.2 `app/services/booking.py` (owner M1)

```python
class BookingError(Exception): ...
class EventTypeNotFound(BookingError): ...
class SlotNotBookable(BookingError): ...      # -> HTTP 422
class SlotTaken(BookingError): ...            # -> HTTP 409
class BookingNotFound(BookingError): ...      # -> HTTP 404
class BookingNotCancellable(BookingError): ...# -> HTTP 409

def create_booking(
    db: Session,
    *,
    event_type_id: str,
    slot_start: datetime,
    customer_name: str,
    customer_email: str,
    calendar: CalendarGateway,
    payments: PaymentGateway,
    customer_phone: str | None = None,
    customer_note: str | None = None,
    now: datetime | None = None,
) -> Booking
    """Validate -> pre-insert sweep -> insert hold -> calendar hold -> WeChat charge.

    Raises SlotNotBookable / SlotTaken / EventTypeNotFound.  Commits before returning.
    `amount_fen` is copied from EventType.price_fen and snapshotted.  `out_trade_no`
    equals `reference`.  On a WeChat failure the booking is rolled back and the
    calendar hold released — never leave a hold with no order.
    """

def expire_stale_holds(
    db: Session,
    *,
    event_type_id: str | None = None,
    slot_start: datetime | None = None,
    now: datetime | None = None,
) -> int
    """The transactional pre-insert sweep of contract §7.2. Returns rows flipped.

    With both filters given it touches only the one slot; with neither it is the
    full sweep. Flipping status is ALL it does — releasing the calendar hold and
    closing the WeChat order is the sweeper's job (§7.3).
    """

def cancel_booking(
    db: Session, reference: str, *, calendar: CalendarGateway, now: datetime | None = None
) -> Booking
    """Only while pending_payment. Releases the calendar hold. Raises
    BookingNotFound / BookingNotCancellable."""

def mark_paid(
    db: Session,
    booking: Booking,
    *,
    transaction_id: str,
    paid_at: datetime | None = None,
) -> Booking
    """pending_payment -> paid, sets provider_transaction_id and paid_at. Idempotent:
    an already-paid booking returns unchanged. Does NOT touch the calendar or email —
    that is `app.tasks.finalize_paid_booking`."""

def get_booking(db: Session, reference: str) -> Booking | None

def booking_summary(booking: Booking) -> str
    """The human description used for both the calendar event and the email."""
```

## 14.3 `app/tasks.py` (owner M6) — importable, synchronously callable

Contract §10 names them; these are the Python signatures. Each opens its own session
via `app.db.session_scope()` and each is safe to call inline from `BackgroundTasks`.

```python
def release_expired_holds() -> int
def finalize_paid_booking(booking_id: str) -> None
def finalize_ticket(seat_id: str) -> None
def release_booking_hold(booking_id: str) -> None
```

`app/routers/payments.py` (M2) dispatches `finalize_paid_booking` or `finalize_ticket`
after responding; it imports the function **inside** the handler so the notify endpoint
still works if `app/tasks.py` is missing. `finalize_ticket` is idempotent on
`ConferenceSeat.ticket_sent_at` — a replayed notify must not email the join link twice.

## 14.4 Cross-module imports — the allowed set

| From | May import |
|---|---|
| M1 (`services/booking.py`, `routers/booking.py`, `routers/admin.py`) | `app.services.availability`, `app.services.conference`, `app.models`, `app.schemas`, `app.deps` (routers only) |
| M2 (`routers/payments.py`, `adapters/payments_*`) | `app.services.booking.mark_paid`, `app.services.conference`, `app.models`, `app.schemas` |
| M3 (`relay/**`) | nothing from `app.*` — the relay is a standalone deployable |
| M4 (`adapters/calendar_*`) | `app.ports.calendar` only |
| M5 (`routers/pages.py`, `templates/`, `static/`) | `app.services.booking.get_booking`, `app.services.availability`, `app.services.conference` |
| M6 (`tasks.py`, `services/sweeper.py`, `adapters/email_*`) | `app.services.booking`, `app.services.conference`, `app.adapters.*` via `app.deps` |
| Conferences (`services/conference.py`, `routers/conferences.py`) | `app.services.availability.conference_intervals`, `app.models`, `app.schemas`, `app.deps` (routers only) |

Anything not in this table needs an integrator decision. Import lazily inside the
function body where a sibling might still be unwritten.

## 14.5 `app/services/conference.py` — ticketed conferences

Added 2026-09-29. Conferences get their own tables, their own service and their own
capacity guard rather than a `capacity` column on `EventType`, because the difference is
structural: `Booking.uq_active_slot` is `UNIQUE(event_type_id, slot_start)` over live
rows, which permits exactly **one** live booking per start — a 100-seat event cannot be
represented in that table at all.

```python
# errors
class ConferenceError(Exception)
class ConferenceNotFound(ConferenceError)
class SeatsClosed(ConferenceError)      # cancelled / inactive / already started / no join link
class SoldOut(ConferenceError)          # every seat taken, counting unexpired holds
class SeatNotFound(ConferenceError)
class SeatNotBookable(ConferenceError)
class SeatNotCancellable(ConferenceError)
class CapacityBelowSeats(ConferenceError)

@dataclass(frozen=True)
class SeatCounts:                       # capacity, taken, paid
    available: int                      # property
    sold_out: bool                      # property

# counting — one primitive, used by both display and allocation
def seat_counts(db, event, *, now=None) -> SeatCounts
def expire_stale_seats(db, *, conference_event_id=None, seat_no=None, now=None) -> int

# reading
def get_event(db, conference_id) -> ConferenceEvent | None
def get_seat(db, reference) -> ConferenceSeat | None
def list_upcoming(db, *, now=None, include_past=False) -> list[ConferenceEvent]
def list_all(db) -> list[ConferenceEvent]
def seats_for(db, event, *, paid_only=False) -> list[ConferenceSeat]
def clashing_bookings(db, event, *, now=None) -> list[str]

# managing (admin)
def create_event(db, *, title, starts_at, description="", duration_minutes=60,
                 timezone=..., price_fen=5000, capacity=100,
                 join_url="", join_note="", active=True) -> ConferenceEvent
def update_event(db, event, **fields) -> ConferenceEvent
def cancel_event(db, event, *, now=None) -> list[str]   # returns PAID refs needing a refund

# selling
def reserve_seat(db, *, conference_event_id, customer_name, customer_email,
                 payments, customer_phone=None, now=None) -> ConferenceSeat
def cancel_seat(db, reference, *, now=None) -> ConferenceSeat
def mark_seat_paid(db, seat, *, transaction_id, paid_at=None) -> ConferenceSeat
def honour_late_seat_payment(db, seat, *, transaction_id, paid_at=None, now=None) -> ConferenceSeat
```

Three properties the implementation depends on:

1. **Capacity is a database invariant.** `ConferenceSeat.uq_live_seat` is a partial
   unique index on `(conference_event_id, seat_no)` over live rows. `reserve_seat`
   allocates the lowest free number in `1..capacity` inside a `begin_nested()` savepoint
   and retries on `IntegrityError`, so two simultaneous payments cannot produce the 101st
   seat — the index refuses the insert, not a `SELECT COUNT(*)` both requests read.
2. **The index predicate is on `status`, not `expires_at`.** A hold that has run out is
   logically dead but still physically `pending_payment`, so it still occupies its number.
   The transactional pre-insert sweep is what frees a number; correctness must not depend
   on the sweeper running (same rule as §7).
3. **Display and enforcement share `_taken_seat_numbers`.** `seat_counts` and
   `reserve_seat` read the same set, so a page cannot advertise a seat the write path
   refuses.

`ConferenceEvent.is_on_sale()` requires a non-empty `join_url`. The meeting link *is* the
product, so an event whose link does not exist yet is scheduled but not sellable — the
owner pastes the link and it opens at that moment.

A conference also blocks 1-1 time, both directions: `conference_intervals` feeds
`generate_slots` (hides) and `create_booking` (refuses, `422`). `active=False` alone does
**not** stop the blocking — only `cancel_event`, which sets `cancelled_at` too.


# 15. Addenda to the frozen contract (integrator)

- **`Booking.code_url`** added to §6 — persists the WeChat Native `code_url` so the
  status page can re-render the QR offline. Additive only.
- **`email-validator`** added to `pyproject.toml` dependencies — `EmailStr` in
  `app/schemas.py` requires it and it was missing.
- **`pythonpath = ["."]`** added to the pytest config so `import app` / `import tests`
  resolve without an editable install.
- **`tests/__init__.py`** added so `from tests.fakes import ...` resolves.

### Integrator addenda, second pass (2026-09-19, after the module build)

Written after the end-to-end pass found two real defects. Each of these is a
deliberate change to a frozen shape; the reasoning matters more than the diff.

- **`Booking.finalized_at`** added to §6. The first cut overloaded
  `calendar_event_id IS NULL` to mean both "calendar confirmed and email sent" and
  "never held". Those are different facts, and merging them meant a booking whose
  hold had been released before the payment landed got **no calendar event and no
  email at all** — silently. `finalized_at` is the workflow marker;
  `calendar_event_id` stays a calendar fact and is no longer cleared on finalise.
- **`app/services/booking.py` gains `PaymentConflict` and `honour_late_payment()`.**
  §11 never said what to do when a verified payment arrives for a booking whose hold
  already expired or was cancelled, and the handler's answer was to ack SUCCESS
  while `mark_paid` did nothing — money taken, booking unchanged, WeChat never
  retrying, audit row claiming "paid". The rule now: a verified payment is never
  dropped. Still pending → normal transition; expired/cancelled but the slot is
  still free → honour it (they paid, and v1 has no refunds, so the only
  non-harmful outcome is the call); expired/cancelled and the slot was resold →
  `PaymentConflict`, a FAIL ack, and an `outcome="paid_conflict"` audit row for
  whoever has to refund.
- **`app/routers/payments.py` refuses to ack SUCCESS unless the booking is
  `paid`.** Belt and braces on the same invariant: if the handler and the service
  ever drift apart again, WeChat must not be told a lie.
- **`owned_intervals()` gained `event_type_id` and `now`.** It used to ignore *any*
  calendar interval we owned, which meant a live hold of event type X did not block
  event type Y at the same time — a latent double-booking hole the moment a second
  event type exists. It now ignores an interval only when the owning booking is for
  the *same* event type (the DB adjudicates those via the partial index and lazy
  expiry) or is already dead. A live hold of a different event type keeps blocking,
  because two 1-1 calls cannot overlap.
- **`app/tasks.py::_finalize_paid_booking` creates the calendar event when there is
  no hold to confirm.** Follows from the late-payment policy: the sweep may have
  released the hold already, and the owner must still get an invitation.
- **`tests/test_integration.py`** added — integrator-owned cross-module guards. The
  three defects above all lived *between* modules, which is exactly where no single
  module agent was looking.
- **`nextjs-integration/`** added — the BUILD_PLAN §3 files for the
  www.zhituoyuan.com repo, kept here as a patch because that repo is not in this
  build. See its README for the two edits still needed in the site repo.
- **Divergence from BUILD_PLAN §5, resolved in favour of the contract:**
  BUILD_PLAN says `POST /api/bookings` returns an inline `qr_svg`; §11 and
  `app/schemas.py` say `code_url`. The contract wins. The payment token is kept out
  of client JS a different way — the slot picker redirects to
  `{PAY_BASE}/book/{reference}` and the QR is rendered server-side into that HTML
  (verified: the status page contains an inline `<svg>` and never the `code_url`
  string).

### Integrator addenda, third pass (2026-09-19, closing the §10 gaps)

- **`app/rate_limit.py` added** — BUILD_PLAN §10's *"rate-limit `POST /api/bookings`"*,
  which the module build left unbuilt. `FixedWindowLimiter` is a plain in-process
  fixed-window counter; the dependency is attached in `app/routers/booking.py` to
  `POST /api/bookings` and to `GET /api/slots`. Three settings added to §5:
  `rate_limit_bookings_per_hour` (20), `rate_limit_slots_per_hour` (300), and
  `trusted_proxy_depth` (0).
  Three decisions worth recording:
  - **The counter counts attempts, not successes.** A limiter that only charges for
    accepted requests is free to bypass — a caller just spams requests that were going
    to fail. Pinned by `test_the_budget_is_spent_by_failed_attempts_too`.
  - **Client identity is the peer address, or the Nth-from-the-right entry of
    `X-Forwarded-For` when `trusted_proxy_depth > 0`.** Counting from the *right*
    matters: the leftmost entry is caller-supplied, so a naive implementation lets an
    attacker mint a fresh identity per request by rotating the header, and the limit
    stops limiting anything.
  - **`POST /api/payments/wechat/notify` is deliberately never throttled.** WeChat's
    retry schedule is part of the payment protocol; a 429 on a retry would strand a
    paid booking. That endpoint is protected by signature verification and by
    `PaymentEvent.transaction_id` uniqueness instead.
- **`create_booking` step 3a — a live booking blocks every *overlapping* slot, not
  just the identical `slot_start`.** A fourth double-booking hole, found while writing
  the rate-limit tests. `slot_step_minutes` (15) is half `duration_minutes` (30), so the
  grid offers starts that overlap each other; `generate_slots` correctly hides the
  neighbour of a live booking, but the booking path did not refuse it, and the partial
  unique index — keyed on `(event_type_id, slot_start)` — cannot see a 15-minute shift.
  A crafted POST, or simply a double-submit 15 minutes apart, produced two overlapping
  1-1 calls and two overlapping calendar holds. Same *class* of defect as the
  grid/path disagreements in the second pass: the grid is advice, the booking path is
  what has to refuse. It raises `SlotTaken` (409, not 422 — the request is well-formed
  and on-grid, it *conflicts*), which is what makes the UI refresh the grid.
  Read-then-insert, so two concurrent requests could still both pass; a Postgres
  exclusion constraint is the real answer when the schema stabilises, and SQLite cannot
  express it at all.
- **`tests/test_rate_limit.py` added**, and `tests/conftest.py` gained
  `_reset_rate_limiters` — the limiters are process-global and keyed by client IP, so
  without a per-test reset the ninth test to post a booking inherits the first test's
  spending. Same isolation guarantee as `_fresh_schema`, for a different kind of state.
- **Test-environment note:** if `tmp_path` fails at fixture setup with a
  `PermissionError`, the sandbox is refusing to create `.pytest_tmp/` inside the repo.
  Run `pytest --basetemp=/tmp/booking-pytest`. Nothing else about the suite changes.

### Integrator addenda, fourth pass (2026-09-21, after the first Railway deploy failed)

- **`Dockerfile.booking` renamed to `Dockerfile`; `railway.booking.json` renamed to
  `railway.json`.** The first deploy failed with *"Railpack failed to prepare the build"*,
  and the service manifest showed `builder: RAILPACK`, `dockerfilePath: null` — the image
  was never built from our Dockerfile. Two independent causes, and the second one is the
  general lesson:
  1. **Railway auto-detects a file named exactly `Dockerfile`.** Any other name is not
     found, with no warning, and Railpack silently takes over. The plain name is what makes
     the booking image build with zero per-service configuration. This matters more than
     tidiness: config-as-code is deprecated, so a build that depends on `dockerfilePath`
     from `railway.json` is a build that stops working at the cutoff. The plain filename is
     the non-deprecated path.
  2. **Railway reads `railway.json` / `railway.toml` at the repository root — nothing
     else.** `railway.booking.json` was therefore invisible: the healthcheck path, the watch
     patterns and the builder choice in it were all silently inactive, and the deploy ran on
     Railway's defaults. `railway.relay.json` keeps its name only because the relay is a
     second service that must select it explicitly.
- **`x-envVars` removed from the Railway config.** Railway's config has no environment-variable
  section; the key was documentation-only, and a non-schema key in a file Railway parses is a
  risk with no upside. The README's env-var tables are the single source of truth for names.
- **Known hazard, documented in the README:** a root `railway.json` applies to *every* service
  deploying from this repository. Once the relay service exists, it must set its config-as-code
  path to `/railway.relay.json`; otherwise it finds the root `Dockerfile`, builds the booking
  image, and serves the booking app on the relay's domain — a failure that looks like success.

### Integrator addenda, fifth pass (2026-09-21, the slot grid offered the same half hour twice)

- **The grid step is now clamped to the slot duration** — `effective_step_minutes(event_type)`
  in `app/services/availability.py`, used by `_rule_grid`, `is_slot_on_grid` and the busy-time
  pad. Reported from a screenshot of the live booking page: a 30-minute consultation listed
  `09:00 09:15 09:30 09:45 …` — 35 buttons for a day that holds 18 appointments.
  - **The root cause is an asymmetry, not a wrong value.** `slot_step_minutes` is a single
    global knob while `duration_minutes` is per event type, so nothing prevented a step finer
    than the duration. 09:00–09:30 and 09:15–09:45 then become two buttons describing the same
    half hour, and only one of them can ever be sold.
  - **The real damage was that the two sides disagreed.** The grid offered 09:15;
    `create_booking` refused it (409) because the *third* pass added an overlap guard. A visitor
    therefore tapped a time the page had just shown as free and was told it was taken. Clamping
    fixes the grid, and because `is_slot_on_grid` uses the same clamped step the refusal is now
    off-grid (422) — grid and write path finally agree. Same class as the second- and third-pass
    defects: *the grid is advice, the booking path is what has to refuse* — except here the
    advice itself was wrong.
  - **Clamping is per event type; validation could not be.** A config check is impossible — the
    duration lives on a database row, not in settings — and no single global default is right
    for both a 15-minute and a 60-minute offering. `max(step, duration)` is a no-op for every
    coherent configuration (hourly starts for a 30-minute call still work) and removes only the
    incoherent ones.
  - **A second mechanism is still needed for off-grid rule alignment.** `is_slot_on_grid` judges
    each rule against *that rule's own* start, so two rules — one on the hour, one on the
    quarter — can still yield 09:00 and 09:15. `generate_slots` therefore drops any candidate
    that overlaps a slot it has already kept, making the returned grid pairwise non-overlapping
    by construction.
  - **`create_booking` step 3a stays.** With the grid clamped a neighbour can no longer be
    offered, so the guard's remaining reachable case is a duplicate POST of a slot that was free
    when the page loaded — exactly the race that must return 409 so the UI refreshes. It also
    keeps the data correct if the step is ever un-clamped. Its comment now says so.
- **`tests/conftest.py` deliberately keeps `SLOT_STEP_MINUTES=15`** — *finer* than the 30-minute
  fixture, i.e. the adversarial configuration. Every availability test therefore runs against
  the case the clamp exists for, and a genuinely fine grid is exercised by using a 15-minute
  *service* rather than a 15-minute step on a 30-minute one.
- **New tests:** `test_the_step_is_clamped_to_the_duration`,
  `test_a_coherent_fine_grid_is_left_alone`, `test_the_grid_never_offers_two_overlapping_starts`
  and `test_is_slot_on_grid_uses_the_clamped_step`, plus three on the HTTP surface —
  `test_a_start_the_grid_never_offered_is_refused`,
  `test_a_duplicate_post_of_a_live_slot_is_a_conflict` and `test_the_grid_offers_no_overlapping_pair`.
  The last three replace the third pass's `test_a_slot_overlapping_a_live_booking_is_refused`,
  whose premise (that 14:15 is on the grid) no longer holds.
- **Deployed configuration:** `SLOT_STEP_MINUTES=30` was also set on the Railway service so the
  stored value matches the grid the clamp produces. With the clamp in place the variable is belt
  and braces; it is set because whoever reads the Railway dashboard should not have to know the
  clamp exists. Verified live: `GET /api/slots` went from 35 starts to 18 — 09:00 → 17:30 in
  30-minute steps.
- **Repo gap noticed while doing this:** there is no `.github/` directory, so nothing runs the
  suite on push. Every "tests pass" claim in these addenda is a local run.


### Integrator addenda, sixth pass (2026-09-29, ticketed conferences)

The owner asked for a second product on the same service: *"conference calls, where up to
100 members can join — so it is a ticketed event. Maximum 100, and they pay directly with
Wechat — cost per seat is 50 Yuan — as soon as they pay, they get confirmation email saying
their seat is reserved and get a link to the conference call with date and time. We will be
doing multiple conference calls per week on different topics and each call will be 1hr.
Will arrange time and date later as we go, but I should be able to do it from our backend.
It should also show how many people have booked so far / how many seats available."*

Decisions taken with the owner before building: the meeting is hosted on **VooV Meeting**
(`voovmeeting.com`) and the link is **pasted by the owner**, not created by an API call;
**one seat per purchase**; **v1's no-refund policy carries over** (a manual refund flag);
and **no 1-1 during a conference**.

- **Conferences are new tables, not a `capacity` column on `EventType`.** The reason is
  structural and worth recording: `Booking.uq_active_slot` is
  `UNIQUE(event_type_id, slot_start)` over live rows, which permits exactly one live booking
  per start. A 100-seat event cannot be represented in `bookings` at all. `ConferenceEvent`
  and `ConferenceSeat` therefore carry their own guard, `uq_live_seat`, the same shape
  generalised from one occupant per slot to `capacity` occupants.
- **Capacity is a database invariant.** `reserve_seat` allocates the lowest free seat number
  and retries on `IntegrityError` inside a `begin_nested()` savepoint, so losing the race for
  a number costs only that INSERT — not the caller's transaction, sweep included.
- **The partial index keys on `status`, not `expires_at`.** A hold that has run out is
  logically dead (§7's lazy expiry) but still physically `pending_payment`, so it still
  occupies its number. The transactional pre-insert sweep is what frees it, and correctness
  must not depend on the sweeper.
- **Display and enforcement share one primitive.** `seat_counts` and `reserve_seat` both read
  `_taken_seat_numbers`, so "剩余 N 席" on the page and the `SoldOut` in the write path cannot
  drift. This is the defect class of the fifth pass, applied pre-emptively.
- **The join link is a secret.** It is delivered only in the ticket email, after a verified
  payment, and appears on no page or endpoint reachable before one — the gate is
  `status == "paid"` in the routers, not in the template, because a template is the kind of
  file someone edits without reading the rule. `GET /api/conferences` does not carry the
  field at all. The ticket page reloads on transition to paid rather than patching the DOM,
  so client JavaScript never holds the URL.
- **`is_on_sale()` requires a non-empty `join_url`.** The meeting link *is* the product:
  selling a seat to an event whose link does not exist yet means a customer pays and receives
  a ticket that cannot tell them where to go — a refund and an apology, not something fixable
  afterwards. The public list distinguishes "即将开放" from "已停止报名" via a separate
  `join_ready` flag, because those are different messages to a visitor.
- **The notify handler now routes across two tables.** It previously looked an
  `out_trade_no` up in `bookings` only, so a `TK…` ticket payment was "unknown order" and the
  money went nowhere. References are self-describing by prefix (`BK…` / `TK…`), so the common
  path is one indexed lookup; the fallback probe exists because a prefix is a convention, not
  a constraint, and dropping a real payment is the worst available outcome. The replay guard,
  amount check and audit row are shared — only the settle call and the post-payment task
  branch — so neither kind of order can end up without them.
- **The 1-1 conflict is enforced in both directions**, per the owner's decision.
  `conference_intervals` feeds `generate_slots` (hides the slots) and `create_booking`
  (refuses with `422`), mirroring how calendar busy time is handled. `active=False` alone does
  **not** stop the blocking: an event off sale but not cancelled is still a meeting the owner
  is holding, and blocking a slot that turns out to be free is visible and fixable whereas a
  1-1 booked during a conference is not. Scheduling *over* an existing live 1-1 is **reported**
  (`clashing_bookings`) rather than refused, because the conference is the commitment that
  cannot move once seats are sold.
- **`finalize_ticket` has no calendar step**, unlike `finalize_paid_booking`. The conference
  is one event the owner created in their own meeting platform, not a hold we placed, so
  there is nothing to confirm and nothing to release. It is idempotent on
  `ConferenceSeat.ticket_sent_at` — a replayed notify that emailed twice would hand out a
  second copy of a secret — and it logs an error when a *paid* seat's event has no
  `join_url`, because that is a work item for a human.
- **New tests:** 27 in `tests/test_conferences.py`, covering the lowest-free-seat allocation,
  the seat after the last one, the `seat_counts` / `reserve_seat` parity contract, an expired
  hold freeing its number, the price snapshot, an event with no link being unsellable, the
  notify route for a `TK…` order, replay, amount mismatch, cancellation, the join link being
  absent before payment and present after (on both the API and the page), the grid and the
  write path both refusing a conference overlap, the clash report, admin auth, the
  capacity-decrease guard, and the unknown-field guard. Suite total: **220 passing**.
- **Consequence for the seed:** `app/seed.py` now also creates a sample conference at 20:00
  Shanghai a week out, **with no `join_url`** — so it is visible as "即将开放" but cannot take
  money. A seed row that a real visitor could buy would be a trap, and a placeholder URL would
  be worse: it would be emailed.
