"""Replay of multi-window, multi-burn-rate alerts over a request history.

The burn rate of an error budget over a window is the window's error ratio
divided by the ratio the objective allows, ``1 - objective``: at a burn rate of 1
the budget lasts exactly the SLO period. A rule fires when the burn rate over a
long window and over a short window both exceed its threshold. The long window
decides how much budget must be at stake before it pages; the short one makes
the alert stop soon after the errors stop, instead of when the long window has
forgotten them.

:data:`WORKBOOK_RULES` are the thresholds recommended in Google's Site
Reliability Workbook ("Alerting on SLOs") for a 30-day objective.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

from slo_ledger._time import microseconds, to_utc
from slo_ledger.intervals import Interval, IntervalSet


@dataclass(frozen=True, slots=True)
class BurnRule:
    """Fire when the burn rate exceeds ``burn_rate`` over both windows.

    ``short_window`` may be ``None`` for a single-window rule (kept for comparison:
    it stops firing only when the long window has forgotten the errors).
    """

    name: str
    burn_rate: Fraction
    long_window: timedelta
    short_window: timedelta | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "burn_rate", Fraction(self.burn_rate))
        if self.burn_rate <= 0:
            raise ValueError("burn_rate must be positive")
        if self.short_window is not None and self.short_window > self.long_window:
            raise ValueError("the short window must not be longer than the long one")

    def budget_spent_to_fire(self, period: timedelta) -> Fraction:
        """Share of a ``period`` budget the long window holds when the rule fires."""
        return self.burn_rate * Fraction(microseconds(self.long_window), microseconds(period))


WORKBOOK_RULES: tuple[BurnRule, ...] = (
    BurnRule("page-1h", Fraction(144, 10), timedelta(hours=1), timedelta(minutes=5)),
    BurnRule("page-6h", Fraction(6), timedelta(hours=6), timedelta(minutes=30)),
    BurnRule("ticket-3d", Fraction(1), timedelta(days=3), timedelta(hours=6)),
)


@dataclass(frozen=True)
class RuleReplay:
    """When one rule was firing, and how many times it could be evaluated."""

    rule: BurnRule
    firing: IntervalSet
    evaluations: int


def replay(
    start: datetime,
    width: timedelta,
    good: Sequence[int],
    bad: Sequence[int],
    objective: Fraction,
    rules: Sequence[BurnRule] = WORKBOOK_RULES,
) -> list[RuleReplay]:
    """Evaluate each rule at the end of every bucket and return when it fired.

    ``good[k]`` and ``bad[k]`` are the requests of bucket ``k``, which covers
    ``[start + k * width, start + (k + 1) * width)``. A rule is evaluated at the end
    of bucket ``k`` only once its long window lies inside the history (no partial
    windows); both windows must be whole numbers of buckets. A window with no
    requests burns nothing. Comparisons are exact: a window fires when
    ``bad > burn_rate * (1 - objective) * total``. A rule that fires at an
    evaluation is taken to fire until the next evaluation, so its firing set is a
    union of ``[end_k, end_k + width)`` spans.
    """
    start = to_utc(start, "replay start")
    if len(good) != len(bad):
        raise ValueError("good and bad must have one value per bucket")
    objective = Fraction(objective)
    if not 0 < objective < 1:
        raise ValueError(f"objective must be in (0, 1), got {objective}")
    width_us = microseconds(width)
    allowed = 1 - objective
    prefix_bad = [0]
    prefix_all = [0]
    for g, b in zip(good, bad, strict=True):
        prefix_bad.append(prefix_bad[-1] + b)
        prefix_all.append(prefix_all[-1] + g + b)

    def window_buckets(span: timedelta) -> int:
        span_us = microseconds(span)
        if span_us <= 0 or span_us % width_us:
            raise ValueError(f"window {span} is not a whole number of {width} buckets")
        return span_us // width_us

    def burning(end: int, length: int, threshold: Fraction) -> bool:
        errors = prefix_bad[end] - prefix_bad[end - length]
        requests = prefix_all[end] - prefix_all[end - length]
        return requests > 0 and errors > threshold * requests

    results: list[RuleReplay] = []
    for rule in rules:
        long_n = window_buckets(rule.long_window)
        short_n = None if rule.short_window is None else window_buckets(rule.short_window)
        threshold = rule.burn_rate * allowed
        spans: list[Interval] = []
        evaluations = 0
        for end in range(long_n, len(good) + 1):
            evaluations += 1
            fires = burning(end, long_n, threshold) and (
                short_n is None or burning(end, short_n, threshold)
            )
            if fires:
                moment = start + end * width
                spans.append(Interval(moment, moment + width))
        results.append(RuleReplay(rule, IntervalSet(spans), evaluations))
    return results


def detection_time(firing: IntervalSet, incident: Interval) -> timedelta | None:
    """Time from the start of ``incident`` to the first firing at or after it."""
    for span in firing:
        if span.end > incident.start:
            return max(span.start - incident.start, timedelta(0))
    return None


def reset_time(firing: IntervalSet, incident: Interval) -> timedelta | None:
    """Time from the end of ``incident`` until the alert raised for it stops firing.

    ``None`` when the alert never fired for the incident; zero when it had already
    stopped by the time the incident ended.
    """
    for span in firing:
        if span.end > incident.start:
            if span.start > incident.end:
                return None
            return max(span.end - incident.end, timedelta(0))
    return None


def workbook_detection_time(
    rule: BurnRule, objective: Fraction, error_ratio: Fraction
) -> timedelta:
    """The workbook's detection time for a constant error ratio starting from zero.

    ``(1 - objective) / error_ratio * long_window * burn_rate``: the time the long
    window takes to accumulate enough errors, with traffic constant.
    """
    factor = (1 - Fraction(objective)) / Fraction(error_ratio) * rule.burn_rate
    return timedelta(microseconds=float(factor * microseconds(rule.long_window)))
