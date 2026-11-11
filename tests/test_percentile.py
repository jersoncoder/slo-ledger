"""95th percentile billing: the convention, an independent implementation, and numpy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from slo_ledger.counters import Bucket, CounterPolicy, Reading, bucketize, counter_steps
from slo_ledger.intervals import Interval
from slo_ledger.percentile import burstable, nearest_rank, nearest_rank_index

T0 = datetime(2026, 3, 1, tzinfo=UTC)
FIVE = timedelta(minutes=5)


def discard_top_five_percent(values: list[int]) -> int:
    """The billing rule as operators state it, written independently of the library."""
    ordered = sorted(values, reverse=True)
    drop = len(ordered) * 5 // 100  # whole samples within the top 5 %
    return ordered[drop]


@given(st.lists(st.integers(0, 10**9), min_size=1, max_size=400))
def test_nearest_rank_is_the_discard_top_five_percent_rule(values: list[int]) -> None:
    assert nearest_rank(values, Fraction(95, 100)) == discard_top_five_percent(values)


@given(
    st.lists(st.integers(0, 10**6), min_size=1, max_size=300),
    st.integers(1, 99),
)
def test_nearest_rank_matches_numpy_inverted_cdf(values: list[int], percent: int) -> None:
    expected = np.percentile(np.array(values), percent, method="inverted_cdf")
    assert nearest_rank(values, Fraction(percent, 100)) == int(expected)


def test_month_sizes() -> None:
    # 30 days of 5-minute samples: 8640, the top 432 are discarded.
    assert nearest_rank_index(8640, Fraction(95, 100)) == 8207
    # 31 days: 8928 samples, 5 % is 446.4, so 446 are discarded.
    assert nearest_rank_index(8928, Fraction(95, 100)) == 8928 - 446 - 1
    assert nearest_rank_index(1, Fraction(95, 100)) == 0
    assert nearest_rank_index(20, Fraction(95, 100)) == 18
    with pytest.raises(ValueError, match="empty"):
        nearest_rank_index(0, Fraction(1, 2))
    with pytest.raises(ValueError, match="p must be"):
        nearest_rank_index(10, Fraction(0))


def bucket(k: int, increase: int, complete: bool = True) -> Bucket:
    start = T0 + k * FIVE
    observed = FIVE if complete else timedelta(0)
    return Bucket(start, start + FIVE, increase, observed, timedelta(0), timedelta(0))


def test_burstable_ignores_a_burst_of_up_to_five_percent() -> None:
    # 100 samples at 1000 octets/s, 5 of them bursting to 1e6: the burst is free.
    buckets = [bucket(k, 300_000 if k >= 5 else 300_000_000) for k in range(100)]
    result = burstable(buckets)
    assert result.billed == 1000
    assert (result.samples, result.discarded, result.missing) == (100, 5, 0)
    # A sixth bursting sample is billed.
    buckets[5] = bucket(5, 300_000_000)
    assert burstable(buckets).billed == 1_000_000


def test_missing_samples_are_excluded_or_counted_as_zero() -> None:
    buckets = [bucket(k, 300 * (k + 1), complete=k % 4 != 0) for k in range(40)]
    excluded = burstable(buckets)
    zeroed = burstable(buckets, missing="zero")
    assert excluded.samples == 30
    assert excluded.missing == 10
    assert zeroed.samples == 40
    assert zeroed.billed <= excluded.billed
    with pytest.raises(ValueError, match="no complete bucket"):
        burstable([bucket(0, 1, complete=False)])


def test_end_to_end_from_counter_readings() -> None:
    # A counter read every 5 minutes for a day at a steady 2 Mb/s, plus one 10-minute
    # burst at 50 Mb/s: the burst is 2 of 288 samples, within the free 5 %.
    readings = []
    value = 0
    for k in range(289):
        readings.append(Reading(T0 + k * FIVE, value))
        rate = 50_000_000 if k in (100, 101) else 2_000_000
        value += rate // 8 * 300
    steps = counter_steps(readings, CounterPolicy(bits=64))
    buckets = bucketize(steps, Interval(T0, T0 + timedelta(days=1)), FIVE)
    result = burstable(buckets)
    assert result.billed * 8 == 2_000_000
