"""Service level indicators: time-based from a gauge, request-based from counters or samples.

Three ways to measure attainment over a window:

* :func:`gauge_sli` reads a gauge sampled at irregular times (a queue's age, a
  replica's delay, a probe's latency) and splits the window into good, bad and
  unknown time.
* :func:`request_sli` reads two counters, good and total requests, and splits
  their increases into buckets.
* :func:`horvitz_thompson` estimates the good share from a sample of request
  records, each carrying the probability with which it was kept.
"""

from __future__ import annotations

import enum
import itertools
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from statistics import NormalDist

from slo_ledger._time import microseconds, to_utc
from slo_ledger.counters import Bucket, CounterPolicy, Reading, Step, bucketize, counter_steps
from slo_ledger.intervals import Interval, IntervalSet

# --------------------------------------------------------------------------
# Time-based SLI from a gauge


class Unknown(enum.Enum):
    """How time with no fresh gauge reading counts toward attainment."""

    EXCLUDE = "exclude"
    """Left out of the denominator: attainment is over known time only."""
    GOOD = "good"
    """Counted as good time."""
    BAD = "bad"
    """Counted as bad time (the conservative choice for the provider)."""


@dataclass(frozen=True, slots=True)
class GaugeReading:
    time: datetime
    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", to_utc(self.time, "gauge reading time"))


@dataclass(frozen=True)
class TimeSLI:
    """A window split into good, bad, unknown and excluded time."""

    window: Interval
    good: IntervalSet
    bad: IntervalSet
    unknown: IntervalSet
    excluded: IntervalSet

    def attainment(self, unknown: Unknown = Unknown.EXCLUDE) -> Fraction | None:
        """Good time over counted time; ``None`` when no time counts."""
        good = microseconds(self.good.total())
        bad = microseconds(self.bad.total())
        rest = microseconds(self.unknown.total())
        if unknown is Unknown.GOOD:
            good += rest
        elif unknown is Unknown.BAD:
            bad += rest
        if good + bad == 0:
            return None
        return Fraction(good, good + bad)

    def longest_breach(self) -> Interval | None:
        return self.bad.longest()


def gauge_sli(
    readings: Iterable[GaugeReading],
    window: Interval,
    *,
    is_bad: Callable[[float], bool],
    staleness: timedelta,
    exclusions: IntervalSet | None = None,
) -> TimeSLI:
    """Split ``window`` by a gauge's readings.

    A reading holds from its time until the next reading, but for no longer than
    ``staleness``: past that the gauge is unknown until a reading arrives. Time
    before the first reading is unknown. Excluded time (maintenance the agreement
    does not count) is removed from good, bad and unknown alike, so a breach that
    an exclusion interrupts counts as two breaches.
    """
    if staleness <= timedelta(0):
        raise ValueError("staleness must be positive")
    items = list(readings)
    for previous, current in itertools.pairwise(items):
        if current.time <= previous.time:
            raise ValueError(
                f"readings must be in strictly increasing time: {previous.time} then {current.time}"
            )
    good: list[Interval] = []
    bad: list[Interval] = []
    for index, reading in enumerate(items):
        until = reading.time + staleness
        if index + 1 < len(items):
            until = min(until, items[index + 1].time)
        (bad if is_bad(reading.value) else good).append(Interval(reading.time, until))
    scope = IntervalSet((window,))
    removed = (exclusions or IntervalSet()).clip(window)
    good_set = IntervalSet(good).clip(window) - removed
    bad_set = IntervalSet(bad).clip(window) - removed
    unknown_set = scope - good_set - bad_set - removed
    return TimeSLI(window, good_set, bad_set, unknown_set, removed)


# --------------------------------------------------------------------------
# Request-based SLI from good and total counters


@dataclass(frozen=True)
class RequestSLI:
    """Good and bad requests per bucket, from a good counter and a total counter."""

    buckets: tuple[Bucket, ...]
    """Buckets of good requests."""
    bad_buckets: tuple[Bucket, ...]
    """Buckets of bad requests (total minus good, step by step)."""
    unresolved: tuple[tuple[Step, Step], ...]
    """Steps of (good, total) whose increases could not both be decided, or disagree."""
    excluded: tuple[bool, ...]
    """For each bucket, whether an exclusion touches it."""

    @property
    def good(self) -> int:
        return sum(b.increase for b, x in zip(self.buckets, self.excluded, strict=True) if not x)

    @property
    def bad(self) -> int:
        return sum(
            b.increase for b, x in zip(self.bad_buckets, self.excluded, strict=True) if not x
        )

    @property
    def total(self) -> int:
        return self.good + self.bad

    def attainment(self) -> Fraction | None:
        return None if self.total == 0 else Fraction(self.good, self.total)


