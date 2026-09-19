"""Test bootstrap and shared fixtures.

**Owned by the integrator.  OFF-LIMITS to module agents** (contract §12).

Two things matter here:

1. Environment variables are set at *module import time*, before any ``app.*``
   module is imported.  ``app/config.py`` reads settings at import and
   ``app/db.py`` builds the engine from them, so setting env vars in a fixture
   would be too late.
2. ``client`` wires the fake gateways through ``app.dependency_overrides``, which
   only works because routers resolve gateways with ``Depends(deps.get_*)``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Environment — MUST happen before the first `import app.*`
# ---------------------------------------------------------------------------
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="booking-tests-"))
_DB_PATH = _TMP_ROOT / "test.db"

os.environ.update(
    {
        "APP_ENV": "dev",
        "DATABASE_URL": f"sqlite:///{_DB_PATH}",
        "SECRET_KEY": "test-secret-key",
        "PUBLIC_BASE_URL": "http://testserver",
        "DEFAULT_TIMEZONE": "Asia/Shanghai",
        "CALENDAR_GATEWAY": "fake",
        "PAYMENT_GATEWAY": "fake",
        "EMAIL_BACKEND": "console",
        "HOLD_MINUTES": "10",
        "SLOT_STEP_MINUTES": "15",
        "SWEEPER_INTERVAL_SECONDS": "60",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from freezegun import freeze_time  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.deps import (  # noqa: E402
    get_calendar_gateway,
    get_email_sender,
    get_payment_gateway,
)
from app.main import app as fastapi_app  # noqa: E402
from app.models import Base, EventType  # noqa: E402
from app.seed import seed  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeCalendarGateway,
    FakePaymentGateway,
    RecordingEmailSender,
    make_wechat_keys,
    make_wechat_notify,
    wechat_settings,
)

__all__ = [
    "FakeCalendarGateway",
    "FakePaymentGateway",
    "RecordingEmailSender",
    "make_wechat_keys",
    "make_wechat_notify",
    "wechat_settings",
]

# Shanghai is UTC+8 year round (no DST), which keeps test arithmetic readable.
SHANGHAI = "Asia/Shanghai"


def utc(*args: int) -> datetime:
    """``utc(2026, 9, 21, 1, 0)`` → aware UTC datetime.  Never build naive ones."""
    return datetime(*args, tzinfo=UTC)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. Time
# ---------------------------------------------------------------------------


@contextmanager
def _freeze_now(moment: datetime) -> Iterator[datetime]:
    """Freeze the clock.  Prefer passing ``now=`` explicitly to services."""
    with freeze_time(moment):
        yield moment


@pytest.fixture
def freeze_now():
    """``with freeze_now(utc(2026, 9, 21, 1, 0)): ...``

    Services take an explicit ``now`` parameter; use this only where the code
    under test has no seam (e.g. ``Booking.is_expired()`` with no argument).
    """
    return _freeze_now


# ---------------------------------------------------------------------------
# 3. Database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_root() -> Iterator[None]:
    yield
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fresh_schema() -> Iterator[None]:
    """Drop and recreate every table before each test — full isolation."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def event_type(db_session: Session) -> EventType:
    """The seeded ``consult-30``: 30 min, 50000 分, Mon–Fri 09:00–18:00 Shanghai."""
    seeded = seed(db_session)
    db_session.commit()
    db_session.refresh(seeded)
    return seeded


# ---------------------------------------------------------------------------
# 4. Gateways + HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_calendar() -> FakeCalendarGateway:
    return FakeCalendarGateway()


@pytest.fixture
def fake_payments() -> FakePaymentGateway:
    return FakePaymentGateway()


@pytest.fixture
def recording_email() -> RecordingEmailSender:
    return RecordingEmailSender()


@pytest.fixture
def client(
    fake_calendar: FakeCalendarGateway,
    fake_payments: FakePaymentGateway,
    recording_email: RecordingEmailSender,
) -> Iterator[TestClient]:
    fastapi_app.dependency_overrides[get_calendar_gateway] = lambda: fake_calendar
    fastapi_app.dependency_overrides[get_payment_gateway] = lambda: fake_payments
    fastapi_app.dependency_overrides[get_email_sender] = lambda: recording_email
    with TestClient(fastapi_app) as test_client:
        yield test_client
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def wechat_keys(tmp_path: Path):
    """Throwaway RSA keypairs for the real WeChat adapter."""
    return make_wechat_keys(tmp_path)
