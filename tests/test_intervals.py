"""Interval algebra against a brute-force model, and calendar months across DST."""

from __future__ import annotations

import calendar
import itertools
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

import pytest
from hypothesis import given
from hypothesis import strategies as st

from slo_ledger.intervals import Interval, IntervalSet, local_day_start, month_window, parse_month

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
SPAN = 60  # endpoints are whole seconds in [0, SPAN]


def at(second: int) -> datetime:
    return EPOCH + timedelta(seconds=second)


@st.composite
def interval_sets(draw: st.DrawFn) -> IntervalSet:
    pairs = draw(st.lists(st.tuples(st.integers(0, SPAN), st.integers(0, SPAN)), max_size=6))
    return IntervalSet(Interval(at(min(a, b)), at(max(a, b))) for a, b in pairs)


def members(s: IntervalSet) -> frozenset[int]:
    """The unit cells [k, k + 1) a set covers; exact because endpoints are whole seconds."""
    return frozenset(k for k in range(SPAN) if at(k) in s)


def window() -> Interval:
    return Interval(at(0), at(SPAN))


@given(interval_sets(), interval_sets())
def test_operations_match_the_brute_force_model(a: IntervalSet, b: IntervalSet) -> None:
    assert members(a | b) == members(a) | members(b)
    assert members(a & b) == members(a) & members(b)
    assert members(a - b) == members(a) - members(b)
    assert members(a.complement(window())) == frozenset(range(SPAN)) - members(a)


@given(interval_sets())
def test_normal_form_is_sorted_disjoint_and_not_touching(a: IntervalSet) -> None:
    items = list(a)
    assert all(not i.empty for i in items)
    for left, right in itertools.pairwise(items):
        assert left.end < right.start
    assert IntervalSet(items) == a  # normalizing twice changes nothing


@given(interval_sets(), interval_sets(), interval_sets())
def test_algebra_laws(a: IntervalSet, b: IntervalSet, c: IntervalSet) -> None:
    assert a | b == b | a
    assert a & b == b & a
    assert (a | b) | c == a | (b | c)
    assert a & (b | c) == (a & b) | (a & c)
    assert a - b == a & b.complement(window())
    w = window()
    assert (a | b).complement(w) == a.complement(w) & b.complement(w)


@given(interval_sets(), interval_sets())
def test_measure_is_additive(a: IntervalSet, b: IntervalSet) -> None:
    assert (a | b).total() + (a & b).total() == a.total() + b.total()
    assert (a - b).total() + (a & b).total() == a.total()
    assert a.total() == timedelta(seconds=len(members(a)))


def test_touching_intervals_merge_and_overlap_is_zero() -> None:
    s = IntervalSet.of((at(0), at(10)), (at(10), at(20)))
    assert list(s) == [Interval(at(0), at(20))]
    assert Interval(at(0), at(10)).overlap(Interval(at(10), at(20))) == timedelta(0)
    assert at(10) not in Interval(at(0), at(10))
    assert at(0) in Interval(at(0), at(10))


def test_longest_prefers_the_earliest_on_a_tie() -> None:
    s = IntervalSet.of((at(0), at(5)), (at(10), at(15)), (at(20), at(22)))
    assert s.longest() == Interval(at(0), at(5))
    assert IntervalSet().longest() is None


def test_intervals_are_stored_in_utc_and_naive_times_are_refused() -> None:
    ny = ZoneInfo("America/New_York")
    i = Interval(datetime(2026, 3, 8, 0, tzinfo=ny), datetime(2026, 3, 9, 0, tzinfo=ny))
    assert i.start.tzinfo is UTC
    # Wall-clock subtraction would say 24 h; the day lost an hour to DST.
    assert i.duration == timedelta(hours=23)
    with pytest.raises(ValueError, match="no UTC offset"):
        Interval(datetime(2026, 1, 1), datetime(2026, 1, 2))
    with pytest.raises(ValueError, match="ends before"):
        Interval(at(5), at(1))


