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
  Dockerfile.booking           owner: M7
  Dockerfile.relay             owner: M7
  railway.booking.json         owner: M7
  railway.relay.json           owner: M7

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

def generate_slots(
    db: Session,
    event_type: EventType,
    local_date: date,
    *,
    calendar: CalendarGateway,
    now: datetime | None = None,
) -> list[Slot]
    """The bookable grid for one local date.

    On the `slot_step_minutes` grid; inside an AvailabilityRule window for that
    weekday; inside [now + min_notice_minutes, now + max_days_ahead];
    not overlapping calendar busy time; not overlapping a live booking.
    Ascending by start.
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
def release_booking_hold(booking_id: str) -> None
```

`app/routers/payments.py` (M2) dispatches `finalize_paid_booking` after responding; it
imports the function **inside** the handler so the notify endpoint still works if
`app/tasks.py` is missing.

## 14.4 Cross-module imports — the allowed set

| From | May import |
|---|---|
| M1 (`services/booking.py`, `routers/booking.py`, `routers/admin.py`) | `app.services.availability`, `app.models`, `app.schemas`, `app.deps` (routers only) |
| M2 (`routers/payments.py`, `adapters/payments_*`) | `app.services.booking.mark_paid`, `app.models`, `app.schemas` |
| M3 (`relay/**`) | nothing from `app.*` — the relay is a standalone deployable |
| M4 (`adapters/calendar_*`) | `app.ports.calendar` only |
| M5 (`routers/pages.py`, `templates/`, `static/`) | `app.services.booking.get_booking`, `app.services.availability` |
| M6 (`tasks.py`, `services/sweeper.py`, `adapters/email_*`) | `app.services.booking`, `app.adapters.*` via `app.deps` |

Anything not in this table needs an integrator decision. Import lazily inside the
function body where a sibling might still be unwritten.

# 15. Addenda to the frozen contract (integrator)

- **`Booking.code_url`** added to §6 — persists the WeChat Native `code_url` so the
  status page can re-render the QR offline. Additive only.
- **`email-validator`** added to `pyproject.toml` dependencies — `EmailStr` in
  `app/schemas.py` requires it and it was missing.
- **`pythonpath = ["."]`** added to the pytest config so `import app` / `import tests`
  resolve without an editable install.
- **`tests/__init__.py`** added so `from tests.fakes import ...` resolves.
