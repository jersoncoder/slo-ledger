# slo-ledger

Reliability and usage statements computed from raw telemetry, written so that
every figure on them can be traced to a stated rule. From counter readings,
gauge readings and sampled request records it settles a calendar month: request
and time-based SLO attainment, error budgets, a replay of multi-window burn-rate
alerts, service credits, and ports billed at their 95th percentile.

The hard part of such statements is rarely the formula. It is the data: counters
that wrap or restart between two readings, polls that go missing, months that
are an hour short on local clocks, records kept at different sampling rates, and
figures that round across a credit threshold. This library makes a decision for
each of those cases, writes it down, reports what it could not decide instead of
guessing, and keeps exact arithmetic (`Fraction` and `Decimal`) wherever a figure
decides money or a verdict. The test suite checks each decision, most of them
with property-based tests against an independent implementation.

## Quick start

```bash
git clone https://github.com/jersoncoder/slo-ledger.git
cd slo-ledger
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"   # or: pip install -e .  (the library has no dependencies)
```

```python
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
```

<!-- quickstart-output:start -->
```text
2026-03-01 05:00:00+00:00 to 2026-04-01 04:00:00+00:00 = 30 days, 23:00:00
12:05 normal   290,000,000
12:10 wrap     604,967,296
12:15 normal   900,000,000
12:20 reset    200,000,000
ambiguous candidates: (600000000, 604967296)
99.899% earns a credit of 0.10
```
<!-- quickstart-output:end -->

## A month, end to end

`examples/generate_example.py` writes a month of synthetic telemetry from one
seeded generator, and `examples/march/config.toml` says what to settle (the
configuration keys and CSV columns are in
[docs/statement-format.md](docs/statement-format.md)):

```bash
python examples/generate_example.py examples/march
slo-ledger statement examples/march/config.toml
```

<!-- statement-output:start -->
```text
# Statement 2026-03 (America/New_York)

Window 2026-03-01T05:00:00Z to 2026-04-01T04:00:00Z, 44580 minutes.

## Request objectives

| Service | Requests | Bad | Attainment | Objective | Budget left | Credit |
|---|---:|---:|---:|---:|---:|---:|
| checkout-api | 120,448,052 | 187,284 | 99.844% | 99.9% | -55.4% | 10% = 120.00 USD |

## Burn-rate alerts replayed

| Service | Rule | Times fired | Minutes firing | First fired |
|---|---|---:|---:|---|
| checkout-api | page-1h | 1 | 90 | 2026-03-12 18:05Z |
| checkout-api | page-6h | 2 | 400 | 2026-03-12 18:05Z |
| checkout-api | ticket-3d | 2 | 955 | 2026-03-12 18:10Z |

## Time objectives

| Service | Bad min | Unknown min | Excluded min | Attainment | Objective | Longest breach | Credit |
|---|---:|---:|---:|---:|---:|---|---:|
| search-freshness | 69.7 | 32.0 | 120.0 | 99.843% | 99.5% | 50.0 min from 2026-03-05 15:22:09Z | none |

## Sampled objectives

| Service | Records | Estimated requests | Estimate | Interval | Objective | Verdict |
|---|---:|---:|---:|---|---:|---|
| search-latency | 55,311 | 9,634,650 | 99.105% | 99.015% to 99.195% (95%) | 99% | met |

## Links billed at the 95th percentile

| Link | Samples | Missing | 95th pct Mbps | Commit Mbps | Burst Mbps | Charge |
|---|---:|---:|---:|---:|---:|---:|
| uplink-a | 8,906 | 10 | 181.596 | 200.000 | 0.000 | 500.00 USD |
| uplink-b | 8,916 | 0 | 64.713 | 50.000 | 14.713 | 216.21 USD |

## Counter steps not counted

| Series | From | To | Kind | Before | After | Candidate increases |
|---|---|---|---|---:|---:|---|
| uplink-a | 2026-03-17 12:05:01Z | 2026-03-17 12:10Z | implausible | 22,979,012,994,120 | 24,082,240,166,678 | - |
| uplink-a | 2026-03-17 12:10Z | 2026-03-17 12:15:07Z | implausible | 24,082,240,166,678 | 22,986,552,554,960 | - |
```
<!-- statement-output:end -->

