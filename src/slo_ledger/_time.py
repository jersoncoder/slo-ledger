"""Timestamps, durations and their text forms.

Every instant inside the library is an aware ``datetime`` in UTC. Python subtracts
two aware datetimes that share a ``tzinfo`` object by their wall-clock fields, so
``datetime(2026, 3, 9, tzinfo=ny) - datetime(2026, 3, 8, tzinfo=ny)`` is 24 hours
although only 23 elapsed across the daylight-saving change. Converting to UTC on
the way in makes every subtraction an elapsed time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_MICROSECOND = timedelta(microseconds=1)
_DURATION = re.compile(r"^\s*(\d+)\s*(ms|s|m|h|d)\s*$")
_UNITS = {
    "ms": timedelta(milliseconds=1),
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


def to_utc(moment: datetime, what: str = "timestamp") -> datetime:
    """Return ``moment`` in UTC; a naive datetime is refused rather than guessed."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} {moment.isoformat()} has no UTC offset")
    return moment.astimezone(UTC)


def parse_timestamp(text: str) -> datetime:
    """Parse an ISO 8601 timestamp that carries an offset (``Z`` or ``+hh:mm``)."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"not an ISO 8601 timestamp: {text!r}") from exc
    return to_utc(moment, f"timestamp {text!r}")


def format_timestamp(moment: datetime) -> str:
    """Format an instant as ISO 8601 in UTC with a ``Z`` suffix."""
    text = to_utc(moment).isoformat()
    return text.removesuffix("+00:00") + "Z"


def parse_duration(text: str) -> timedelta:
    """Parse a duration such as ``"30s"``, ``"5m"``, ``"6h"`` or ``"3d"``."""
    match = _DURATION.match(text)
    if match is None:
        raise ValueError(f"not a duration (expected e.g. '5m', '6h', '3d'): {text!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def microseconds(span: timedelta) -> int:
    """Exact length of ``span`` in integer microseconds."""
    return span // _MICROSECOND


def minutes(span: timedelta) -> float:
    """Length of ``span`` in minutes, for display."""
    return span.total_seconds() / 60.0
