"""A monthly statement from a TOML configuration and CSV telemetry.

The configuration names the month and its time zone, then lists what to settle:
``[[requests]]`` objectives from good and total counters, ``[[gauge]]`` objectives
from a sampled gauge, ``[[sampled]]`` objectives from probability-sampled request
records, and ``[[link]]`` ports billed at their 95th percentile. Paths are relative
to the configuration file. ``docs/statement-format.md`` describes every key and
CSV column.
"""

from __future__ import annotations

import csv
import json
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, TypeVar

from slo_ledger._time import format_timestamp, minutes, parse_duration, parse_timestamp
from slo_ledger.burnrate import WORKBOOK_RULES, replay
from slo_ledger.counters import CounterPolicy, Reading, Step, bucketize, counter_steps
from slo_ledger.credits import CreditTable, CreditTier, credit_amount, round_half_up, truncate
from slo_ledger.intervals import Interval, IntervalSet, month_window, parse_month
from slo_ledger.percentile import burstable
from slo_ledger.slo import (
    GaugeReading,
    SampledRecord,
    Unknown,
    error_budget,
    gauge_sli,
    horvitz_thompson,
    request_sli,
    time_error_budget,
)

Table = Mapping[str, Any]
T = TypeVar("T")


class ConfigError(ValueError):
    """The configuration or one of its inputs cannot be used."""


# --------------------------------------------------------------------------
# Reading inputs


