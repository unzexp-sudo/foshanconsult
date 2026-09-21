# syntax=docker/dockerfile:1
#
# Booking API — FastAPI + synchronous SQLAlchemy 2.0.
#
# Deployed on Railway now and moved to the ICP-filed mainland host at go-live
# (BUILD_PLAN §2 and §9 P8).  It owns bookings, slots, WeChat Pay and the notify
# endpoint, and it never holds a Google credential in production: calendar work
# goes through the relay service (see Dockerfile.relay).
#
# WHY THIS FILE IS NAMED `Dockerfile` AND NOT `Dockerfile.booking`:
# Railway auto-detects a file named exactly `Dockerfile` in the service's root
# directory and builds with it.  A differently-named Dockerfile is *not* found, so
# Railway silently falls back to Railpack — which failed this build with
# "Railpack failed to prepare the build".  The plain name is what makes the booking
# service build with no per-service configuration at all.  Dockerfile.relay keeps
# its name because the relay is a second service and must set its Dockerfile path
# explicitly; see the README's deployment section.
#
# Build context is the repository root:
#   docker build -t booking-service .
#   docker run --rm -p 8000:8000 --env-file .env booking-service
#
# The periodic sweeper is this same image with a different command:
#   python -m app.services.sweeper

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies are read out of pyproject.toml — the single source of truth — so
# the image cannot silently drift from the versions the test suite runs against.
COPY pyproject.toml ./
RUN python -c "\
import subprocess, sys, tomllib; \
deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; \
subprocess.check_call([sys.executable, '-m', 'pip', 'install', *deps])"

# Only the booking app.  relay/ is a separate deployable and app/ never imports it.
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

# Railway injects PORT; 8000 is the local default.  Shell form so ${PORT} expands.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
