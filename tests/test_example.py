"""The worked example regenerates byte for byte and its statement matches the committed one."""

from __future__ import annotations

import csv
import hashlib
import shutil
from collections import Counter
from datetime import timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from slo_ledger._time import format_timestamp, parse_timestamp
from slo_ledger.cli import main
from slo_ledger.counters import CounterPolicy, Reading, StepKind, bucketize, counter_steps
from slo_ledger.credits import truncated_percent
from slo_ledger.intervals import month_window

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "march"


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import runpy

    directory = tmp_path_factory.mktemp("march")
    shutil.copy(EXAMPLE / "config.toml", directory / "config.toml")
    module = runpy.run_path(str(ROOT / "examples" / "generate_example.py"))
    module["generate"](directory)
    return directory


def digest(directory: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((directory / "data").glob("*.csv"))
    }


def test_generation_is_deterministic(generated: Path, tmp_path: Path) -> None:
    import runpy

    module = runpy.run_path(str(ROOT / "examples" / "generate_example.py"))
    module["generate"](tmp_path)
    assert digest(tmp_path) == digest(generated)


@pytest.mark.parametrize(
    ("fmt", "name"), [("markdown", "statement.md"), ("json", "statement.json")]
)
def test_statement_matches_the_committed_one(generated: Path, fmt: str, name: str) -> None:
    out = generated / name
    assert main(["statement", str(generated / "config.toml"), "--format", fmt, "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == (EXAMPLE / name).read_text(encoding="utf-8")


def test_the_example_shows_what_the_readme_says(generated: Path) -> None:
    from slo_ledger.statement import build_statement

    statement = build_statement(generated / "config.toml")
    requests = statement.requests[0]
    assert requests.met is False
    assert requests.credit is not None
    assert str(requests.credit.amount) == "120.00"
    page = next(a for a in requests.alerts if a.rule == "page-1h")
    assert len(page.spans) == 1  # the 40 % incident; the 0.9 % one never pages
    # The incident ran 14:00 to 15:30 New York time (18:00Z to 19:30Z): the page
    # fires at the first evaluation and stops one short window after the end.
    assert format_timestamp(page.spans[0].start) == "2026-03-12T18:05:00Z"
    assert format_timestamp(page.spans[0].end) == "2026-03-12T19:35:00Z"
    slow = next(a for a in requests.alerts if a.rule == "page-6h")
    assert len(slow.spans) == 2  # both incidents
    ticket = next(a for a in requests.alerts if a.rule == "ticket-3d")
    assert len(ticket.spans) == 2
    gauge = statement.gauges[0]
    assert gauge.excluded.total_seconds() == 2 * 3600  # the maintenance window
    assert gauge.longest_breach is not None
    assert round(gauge.longest_breach.duration.total_seconds() / 60) == 50  # the stuck indexer
    link_a, link_b = statement.links
    assert link_a.burst_mbps == 0  # the backup bursts fit in the free 5 %
    assert link_a.missing > 0  # the missed poll and the garbled reading
    assert link_b.burst_mbps > 0
    kinds = {n.kind for n in statement.not_counted}
    assert kinds == {"implausible"}


def test_weighting_moves_the_sampled_estimate(generated: Path) -> None:
    from slo_ledger.statement import build_statement

    estimate = build_statement(generated / "config.toml").sampled[0]
    with (generated / "data" / "search_latency_samples.csv").open() as handle:
        rows = [r for r in csv.DictReader(handle) if "2026-03-01T05" <= r["time"] < "2026-04-01T04"]
    unweighted = sum(float(r["latency_ms"]) <= 400 for r in rows) / len(rows)
    # Records were kept at 1 % before 10 March and at 0.4 % after it, and the slow
    # week came after the change: counting records as equals overstates attainment.
    assert estimate.records == len(rows)
    assert unweighted > estimate.high
    assert truncated_percent(Fraction(unweighted).limit_denominator(10**9)) == "99.260%"
    assert estimate.verdict == "met"


def readings(path: Path, column: str) -> list[Reading]:
    with path.open(newline="") as handle:
        return [Reading(parse_timestamp(r["time"]), int(r[column])) for r in csv.DictReader(handle)]


def test_the_counter_events_the_readme_describes(generated: Path) -> None:
    data = generated / "data"
    # checkout-api: one reset, at the 03:12 EDT restart on 20 March (07:12Z).
    checkout = counter_steps(
        readings(data / "checkout_requests.csv", "total"),
        CounterPolicy(bits=64, max_rate=Fraction(500)),
    )
    resets = [s for s in checkout if s.kind is StepKind.RESET]
    assert [format_timestamp(s.end) for s in resets] == ["2026-03-20T07:15:00Z"]
    # uplink-a: one reset (22 March) and the two implausible steps.
    line_a = CounterPolicy(bits=64, max_rate=Fraction(1000 * 10**6, 8))
    steps_a = counter_steps(readings(data / "uplink_a.csv", "octets"), line_a)
    kinds_a = Counter(s.kind for s in steps_a)
    assert kinds_a[StepKind.RESET] == 1
    assert kinds_a[StepKind.IMPLAUSIBLE] == 2
    # uplink-b: thousands of wraps, one reset at the listed restart, nothing else.
    with (data / "uplink_b_restarts.csv").open(newline="") as handle:
        listed = tuple(parse_timestamp(r["time"]) for r in csv.DictReader(handle))
    line_b = CounterPolicy(
        bits=32, max_rate=Fraction(100 * 10**6, 8), restarts=listed, restarts_complete=True
    )
    kinds_b = Counter(
        s.kind for s in counter_steps(readings(data / "uplink_b.csv", "octets"), line_b)
    )
    assert set(kinds_b) == {StepKind.NORMAL, StepKind.WRAP, StepKind.RESET}
    assert kinds_b[StepKind.RESET] == 1
    assert kinds_b[StepKind.WRAP] > 1000
    # uplink-a's backup bursts are about 1 % of the samples, inside the free 5 %.
    buckets = bucketize(
        steps_a,
        month_window(2026, 3, "America/New_York"),
        timedelta(minutes=5),
        max_gap=timedelta(minutes=10),
    )
    complete = [b for b in buckets if b.complete]
    above = sum(1 for b in complete if b.rate * 8 > 200 * 10**6)
    assert 0.005 < above / len(complete) < 0.02
