"""Write the synthetic telemetry of the worked example (March 2026, New York).

    python examples/generate_example.py examples/march

writes ``examples/march/data/*.csv``. Everything is drawn from one seeded
generator, so the files, and the statement built from them, are the same on
every run. The month contains the events the README discusses: two incidents and
a process restart on the request counters, a stuck indexer, a maintenance window
and a collector outage on the freshness gauge, a change of sampling rate on the
latency records, and on the two ports a restart, a missed poll, a garbled reading
and many 32-bit wraps.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 20260301
NEW_YORK = ZoneInfo("America/New_York")
# One hour of margin on each side of the month (05:00Z to 04:00Z, DST in between).
START = datetime(2026, 3, 1, 4, tzinfo=UTC)
END = datetime(2026, 4, 1, 5, tzinfo=UTC)
MINUTES = int((END - START) / timedelta(minutes=1))


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A New York wall-clock time as a UTC instant."""
    return datetime(year, month, day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_of(moment: datetime) -> int:
    return int((moment - START) / timedelta(minutes=1))


def diurnal(moment: datetime) -> float:
    """1 at the daily low (04:00 local), up to 2.6 at the high (16:00 local)."""
    hour = moment.astimezone(NEW_YORK).hour + moment.minute / 60
    return 1.8 - 0.8 * math.cos((hour - 4) / 24 * 2 * math.pi)


class Cumulative:
    """A count accumulated at a piecewise-constant rate per minute."""

    def __init__(self, per_minute: Sequence[float]) -> None:
        self.per_minute = per_minute
        self.prefix = [0.0]
        for amount in per_minute:
            self.prefix.append(self.prefix[-1] + amount)

    def at(self, moment: datetime) -> int:
        offset = (moment - START) / timedelta(minutes=1)
        whole = min(int(offset), len(self.per_minute) - 1)
        return int(self.prefix[whole] + (offset - whole) * self.per_minute[whole])


def write(path: Path, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle, lineterminator="\n")
        out.writerow(header)
        out.writerows(rows)


def counter_value(
    total: Cumulative, moment: datetime, restarts: Sequence[datetime], modulus: int | None
) -> int:
    """The counter's value: counts since its last restart, modulo its width."""
    index = bisect.bisect_right(list(restarts), moment)
    since = total.at(moment) - (total.at(restarts[index - 1]) if index else 0)
    return since % modulus if modulus else since


def requests(rng: random.Random, out: Path) -> None:
    incident_a = (local(2026, 3, 12, 14, 0), local(2026, 3, 12, 15, 30), 0.40)
    incident_b = (local(2026, 3, 24, 9, 0), local(2026, 3, 24, 17, 0), 0.009)
    restart = local(2026, 3, 20, 3, 12)
    good_rate: list[float] = []
    bad_rate: list[float] = []
    for k in range(MINUTES):
        moment = START + timedelta(minutes=k)
        traffic = 60 * 25 * diurnal(moment) * (1 + rng.gauss(0, 0.03))
        ratio = 0.0003
        for begin, end, share in (incident_a, incident_b):
            if begin <= moment < end:
                ratio = share
        good_rate.append(traffic * (1 - ratio))
        bad_rate.append(traffic * ratio)
    good = Cumulative(good_rate)
    total = Cumulative([g + b for g, b in zip(good_rate, bad_rate, strict=True)])
    rows = []
    moment = START
    while moment <= END:
        rows.append(
            [
                stamp(moment),
                counter_value(good, moment, [restart], None),
                counter_value(total, moment, [restart], None),
            ]
        )
        moment += timedelta(minutes=5)
    write(out / "checkout_requests.csv", ["time", "good", "total"], rows)


def freshness(rng: random.Random, out: Path) -> None:
    stuck = (local(2026, 3, 5, 10, 20), local(2026, 3, 5, 11, 12))
    reindex = (local(2026, 3, 15, 2, 0), local(2026, 3, 15, 4, 0))
    outage = (local(2026, 3, 18, 10, 0), local(2026, 3, 18, 10, 35))
    spikes = [local(2026, 3, d, h, 0) for d, h in ((3, 9), (9, 22), (27, 13))]
    rows = []
    moment = START
    while moment <= END:
        value = 45 + 20 * rng.random()
        if stuck[0] <= moment < stuck[1]:
            value = 300 + (moment - stuck[0]).total_seconds()
        if reindex[0] <= moment < reindex[1]:
            value = 900 + 300 * rng.random()
        if any(s <= moment < s + timedelta(minutes=7) for s in spikes):
            value = 320 + 60 * rng.random()
        if not outage[0] <= moment < outage[1]:
            rows.append([stamp(moment), f"{value:.1f}"])
        moment += timedelta(seconds=120 + rng.randint(-20, 20))
    write(out / "search_freshness.csv", ["time", "value"], rows)
    write(
        out / "maintenance.csv",
        ["start", "end"],
        [[stamp(reindex[0]), stamp(reindex[1])]],
    )


def latency(rng: random.Random, out: Path) -> None:
    change = local(2026, 3, 10, 0, 0)
    regression = (local(2026, 3, 16, 0, 0), local(2026, 3, 23, 0, 0))
    rows = []
    for k in range(MINUTES):
        moment = START + timedelta(minutes=k)
        count = round(60 * 2 * diurnal(moment))  # about 2 to 5 requests a second
        probability = 0.01 if moment < change else 0.004
        slow = 0.03 if regression[0] <= moment < regression[1] else 0.004
        for _ in range(count):
            if rng.random() >= probability:
                continue
            if rng.random() < slow:
                ms = 400 + rng.expovariate(1 / 300)
            else:
                ms = min(399.0, rng.lognormvariate(math.log(120), 0.35))
            second = rng.randint(0, 59)
            rows.append([stamp(moment + timedelta(seconds=second)), f"{ms:.1f}", probability])
    rows.sort(key=lambda row: str(row[0]))
    write(out / "search_latency_samples.csv", ["time", "latency_ms", "probability"], rows)


def port(
    rng: random.Random,
    out: Path,
    name: str,
    mbps: Callable[[datetime], float],
    modulus: int | None,
    restarts: Sequence[datetime],
    missed: tuple[datetime, datetime] | None,
    garbled: datetime | None = None,
) -> None:
    octets_per_minute = [
        mbps(START + timedelta(minutes=k)) * 1e6 / 8 * 60 * (1 + rng.gauss(0, 0.02))
        for k in range(MINUTES)
    ]
    total = Cumulative(octets_per_minute)
    rows = []
    grid = START
    while grid <= END:
        moment = grid + timedelta(seconds=rng.randint(0, 8))
        value = counter_value(total, moment, restarts, modulus)
        if garbled is not None and grid == garbled:
            value += 1 << 40  # a reading no port could have produced
        if missed is None or not missed[0] <= moment < missed[1]:
            rows.append([stamp(moment), value])
        grid += timedelta(minutes=5)
    write(out / f"{name}.csv", ["time", "octets"], rows)


def links(rng: random.Random, out: Path) -> None:
    bursts = [
        (local(2026, 3, d, h, 0), local(2026, 3, d, h + 2, 0))
        for d, h in ((6, 1), (13, 1), (20, 1), (27, 1))
    ]

    def uplink_a(moment: datetime) -> float:
        backup = any(b <= moment < e for b, e in bursts)
        return 70 * diurnal(moment) + (500 if backup else 0)

    def uplink_b(moment: datetime) -> float:
        return 25 * diurnal(moment)

    port(
        rng,
        out,
        "uplink_a",
        uplink_a,
        None,
        [local(2026, 3, 22, 4, 41)],
        (local(2026, 3, 9, 13, 2), local(2026, 3, 9, 13, 27)),
        local(2026, 3, 17, 8, 10),
    )
    restart = local(2026, 3, 11, 2, 17)
    port(rng, out, "uplink_b", uplink_b, 1 << 32, [restart], None)
    write(out / "uplink_b_restarts.csv", ["time"], [[stamp(restart)]])


def generate(directory: Path, seed: int = SEED) -> None:
    rng = random.Random(seed)
    out = directory / "data"
    requests(rng, out)
    freshness(rng, out)
    latency(rng, out)
    links(rng, out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("directory", type=Path, help="where to write data/*.csv")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    generate(args.directory, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