What the month contains, and how the statement reads it (each point is checked
in [`tests/test_example.py`](tests/test_example.py)):

* **checkout-api** had a 90-minute incident with 40 % of requests failing on
  12 March (14:00 to 15:30 in New York, 18:00Z to 19:30Z) and an 8-hour one at
  0.9 % on 24 March, and its process restarted on 20 March, which reset both
  counters. The restart is decided from the rate bound (a 64-bit wrap is
  impossible there); the requests counted between the last reading and the
  restart are lost, as with any counter reset. The month misses 99.9 %, and the
  credit tier is chosen on the exact attainment. The fast page fires at the
  first evaluation after the first incident starts and stops five minutes after
  it ends, the short window's length. The second incident burns at 9 times the
  allowed rate, below the fast page's 14.4, so only the 6-hour rule and the
  ticket fire for it.
* **search-freshness** has a stuck indexer (the 50-minute breach), a reindex at
  02:00 on 15 March inside a declared maintenance window (excluded), and a
  35-minute collector outage, which leaves 32 minutes unknown once the last
  reading before it goes stale after 5 minutes. Unknown time is excluded from the
  attainment, as configured.
* **search-latency** records were kept with probability 1 % until 10 March and
  0.4 % after, and a slow week came after the change. Weighted by the inverse of
  their probabilities they estimate 99.105 %. Counted as equals they would say
  99.260 %, outside the whole confidence interval.
* **uplink-a** carried four two-hour backup bursts at 500 Mb/s above its usual
  traffic. They are about 1 % of the month's samples, inside the free 5 %, so the
  95th percentile stays under the commit. Its device restarted on 22 March, one
  poll was missed (a 25-minute gap), and one reading was garbled. The two steps
  around the garbled reading exceed the line rate whichever way they are read, so
  they are reported and not counted, and the buckets they cover leave the billing
  sample with those of the missed poll.
