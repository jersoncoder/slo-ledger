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
