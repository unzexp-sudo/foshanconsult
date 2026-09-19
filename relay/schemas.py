"""Wire schemas for the relay HTTP contract (contract §9).

Datetimes cross the wire as ISO-8601 strings with an explicit UTC offset.  The
relay is a thin proxy, so they are kept as strings and passed straight through to
Google rather than parsed and re-serialised — that also keeps the request bytes
identical to the bytes that were signed.
"""

from __future__ import annotations

from pydantic import BaseModel


class FreeBusyRequest(BaseModel):
    time_min: str
    time_max: str


class BusyInterval(BaseModel):
    start: str
    end: str


class FreeBusyResponse(BaseModel):
    busy: list[BusyInterval]


class CreateEventRequest(BaseModel):
    summary: str
    description: str
    start: str
    end: str
    reference: str
    transparent: bool


class CreateEventResponse(BaseModel):
    event_id: str
    html_link: str | None = None


class PatchEventRequest(BaseModel):
    summary: str
    description: str


class PatchEventResponse(BaseModel):
    event_id: str


class DeleteEventResponse(BaseModel):
    deleted: bool = True
