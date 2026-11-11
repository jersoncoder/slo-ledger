"""Quick start: month windows, counter steps and an exact credit decision."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

from slo_ledger import (
    CounterPolicy,
    CreditTable,
    CreditTier,
    Reading,
    counter_steps,
    month_window,
    truncated_percent,
)

# March 2026 on New York's clocks is one hour short: daylight saving starts on the 8th.
march = month_window(2026, 3, "America/New_York")
print(march.start, "to", march.end, "=", march.duration)

# A 32-bit octet counter on a 100 Mb/s port, read every 5 minutes. Its restarts are
# known from the device's uptime, so a decrease with no restart can only be a wrap.
t0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
values = [4_000_000_000, 4_290_000_000, 600_000_000, 1_500_000_000, 200_000_000]
readings = [Reading(t0 + timedelta(minutes=5 * k), v) for k, v in enumerate(values)]
policy = CounterPolicy(
    bits=32,
    max_rate=Fraction(100_000_000, 8),  # octets per second at line rate
    restarts=(t0 + timedelta(minutes=17),),
    restarts_complete=True,
)
for step in counter_steps(readings, policy):
    print(step.end.strftime("%H:%M"), f"{step.kind.value:<6}", f"{step.increase:>13,}")

# Without the restart list, the same decrease could be a wrap or a reset.
unsure = counter_steps(readings[1:3], CounterPolicy(bits=32, max_rate=policy.max_rate))[0]
print(unsure.kind.value, "candidates:", unsure.candidates)

# 99.8996 % misses a 99.9 % objective even though it rounds to 99.90 %.
table = CreditTable((CreditTier(Decimal("0.999"), Decimal("0.10")),))
attainment = Fraction(998_996, 1_000_000)
print(truncated_percent(attainment), "earns a credit of", table.credit_share(attainment))
