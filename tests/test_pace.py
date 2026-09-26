from datetime import datetime, timedelta, timezone

from grok_bot_pace.api import parse_usage_payload
from grok_bot_pace.icons import COLORS, badge_color, badge_image, badge_label
from grok_bot_pace.pace import classify, compute_pace, expected_percent, format_hours, status_label
from grok_bot_pace.supergrok import format_products, parse_credits_payload


def test_expected_percent_bounds() -> None:
    start = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    assert expected_percent(start, end, start) == 0
    assert expected_percent(start, end, end) == 100
    mid = start + timedelta(days=3.5)
    assert abs(expected_percent(start, end, mid) - 50) < 0.01
    assert expected_percent(start, end, start - timedelta(hours=2)) == 0
    assert expected_percent(start, end, end + timedelta(hours=2)) == 100


def test_classify_thresholds() -> None:
    assert classify(20, 30) == "under"
    assert classify(30, 30) == "on_track"
    assert classify(36, 30) == "on_track"
    assert classify(40, 30) == "over"
    assert classify(96, 50) == "critical"
    assert classify(10, 10, available=False) == "critical"


def test_parse_usage_payload_camel_case() -> None:
    snap = parse_usage_payload(
        {
            "usagePercent": 20.825262,
            "hasAvailableUsage": True,
            "nextResetTimestampUtc": "2026-09-21T12:12:12.890Z",
            "currentPeriodStart": "2026-09-14T12:12:12.890Z",
            "grokPlanLabel": "SuperGrok",
        },
        fetched_at=datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc),
    )
    now = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
    pace = compute_pace(snap, now=now)
    assert abs(pace.used_percent - 20.825262) < 1e-6
    assert 29 < pace.expected_percent < 32
    assert pace.status == "under"
    assert pace.plan_label == "SuperGrok"
    assert pace.delta < 0


def _snap(used: float, start: datetime, end: datetime, now: datetime):
    return parse_usage_payload(
        {
            "usagePercent": used,
            "hasAvailableUsage": True,
            "currentPeriodStart": start.isoformat(),
            "nextResetTimestampUtc": end.isoformat(),
            "grokPlanLabel": "SuperGrok",
        },
        fetched_at=now,
    )


def test_badge_label_is_absolute_delta() -> None:
    start = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    now = start + timedelta(days=3.5)
    under = compute_pace(_snap(20, start, end, now), now=now)
    over = compute_pace(_snap(62, start, end, now), now=now)
    even = compute_pace(_snap(50, start, end, now), now=now)
    assert badge_label(under) == "30"
    assert badge_label(over) == "12"
    assert badge_label(even) == "0"
    assert badge_label(None) == "--"
    assert badge_color(under) == COLORS["under"]
    assert badge_color(over) == COLORS["over"]
    assert badge_color(even) == COLORS["under"]
    icon16 = badge_image(under, size=16)
    assert icon16.size == (16, 16)
    assert icon16.getpixel((8, 8))[:3] != (0, 0, 0)


def test_parse_supergrok_credits() -> None:
    snap = parse_credits_payload(
        {
            "config": {
                "creditUsagePercent": 22.0,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-09-14T02:33:23.998896+00:00",
                    "end": "2026-09-21T02:33:23.998896+00:00",
                },
                "productUsage": [
                    {"product": "GrokBuild", "usagePercent": 18.0},
                    {"product": "GrokChat", "usagePercent": 3.0},
                    {"product": "GrokTasks", "usagePercent": 1.0},
                ],
            }
        },
        fetched_at=datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc),
    )
    assert snap.used_percent == 22.0
    assert format_products(snap.products) == "Build 18% · Chat 3% · Tasks 1%"
    usage = snap.as_usage()
    assert usage.plan_label == "Super Grok"
    assert usage.used_percent == 22.0


def test_format_hours() -> None:
    assert format_hours(timedelta(days=4, hours=21)) == "4d 21h"
    assert format_hours(timedelta(hours=3, minutes=12)) == "3h 12m"
    assert format_hours(timedelta(minutes=9)) == "9m"


def test_supergrok_pace_difference_matches_grok_bot() -> None:
    start = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    now = start + timedelta(days=3.5)

    def credits(used: float):
        return parse_credits_payload(
            {"config": {"creditUsagePercent": used, "currentPeriod": {"start": start.isoformat(), "end": end.isoformat()}}},
            fetched_at=now,
        )

    under = compute_pace(credits(22).as_usage(), now=now)
    over = compute_pace(credits(62).as_usage(), now=now)
    even = compute_pace(credits(52).as_usage(), now=now)
    assert status_label(under) == "Under pace by 28 pts"
    assert status_label(over) == "Over pace by 12 pts"
    assert status_label(even) == "On track for the week"
    assert status_label(compute_pace(credits(100).as_usage(), now=now)) == "Quota exhausted"
    assert (under.status, over.status, even.status) == ("under", "over", "on_track")
    assert status_label(over) == status_label(compute_pace(_snap(62, start, end, now), now=now))
