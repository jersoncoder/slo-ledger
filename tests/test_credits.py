"""Credit tiers on exact attainment, exact rounding of money and percentages."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from slo_ledger.credits import (
    CreditTable,
    CreditTier,
    credit_amount,
    round_half_up,
    truncate,
    truncated_percent,
)

TABLE = CreditTable(
    (
        CreditTier(Decimal("0.95"), Decimal("1")),
        CreditTier(Decimal("0.999"), Decimal("0.10")),
        CreditTier(Decimal("0.99"), Decimal("0.25")),
    )
)


@pytest.mark.parametrize(
    ("attainment", "share"),
    [
        (Fraction(1), Decimal(0)),
        (Fraction(999, 1000), Decimal(0)),  # exactly at the threshold meets it
        (Fraction(998_996, 1_000_000), Decimal("0.10")),  # displays as 99.899 %
        (Fraction(99, 100), Decimal("0.10")),
        (Fraction(98, 100), Decimal("0.25")),
        (Fraction(94, 100), Decimal("1")),
    ],
)
def test_tier_lookup_uses_the_exact_attainment(attainment: Fraction, share: Decimal) -> None:
    assert TABLE.credit_share(attainment) == share


def test_rounded_display_would_have_hidden_the_breach() -> None:
    attainment = Fraction(9_989_996, 10_000_000)  # 99.89996 %
    rounded = (Decimal(attainment.numerator) / attainment.denominator * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    assert rounded == Decimal("99.90")  # a rounded figure looks like it meets 99.9 %
    assert truncated_percent(attainment, 2) == "99.89%"
    assert TABLE.credit_share(attainment) == Decimal("0.10")


def test_table_is_sorted_and_validated() -> None:
    assert [t.below for t in TABLE.tiers] == [Decimal("0.999"), Decimal("0.99"), Decimal("0.95")]
    with pytest.raises(ValueError, match="larger credit"):
        CreditTable(
            (
                CreditTier(Decimal("0.99"), Decimal("0.5")),
                CreditTier(Decimal("0.9"), Decimal("0.1")),
            )
        )
    with pytest.raises(ValueError, match="share the threshold"):
        CreditTable(
            (
                CreditTier(Decimal("0.99"), Decimal("0.1")),
                CreditTier(Decimal("0.99"), Decimal("0.2")),
            )
        )
    with pytest.raises(ValueError, match="credit must be"):
        CreditTable((CreditTier(Decimal("0.99"), Decimal("0")),))


def test_credit_amounts_round_half_up_once() -> None:
    assert credit_amount(Decimal("1200.00"), Decimal("0.10")) == Decimal("120.00")
    assert credit_amount(Decimal("0.05"), Decimal("0.5")) == Decimal("0.03")  # 0.025 up
    assert credit_amount(Decimal("33.33"), Decimal("0.25")) == Decimal("8.33")  # 8.3325
    assert credit_amount(Decimal("0.10"), Decimal("0.25"), Decimal("0.001")) == Decimal("0.025")


def as_decimal(f: Fraction) -> Decimal:
    return Decimal(f.numerator) / Decimal(f.denominator)


@given(st.fractions(min_value=-(10**6), max_value=10**6, max_denominator=10**6))
def test_exact_rounding_agrees_with_decimal_when_decimal_is_exact(value: Fraction) -> None:
    # Values with a short decimal expansion are exact in Decimal, so Decimal's own
    # rounding is a correct reference for them.
    scaled = value * 10**6
    exact = Fraction(round(scaled), 10**6)
    reference = as_decimal(exact)
    assert round_half_up(exact) == reference.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert truncate(exact, Decimal("0.01")) == reference.quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )


def test_rounding_is_exact_past_decimal_precision() -> None:
    # One part in 10**40 below a half cent: 28-digit Decimal division would round it
    # up to the tie and then round the tie up. The exact rule rounds down.
    value = Fraction(5, 1000) - Fraction(1, 10**40)
    naive = as_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert naive == Decimal("0.01")
    assert round_half_up(value) == Decimal("0.00")
    assert round_half_up(Fraction(5, 1000)) == Decimal("0.01")
    assert round_half_up(Fraction(-5, 1000)) == Decimal("-0.01")
    # And a value a hair below 99.9 % is never displayed as 99.900 %.
    assert truncated_percent(Fraction(999, 1000) - Fraction(1, 10**40)) == "99.899%"