def read_rows(
    path: Path, columns: tuple[str, ...], parse: Callable[[dict[str, str]], T]
) -> list[T]:
    """Parse every row of a CSV file that has at least ``columns``.

    Errors name the file and line, so a bad row in a month of data is easy to find.
    """
    try:
        handle = path.open(newline="", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    items: list[T] = []
    with handle:
        reader = csv.DictReader(handle)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise ConfigError(f"{path}: missing column(s) {', '.join(missing)}")
        for line, row in enumerate(reader, start=2):
            try:
                items.append(parse(row))
            except (ValueError, InvalidOperation, TypeError) as exc:
                raise ConfigError(f"{path}:{line}: {exc}") from exc
    return items


def read_spans(path: Path) -> IntervalSet:
    """Read ``start,end`` rows as an interval set."""
    return IntervalSet(
        read_rows(
            path,
            ("start", "end"),
            lambda row: Interval(parse_timestamp(row["start"]), parse_timestamp(row["end"])),
        )
    )


def read_times(path: Path) -> tuple[datetime, ...]:
    return tuple(read_rows(path, ("time",), lambda row: parse_timestamp(row["time"])))


# --------------------------------------------------------------------------
# Configuration helpers


def _get(table: Table, key: str, where: str) -> Any:
    if key not in table:
        raise ConfigError(f"{where}: missing key {key!r}")
    return table[key]


def _fraction(value: Any, key: str, where: str) -> Fraction:
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ConfigError(f"{where}: {key} must be a number, got {value!r}") from exc


def _decimal(value: Any, key: str, where: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"{where}: {key} must be a number, got {value!r}") from exc


def _duration(value: Any, key: str, where: str) -> timedelta:
    try:
        return parse_duration(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where}: {key}: {exc}") from exc


def _credits(table: Table, where: str) -> tuple[CreditTable, Decimal] | None:
    if "credits" not in table:
        return None
    tiers = [
        CreditTier(
            _decimal(_get(t, "below", where), "below", where),
            _decimal(_get(t, "credit", where), "credit", where),
        )
        for t in table["credits"]
    ]
    try:
        credit_table = CreditTable(tuple(tiers))
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    return credit_table, _decimal(_get(table, "fee", where), "fee", where)


def _plain(value: Fraction) -> str:
    """A configured number (an objective) as the decimal it was written as."""
    return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


def _ratio(value: Fraction | None, places: int = 9) -> str | None:
    if value is None:
        return None
    return str(truncate(value, Decimal(1).scaleb(-places)))


# --------------------------------------------------------------------------
# Results


@dataclass(frozen=True)
class StepNote:
    """A counter step that was not counted."""

    series: str
    start: datetime
    end: datetime
    kind: str
    before: int
    after: int
    candidates: tuple[int, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "start": format_timestamp(self.start),
            "end": format_timestamp(self.end),
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
            "candidates": list(self.candidates),
        }


def _note(series: str, step: Step, kind: str | None = None) -> StepNote:
    return StepNote(
        series,
        step.start,
        step.end,
        kind or step.kind.value,
        step.before,
        step.after,
        step.candidates,
    )


@dataclass(frozen=True)
class Credit:
    share: Decimal
    amount: Decimal

    def to_json(self) -> dict[str, str]:
        return {"share": str(self.share), "amount": str(self.amount)}


def _credit(
    terms: tuple[CreditTable, Decimal] | None, attainment: Fraction | None
) -> Credit | None:
    if terms is None or attainment is None:
        return None
    table, fee = terms
    share = table.credit_share(attainment)
    return Credit(share, credit_amount(fee, share))


@dataclass(frozen=True)
class AlertSummary:
    rule: str
    evaluations: int
    spans: tuple[Interval, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "evaluations": self.evaluations,
            "firing": [
                {"start": format_timestamp(s.start), "end": format_timestamp(s.end)}
                for s in self.spans
            ],
            "firing_minutes": minutes(sum((s.duration for s in self.spans), timedelta(0))),
        }


@dataclass(frozen=True)
class RequestsResult:
    name: str
    objective: Fraction
    good: int
    total: int
    attainment: Fraction | None
    budget_remaining: Fraction | None
    met: bool | None
    excluded_buckets: int
    credit: Credit | None
    alerts: tuple[AlertSummary, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "objective": _plain(self.objective),
            "good": self.good,
            "total": self.total,
            "attainment": _ratio(self.attainment),
            "budget_remaining": _ratio(self.budget_remaining, 4),
            "met": self.met,
            "excluded_buckets": self.excluded_buckets,
            "credit": None if self.credit is None else self.credit.to_json(),
            "alerts": [a.to_json() for a in self.alerts],
        }


@dataclass(frozen=True)
class GaugeResult:
    name: str
    objective: Fraction
    unknown_policy: Unknown
    good: timedelta
    bad: timedelta
    unknown: timedelta
    excluded: timedelta
    attainment: Fraction | None
    budget_remaining: Fraction | None
    met: bool | None
    longest_breach: Interval | None
    credit: Credit | None

    def to_json(self) -> dict[str, Any]:
        breach = self.longest_breach
        return {
            "name": self.name,
            "objective": _plain(self.objective),
            "unknown_counts_as": self.unknown_policy.value,
            "good_minutes": minutes(self.good),
            "bad_minutes": minutes(self.bad),
            "unknown_minutes": minutes(self.unknown),
            "excluded_minutes": minutes(self.excluded),
            "attainment": _ratio(self.attainment),
            "budget_remaining": _ratio(self.budget_remaining, 4),
            "met": self.met,
            "longest_breach": None
            if breach is None
            else {
                "start": format_timestamp(breach.start),
                "end": format_timestamp(breach.end),
                "minutes": minutes(breach.duration),
            },
            "credit": None if self.credit is None else self.credit.to_json(),
        }


@dataclass(frozen=True)
class SampledResult:
    name: str
    objective: Fraction
    records: int
    estimated_requests: float
    estimate: float
    low: float
    high: float
    level: float

    @property
    def verdict(self) -> str:
        if self.low >= self.objective:
            return "met"
        if self.high < self.objective:
            return "missed"
        return "undecided"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "objective": _plain(self.objective),
            "records": self.records,
            "estimated_requests": round(self.estimated_requests, 1),
            "estimate": round(self.estimate, 6),
            "interval": [round(self.low, 6), round(self.high, 6)],
            "level": self.level,
            "verdict": self.verdict,
        }


