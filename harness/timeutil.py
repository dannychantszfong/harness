"""UTC time helpers.

Harness state files written before 1.0 carry naive timestamps produced by
`datetime.utcnow()` (no offset). They are UTC by construction, so the
tolerant readers here attach `timezone.utc` instead of rejecting them; new
files are written tz-aware.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC; convert aware ones to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_utc(text: str) -> datetime:
    """`datetime.fromisoformat` that accepts both naive and offset strings."""
    return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


# Pydantic field type: parses naive or aware input, always yields aware UTC.
UTCDateTime = Annotated[datetime, AfterValidator(ensure_utc)]
