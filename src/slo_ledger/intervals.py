"""Half-open time intervals, sets of them, and calendar months in a time zone.

An :class:`Interval` is ``[start, end)``: it contains ``start`` and not ``end``, so
two intervals that touch (one ends where the next starts) share no instant and
their lengths add up. An :class:`IntervalSet` keeps its intervals sorted,
disjoint and non-touching, which makes equality structural: two sets that cover
the same instants compare equal however they were built.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from slo_ledger._time import to_utc


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    """The half-open span ``[start, end)``, stored in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = to_utc(self.start, "interval start")
        end = to_utc(self.end, "interval end")
        if end < start:
            raise ValueError(f"interval ends before it starts: {start} > {end}")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def empty(self) -> bool:
        return self.start == self.end

    def __contains__(self, moment: object) -> bool:
        if not isinstance(moment, datetime):
            return False
        instant = to_utc(moment)
        return self.start <= instant < self.end

    def overlap(self, other: Interval) -> timedelta:
        """Length of the intersection of two intervals (zero if they are disjoint)."""
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return max(end - start, timedelta(0))


class IntervalSet:
    """A finite union of half-open intervals in normal form."""

    __slots__ = ("_items",)

    _items: tuple[Interval, ...]

    def __init__(self, intervals: Iterable[Interval] = ()) -> None:
        merged: list[Interval] = []
        for item in sorted(i for i in intervals if not i.empty):
            if merged and item.start <= merged[-1].end:
                last = merged[-1]
                if item.end > last.end:
                    merged[-1] = Interval(last.start, item.end)
            else:
                merged.append(item)
        self._items = tuple(merged)

    @classmethod
    def of(cls, *pairs: tuple[datetime, datetime]) -> IntervalSet:
        """Build a set from ``(start, end)`` pairs."""
        return cls(Interval(start, end) for start, end in pairs)

    # Container protocol -------------------------------------------------

    def __iter__(self) -> Iterator[Interval]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IntervalSet):
            return NotImplemented
        return self._items == other._items

    def __hash__(self) -> int:
        return hash(self._items)

    def __repr__(self) -> str:
        inner = ", ".join(f"[{i.start.isoformat()}, {i.end.isoformat()})" for i in self._items)
        return f"IntervalSet({inner})"

    def __contains__(self, moment: object) -> bool:
        return any(moment in item for item in self._items)

    # Algebra ------------------------------------------------------------

    def union(self, other: IntervalSet) -> IntervalSet:
        return IntervalSet((*self._items, *other._items))

    def intersection(self, other: IntervalSet) -> IntervalSet:
        result: list[Interval] = []
        a, b = self._items, other._items
        i = j = 0
        while i < len(a) and j < len(b):
            start = max(a[i].start, b[j].start)
            end = min(a[i].end, b[j].end)
            if start < end:
                result.append(Interval(start, end))
            if a[i].end <= b[j].end:
                i += 1
            else:
                j += 1
        return IntervalSet(result)

    def complement(self, window: Interval) -> IntervalSet:
        """The instants of ``window`` that are not in this set."""
        result: list[Interval] = []
        cursor = window.start
        for item in self.clip(window):
            if item.start > cursor:
                result.append(Interval(cursor, item.start))
            cursor = item.end
        if cursor < window.end:
            result.append(Interval(cursor, window.end))
        return IntervalSet(result)

    def difference(self, other: IntervalSet) -> IntervalSet:
        if not self._items:
            return self
        hull = Interval(self._items[0].start, self._items[-1].end)
        return self.intersection(other.complement(hull))

    __or__ = union
    __and__ = intersection
    __sub__ = difference

    def clip(self, window: Interval) -> IntervalSet:
        """The part of this set inside ``window``."""
        return self.intersection(IntervalSet((window,)))

    # Measures -----------------------------------------------------------

    def total(self) -> timedelta:
        return sum((item.duration for item in self._items), timedelta(0))

    def longest(self) -> Interval | None:
        """The longest interval; the earliest one wins a tie."""
        best: Interval | None = None
        for item in self._items:
            if best is None or item.duration > best.duration:
                best = item
        return best


def local_day_start(day: date, zone: ZoneInfo) -> datetime:
    """The first instant of ``day`` on the clocks of ``zone``, in UTC.

    That is local midnight, except where the clocks skip midnight (a transition at
    00:00 that moves them forward): there the day starts at the transition, the
    first instant whose local date is ``day``.
    """
    naive = datetime(day.year, day.month, day.day)
    candidate = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if candidate.astimezone(zone).replace(tzinfo=None) == naive:
        return candidate
    # Midnight is inside a gap. With fold=0 it maps through the offset in force
    # before the gap (at or after the transition), with fold=1 through the one
    # after it (before the transition): search between the two for the first
    # instant whose local date is ``day``.
    late = int(candidate.timestamp())
    early = int(naive.replace(tzinfo=zone, fold=1).astimezone(UTC).timestamp())
    if early > late:
        early, late = late, early
    while early < late:
        middle = (early + late) // 2
        if datetime.fromtimestamp(middle, zone).date() >= day:
            late = middle
        else:
            early = middle + 1
    return datetime.fromtimestamp(late, UTC)


def month_window(year: int, month: int, zone: str | ZoneInfo) -> Interval:
    """The calendar month ``year-month`` on the clocks of ``zone``, as a UTC interval."""
    tz = zone if isinstance(zone, ZoneInfo) else ZoneInfo(zone)
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1 to 12, got {month}")
    first = date(year, month, 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return Interval(local_day_start(first, tz), local_day_start(following, tz))


def parse_month(text: str) -> tuple[int, int]:
    """Parse ``"YYYY-MM"``."""
    try:
        year_text, month_text = text.strip().split("-")
        year, month = int(year_text), int(month_text)
    except ValueError as exc:
        raise ValueError(f"not a month in the form YYYY-MM: {text!r}") from exc
    if not 1 <= month <= 12:
        raise ValueError(f"not a month in the form YYYY-MM: {text!r}")
    return year, month
