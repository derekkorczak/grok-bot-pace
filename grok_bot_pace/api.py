"""Grok Bot weekly usage from Cursor's GetSandUsageStatus endpoint."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus"


@dataclass
class UsageSnapshot:
    used_percent: float
    period_start: datetime
    period_end: datetime
    available: bool
    plan_label: str
    fetched_at: datetime
    cursor_plan_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "available": self.available,
            "plan_label": self.plan_label,
            "fetched_at": self.fetched_at.isoformat(),
            "cursor_plan_name": self.cursor_plan_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageSnapshot:
        return cls(
            used_percent=float(data["used_percent"]),
            period_start=parse_time(data["period_start"]),
            period_end=parse_time(data["period_end"]),
            available=bool(data.get("available", True)),
            plan_label=str(data.get("plan_label") or "Grok Bot"),
            fetched_at=parse_time(data["fetched_at"]),
            cursor_plan_name=data.get("cursor_plan_name"),
        )


def _obfuscate(buf: bytearray) -> bytearray:
    prev = 165
    for i, value in enumerate(buf):
        buf[i] = ((value ^ prev) + (i % 256)) & 255
        prev = buf[i]
    return buf


def cursor_checksum(machine_id: str, now_ms: int | None = None) -> str:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    ks = now // 1_000_000
    raw = bytearray(
        [
            (ks >> 40) & 255,
            (ks >> 32) & 255,
            (ks >> 24) & 255,
            (ks >> 16) & 255,
            (ks >> 8) & 255,
            ks & 255,
        ]
    )
    encoded = base64.urlsafe_b64encode(bytes(_obfuscate(raw))).decode("ascii").rstrip("=")
    return encoded + machine_id


def parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pick(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def parse_usage_payload(data: dict[str, Any], fetched_at: datetime | None = None) -> UsageSnapshot:
    used = _pick(data, "usagePercent", "usage_percent")
    start = _pick(data, "currentPeriodStart", "current_period_start")
    end = _pick(data, "nextResetTimestampUtc", "next_reset_timestamp_utc")
    if used is None or start is None or end is None:
        raise RuntimeError("usage response is missing percent or period timestamps")
    plan = _pick(data, "grokPlanLabel", "grok_plan_label") or "Grok Bot"
    available = _pick(data, "hasAvailableUsage", "has_available_usage")
    return UsageSnapshot(
        used_percent=float(used),
        period_start=parse_time(start),
        period_end=parse_time(end),
        available=True if available is None else bool(available),
        plan_label=str(plan),
        fetched_at=fetched_at or datetime.now(timezone.utc),
        cursor_plan_name=_pick(data, "cursorPlanName", "cursor_plan_name"),
    )


def fetch_usage(access_token: str) -> UsageSnapshot:
    machine_id = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "x-cursor-checksum": cursor_checksum(machine_id),
        "x-cursor-client-type": "sand",
        "x-cursor-client-version": "0.1.0",
        "x-sand-box-namespace": "prod",
        "x-ghost-mode": "true",
        "x-request-id": str(uuid.uuid4()),
    }
    req = Request(USAGE_URL, data=b"{}", method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:240]}") from exc
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("usage response was not JSON")
    if "usagePercent" not in payload and "usage_percent" not in payload:
        message = payload.get("message") or payload.get("code") or "unexpected usage response"
        raise RuntimeError(str(message))
    return parse_usage_payload(payload)
