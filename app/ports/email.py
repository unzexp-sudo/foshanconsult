"""Email port.  Frozen by docs/MODULE_CONTRACT.md §8."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...
