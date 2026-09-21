"""Database engine and session plumbing.

Synchronous SQLAlchemy 2.0 on purpose: the web app and the Celery worker share one
engine and one sessionmaker (contract §3).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def utcnow() -> datetime:
    """Timezone-aware UTC now.  Never use datetime.utcnow()."""
    return datetime.now(UTC)


#: Schemes that name Postgres without naming a driver.
_DRIVERLESS_POSTGRES = ("postgres://", "postgresql://")


def normalise_database_url(url: str) -> str:
    """Point a bare Postgres URL at psycopg v3.

    Railway, Heroku and most managed providers hand out ``postgresql://…`` (or the
    legacy ``postgres://…``).  SQLAlchemy reads that as "use psycopg2" — which this
    project does not install; it installs ``psycopg`` v3 — so the app dies with
    ``ModuleNotFoundError: No module named 'psycopg2'`` on a connection string that is
    otherwise perfectly correct.  Rewriting the scheme here means ``DATABASE_URL`` can
    be set straight from the provider's own reference variable instead of being
    hand-edited, which is one less silent deployment failure.

    An *explicit* driver (``postgresql+psycopg2://``, ``postgresql+asyncpg://``) is left
    alone: that is a deliberate choice, and quietly overriding it would be worse than
    failing loudly.
    """
    lowered = url.lower()
    for scheme in _DRIVERLESS_POSTGRES:
        if lowered.startswith(scheme):
            return f"postgresql+psycopg://{url[len(scheme) :]}"
    return url


def _build_engine(url: str):
    url = normalise_database_url(url)
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = _build_engine(settings.database_url)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver level
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For Celery tasks and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create tables.  Fine for v1; swap for Alembic when the schema stabilises."""
    from app import models  # noqa: F401  (register mappers)
    from app.models import Base

    Base.metadata.create_all(engine)
