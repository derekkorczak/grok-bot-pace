"""Compare actual Grok Bot usage to an even weekly pace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from grok_bot_pace.api import UsageSnapshot

Status = str  # under | on_track | over | critical


@dataclass(frozen=True)
class Pace:
    used_percent: float
    expected_percent: float
    delta: float
    status: Status
    period_start: datetime
    period_end: datetime
    now: datetime
    available: bool
    plan_label: str
    fetched_at: datetime | None = None
    stale: bool = False
    error: str | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    @property
    def resets_in(self) -> timedelta:
        remaining = self.period_end - self.now
        return remaining if remaining.total_seconds() > 0 else timedelta(0)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def expected_percent(period_start: datetime, period_end: datetime, now: datetime) -> float:
    total = (period_end - period_start).total_seconds()
    if total <= 0:
        return 100.0
    elapsed = (now - period_start).total_seconds()
    return clamp(elapsed / total * 100.0, 0.0, 100.0)


def classify(used_percent: float, expected: float, available: bool = True) -> Status:
    if not available or used_percent >= 95.0:
        return "critical"
    delta = used_percent - expected
    if delta > 8.0:
        return "over"
    if delta < -8.0:
        return "under"
    return "on_track"


def compute_pace(snapshot: UsageSnapshot, now: datetime | None = None, stale: bool = False) -> Pace:
    current = now or datetime.now(timezone.utc)
    expected = expected_percent(snapshot.period_start, snapshot.period_end, current)
    used = clamp(float(snapshot.used_percent), 0.0, 100.0)
    return Pace(
        used_percent=used,
        expected_percent=expected,
        delta=used - expected,
        status=classify(used, expected, snapshot.available),
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        now=current,
        available=snapshot.available,
        plan_label=snapshot.plan_label,
        fetched_at=snapshot.fetched_at,
        stale=stale,
    )


def format_hours(delta: timedelta) -> str:
    seconds = int(max(0, delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def status_label(pace: Pace) -> str:
    pts = abs(pace.delta)
    if pace.status == "critical":
        if not pace.available:
            return "Quota exhausted"
        return "Nearly empty"
    if pace.status == "over":
        return f"Over pace by {pts:.0f} pts"
    if pace.status == "under":
        return f"Under pace by {pts:.0f} pts"
    return "On track for the week"


def tooltip_text(pace: Pace) -> str:
    stale = " · stale" if pace.stale else ""
    return (
        f"Grok Bot  {pace.used_percent:.0f}% used  ·  "
        f"should be {pace.expected_percent:.0f}%  ·  "
        f"{status_label(pace)}  ·  "
        f"resets in {format_hours(pace.resets_in)}{stale}"
    )