@dataclass(frozen=True)
class LinkResult:
    name: str
    samples: int
    missing: int
    discarded: int
    p95_mbps: Fraction
    commit_mbps: Fraction
    burst_mbps: Fraction
    charge: Decimal

    def to_json(self) -> dict[str, Any]:
        unit = Decimal("0.001")
        return {
            "name": self.name,
            "samples": self.samples,
            "missing": self.missing,
            "discarded": self.discarded,
            "p95_mbps": str(round_half_up(self.p95_mbps, unit)),
            "commit_mbps": str(round_half_up(self.commit_mbps, unit)),
            "burst_mbps": str(round_half_up(self.burst_mbps, unit)),
            "charge": str(self.charge),
        }


@dataclass(frozen=True)
class Statement:
    month: str
    timezone: str
    currency: str
    window: Interval
    requests: tuple[RequestsResult, ...] = ()
    gauges: tuple[GaugeResult, ...] = ()
    sampled: tuple[SampledResult, ...] = ()
    links: tuple[LinkResult, ...] = ()
    not_counted: tuple[StepNote, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "timezone": self.timezone,
            "currency": self.currency,
            "window": {
                "start": format_timestamp(self.window.start),
                "end": format_timestamp(self.window.end),
                "minutes": minutes(self.window.duration),
            },
            "requests": [r.to_json() for r in self.requests],
            "gauges": [g.to_json() for g in self.gauges],
            "sampled": [s.to_json() for s in self.sampled],
            "links": [link.to_json() for link in self.links],
            "not_counted": [n.to_json() for n in self.not_counted],
        }

    def to_json_text(self) -> str:
        return json.dumps(self.to_json(), indent=2, sort_keys=False) + "\n"


# --------------------------------------------------------------------------
# Building a statement


def _policy(table: Table, where: str, base: Path, rate_scale: Fraction) -> CounterPolicy:
    bits = table.get("bits", 64)
    if bits not in (32, 64, None):
        raise ConfigError(f"{where}: bits must be 32 or 64")
    rate_key = next((k for k in ("max_rate", "line_rate_mbps") if k in table), None)
    max_rate = None
    if rate_key is not None:
        max_rate = _fraction(table[rate_key], rate_key, where) * rate_scale
    restarts: tuple[datetime, ...] = ()
    if "restarts" in table:
        restarts = read_times(base / str(table["restarts"]))
    return CounterPolicy(
        bits=bits,
        max_rate=max_rate,
        restarts=restarts,
        restarts_complete=bool(table.get("restarts_complete", False)),
    )


def _requests(table: Table, window: Interval, base: Path, notes: list[StepNote]) -> RequestsResult:
    name = str(_get(table, "name", "[[requests]]"))
    where = f"[[requests]] {name}"
    objective = _fraction(_get(table, "objective", where), "objective", where)
    width = _duration(table.get("bucket", "5m"), "bucket", where)
    path = base / str(_get(table, "readings", where))
    readings = read_rows(
        path,
        ("time", "good", "total"),
        lambda row: (parse_timestamp(row["time"]), int(row["good"]), int(row["total"])),
    )
    exclusions = read_spans(base / str(table["exclusions"])) if "exclusions" in table else None
    max_gap = _duration(table["max_gap"], "max_gap", where) if "max_gap" in table else None
    try:
        sli = request_sli(
            readings,
            window,
            width,
            policy=_policy(table, where, base, Fraction(1)),
            exclusions=exclusions,
            max_gap=max_gap,
        )
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    for good_step, total_step in sli.unresolved:
        if total_step.start >= window.end or total_step.end <= window.start:
            continue
        if not total_step.determined:
            notes.append(_note(f"{name} total", total_step))
        elif not good_step.determined:
            notes.append(_note(f"{name} good", good_step))
        else:
            notes.append(_note(f"{name} good", good_step, "good exceeds total"))
    attainment = sli.attainment()
    budget = None if sli.total == 0 else error_budget(objective, sli.good, sli.total)
    alerts: list[AlertSummary] = []
    if table.get("alerts", False):
        good = [0 if x else b.increase for b, x in zip(sli.buckets, sli.excluded, strict=True)]
        bad = [0 if x else b.increase for b, x in zip(sli.bad_buckets, sli.excluded, strict=True)]
        for result in replay(window.start, width, good, bad, objective, WORKBOOK_RULES):
            alerts.append(AlertSummary(result.rule.name, result.evaluations, tuple(result.firing)))
    return RequestsResult(
        name=name,
        objective=objective,
        good=sli.good,
        total=sli.total,
        attainment=attainment,
        budget_remaining=None if budget is None else budget.remaining,
        met=None if budget is None else budget.met,
        excluded_buckets=sum(sli.excluded),
        credit=_credit(_credits(table, where), attainment),
        alerts=tuple(alerts),
    )


