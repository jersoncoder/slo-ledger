"""Service credits from attainment, with exact thresholds and stated money rounding.

A credit table says which share of the monthly fee is credited back when the
month's attainment falls below each threshold. Two rules keep a statement
defensible:

* The tier is chosen on the exact attainment (a ``Fraction``), never on a rounded
  figure: 99.8996 % is below 99.9 % even though it rounds to 99.90 %.
* Displayed percentages are truncated, not rounded, so a statement never shows a
  figure that meets a threshold the exact value misses.

Money is ``Decimal``. A credit is the fee times the tier's share, rounded once to
the currency's minor unit, half away from zero (``ROUND_HALF_UP`` in
:mod:`decimal`). Every rounding here is done on exact rationals with integer
arithmetic, so no intermediate rounding can move a result across a boundary.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction


@dataclass(frozen=True, slots=True)
class CreditTier:
    """Attainment strictly below ``below`` earns ``credit`` (a share of the fee)."""

    below: Decimal
    credit: Decimal


@dataclass(frozen=True)
class CreditTable:
    """Credit tiers, kept sorted from the highest threshold to the lowest."""

    tiers: tuple[CreditTier, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.tiers, key=lambda t: t.below, reverse=True))
        for tier in ordered:
            if not 0 < tier.below <= 1:
                raise ValueError(f"threshold must be in (0, 1], got {tier.below}")
            if not 0 < tier.credit <= 1:
                raise ValueError(f"credit must be in (0, 1], got {tier.credit}")
        for higher, lower in itertools.pairwise(ordered):
            if higher.below == lower.below:
                raise ValueError(f"two tiers share the threshold {higher.below}")
            if lower.credit <= higher.credit:
                raise ValueError("a lower threshold must earn a larger credit")
        object.__setattr__(self, "tiers", ordered)

    def credit_share(self, attainment: Fraction) -> Decimal:
        """The share of the fee credited for ``attainment`` (zero when no tier applies)."""
        share = Decimal(0)
        for tier in self.tiers:
            if attainment < Fraction(tier.below):
                share = tier.credit
        return share


def _scaled(value: Fraction, unit: Decimal) -> Fraction:
    return value / Fraction(unit)


def round_half_up(value: Fraction | Decimal, unit: Decimal = Decimal("0.01")) -> Decimal:
    """Round to a multiple of ``unit``, ties away from zero, exactly."""
    exact = Fraction(value)
    scaled = abs(_scaled(exact, unit))
    whole, rest = divmod(scaled.numerator, scaled.denominator)
    if 2 * rest >= scaled.denominator:
        whole += 1
    sign = -1 if exact < 0 else 1
    return (Decimal(sign * whole) * unit).quantize(unit)


def truncate(value: Fraction | Decimal, unit: Decimal) -> Decimal:
    """Round to a multiple of ``unit`` toward zero, exactly."""
    exact = Fraction(value)
    scaled = abs(_scaled(exact, unit))
    whole = scaled.numerator // scaled.denominator
    sign = -1 if exact < 0 else 1
    return (Decimal(sign * whole) * unit).quantize(unit)


def credit_amount(fee: Decimal, share: Decimal, minor_unit: Decimal = Decimal("0.01")) -> Decimal:
    """The credit for ``fee`` at ``share``, rounded once to the minor unit."""
    return round_half_up(Fraction(fee) * Fraction(share), minor_unit)


def truncated_percent(value: Fraction, places: int = 3) -> str:
    """``value`` as a percentage truncated toward zero to ``places`` decimals."""
    return f"{truncate(value * 100, Decimal(1).scaleb(-places))}%"
