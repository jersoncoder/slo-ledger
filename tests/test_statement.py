"""Statements built from small hand-written configurations and CSV files."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from slo_ledger.cli import main
from slo_ledger.statement import ConfigError, build_statement, render_markdown


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def config(tmp_path: Path, body: str, month: str = "2026-02", zone: str = "UTC") -> Path:
    head = f'[statement]\nmonth = "{month}"\ntimezone = "{zone}"\ncurrency = "EUR"\n\n'
    return write(tmp_path / "config.toml", head + body)


def flat_requests(tmp_path: Path, bad_per_hour: int) -> None:
    # February 2026 in UTC, read hourly: 1000 requests an hour, some of them bad.
    rows = ["time,good,total"]
    good = total = 0
    for hour in range(28 * 24 + 1):
        day, h = divmod(hour, 24)
        rows.append(f"2026-02-{day + 1:02d}T{h:02d}:00:00Z,{good},{total}")
        good += 1000 - bad_per_hour
        total += 1000
    rows[-1] = rows[-1].replace("2026-02-29T00", "2026-03-01T00")
    write(tmp_path / "requests.csv", "\n".join(rows) + "\n")


REQUESTS = """
[[requests]]
name = "api"
readings = "requests.csv"
objective = "0.999"
bucket = "1h"
fee = "100.00"
credits = [{ below = "0.999", credit = "0.10" }, { below = "0.99", credit = "0.25" }]
"""


def test_request_objective_met_and_missed(tmp_path: Path) -> None:
    flat_requests(tmp_path, bad_per_hour=1)
    result = build_statement(config(tmp_path, REQUESTS)).requests[0]
    assert (result.good, result.total) == (999 * 672, 1000 * 672)
    assert result.met is True
    assert result.credit is not None
    assert result.credit.share == 0
    flat_requests(tmp_path, bad_per_hour=2)
    result = build_statement(config(tmp_path, REQUESTS)).requests[0]
    assert result.met is False
    assert result.credit is not None
    assert (result.credit.share, result.credit.amount) == (Decimal("0.10"), Decimal("10.00"))


def test_gauge_objective_with_unknown_time(tmp_path: Path) -> None:
    rows = ["time,value"]
    rows += [f"2026-02-01T{h:02d}:00:00Z,{'500' if h == 3 else '10'}" for h in range(12)]
    write(tmp_path / "gauge.csv", "\n".join(rows) + "\n")
    body = """
[[gauge]]
name = "lag"
readings = "gauge.csv"
bad_above = 100
staleness = "1h"
objective = "0.99"
unknown = "bad"
"""
    result = build_statement(config(tmp_path, body)).gauges[0]
    hours = 28 * 24
    assert result.bad.total_seconds() == 3600
    assert result.unknown.total_seconds() == (hours - 12) * 3600
    assert result.attainment is not None
    assert result.attainment == pytest.approx(11 / hours)
    assert result.longest_breach is not None


def test_markdown_and_json_agree(tmp_path: Path) -> None:
    flat_requests(tmp_path, bad_per_hour=2)
    statement = build_statement(config(tmp_path, REQUESTS))
    text = render_markdown(statement)
    data = statement.to_json()
    assert "| api | 672,000 | 1,344 | 99.800% | 99.9% | -100.0% | 10% = 10.00 EUR |" in text
    assert data["requests"][0]["attainment"] == "0.998000000"
    assert data["requests"][0]["budget_remaining"] == "-1.0000"
    assert json.loads(statement.to_json_text()) == data


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[[requests]]\nname = "x"\n', "missing key 'objective'"),
        ('[[requests]]\nname = "x"\nobjective = "0.9"\nreadings = "nope.csv"\n', "cannot read"),
        ('[[gauge]]\nname = "g"\nobjective = "0.9"\n', "exactly one of bad_above"),
        (
            '[[requests]]\nname = "x"\nobjective = "0.9"\nreadings = "r.csv"\nbucket = "5 min"\n',
            "not a duration",
        ),
        ('[[link]]\nname = "l"\nreadings = "r.csv"\nbits = 16\n', "bits must be 32 or 64"),
    ],
)
def test_configuration_errors_name_the_problem(tmp_path: Path, body: str, message: str) -> None:
    write(tmp_path / "r.csv", "time,good,total,octets\n")
    with pytest.raises(ConfigError, match=message):
        build_statement(config(tmp_path, body))


def test_bad_rows_name_file_and_line(tmp_path: Path) -> None:
    write(
        tmp_path / "requests.csv",
        "time,good,total\n2026-02-01T00:00:00Z,0,0\n2026-02-01T01:00:00,5,5\n",
    )
    with pytest.raises(ConfigError, match=r"requests\.csv:3: .*no UTC offset"):
        build_statement(config(tmp_path, REQUESTS))
    write(tmp_path / "requests.csv", "time,good\n")
    with pytest.raises(ConfigError, match="missing column"):
        build_statement(config(tmp_path, REQUESTS))


def test_cli_writes_both_formats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    flat_requests(tmp_path, bad_per_hour=1)
    path = config(tmp_path, REQUESTS)
    assert main(["statement", str(path)]) == 0
    assert capsys.readouterr().out.startswith("# Statement 2026-02 (UTC)")
    out = tmp_path / "s.json"
    assert main(["statement", str(path), "--format", "json", "--output", str(out)]) == 0
    assert json.loads(out.read_text())["requests"][0]["met"] is True
    assert main(["month", "2026-11", "America/New_York"]) == 0
    assert capsys.readouterr().out == (
        "2026-11-01T04:00:00Z to 2026-12-01T05:00:00Z: 43260 minutes\n"
    )
    assert main(["statement", str(tmp_path / "missing.toml")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_undecided_request_steps_are_listed_not_counted(tmp_path: Path) -> None:
    write(
        tmp_path / "requests.csv",
        "time,good,total\n"
        "2026-02-01T00:00:00Z,0,0\n"
        "2026-02-01T01:00:00Z,900,1000\n"
        "2026-02-01T02:00:00Z,50,60\n"  # a decrease, and no rate bound to judge it
        "2026-02-01T03:00:00Z,950,1060\n",
    )
    statement = build_statement(config(tmp_path, REQUESTS))
    assert statement.requests[0].total == 2000
    (note,) = statement.not_counted
    assert (note.series, note.kind, note.candidates) == (
        "api total",
        "ambiguous",
        (60, 2**64 - 940),
    )
    assert "| api total | 2026-02-01 01:00Z | 2026-02-01 02:00Z | ambiguous |" in render_markdown(
        statement
    )