@pytest.mark.parametrize(
    ("zone", "year", "month", "minutes"),
    [
        ("America/New_York", 2026, 3, 31 * 1440 - 60),  # spring forward
        ("America/New_York", 2026, 11, 30 * 1440 + 60),  # fall back
        ("America/New_York", 2026, 6, 30 * 1440),
        ("Europe/London", 2026, 3, 31 * 1440 - 60),
        ("Australia/Sydney", 2026, 4, 30 * 1440 + 60),  # southern hemisphere: ends in April
        ("Australia/Sydney", 2026, 10, 31 * 1440 - 60),
        ("Australia/Lord_Howe", 2026, 4, 30 * 1440 + 30),  # a 30-minute shift
        ("Asia/Kolkata", 2026, 3, 31 * 1440),  # no DST
        ("UTC", 2028, 2, 29 * 1440),
    ],
)
def test_month_lengths(zone: str, year: int, month: int, minutes: int) -> None:
    assert month_window(year, month, zone).duration == timedelta(minutes=minutes)


def test_a_month_whose_first_midnight_does_not_exist() -> None:
    # Paraguay moved its clocks from 00:00 to 01:00 on 1 October 2017.
    zone = ZoneInfo("America/Asuncion")
    start = local_day_start(date(2017, 10, 1), zone)
    assert start == datetime(2017, 10, 1, 4, tzinfo=UTC)
    assert start.astimezone(zone).hour == 1
    assert month_window(2017, 9, zone).end == start
    assert month_window(2017, 9, zone).duration == timedelta(days=30)
    assert month_window(2017, 10, zone).duration == timedelta(days=31, hours=-1)


ZONES = sorted(
    z
    for z in available_timezones()
    if z.startswith(("America/", "Europe/", "Australia/", "Asia/", "Africa/", "Pacific/"))
)


@given(
    st.sampled_from(ZONES),
    st.integers(1995, 2035),
    st.integers(1, 12),
)
def test_consecutive_months_tile_the_time_line(zone: str, year: int, month: int) -> None:
    this = month_window(year, month, zone)
    following = month_window(year + month // 12, month % 12 + 1, zone)
    assert this.end == following.start
    tz = ZoneInfo(zone)
    # The month's length is its days, corrected by the change in UTC offset between
    # its first and last local midnights (fold=0: the offset before any transition).
    days = calendar.monthrange(year, month)[1]
    first = datetime(year, month, 1, tzinfo=tz).utcoffset()
    last = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=tz).utcoffset()
    assert first is not None
    assert last is not None
    assert this.duration == timedelta(days=days) - (last - first)
    # The first instant belongs to the month and the one before does not.
    assert this.start.astimezone(tz).date() == date(year, month, 1)
    before = (this.start - timedelta(seconds=1)).astimezone(tz).date()
    assert before < date(year, month, 1)


def test_parse_month() -> None:
    assert parse_month("2026-03") == (2026, 3)
    for bad in ("2026-13", "2026", "March"):
        with pytest.raises(ValueError, match="YYYY-MM"):
            parse_month(bad)


def test_every_skipped_month_start_in_the_tz_database() -> None:
    # Every zone and month from 2000 to 2030 whose first local midnight falls in a
    # gap: the month must start at the first instant whose local date is the 1st.
    skipped = 0
    for name in ZONES:
        zone = ZoneInfo(name)
        for year in range(2000, 2031):
            for month in range(1, 13):
                naive = datetime(year, month, 1)
                if (
                    naive.replace(tzinfo=zone).astimezone(UTC).astimezone(zone).replace(tzinfo=None)
                    == naive
                ):
                    continue
                skipped += 1
                start = local_day_start(date(year, month, 1), zone)
                assert start.astimezone(zone).date() == date(year, month, 1)
                assert (start - timedelta(seconds=1)).astimezone(zone).date() < date(year, month, 1)
    assert skipped > 10  # the scan did find such months
