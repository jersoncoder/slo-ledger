"""Percentiles by nearest rank, and burstable billing at the 95th percentile.

Burstable (95th percentile) billing measures a port's rate over every 5-minute
interval of the billing period, sorts the samples, discards the highest 5 % and
bills the highest sample that is left, so a customer can burst above the
committed rate for up to 5 % of the period (about 36 hours in a 30-day month)
without paying for it. When 5 % of the sample count is not a whole number, this
library discards the whole number of samples below it. That is exactly the
nearest-rank percentile, ``sorted[ceil(0.95 * n) - 1]``, and it is computed with
integers so that no floating-point product decides the rank.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, TypeVar

from slo_ledger.counters import Bucket

MissingPolicy = Literal["exclude", "zero"]
T = TypeVar("T", int, Fraction, float)


def nearest_rank_index(count: int, p: Fraction) -> int:
    """Zero-based index of the nearest-rank ``p`` percentile in ``count`` sorted values."""
    if count <= 0:
        raise ValueError("the percentile of an empty sample is undefined")
    if not 0 < p <= 1:
        raise ValueError(f"p must be in (0, 1], got {p}")
    numerator = p.numerator * count
    rank = -(-numerator // p.denominator)  # ceil(p * count) in integers
    return rank - 1


def nearest_rank(values: Sequence[T], p: Fraction) -> T:
    """The smallest value with at least ``p`` of the sample at or below it."""
    ordered = sorted(values)
    return ordered[nearest_rank_index(len(ordered), Fraction(p))]


@dataclass(frozen=True, slots=True)
class BurstableResult:
    """A 95th percentile (or other) billing figure and what it was taken from."""

    samples: int
    """Samples in the billing sample after the missing-sample policy."""
    missing: int
    """Buckets that were not complete and were excluded or counted as zero."""
    discarded: int
    """Highest samples discarded before the billed one."""
    billed: Fraction
    """The billed rate, in the samples' unit."""


def billing_samples(
    buckets: Iterable[Bucket], missing: MissingPolicy = "exclude"
) -> tuple[list[Fraction], int]:
    """Rates of complete buckets, and how many buckets were not complete.

    A bucket is complete when determined readings no further apart than the gap
    limit cover all of it. Incomplete buckets are left out of the sample
    (``"exclude"``, the default: the percentile is taken over measured intervals)
    or counted as a zero rate (``"zero"``, which can only lower the bill).
    """
    rates: list[Fraction] = []
    absent = 0
    for bucket in buckets:
        if bucket.complete:
            rates.append(bucket.rate)
        else:
            absent += 1
            if missing == "zero":
                rates.append(Fraction(0))
    return rates, absent


def burstable(
    buckets: Iterable[Bucket],
    *,
    percentile: Fraction = Fraction(95, 100),
    missing: MissingPolicy = "exclude",
) -> BurstableResult:
    """The burstable billing rate of a period's buckets (see the module docstring)."""
    rates, absent = billing_samples(buckets, missing)
    if not rates:
        raise ValueError("no complete bucket to bill from")
    index = nearest_rank_index(len(rates), percentile)
    billed = sorted(rates)[index]
    return BurstableResult(
        samples=len(rates), missing=absent, discarded=len(rates) - 1 - index, billed=billed
    )
