"""Increases of monotonic counters between readings, and their split into buckets.

A counter only ever goes up, until it wraps around its width or its process
restarts and it starts again from zero. Between two readings ``v0`` at ``t0`` and
``v1`` at ``t1`` the increase is ``v1 - v0`` when ``v1 >= v0``; when ``v1 < v0``
it is either a wrap (``v1 + 2**bits - v0``) or a reset (``v1``, counted from zero;
whatever the counter added before it restarted is lost). Values alone cannot
always tell the two apart. :func:`counter_steps` decides each step from the
counter's width, a stated largest plausible rate and, when they are known, the
instants at which the counter restarted. A step it cannot decide is reported as
unresolved with its candidate increases, and is never counted.
"""

from __future__ import annotations

import enum
import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction

from slo_ledger._time import microseconds, to_utc
from slo_ledger.intervals import Interval


class StepKind(enum.Enum):
    """How the increase of one step was decided."""

    NORMAL = "normal"
    """The counter went up (or stayed) and the increase is within the rate bound."""
    WRAP = "wrap"
    """The counter went down and only a wrap around its width explains it."""
    RESET = "reset"
    """The counter restarted from zero (known restart, or the only explanation)."""
    AMBIGUOUS = "ambiguous"
    """The counter went down and both a wrap and a reset explain it."""
    UNDETERMINED = "undetermined"
    """The rate bound allows a full wrap within the step, so any number of wraps may hide in it."""
    IMPLAUSIBLE = "implausible"
    """No explanation keeps the increase within the rate bound."""


DETERMINED = frozenset({StepKind.NORMAL, StepKind.WRAP, StepKind.RESET})


@dataclass(frozen=True, slots=True)
class Reading:
    """One reading of a counter."""

    time: datetime
    value: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", to_utc(self.time, "reading time"))
        if self.value < 0:
            raise ValueError(f"counter value is negative at {self.time}: {self.value}")


@dataclass(frozen=True, slots=True)
class CounterPolicy:
    """What is known about a counter besides its readings.

    ``bits`` is the counter's width (32 or 64 for most network and process
    counters); ``None`` means it never wraps and every decrease is a reset.
    ``max_rate`` is the largest increase per second the counted thing can produce
    (a port's line rate in its unit, a service's peak request rate); without it a
    decrease of a wrapping counter is always ambiguous. ``restarts`` lists instants
    at which the counter is known to have restarted, and ``restarts_complete``
    states that the list is complete, so that a decrease with no restart in its
    step can only be a wrap.
    """

    bits: int | None = 64
    max_rate: Fraction | None = None
    restarts: tuple[datetime, ...] = field(default=())
    restarts_complete: bool = False

    def __post_init__(self) -> None:
        if self.bits is not None and self.bits <= 0:
            raise ValueError(f"bits must be positive, got {self.bits}")
        if self.max_rate is not None:
            rate = Fraction(self.max_rate)
            if rate <= 0:
                raise ValueError(f"max_rate must be positive, got {self.max_rate}")
            object.__setattr__(self, "max_rate", rate)
        object.__setattr__(
            self, "restarts", tuple(sorted(to_utc(t, "restart") for t in self.restarts))
        )

    @property
    def modulus(self) -> int | None:
        return None if self.bits is None else 1 << self.bits


@dataclass(frozen=True, slots=True)
class Step:
    """The change of a counter between two consecutive readings."""

    start: datetime
    end: datetime
    before: int
    after: int
    kind: StepKind
    increase: int | None
    """The decided increase; ``None`` when the step is not determined."""
    candidates: tuple[int, ...] = ()
    """For an ambiguous step, the increases of each explanation (reset, wrap)."""

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)

    @property
    def determined(self) -> bool:
        return self.kind in DETERMINED


def _classify(
    t0: datetime, v0: int, t1: datetime, v1: int, policy: CounterPolicy, restarted: bool
) -> Step:
    modulus = policy.modulus
    cap: Fraction | None = None
    if policy.max_rate is not None:
        cap = policy.max_rate * Fraction(microseconds(t1 - t0), 1_000_000)

    def step(kind: StepKind, increase: int | None, candidates: tuple[int, ...] = ()) -> Step:
        return Step(t0, t1, v0, v1, kind, increase, candidates)

    def within(increase: int) -> bool:
        return cap is None or increase <= cap

    if restarted:
        return step(StepKind.RESET, v1) if within(v1) else step(StepKind.IMPLAUSIBLE, None)
    if modulus is not None and cap is not None and cap >= modulus:
        return step(StepKind.UNDETERMINED, None)
    delta = v1 - v0
    if delta >= 0:
        return step(StepKind.NORMAL, delta) if within(delta) else step(StepKind.IMPLAUSIBLE, None)
    wrap_ok = modulus is not None and within(delta + modulus)
    reset_ok = not policy.restarts_complete and within(v1)
    if wrap_ok and reset_ok:
        assert modulus is not None
        return step(StepKind.AMBIGUOUS, None, (v1, delta + modulus))
    if wrap_ok:
        assert modulus is not None
        return step(StepKind.WRAP, delta + modulus)
    if reset_ok:
        return step(StepKind.RESET, v1)
    return step(StepKind.IMPLAUSIBLE, None)