def request_sli(
    readings: Sequence[tuple[datetime, int, int]],
    window: Interval,
    width: timedelta,
    *,
    policy: CounterPolicy,
    exclusions: IntervalSet | None = None,
    max_gap: timedelta | None = None,
) -> RequestSLI:
    """Good and bad requests per bucket from ``(time, good, total)`` counter readings.

    Both counters are read in the same rows, and both are decided with the same
    policy (they live in the same process, so they restart together). A step
    counts only when both increases are decided and the good increase does not
    exceed the total one; bad requests are then ``total - good`` for the step, and
    good and bad are each spread over the buckets, so a bucket never has more
    good requests than total ones. A bucket that any exclusion touches is left
    out whole.
    """
    good_steps = counter_steps((Reading(t, g) for t, g, _ in readings), policy)
    total_steps = counter_steps((Reading(t, n) for t, _, n in readings), policy)
    kept_good: list[Step] = []
    kept_bad: list[Step] = []
    unresolved: list[tuple[Step, Step]] = []
    for g, n in zip(good_steps, total_steps, strict=True):
        if g.increase is None or n.increase is None or g.increase > n.increase:
            unresolved.append((g, n))
            continue
        kept_good.append(g)
        kept_bad.append(
            Step(n.start, n.end, n.before, n.after, n.kind, n.increase - g.increase, ())
        )
    good_buckets = bucketize(kept_good, window, width, max_gap=max_gap)
    bad_buckets = bucketize(kept_bad, window, width, max_gap=max_gap)
    removed = (exclusions or IntervalSet()).clip(window)
    excluded = tuple(bool(removed & IntervalSet((Interval(b.start, b.end),))) for b in good_buckets)
    return RequestSLI(tuple(good_buckets), tuple(bad_buckets), tuple(unresolved), excluded)


# --------------------------------------------------------------------------
# Request-based SLI from probability-sampled records


@dataclass(frozen=True, slots=True)
class SampledRecord:
    """A kept request record and the probability with which it was kept."""

    probability: float
    good: bool

    def __post_init__(self) -> None:
        if not 0 < self.probability <= 1:
            raise ValueError(f"inclusion probability must be in (0, 1], got {self.probability}")


@dataclass(frozen=True, slots=True)
class SampledEstimate:
    """A Horvitz-Thompson estimate of the good share of requests."""

    records: int
    total: float
    """Estimated (or known) number of requests in the population."""
    bad: float
    """Estimated number of bad requests."""
    ratio: float
    """Estimated good share."""
    variance: float
    """Estimated variance of ``ratio``."""
    known_total: bool

    def interval(self, level: float = 0.95) -> tuple[float, float]:
        """Normal-approximation confidence interval for ``ratio``, clipped to [0, 1]."""
        z = NormalDist().inv_cdf(0.5 + level / 2)
        half = z * math.sqrt(self.variance)
        return max(0.0, self.ratio - half), min(1.0, self.ratio + half)


def horvitz_thompson(
    records: Iterable[SampledRecord], *, known_total: int | None = None
) -> SampledEstimate:
    """Estimate the good share of a population from records kept with known probabilities.

    Each request was kept independently with probability ``p_i`` (Poisson
    sampling), which may depend on the request's own attributes as long as it is
    recorded with it and is positive for every request. Weighting each kept
    record by ``1 / p_i`` gives unbiased estimates of population totals.

    With the population size ``N`` known (from an unsampled request counter), the
    good share is ``1 - B/N`` with ``B = sum(1/p_i)`` over bad records: unbiased,
    with variance estimated by ``sum((1 - p_i) / p_i**2) / N**2`` over bad records.
    Without it, the share is the ratio of two estimated totals (the Hajek
    estimator), slightly biased in small samples, with the linearized variance
    ``sum((1 - p_i) / p_i**2 * (y_i - R)**2) / T**2``.
    """
    items = list(records)
    weights = [1.0 / r.probability for r in items]
    bad_weight = sum(w for w, r in zip(weights, items, strict=True) if not r.good)
    if known_total is not None:
        if known_total <= 0:
            raise ValueError("known_total must be positive")
        total = float(known_total)
        ratio = 1.0 - bad_weight / total
        spread = sum((1.0 - r.probability) / r.probability**2 for r in items if not r.good)
        variance = spread / total**2
    else:
        total = sum(weights)
        if total == 0:
            raise ValueError("no records to estimate from")
        ratio = 1.0 - bad_weight / total
        spread = sum(
            (1.0 - r.probability) / r.probability**2 * ((1.0 if r.good else 0.0) - ratio) ** 2
            for r in items
        )
        variance = spread / total**2
    return SampledEstimate(
        records=len(items),
        total=total,
        bad=bad_weight,
        ratio=ratio,
        variance=variance,
        known_total=known_total is not None,
    )


# --------------------------------------------------------------------------
# Error budgets


@dataclass(frozen=True, slots=True)
class ErrorBudget:
    """How much of an objective's allowance of bad events (or bad time) was used."""

    objective: Fraction
    allowed: Fraction
    """Bad events (or microseconds) the objective allows over the window."""
    consumed: Fraction
    """Bad events (or microseconds) that happened."""

    @property
    def remaining(self) -> Fraction | None:
        """Share of the budget left: 1 untouched, 0 spent, negative overspent."""
        if self.allowed == 0:
            return None
        return 1 - self.consumed / self.allowed

    @property
    def met(self) -> bool:
        return self.consumed <= self.allowed


def error_budget(objective: Fraction, good: int, total: int) -> ErrorBudget:
    """Error budget of ``total`` events of which ``good`` were good."""
    objective = Fraction(objective)
    if not 0 < objective < 1:
        raise ValueError(f"objective must be in (0, 1), got {objective}")
    if not 0 <= good <= total:
        raise ValueError(f"need 0 <= good <= total, got good={good}, total={total}")
    return ErrorBudget(objective, (1 - objective) * total, Fraction(total - good))


def time_error_budget(
    objective: Fraction, sli: TimeSLI, unknown: Unknown = Unknown.EXCLUDE
) -> ErrorBudget:
    """Error budget of a time-based SLI, in microseconds of counted time."""
    good = microseconds(sli.good.total())
    bad = microseconds(sli.bad.total())
    rest = microseconds(sli.unknown.total())
    if unknown is Unknown.GOOD:
        good += rest
    elif unknown is Unknown.BAD:
        bad += rest
    return error_budget(objective, good, good + bad)
