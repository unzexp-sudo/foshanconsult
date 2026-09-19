"""Periodic hygiene sweeper — contract §7.3.

Pure hygiene: it releases the calendar hold and closes the WeChat order for rows the
DB has already flipped to ``expired``.  Nothing here is required for correctness —
§7's lazy expiry and the transactional pre-insert sweep free slots on their own — so
a dead sweeper costs only stale calendar holds, never a stuck slot.

Run it as its own process::

    python -m app.services.sweeper

Importing this module starts nothing (the loop lives behind ``__main__``), so the web
app can import it — or not — without side effects.
"""

from __future__ import annotations

import logging
import time

from app.config import settings

logger = logging.getLogger("sweeper")

__all__ = ["run_forever", "sweep_once"]


def sweep_once() -> int:
    """One unit of work: release every expired hold that still needs it.

    Returns the number of bookings processed.  Imported lazily so a test (or an
    operator) can swap ``app.tasks.release_expired_holds`` and so importing this
    module never pulls in the task layer.
    """
    from app.tasks import release_expired_holds

    processed = release_expired_holds()
    logger.info("sweep complete: released %d expired hold(s)", processed)
    return processed


def run_forever(interval_seconds: int | None = None) -> None:
    """Call :func:`sweep_once` every ``interval_seconds``, forever.

    An exception is logged and the loop continues: a sweeper that dies on the first
    transient error is worse than no sweeper at all.
    """
    interval = (
        interval_seconds
        if interval_seconds is not None
        else settings.sweeper_interval_seconds
    )
    logger.info("sweeper starting: interval=%ss", interval)
    while True:
        try:
            sweep_once()
        except Exception:  # noqa: BLE001 - keep going, always
            logger.exception("sweep failed; continuing")
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