def _gauge(table: Table, window: Interval, base: Path) -> GaugeResult:
    name = str(_get(table, "name", "[[gauge]]"))
    where = f"[[gauge]] {name}"
    objective = _fraction(_get(table, "objective", where), "objective", where)
    if ("bad_above" in table) == ("bad_below" in table):
        raise ConfigError(f"{where}: give exactly one of bad_above or bad_below")
    if "bad_above" in table:
        limit = float(table["bad_above"])

        def is_bad(value: float) -> bool:
            return value > limit
    else:
        limit = float(table["bad_below"])

        def is_bad(value: float) -> bool:
            return value < limit

    try:
        policy = Unknown(str(table.get("unknown", "exclude")))
    except ValueError as exc:
        raise ConfigError(f"{where}: unknown must be exclude, good or bad") from exc
    path = base / str(_get(table, "readings", where))
    readings = read_rows(
        path,
        ("time", "value"),
        lambda row: GaugeReading(parse_timestamp(row["time"]), float(row["value"])),
    )
    exclusions = read_spans(base / str(table["exclusions"])) if "exclusions" in table else None
    try:
        sli = gauge_sli(
            readings,
            window,
            is_bad=is_bad,
            staleness=_duration(_get(table, "staleness", where), "staleness", where),
            exclusions=exclusions,
        )
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    attainment = sli.attainment(policy)
    budget = None if attainment is None else time_error_budget(objective, sli, policy)
    return GaugeResult(
        name=name,
        objective=objective,
        unknown_policy=policy,
        good=sli.good.total(),
        bad=sli.bad.total(),
        unknown=sli.unknown.total(),
        excluded=sli.excluded.total(),
        attainment=attainment,
        budget_remaining=None if budget is None else budget.remaining,
        met=None if budget is None else budget.met,
        longest_breach=sli.longest_breach(),
        credit=_credit(_credits(table, where), attainment),
    )


def _sampled(table: Table, window: Interval, base: Path) -> SampledResult:
    name = str(_get(table, "name", "[[sampled]]"))
    where = f"[[sampled]] {name}"
    objective = _fraction(_get(table, "objective", where), "objective", where)
    threshold = float(_get(table, "good_within_ms", where))
    level = float(table.get("level", 0.95))
    path = base / str(_get(table, "records", where))
    rows = read_rows(
        path,
        ("time", "latency_ms", "probability"),
        lambda row: (
            parse_timestamp(row["time"]),
            SampledRecord(float(row["probability"]), float(row["latency_ms"]) <= threshold),
        ),
    )
    records = [record for moment, record in rows if moment in window]
    if not records:
        raise ConfigError(f"{where}: no records inside the month")
    estimate = horvitz_thompson(records)
    low, high = estimate.interval(level)
    return SampledResult(
        name, objective, estimate.records, estimate.total, estimate.ratio, low, high, level
    )


