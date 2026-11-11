"""Gauge, counter and sampled SLIs, error budgets."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from statistics import fmean, stdev

import pytest
from hypothesis import given
from hypothesis import strategies as st

from slo_ledger.counters import CounterPolicy
from slo_ledger.intervals import Interval, IntervalSet
from slo_ledger.slo import (
    GaugeReading,
    SampledRecord,
    Unknown,
    error_budget,
    gauge_sli,
    horvitz_thompson,
    request_sli,
    time_error_budget,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Gauge


def test_gauge_step_semantics_with_staleness() -> None:
    readings = [
        GaugeReading(at(0), 5.0),
        GaugeReading(at(2), 50.0),  # bad from 2 to 4
        GaugeReading(at(4), 5.0),  # good from 4 to 7, then stale
        GaugeReading(at(10), 5.0),  # good from 10 to 13
    ]
    sli = gauge_sli(
        readings, Interval(at(0), at(15)), is_bad=lambda v: v > 30, staleness=3 * MINUTE
    )
    assert sli.bad == IntervalSet.of((at(2), at(4)))
    assert sli.good.total() == 8 * MINUTE
    assert sli.unknown == IntervalSet.of((at(7), at(10)), (at(13), at(15)))
    assert sli.attainment(Unknown.EXCLUDE) == Fraction(8, 10)
    assert sli.attainment(Unknown.GOOD) == Fraction(13, 15)
    assert sli.attainment(Unknown.BAD) == Fraction(8, 15)
    assert sli.longest_breach() == Interval(at(2), at(4))


def test_time_before_the_first_reading_is_unknown_and_window_clips() -> None:
    readings = [GaugeReading(at(-30), 1.0), GaugeReading(at(5), 99.0)]
    sli = gauge_sli(
        readings, Interval(at(0), at(10)), is_bad=lambda v: v > 30, staleness=60 * MINUTE
    )
    assert sli.good == IntervalSet.of((at(0), at(5)))
    assert sli.bad == IntervalSet.of((at(5), at(10)))
    assert not sli.unknown


def test_an_exclusion_splits_a_breach_and_leaves_every_denominator() -> None:
    readings = [GaugeReading(at(m), 99.0) for m in range(0, 30)]
    exclusion = IntervalSet.of((at(10), at(15)))
    sli = gauge_sli(
        readings,
        Interval(at(0), at(30)),
        is_bad=lambda v: v > 30,
        staleness=2 * MINUTE,
        exclusions=exclusion,
    )
    assert sli.excluded.total() == 5 * MINUTE
    assert sli.bad.total() == 25 * MINUTE
    assert len(sli.bad) == 2
    assert sli.longest_breach() == Interval(at(15), at(30))


@given(
    st.lists(
        st.tuples(st.integers(1, 20), st.floats(0, 100, allow_nan=False)),
        min_size=1,
        max_size=30,
    ),
    st.integers(1, 15),
)
def test_gauge_partitions_the_window(samples: list[tuple[int, float]], stale: int) -> None:
    moment = -5
    readings = []
    for gap, value in samples:
        moment += gap
        readings.append(GaugeReading(at(moment), value))
    window = Interval(at(0), at(200))
    excl = IntervalSet.of((at(50), at(60)))
    sli = gauge_sli(
        readings, window, is_bad=lambda v: v > 50, staleness=stale * MINUTE, exclusions=excl
    )
    parts = [sli.good, sli.bad, sli.unknown, sli.excluded]
    assert sum((p.total() for p in parts), timedelta(0)) == window.duration
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            assert not (a & b)


def test_gauge_validation() -> None:
    with pytest.raises(ValueError, match="staleness"):
        gauge_sli([], Interval(at(0), at(1)), is_bad=lambda v: v > 0, staleness=timedelta(0))
    with pytest.raises(ValueError, match="strictly increasing"):
        gauge_sli(
            [GaugeReading(at(1), 0), GaugeReading(at(1), 0)],
            Interval(at(0), at(2)),
            is_bad=lambda v: v > 0,
            staleness=MINUTE,
        )


# ---------------------------------------------------------------------------
# Counters


def test_request_sli_from_good_and_total_counters() -> None:
    rows = [
        (at(0), 0, 0),
        (at(5), 990, 1000),
        (at(10), 1980, 2000),
        (at(15), 10, 10),  # both counters restarted
        (at(20), 1000, 1010),
    ]
    sli = request_sli(
        rows,
        Interval(at(0), at(20)),
        5 * MINUTE,
        policy=CounterPolicy(bits=64, max_rate=Fraction(100)),
    )
    assert (sli.good, sli.bad) == (990 + 990 + 10 + 990, 10 + 10 + 0 + 10)
    assert sli.attainment() == Fraction(2980, 3010)
    assert not sli.unresolved


def test_request_sli_drops_buckets_an_exclusion_touches() -> None:
    rows = [(at(5 * k), 90 * k, 100 * k) for k in range(5)]
    sli = request_sli(
        rows,
        Interval(at(0), at(20)),
        5 * MINUTE,
        policy=CounterPolicy(bits=64),
        exclusions=IntervalSet.of((at(6), at(7))),
    )
    assert sli.excluded == (False, True, False, False)
    assert sli.total == 300


def test_request_sli_reports_steps_it_cannot_decide() -> None:
    rows = [(at(0), 0, 0), (at(5), 90, 100), (at(10), 5, 10)]
    sli = request_sli(
        rows, Interval(at(0), at(10)), 5 * MINUTE, policy=CounterPolicy(bits=64)
    )  # no rate bound: the decrease is a wrap or a reset
    assert len(sli.unresolved) == 1
    assert sli.total == 100


@given(
    st.lists(
        st.tuples(st.integers(1, 9), st.integers(0, 500), st.integers(0, 500)),
        min_size=1,
        max_size=25,
    )
)
def test_no_bucket_has_more_good_than_total(steps: list[tuple[int, int, int]]) -> None:
    rows = [(at(0), 0, 0)]
    moment = good = total = 0
    for gap, g, b in steps:
        moment += gap
        good += g
        total += g + b
        rows.append((at(moment), good, total))
    window = Interval(at(0), at(240))
    sli = request_sli(rows, window, MINUTE, policy=CounterPolicy(bits=None))
    assert sli.good == good
    assert sli.total == total
    assert all(b.increase >= 0 for b in sli.bad_buckets)


# ---------------------------------------------------------------------------
# Horvitz-Thompson


def population(rng: random.Random, size: int) -> list[bool]:
    return [rng.random() >= 0.02 for _ in range(size)]


def probability(good: bool, phase: int) -> float:
    # The kept share differs by outcome and by period; each record carries it.
    if not good:
        return 0.5
    return 0.05 if phase == 0 else 0.02


def test_horvitz_thompson_is_unbiased_and_its_interval_covers() -> None:
    # Seeded simulation: 1500 repetitions of sampling a fixed population of 4000
    # requests (2 % bad). Stated tolerances: the mean estimate within 4 standard
    # errors of the truth, and the 95 % interval covering the truth in 92 % to 98 %
    # of repetitions.
    rng = random.Random(20261111)
    truth = population(rng, 4000)
    phases = [0 if k < 2000 else 1 for k in range(4000)]
    true_ratio = sum(truth) / len(truth)
    totals, ratios, covered_known, covered_ratio = [], [], 0, 0
    reps = 1500
    for _ in range(reps):
        kept = [
            SampledRecord(p, good)
            for good, phase in zip(truth, phases, strict=True)
            if rng.random() < (p := probability(good, phase))
        ]
        known = horvitz_thompson(kept, known_total=len(truth))
        free = horvitz_thompson(kept)
        totals.append(free.total)
        ratios.append(known.ratio)
        low, high = known.interval()
        covered_known += low <= true_ratio <= high
        low, high = free.interval()
        covered_ratio += low <= true_ratio <= high
    assert abs(fmean(totals) - len(truth)) < 4 * stdev(totals) / reps**0.5
    assert abs(fmean(ratios) - true_ratio) < 4 * stdev(ratios) / reps**0.5
    assert 0.92 <= covered_known / reps <= 0.98
    assert 0.92 <= covered_ratio / reps <= 0.98


def test_unweighted_share_is_biased_when_probabilities_differ() -> None:
    rng = random.Random(7)
    truth = population(rng, 20000)
    kept = [SampledRecord(probability(g, 0), g) for g in truth if rng.random() < probability(g, 0)]
    naive = sum(r.good for r in kept) / len(kept)
    weighted = horvitz_thompson(kept).ratio
    true_ratio = sum(truth) / len(truth)
    assert abs(weighted - true_ratio) < 0.003
    assert abs(naive - true_ratio) > 0.1  # bad records are 10 times as likely to be kept


def test_every_record_kept_gives_the_exact_share_with_no_variance() -> None:
    records = [SampledRecord(1.0, k % 10 != 0) for k in range(100)]
    estimate = horvitz_thompson(records)
    assert estimate.ratio == pytest.approx(0.9)
    assert estimate.variance == 0
    assert estimate.interval() == pytest.approx((0.9, 0.9))


def test_sampled_record_validation() -> None:
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="inclusion probability"):
            SampledRecord(bad, True)
    with pytest.raises(ValueError, match="no records"):
        horvitz_thompson([])


# ---------------------------------------------------------------------------
# Error budgets


def test_error_budget_arithmetic() -> None:
    budget = error_budget(Fraction(999, 1000), good=999_500, total=1_000_000)
    assert budget.allowed == 1000
    assert budget.consumed == 500
    assert budget.remaining == Fraction(1, 2)
    assert budget.met
    over = error_budget(Fraction(999, 1000), good=998_000, total=1_000_000)
    assert over.remaining == -1
    assert not over.met
    with pytest.raises(ValueError, match="objective"):
        error_budget(Fraction(1), 1, 1)
    with pytest.raises(ValueError, match="good <= total"):
        error_budget(Fraction(1, 2), 5, 4)


def test_time_error_budget_of_a_30_day_month() -> None:
    # 99.9 % of 30 days allows 43.2 minutes of bad time.
    readings = [GaugeReading(T0, 0.0), GaugeReading(at(100), 99.0), GaugeReading(at(130), 0.0)]
    window = Interval(T0, T0 + timedelta(days=30))
    sli = gauge_sli(readings, window, is_bad=lambda v: v > 1, staleness=timedelta(days=31))
    budget = time_error_budget(Fraction(999, 1000), sli)
    assert budget.allowed == Fraction(432, 10) * 60_000_000
    assert budget.consumed == 30 * 60_000_000
    assert budget.met
