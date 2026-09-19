"""FastAPI application factory.

Routers are registered inside ``try/except ImportError`` so the app boots — and
``/healthz`` answers — even while a sibling module has not been written yet
(contract §4).  That property is what lets the test suite run during a parallel
build; do not "tidy" it away.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db

logger = logging.getLogger("booking")

STATIC_DIR = Path(__file__).parent / "static"

# (module_path, attribute, label)
_ROUTERS: tuple[tuple[str, str, str], ...] = (
    ("app.routers.pages", "router", "M5 pages"),
    ("app.routers.booking", "router", "M1 booking"),
    ("app.routers.payments", "router", "M2 payments"),
    ("app.routers.admin", "router", "M1 admin"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # v1 uses create_all(); swap for Alembic when the schema stabilises.
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="zhituoyuan booking service",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.is_dev else None,
        redoc_url=None,
    )

    for module_path, attr, label in _ROUTERS:
        try:
            module = __import__(module_path, fromlist=[attr])
            app.include_router(getattr(module, attr))
        except ImportError as exc:  # module not written yet
            logger.warning("router %s (%s) unavailable: %s", module_path, label, exc)
        except Exception as exc:  # noqa: BLE001 - a broken sibling must not stop boot
            logger.error("router %s (%s) failed to register: %r", module_path, label, exc)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


app = create_app()
