"""Shared serialisation rules.

The web app's TypeScript types are the contract: dates are `YYYY-MM-DD`,
clock times are `HH:mm`, and stamps that carry both are `YYYY-MM-DD HH:mm`.
Times are rendered in the clinic's own timezone, because "10:05" on a waiting
room screen means ten past ten *there*.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict

DEFAULT_TZ = "Europe/London"


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _local(value: datetime, tz: str | None) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone(tz))


def iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def clock(value: datetime | None, tz: str | None = None) -> str | None:
    """`HH:mm` — how the queue and the display screen show a moment."""
    return _local(value, tz).strftime("%H:%M") if value else None


def stamp(value: datetime | None, tz: str | None = None) -> str | None:
    """`YYYY-MM-DD HH:mm`."""
    return _local(value, tz).strftime("%Y-%m-%d %H:%M") if value else None


def day(value: datetime | None, tz: str | None = None) -> str | None:
    return _local(value, tz).strftime("%Y-%m-%d") if value else None


def money(value: Decimal | float | int | None) -> float:
    return float(value or 0)


class ApiModel(BaseModel):
    """Response models: no extra fields, and enums render as their values."""

    model_config = ConfigDict(from_attributes=True, use_enum_values=True, extra="forbid")


def _valid_id(value: str) -> str:
    """Ids arrive as strings and are compared as strings, but they must still be
    well formed: an empty or malformed one used to reach PostgreSQL as a uuid
    cast and come back as a 500 rather than a 422.
    """
    try:
        uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("not a valid id") from None
    return value


IdStr = Annotated[str, AfterValidator(_valid_id)]


class InputModel(BaseModel):
    """Request bodies reject unknown keys.

    That is a security property, not tidiness: a client cannot smuggle
    `tenant_id`, `status`, or `dispensed_qty` into a create call and have it
    quietly persisted.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value
