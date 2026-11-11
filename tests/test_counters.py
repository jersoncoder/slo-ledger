"""Counter steps (wraps, resets, ambiguity) and their exact split into buckets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from slo_ledger.counters import (
    CounterPolicy,
    Reading,
    StepKind,
    apportion,
    bucketize,
    counter_steps,
)
from slo_ledger.intervals import Interval

T0 = datetime(2026, 3, 1, tzinfo=UTC)
M32 = 1 << 32


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# A 100 Mb/s port counts at most 12.5 million octets per second.
PORT = Fraction(12_500_000)


def test_normal_increase() -> None:
    step = counter_steps([Reading(at(0), 10), Reading(at(60), 25)], CounterPolicy())[0]
    assert (step.kind, step.increase) == (StepKind.NORMAL, 15)


def test_wrap_of_a_32_bit_counter_near_the_top() -> None:
    # 300 s at 100 Mb/s is at most 3.75e9 octets, less than 2**32: one wrap at most.
    policy = CounterPolicy(bits=32, max_rate=PORT, restarts_complete=True)
    step = counter_steps([Reading(at(0), M32 - 1000), Reading(at(300), 500)], policy)[0]
    assert (step.kind, step.increase) == (StepKind.WRAP, 1500)


def test_a_decrease_is_ambiguous_without_restart_knowledge() -> None:
    policy = CounterPolicy(bits=32, max_rate=PORT)
    step = counter_steps([Reading(at(0), M32 - 1000), Reading(at(300), 500)], policy)[0]
    assert step.kind is StepKind.AMBIGUOUS
    assert step.increase is None
    assert step.candidates == (500, 1500)  # reset, wrap


def test_a_known_restart_decides_a_reset() -> None:
    policy = CounterPolicy(bits=32, max_rate=PORT, restarts=(at(120),), restarts_complete=True)
    readings = [Reading(at(0), M32 - 1000), Reading(at(300), 500), Reading(at(600), 900)]
    first, second = counter_steps(readings, policy)
    assert (first.kind, first.increase) == (StepKind.RESET, 500)
    assert (second.kind, second.increase) == (StepKind.NORMAL, 400)


def test_a_restart_counts_from_zero_even_when_the_value_went_up() -> None:
    policy = CounterPolicy(bits=64, restarts=(at(10),))
    step = counter_steps([Reading(at(0), 100), Reading(at(60), 700)], policy)[0]
    assert (step.kind, step.increase) == (StepKind.RESET, 700)


def test_restart_on_the_reading_instant_belongs_to_the_step_that_ends_there() -> None:
    policy = CounterPolicy(bits=64, restarts=(at(60),))
    readings = [Reading(at(0), 100), Reading(at(60), 5), Reading(at(120), 50)]
    first, second = counter_steps(readings, policy)
    assert first.kind is StepKind.RESET
    assert second.kind is StepKind.NORMAL


def test_a_64_bit_decrease_is_a_reset_when_a_wrap_is_implausible() -> None:
    policy = CounterPolicy(bits=64, max_rate=Fraction(125_000_000_000))  # 1 Tb/s
    step = counter_steps([Reading(at(0), 9_000_000), Reading(at(300), 4_000)], policy)[0]
    assert (step.kind, step.increase) == (StepKind.RESET, 4_000)


def test_a_counter_without_width_never_wraps() -> None:
    policy = CounterPolicy(bits=None)
    step = counter_steps([Reading(at(0), 50), Reading(at(1), 3)], policy)[0]
    assert (step.kind, step.increase) == (StepKind.RESET, 3)


def test_polling_too_slow_for_the_width_is_undetermined() -> None:
    # 1 Gb/s for 300 s is 3.75e10 octets: a 32-bit counter can wrap several times.
    policy = CounterPolicy(bits=32, max_rate=Fraction(125_000_000))
    step = counter_steps([Reading(at(0), 10), Reading(at(300), 20)], policy)[0]
    assert step.kind is StepKind.UNDETERMINED
    assert step.increase is None


def test_an_increase_above_the_rate_bound_is_implausible() -> None:
    policy = CounterPolicy(bits=64, max_rate=Fraction(10))
    step = counter_steps([Reading(at(0), 0), Reading(at(10), 101)], policy)[0]
    assert step.kind is StepKind.IMPLAUSIBLE
    assert counter_steps([Reading(at(0), 0), Reading(at(10), 100)], policy)[0].increase == 100


def test_complete_restarts_and_no_plausible_wrap_is_implausible() -> None:
    policy = CounterPolicy(bits=64, max_rate=Fraction(10), restarts_complete=True)
    step = counter_steps([Reading(at(0), 100), Reading(at(10), 5)], policy)[0]
    assert step.kind is StepKind.IMPLAUSIBLE


def test_input_validation() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        counter_steps([Reading(at(1), 1), Reading(at(1), 2)], CounterPolicy())
    with pytest.raises(ValueError, match="does not fit in 32 bits"):
        counter_steps([Reading(at(0), M32)], CounterPolicy(bits=32))
    with pytest.raises(ValueError, match="negative"):
        Reading(at(0), -1)
    with pytest.raises(ValueError, match="no UTC offset"):
        Reading(datetime(2026, 1, 1), 1)


# ---------------------------------------------------------------------------
# A simulated counter: the step decisions recover the true increases.


@given(
    st.lists(st.integers(0, 3_000), min_size=1, max_size=40),
    st.integers(0, M32 - 1),
)
def test_wraps_are_recovered_when_restarts_are_known(increments: list[int], start: int) -> None:
    # A 32-bit counter that counts up to 3000 per 10 s step, wrapping freely.
    policy = CounterPolicy(bits=32, max_rate=Fraction(300), restarts_complete=True)
    readings = [Reading(at(0), start)]
    value = start
    for k, inc in enumerate(increments, start=1):
        value = (value + inc) % M32
        readings.append(Reading(at(10 * k), value))
    steps = counter_steps(readings, policy)
    assert [s.increase for s in steps] == increments
    assert all(s.kind in (StepKind.NORMAL, StepKind.WRAP) for s in steps)


# ---------------------------------------------------------------------------
# Apportionment and buckets


@given(
    st.integers(0, 10**12),
    st.lists(st.integers(0, 10**6), min_size=1, max_size=12).filter(lambda w: sum(w) > 0),
)
def test_apportion_is_exact_and_within_one_unit(total: int, weights: list[int]) -> None:
    shares = apportion(total, weights)
    assert sum(shares) == total
    whole = sum(weights)
    for share, weight in zip(shares, weights, strict=True):
        assert abs(Fraction(share) - Fraction(total * weight, whole)) < 1
        if weight == 0:
            assert share == 0


def test_apportion_breaks_ties_toward_the_earliest() -> None:
    assert apportion(1, [1, 1]) == [1, 0]
    assert apportion(5, [1, 1, 1]) == [2, 2, 1]


@st.composite
def readings_strategy(draw: st.DrawFn) -> list[Reading]:
    gaps = draw(st.lists(st.integers(1, 1_500), min_size=1, max_size=30))
    increments = draw(st.lists(st.integers(0, 10**9), min_size=len(gaps), max_size=len(gaps)))
    first = draw(st.integers(-200, 200))
    readings = [Reading(at(first), 0)]
    moment, value = first, 0
    for gap, inc in zip(gaps, increments, strict=True):
        moment += gap
        value += inc
        readings.append(Reading(at(moment), value))
    return readings


@given(readings_strategy())
def test_buckets_conserve_every_unit_inside_a_covering_window(readings: list[Reading]) -> None:
    steps = counter_steps(readings, CounterPolicy(bits=None))
    start = T0 - timedelta(minutes=5)
    window = Interval(start, start + timedelta(hours=14))
    assert readings[-1].time <= window.end
    buckets = bucketize(steps, window, timedelta(minutes=5))
    assert sum(b.increase for b in buckets) == readings[-1].value - readings[0].value


@given(readings_strategy(), st.integers(0, 120), st.integers(1, 120))
def test_buckets_match_the_exact_proportional_split(
    readings: list[Reading], offset_min: int, length_min: int
) -> None:
    steps = counter_steps(readings, CounterPolicy(bits=None))
    window = Interval(at(offset_min * 60), at((offset_min + length_min) * 60))
    width = timedelta(minutes=1)
    buckets = bucketize(steps, window, width)
    for bucket in buckets:
        span = Interval(bucket.start, bucket.end)
        exact = Fraction(0)
        contributing = 0
        for s in steps:
            overlap = s.interval.overlap(span)
            if overlap:
                contributing += 1
                exact += (
                    Fraction(s.increase or 0)
                    * Fraction(overlap.total_seconds())
                    / Fraction((s.end - s.start).total_seconds())
                )
        assert abs(bucket.increase - exact) < max(contributing, 1)


def test_bucket_coverage_flags_gaps_and_unresolved_steps() -> None:
    policy = CounterPolicy(bits=64)  # no rate bound: a decrease could be a wrap or a reset
    readings = [
        Reading(at(0), 0),
        Reading(at(300), 3_000),
        Reading(at(1500), 6_000),  # a 20-minute gap
        Reading(at(1800), 10),  # a decrease: ambiguous without restart knowledge
    ]
    steps = counter_steps(readings, policy)
    window = Interval(at(0), at(1800))
    buckets = bucketize(steps, window, timedelta(minutes=5), max_gap=timedelta(minutes=10))
    assert [b.complete for b in buckets] == [True, False, False, False, False, False]
    assert buckets[1].interpolated == timedelta(minutes=5)
    assert buckets[5].unresolved == timedelta(minutes=5)
    assert [b.increase for b in buckets] == [3_000, 750, 750, 750, 750, 0]
    assert buckets[0].rate == Fraction(10)


def test_window_must_be_whole_buckets() -> None:
    with pytest.raises(ValueError, match="whole number"):
        bucketize([], Interval(at(0), at(301)), timedelta(minutes=5))


def test_a_restart_missing_from_a_complete_list_is_taken_for_a_wrap() -> None:
    # The limitation the README states: the restart at 150 s is not in the list
    # declared complete, and a wrap explains the decrease, so the step is miscounted.
    policy = CounterPolicy(bits=32, max_rate=PORT, restarts_complete=True)
    true_since_restart = 300_000_000
    readings = [Reading(at(0), 2_000_000_000), Reading(at(300), true_since_restart)]
    (step,) = counter_steps(readings, policy)
    assert step.kind is StepKind.WRAP
    assert step.increase == M32 - 2_000_000_000 + true_since_restart
    assert step.increase > 2 * true_since_restart  # far more than really counted
