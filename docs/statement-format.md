# Statement configuration and inputs

`slo-ledger statement CONFIG` reads one TOML file. Paths in it are relative to the
file. Every timestamp in every CSV file is ISO 8601 with an offset (`Z` or
`+hh:mm`); a timestamp without one is refused with its file and line, because a
naive time cannot be placed on the time line without guessing.

Numbers that decide a result (objectives, thresholds of credit tiers, fees,
prices, commits) may be written as strings, `"0.999"`, so that TOML never turns
them into binary floating point. They are read as exact fractions or decimals.

## `[statement]`

| Key | Required | Meaning |
|---|---|---|
| `month` | yes | The month to settle, `YYYY-MM`. |
| `timezone` | yes | The IANA zone whose clocks define the month, e.g. `America/New_York`. |
| `currency` | no | A label printed after money amounts. |

The month runs from the first instant of its first day to the first instant of
the next month's first day on that zone's clocks. That is local midnight, except
where the clocks skip midnight, in which case the day starts when they resume.

## `[[requests]]`: good and total counters

CSV columns: `time,good,total`, the two counters read in the same rows.

| Key | Default | Meaning |
|---|---|---|
| `name` | | Printed name. |
| `readings` | | The CSV file. |
| `objective` | | Target good share, e.g. `"0.999"`. |
| `bucket` | `"5m"` | Bucket width; the month must be a whole number of buckets. |
| `bits` | `64` | Counter width: `32` or `64`. |
| `max_rate` | none | Largest plausible increase per second of the total counter. |
| `restarts` | none | CSV with a `time` column: instants the process restarted. |
| `restarts_complete` | `false` | The restart list is complete: a decrease with no restart is a wrap. |
| `max_gap` | none | Steps longer than this are marked interpolated. |
| `exclusions` | none | CSV `start,end`: time the objective does not count. A bucket an exclusion touches is left out whole. |
| `alerts` | `false` | Replay the workbook's three burn-rate rules over the month. |
| `fee`, `credits` | none | Monthly fee and credit tiers, `[{ below = "0.999", credit = "0.10" }, ...]`. |

## `[[gauge]]`: a sampled gauge

CSV columns: `time,value`.

| Key | Default | Meaning |
|---|---|---|
| `name`, `readings`, `objective` | | As above. |
| `bad_above` or `bad_below` | | Exactly one: the value is bad strictly above (below) it. |
| `staleness` | | How long a reading stays valid without a newer one. |
| `unknown` | `"exclude"` | Time with no valid reading: `exclude`, `good` or `bad`. |
| `exclusions`, `fee`, `credits` | none | As above. Excluded time leaves good, bad and unknown alike. |

## `[[sampled]]`: probability-sampled request records

CSV columns: `time,latency_ms,probability`. `probability` is the probability with
which the record was kept, in (0, 1]. Records outside the month are ignored.

| Key | Default | Meaning |
|---|---|---|
| `name`, `records`, `objective` | | As above (`records` is the CSV file). |
| `good_within_ms` | | A request is good when `latency_ms <= good_within_ms`. |
| `level` | `0.95` | Confidence level of the reported interval. |

The verdict is `met` when the whole interval is at or above the objective,
`missed` when it is entirely below, and `undecided` otherwise. Sampled objectives
earn no credit: an estimate with an interval is reported, not billed.

## `[[link]]`: a port billed at the 95th percentile

CSV columns: `time,octets`, an octet counter.

| Key | Default | Meaning |
|---|---|---|
| `name`, `readings` | | As above. |
| `bits` | `64` | Counter width: `32` or `64`. |
| `line_rate_mbps` | none | The port's line rate, the bound on a plausible increase. |
| `restarts`, `restarts_complete`, `max_gap` | | As for requests. |
| `bucket` | `"5m"` | Sample width. |
| `missing` | `"exclude"` | Buckets not fully covered by decided steps within `max_gap`: `exclude` them from the sample or count them as `zero`. |
| `commit_mbps` | | Committed rate. |
| `commit_price` | | Price per committed Mbps. |
| `burst_price` | | Price per Mbps of 95th percentile above the commit. |

The charge is `commit_mbps * commit_price + max(0, p95 - commit_mbps) * burst_price`,
computed exactly and rounded once to the cent, half up. Rates are shown in Mbps
(10^6 bits per second), rounded half up to three decimals.

## Output

`--format markdown` (the default) prints the tables of the README's example.
`--format json` gives the same figures, plus every firing span of every alert
rule and every counter step that was not counted. Attainments in JSON are
truncated to nine decimals and budget shares to four, never rounded up.