* **uplink-b** is an older 100 Mb/s port with a 32-bit counter, which wraps
  thousands of times in the month at its traffic. With its restart list declared
  complete (it comes from the device's uptime), every decrease but the one at the
  listed restart is decided as a wrap.

## Design

| Module | What it does |
|---|---|
| [`intervals`](src/slo_ledger/intervals.py) | Half-open intervals in UTC, interval sets in normal form (union, intersection, difference, complement), calendar months on a zone's clocks. |
| [`counters`](src/slo_ledger/counters.py) | Decides each step between two counter readings (normal, wrap, reset, ambiguous, undetermined, implausible) and spreads decided increases over buckets. |
| [`percentile`](src/slo_ledger/percentile.py) | Nearest-rank percentiles in integer arithmetic; burstable billing over complete buckets. |
| [`slo`](src/slo_ledger/slo.py) | Time-based SLI from a gauge, request-based SLI from counters, Horvitz-Thompson estimates from sampled records, error budgets. |
| [`burnrate`](src/slo_ledger/burnrate.py) | Replays multi-window, multi-burn-rate rules over a bucketed history; detection and reset times. |
| [`credits`](src/slo_ledger/credits.py) | Credit tiers on exact attainment, exact half-up money rounding and truncated percentages. |
| [`statement`](src/slo_ledger/statement.py), [`cli`](src/slo_ledger/cli.py) | Reads a TOML configuration and CSV files, assembles a statement, renders Markdown or JSON. |

The library has no third-party dependencies (only `tzdata` on Windows, which has
no system time zone database). Everything is in memory: a month of 5-minute
data is about nine thousand rows per series.

## Decisions and trade-offs

* **UTC inside, local clocks only for months.** Python subtracts two aware
  datetimes that share a `tzinfo` by their wall-clock fields, so midnight to
  midnight across a daylight-saving change comes out as 24 hours instead of 23.
  Every instant is converted to UTC on the way in, and every subtraction is then
  an elapsed time. A month is the span between the first instants of two local
  first days. Where a zone's clocks skip midnight (Paraguay on 1 October 2017),
  the day starts when the clocks resume; a test scans the whole time zone
  database for such months.
* **Half-open intervals in normal form.** `[start, end)` makes touching intervals
  share no instant, so lengths add up and adjacent breaches merge. Sets keep
  their intervals sorted, disjoint and non-touching, so two sets that cover the
  same instants are equal whatever built them.
* **A counter step is decided, or reported, never guessed.** A decrease is a
  wrap when `v1 + 2**bits - v0` fits under the rate bound, and a reset when `v1`
  does and the restart list is not declared complete. One plausible explanation
  decides the step; two leave it ambiguous with both candidate increases; none
  makes it implausible. If the rate bound allows a whole wrap within one step,
  the step is undetermined however the values moved, since any number of wraps
  may hide in it. This is why a 32-bit counter cannot be read every 5 minutes on
  a 1 Gb/s port, and why RFC 2863 requires 64-bit octet counters on interfaces
  faster than 20 Mb/s.
  A known restart inside a step makes it a reset counted from zero.
* **A constant rate between readings, and integer shares.** A step's increase is
  spread over the buckets it overlaps in proportion to the overlap, as integers,
  by the largest-remainder method: shares sum to the increase exactly and each is
  within one unit of its exact proportional share. Conservation is therefore an
  equality the tests assert, not a tolerance.
* **Billing samples are complete buckets.** A bucket enters the 95th percentile
  only when decided steps no longer than the gap limit cover all of it.
  Incomplete buckets are excluded (the percentile is over measured intervals) or
  counted as zero, which can only lower a bill; the count is reported either way.
* **The 95th percentile by nearest rank.** Discarding the whole number of samples
  within the top 5 % and billing the highest one left is exactly the nearest-rank
  percentile (type 1 of Hyndman and Fan), computed with integers.
* **Gauges hold until the next reading or until stale.** Time with no valid
  reading is unknown and can be excluded, counted good or counted bad. Excluded
  time (declared maintenance) leaves every denominator, and a breach an exclusion
  interrupts counts as two breaches.
* **Good and bad requests are spread separately.** Bad requests are `total - good`
  per step, and each is apportioned on its own, so no bucket ever shows more good
  requests than total ones. A bucket an exclusion touches is left out whole,
  because a counter cannot say which of its requests fell inside the exclusion.
* **Sampled records are weighted by their recorded probability.** With the
  population size known (an unsampled counter), `1 - B/N` is unbiased. Without
  it, the ratio of two Horvitz-Thompson totals is used (the Hajek estimator),
  with its linearized variance. Intervals use the normal approximation. A
  sampled objective is reported with its interval and a verdict, and earns no
  credit.
* **Burn-rate rules see whole windows only.** A rule is first evaluated when its
  long window lies inside the history, so no partial window can page. The
  comparison is the workbook's strict `>`, done in exact arithmetic, and a window
  with no requests burns nothing.
* **Thresholds on exact values, displays truncated.** Credit tiers compare the
  exact attainment; displayed percentages are truncated, never rounded up, so a
  statement cannot show 99.90 % next to a credit for missing 99.9 %. Money is
  rounded once, half up, with integer arithmetic on exact rationals; a test shows
  where 28-digit `Decimal` division would round a value just below half a cent up.

## How correctness is tested

`pytest` runs the whole suite in about ten seconds.

* **Interval algebra** ([`test_intervals.py`](tests/test_intervals.py)): union,
  intersection, difference and complement against a brute-force set of unit cells;
  commutativity, associativity, distributivity, De Morgan and additivity of
  measure; month lengths across DST in both hemispheres, a 30-minute shift and a
  leap year; consecutive months tile the time line in every zone; every skipped
  month start from 2000 to 2030.
* **Counters** ([`test_counters.py`](tests/test_counters.py)): each decision rule
  on its own; a simulated 32-bit counter whose wraps are all recovered; exact
  conservation of bucket shares and agreement with the exact proportional split
  within one unit per contributing step; coverage flags for gaps and undecided
  steps.
* **Percentiles** ([`test_percentile.py`](tests/test_percentile.py)): nearest
  rank equals an independent "discard the top 5 %" implementation and numpy's
  `inverted_cdf` for every percentile; a burst of exactly 5 % is free and one
  more sample is billed.
* **SLIs** ([`test_slo.py`](tests/test_slo.py)): gauge time partitions the window
  exactly into good, bad, unknown and excluded; no bucket has more good requests
  than total; a seeded simulation (1500 repetitions) shows the Horvitz-Thompson
  totals and ratio are unbiased within four standard errors and that the 95 %
  intervals cover the truth between 92 % and 98 % of the time; the unweighted
  share is biased when probabilities differ.
* **Burn rates** ([`test_burnrate.py`](tests/test_burnrate.py)): detection times
  equal the workbook's formula, rounded up to the next evaluation, for error
  ratios from 2 % to 100 %; the short window resets the alert within its own
  length, where the long window alone keeps paging for most of an hour; the
  replay equals direct summation on random histories.
* **Credits** ([`test_credits.py`](tests/test_credits.py)): tier boundaries,
  exact rounding against `decimal` where `decimal` is exact, and past its
  precision.
* **Statements and the example** ([`test_statement.py`](tests/test_statement.py),
  [`test_example.py`](tests/test_example.py), [`test_readme.py`](tests/test_readme.py)):
  configuration errors name the file and line; the example data regenerate byte
  for byte; the statement equals the committed
  [`statement.md`](examples/march/statement.md) and
  [`statement.json`](examples/march/statement.json) and this README's copy; the
  quick start prints what this README shows.

## Limitations

* A restart followed, within the same step, by growth past the previous value
  looks like a normal increase. Only a restart list catches it (Prometheus has
  the same blind spot).
* A restart missing from a list declared complete is taken for a wrap whenever a
  wrap explains the decrease, which on a 32-bit counter near its rate bound is
  most of the time. A test shows the miscount. Declare a list complete only when
  it comes from something like device uptime.
* Rates are assumed constant between two readings. A burst shorter than the
  polling interval is spread over it, and a 95th percentile of 5-minute samples
  cannot see inside a sample.
* The Horvitz-Thompson variance assumes each record was kept independently. A
  scheme that keeps or drops records in groups (a whole trace at once, say)
  needs a different variance, and the normal interval is poor when only a
  handful of bad records were kept.
* The burn-rate replay evaluates at bucket ends with perfect data; a real alert
  evaluates on its own schedule over data that arrives late. Rules cannot fire in
  the first long window of the history given to them.
* Inputs are CSV files; there is no connector to a monitoring system, and a
  month is processed in memory.

## Development

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy            # strict, over src, tests and examples
pytest
```

CI runs the same on Python 3.11, 3.12 and 3.13.

## References

* B. Beyer, N. R. Murphy, D. K. Rensin, K. Kawahara, S. Thorne (eds.), *The Site
  Reliability Workbook*, O'Reilly, 2018, chapter 5, "Alerting on SLOs":
  https://sre.google/workbook/alerting-on-slos/
* D. G. Horvitz and D. J. Thompson, "A generalization of sampling without
  replacement from a finite universe", *Journal of the American Statistical
  Association* 47 (1952), 663 to 685.
* C.-E. Sarndal, B. Swensson and J. Wretman, *Model Assisted Survey Sampling*,
  Springer, 1992 (the ratio estimator and its linearized variance).
* K. McCloghrie and F. Kastenholz, RFC 2863, "The Interfaces Group MIB", 2000:
  counter size on high-speed interfaces and `ifCounterDiscontinuityTime`.
* Prometheus documentation, counters and the handling of counter resets by
  `rate()` and `increase()`: https://prometheus.io/docs/concepts/metric_types/#counter
* X. Dimitropoulos, P. Hurley, A. Kind and M. P. Stoecklin, "On the 95-percentile
  billing method", *Passive and Active Network Measurement* (PAM), 2009.
* R. J. Hyndman and Y. Fan, "Sample quantiles in statistical packages", *The
  American Statistician* 50 (1996), 361 to 365.
* A. Belopolsky and L. Ellis, PEP 495, "Local Time Disambiguation" (the `fold`
  attribute), 2015.

## License

MIT. See [LICENSE](LICENSE).