_MBPS = Fraction(1_000_000, 8)  # octets per second in one megabit per second


def _link(table: Table, window: Interval, base: Path, notes: list[StepNote]) -> LinkResult:
    name = str(_get(table, "name", "[[link]]"))
    where = f"[[link]] {name}"
    path = base / str(_get(table, "readings", where))
    readings = read_rows(
        path,
        ("time", "octets"),
        lambda row: Reading(parse_timestamp(row["time"]), int(row["octets"])),
    )
    width = _duration(table.get("bucket", "5m"), "bucket", where)
    max_gap = _duration(table["max_gap"], "max_gap", where) if "max_gap" in table else None
    missing = str(table.get("missing", "exclude"))
    if missing not in ("exclude", "zero"):
        raise ConfigError(f"{where}: missing must be exclude or zero")
    try:
        steps = counter_steps(readings, _policy(table, where, base, _MBPS))
        buckets = bucketize(steps, window, width, max_gap=max_gap)
        result = burstable(buckets, missing="zero" if missing == "zero" else "exclude")
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    notes.extend(
        _note(name, s)
        for s in steps
        if not s.determined and s.start < window.end and s.end > window.start
    )
    p95 = result.billed / _MBPS
    commit = _fraction(_get(table, "commit_mbps", where), "commit_mbps", where)
    burst = max(p95 - commit, Fraction(0))
    commit_price = _decimal(_get(table, "commit_price", where), "commit_price", where)
    burst_price = _decimal(_get(table, "burst_price", where), "burst_price", where)
    charge = round_half_up(commit * Fraction(commit_price) + burst * Fraction(burst_price))
    return LinkResult(
        name, result.samples, result.missing, result.discarded, p95, commit, burst, charge
    )


def build_statement(config_path: Path) -> Statement:
    """Read a configuration file and settle everything it lists."""
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
    base = config_path.parent
    head = _get(config, "statement", str(config_path))
    month_text = str(_get(head, "month", "[statement]"))
    zone = str(_get(head, "timezone", "[statement]"))
    try:
        year, month = parse_month(month_text)
        window = month_window(year, month, zone)
    except (ValueError, KeyError) as exc:
        raise ConfigError(f"[statement]: {exc}") from exc
    notes: list[StepNote] = []
    return Statement(
        month=month_text,
        timezone=zone,
        currency=str(head.get("currency", "")),
        window=window,
        requests=tuple(_requests(t, window, base, notes) for t in config.get("requests", [])),
        gauges=tuple(_gauge(t, window, base) for t in config.get("gauge", [])),
        sampled=tuple(_sampled(t, window, base) for t in config.get("sampled", [])),
        links=tuple(_link(t, window, base, notes) for t in config.get("link", [])),
        not_counted=tuple(notes),
    )


# --------------------------------------------------------------------------
# Markdown


def _pct(value: Fraction | float | None, places: int = 3) -> str:
    if value is None:
        return "n/a"
    exact = value if isinstance(value, Fraction) else Fraction(value)
    return f"{truncate(exact * 100, Decimal(1).scaleb(-places))}%"


