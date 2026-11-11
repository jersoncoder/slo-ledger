"""Burn-rate replay against the workbook's detection-time formula and reset behaviour."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from slo_ledger.burnrate import (
    WORKBOOK_RULES,
    BurnRule,
    detection_time,
    replay,
    reset_time,
    workbook_detection_time,
)
from slo_ledger.intervals import Interval, IntervalSet

T0 = datetime(2026, 3, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
OBJECTIVE = Fraction(999, 1000)
PAGE_1H = WORKBOOK_RULES[0]
PAGE_6H = WORKBOOK_RULES[1]
TRAFFIC = 1000  # requests per minute, constant


def history(
    minutes: int, incident: tuple[int, int], error_ratio: Fraction
) -> tuple[list[int], list[int]]:
    """Constant traffic; a share ``error_ratio`` of it fails during ``incident`` minutes."""
    good, bad = [], []
    for k in range(minutes):
        failing = int(TRAFFIC * error_ratio) if incident[0] <= k < incident[1] else 0
        good.append(TRAFFIC - failing)
        bad.append(failing)
    return good, bad


def firing(rule: BurnRule, good: list[int], bad: list[int]) -> IntervalSet:
    (result,) = replay(T0, MINUTE, good, bad, OBJECTIVE, [rule])
    return result.firing


def test_workbook_rules_spend_the_stated_budget_shares() -> None:
    month = timedelta(days=30)
    assert [r.budget_spent_to_fire(month) for r in WORKBOOK_RULES] == [
        Fraction(2, 100),
        Fraction(5, 100),
        Fraction(10, 100),
    ]


def test_detection_time_of_a_total_outage_is_the_workbook_value() -> None:
    # The workbook's example: 100 % errors against 99.9 % fire the 1 h rule in
    # 0.001 / 1 * 3600 s * 14.4 = 51.84 s.
    assert workbook_detection_time(PAGE_1H, OBJECTIVE, Fraction(1)) == timedelta(seconds=51.84)
    good, bad = history(300, (120, 300), Fraction(1))
    fired = firing(PAGE_1H, good, bad)
    incident = Interval(T0 + 120 * MINUTE, T0 + 300 * MINUTE)
    # Evaluated once a minute, the first evaluation after 51.84 s is at one minute.
    assert detection_time(fired, incident) == MINUTE


@pytest.mark.parametrize("percent", [100, 50, 10, 5, 2])
def test_detection_matches_the_formula_rounded_up_to_the_evaluation(percent: int) -> None:
    error_ratio = Fraction(percent, 100)
    good, bad = history(600, (120, 600), error_ratio)
    incident = Interval(T0 + 120 * MINUTE, T0 + 600 * MINUTE)
    detected = detection_time(firing(PAGE_1H, good, bad), incident)
    formula = workbook_detection_time(PAGE_1H, OBJECTIVE, error_ratio)
    # Fires at the first whole minute strictly past the formula (the comparison is >).
    expected = (math.floor(formula / MINUTE) + 1) * MINUTE
    assert detected == expected
    assert timedelta(0) < detected - formula <= MINUTE


def test_a_burn_below_the_threshold_never_fires() -> None:
    # 1 % errors is a burn rate of 10, below 14.4: the 1 h page stays quiet,
    # while the 6 h page (threshold 6) fires.
    good, bad = history(900, (60, 900), Fraction(1, 100))
    assert not firing(PAGE_1H, good, bad)
    assert firing(PAGE_6H, good, bad)


def test_the_short_window_resets_the_alert_soon_after_the_errors_stop() -> None:
    good, bad = history(400, (100, 160), Fraction(1))
    incident = Interval(T0 + 100 * MINUTE, T0 + 160 * MINUTE)
    with_short = reset_time(firing(PAGE_1H, good, bad), incident)
    long_only = BurnRule("1h-only", PAGE_1H.burn_rate, PAGE_1H.long_window, None)
    without_short = reset_time(firing(long_only, good, bad), incident)
    assert with_short is not None
    assert without_short is not None
    assert with_short <= PAGE_1H.short_window  # type: ignore[operator]
    # The long window alone keeps paging until it has forgotten most of the hour.
    assert without_short > 50 * MINUTE


def test_no_partial_windows_and_no_traffic_burns_nothing() -> None:
    good, bad = [0] * 120, [0] * 120
    (result,) = replay(T0, MINUTE, good, bad, OBJECTIVE, [PAGE_1H])
    assert result.evaluations == 120 - 60 + 1
    assert not result.firing
    # Errors in the first hour are judged only once a whole hour exists.
    good, bad = history(61, (0, 61), Fraction(1))
    (result,) = replay(T0, MINUTE, good, bad, OBJECTIVE, [PAGE_1H])
    assert result.firing == IntervalSet.of((T0 + 60 * MINUTE, T0 + 62 * MINUTE))


def brute_force(
    good: list[int], bad: list[int], rule: BurnRule, width: timedelta, objective: Fraction
) -> list[int]:
    """Evaluation indices at which the rule fires, by direct summation."""
    long_n = rule.long_window // width
    short_n = rule.short_window // width if rule.short_window else None
    limit = rule.burn_rate * (1 - objective)
    fired = []
    for end in range(long_n, len(good) + 1):

        def burns(n: int, end: int = end) -> bool:
            errors = sum(bad[end - n : end])
            requests = errors + sum(good[end - n : end])
            return requests > 0 and Fraction(errors, requests) > limit

        if burns(long_n) and (short_n is None or burns(short_n)):
            fired.append(end)
    return fired


@given(
    st.lists(st.tuples(st.integers(0, 50), st.integers(0, 3)), min_size=12, max_size=80),
    st.integers(1, 6),
    st.integers(0, 6),
)
def test_replay_matches_direct_summation(
    buckets: list[tuple[int, int]], long_n: int, short_n: int
) -> None:
    good = [g for g, _ in buckets]
    bad = [b for _, b in buckets]
    short = None if short_n == 0 else min(short_n, long_n) * MINUTE
    rule = BurnRule("r", Fraction(144, 10), long_n * MINUTE, short)
    objective = Fraction(99, 100)
    (result,) = replay(T0, MINUTE, good, bad, objective, [rule])
    expected = brute_force(good, bad, rule, MINUTE, objective)
    spans = [Interval(T0 + e * MINUTE, T0 + (e + 1) * MINUTE) for e in expected]
    assert result.firing == IntervalSet(spans)


def test_validation() -> None:
    odd = BurnRule("odd", Fraction(1), timedelta(minutes=61), None)
    with pytest.raises(ValueError, match="whole number"):
        replay(T0, 2 * MINUTE, [1] * 60, [0] * 60, OBJECTIVE, [odd])
    with pytest.raises(ValueError, match="short window"):
        BurnRule("x", Fraction(1), MINUTE, 2 * MINUTE)
    with pytest.raises(ValueError, match="one value per bucket"):
        replay(T0, MINUTE, [1], [], OBJECTIVE)