def counter_steps(readings: Iterable[Reading], policy: CounterPolicy) -> list[Step]:
    """Decide the increase of the counter between each pair of consecutive readings.

    Readings must be in strictly increasing time. A step ``(t0, t1]`` that contains
    a known restart is a reset. Otherwise, with ``cap = max_rate * (t1 - t0)``:

    * if the counter wraps and ``cap`` reaches its modulus, the step is
      undetermined (a whole wrap fits in it, so the value change says nothing);
    * if ``v1 >= v0`` the increase is ``v1 - v0`` (implausible above ``cap``);
    * if ``v1 < v0``, a wrap is plausible when ``v1 + 2**bits - v0 <= cap`` and a
      reset when ``v1 <= cap`` and the restart list is not complete. One plausible
      explanation decides the step; two make it ambiguous; none, implausible.
    """
    items = list(readings)
    modulus = policy.modulus
    for item in items:
        if modulus is not None and item.value >= modulus:
            raise ValueError(
                f"value {item.value} at {item.time} does not fit in {policy.bits} bits"
            )
    steps: list[Step] = []
    restart_index = 0
    restarts = policy.restarts
    for previous, current in itertools.pairwise(items):
        if current.time <= previous.time:
            raise ValueError(
                f"readings must be in strictly increasing time: {previous.time} then {current.time}"
            )
        while restart_index < len(restarts) and restarts[restart_index] <= previous.time:
            restart_index += 1
        restarted = restart_index < len(restarts) and restarts[restart_index] <= current.time
        steps.append(
            _classify(previous.time, previous.value, current.time, current.value, policy, restarted)
        )
    return steps


@dataclass(frozen=True, slots=True)
class Bucket:
    """The share of a counter's increase that falls in ``[start, end)``.

    ``observed`` is the part of the bucket covered by determined steps no longer
    than the gap limit, ``interpolated`` the part covered by longer determined
    steps (their increase is spread at a constant rate), and ``unresolved`` the part
    covered by steps that were not determined. Whatever is left was not covered by
    any step.
    """

    start: datetime
    end: datetime
    increase: int
    observed: timedelta
    interpolated: timedelta
    unresolved: timedelta

    @property
    def width(self) -> timedelta:
        return self.end - self.start

    @property
    def complete(self) -> bool:
        """True when determined steps within the gap limit cover the whole bucket."""
        return self.observed == self.width

    @property
    def rate(self) -> Fraction:
        """Mean increase per second over the bucket."""
        return Fraction(self.increase * 1_000_000, microseconds(self.width))


def apportion(total: int, weights: Sequence[int]) -> list[int]:
    """Split ``total`` units in proportion to ``weights`` by largest remainders.

    Each share is the floor of its exact proportional share, and the units left
    over go one each to the largest fractional remainders, the earliest share
    winning a tie. The shares are integers, they sum to ``total`` exactly, and each
    differs from its exact share by less than one unit.
    """
    if total < 0:
        raise ValueError("total must not be negative")
    whole = sum(weights)
    if whole <= 0 or any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative with a positive sum")
    floors = [total * w // whole for w in weights]
    remainders = [total * w % whole for w in weights]
    left = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda k: (-remainders[k], k))
    for k in order[:left]:
        floors[k] += 1
    return floors


def bucketize(
    steps: Iterable[Step],
    window: Interval,
    width: timedelta,
    *,
    max_gap: timedelta | None = None,
) -> list[Bucket]:
    """Spread each determined step's increase over consecutive buckets of ``width``.

    Buckets start at ``window.start``; the window must be a whole number of
    buckets. A step's increase is shared between the buckets it overlaps (and the
    time outside the window) in proportion to the overlap, by
    :func:`apportion`, so the bucket increases are integers and the shares of a
    step always add up to its increase. The rule assumes a constant rate between
    two readings; steps longer than ``max_gap`` are flagged as interpolated.
    """
    width_us = microseconds(width)
    span_us = microseconds(window.duration)
    if width_us <= 0:
        raise ValueError("bucket width must be positive")
    if span_us % width_us:
        raise ValueError(f"window of {window.duration} is not a whole number of {width} buckets")
    count = span_us // width_us
    increase = [0] * count
    observed = [0] * count
    interpolated = [0] * count
    unresolved = [0] * count

    for step in steps:
        if step.end <= window.start or step.start >= window.end:
            continue
        first_us = max(microseconds(step.start - window.start), 0)
        last_us = min(microseconds(step.end - window.start), span_us)
        first_bucket = first_us // width_us
        last_bucket = (last_us - 1) // width_us
        pieces: list[int] = []
        overlaps: list[int] = []
        for k in range(first_bucket, last_bucket + 1):
            low = max(first_us, k * width_us)
            high = min(last_us, (k + 1) * width_us)
            overlaps.append(high - low)
        if step.increase is not None:
            step_us = microseconds(step.end - step.start)
            outside = step_us - sum(overlaps)
            pieces = apportion(step.increase, [*overlaps, outside])
        long_step = max_gap is not None and step.end - step.start > max_gap
        for offset, k in enumerate(range(first_bucket, last_bucket + 1)):
            if step.increase is None:
                unresolved[k] += overlaps[offset]
                continue
            increase[k] += pieces[offset]
            if long_step:
                interpolated[k] += overlaps[offset]
            else:
                observed[k] += overlaps[offset]

    start = window.start
    return [
        Bucket(
            start=start + k * width,
            end=start + (k + 1) * width,
            increase=increase[k],
            observed=timedelta(microseconds=observed[k]),
            interpolated=timedelta(microseconds=interpolated[k]),
            unresolved=timedelta(microseconds=unresolved[k]),
        )
        for k in range(count)
    ]