def _objective(value: Fraction) -> str:
    text = format(Decimal(value.numerator) / Decimal(value.denominator) * 100, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "%"


def _credit_text(credit: Credit | None, currency: str) -> str:
    if credit is None:
        return "n/a"
    if credit.share == 0:
        return "none"
    return f"{_objective(Fraction(credit.share))} = {credit.amount} {currency}".rstrip()


def _when(moment: datetime) -> str:
    text = format_timestamp(moment)
    if text.endswith(":00Z"):
        text = text[: -len(":00Z")] + "Z"
    return text.replace("T", " ")


def render_markdown(statement: Statement) -> str:
    """The statement as Markdown tables."""
    lines = [
        f"# Statement {statement.month} ({statement.timezone})",
        "",
        f"Window {format_timestamp(statement.window.start)} to "
        f"{format_timestamp(statement.window.end)}, "
        f"{minutes(statement.window.duration):.0f} minutes.",
    ]
    if statement.requests:
        lines += [
            "",
            "## Request objectives",
            "",
            "| Service | Requests | Bad | Attainment | Objective | Budget left | Credit |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for r in statement.requests:
            budget = "n/a" if r.budget_remaining is None else _pct(r.budget_remaining, 1)
            lines.append(
                f"| {r.name} | {r.total:,} | {r.total - r.good:,} | {_pct(r.attainment)} "
                f"| {_objective(r.objective)} | {budget} "
                f"| {_credit_text(r.credit, statement.currency)} |"
            )
        replays = [(r.name, a) for r in statement.requests for a in r.alerts]
        if replays:
            lines += [
                "",
                "## Burn-rate alerts replayed",
                "",
                "| Service | Rule | Times fired | Minutes firing | First fired |",
                "|---|---|---:|---:|---|",
            ]
            for service, alert in replays:
                total = sum((s.duration for s in alert.spans), timedelta(0))
                first = _when(alert.spans[0].start) if alert.spans else "-"
                lines.append(
                    f"| {service} | {alert.rule} | {len(alert.spans)} "
                    f"| {minutes(total):.0f} | {first} |"
                )
    if statement.gauges:
        lines += [
            "",
            "## Time objectives",
            "",
            "| Service | Bad min | Unknown min | Excluded min | Attainment | Objective "
            "| Longest breach | Credit |",
            "|---|---:|---:|---:|---:|---:|---|---:|",
        ]
        for g in statement.gauges:
            breach = g.longest_breach
            longest = (
                "-"
                if breach is None
                else f"{minutes(breach.duration):.1f} min from {_when(breach.start)}"
            )
            lines.append(
                f"| {g.name} | {minutes(g.bad):.1f} | {minutes(g.unknown):.1f} "
                f"| {minutes(g.excluded):.1f} | {_pct(g.attainment)} | {_objective(g.objective)} "
                f"| {longest} | {_credit_text(g.credit, statement.currency)} |"
            )
    if statement.sampled:
        lines += [
            "",
            "## Sampled objectives",
            "",
            "| Service | Records | Estimated requests | Estimate | Interval | Objective "
            "| Verdict |",
            "|---|---:|---:|---:|---|---:|---|",
        ]
        for s in statement.sampled:
            level = _objective(Fraction(str(s.level)))
            lines.append(
                f"| {s.name} | {s.records:,} | {s.estimated_requests:,.0f} | {_pct(s.estimate)} "
                f"| {_pct(s.low)} to {_pct(s.high)} ({level}) | {_objective(s.objective)} "
                f"| {s.verdict} |"
            )
    if statement.links:
        lines += [
            "",
            "## Links billed at the 95th percentile",
            "",
            "| Link | Samples | Missing | 95th pct Mbps | Commit Mbps | Burst Mbps | Charge |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        unit = Decimal("0.001")
        currency = f" {statement.currency}" if statement.currency else ""
        for link in statement.links:
            lines.append(
                f"| {link.name} | {link.samples:,} | {link.missing:,} "
                f"| {round_half_up(link.p95_mbps, unit)} | {round_half_up(link.commit_mbps, unit)} "
                f"| {round_half_up(link.burst_mbps, unit)} "
                f"| {link.charge}{currency} |"
            )
    if statement.not_counted:
        lines += [
            "",
            "## Counter steps not counted",
            "",
            "| Series | From | To | Kind | Before | After | Candidate increases |",
            "|---|---|---|---|---:|---:|---|",
        ]
        for n in statement.not_counted:
            candidates = ", ".join(f"{c:,}" for c in n.candidates) or "-"
            lines.append(
                f"| {n.series} | {_when(n.start)} | {_when(n.end)} | {n.kind} "
                f"| {n.before:,} | {n.after:,} | {candidates} |"
            )
    return "\n".join(lines) + "\n"
