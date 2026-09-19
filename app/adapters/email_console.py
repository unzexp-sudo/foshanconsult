"""Console email adapter — the dev default (contract §8, ``EMAIL_BACKEND=console``).

Constructible with no arguments because ``app.deps.get_email_sender`` builds it that
way.  It prints instead of sending so local dev and tests never touch a socket.
"""

from __future__ import annotations

import sys

__all__ = ["ConsoleEmailSender"]

_RULE = "=" * 72


class ConsoleEmailSender:
    """Print the message to stdout with a clear delimiter."""

    def send(self, *, to: str, subject: str, body: str) -> None:
        stream = sys.stdout
        print(_RULE, file=stream)
        print(f"[email] to:      {to}", file=stream)
        print(f"[email] subject: {subject}", file=stream)
        print("-" * 72, file=stream)
        print(body, file=stream)
        print(_RULE, file=stream)
        stream.flush()
