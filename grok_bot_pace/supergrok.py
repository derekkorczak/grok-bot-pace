"""Super Grok weekly usage from the Grok CLI billing endpoint."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from grok_bot_pace.api import UsageSnapshot, parse_time

CREDITS_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"

PRODUCT_LABELS = {
    "GrokBuild": "Build",
    "GrokChat": "Chat",
    "GrokTasks": "Tasks",
    "GrokImagine": "Imagine",
    "GrokVoice": "Voice",
    "Api": "API",
    "API": "API",
}


@dataclass
class SuperGrokSnapshot:
    used_percent: float
    period_start: datetime
    period_end: datetime
    fetched_at: datetime
    products: list[tuple[str, float]]

    def as_usage(self) -> UsageSnapshot:
        return UsageSnapshot(
            used_percent=self.used_percent,
            period_start=self.period_start,
            period_end=self.period_end,
            available=self.used_percent < 100,
            plan_label="Super Grok",
            fetched_at=self.fetched_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "products": [{"name": name, "percent": pct} for name, pct in self.products],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SuperGrokSnapshot:
        products = []
        for item in data.get("products") or []:
            products.append((str(item["name"]), float(item["percent"])))
        return cls(
            used_percent=float(data["used_percent"]),
            period_start=parse_time(data["period_start"]),
            period_end=parse_time(data["period_end"]),
            fetched_at=parse_time(data["fetched_at"]),
            products=products,
        )


def grok_home() -> Path:
    override = os.environ.get("GROK_HOME")
    if override:
        return Path(override)
    return Path.home() / ".grok"


def _read_cli_token() -> str:
    path = grok_home() / "auth.json"
    if not path.exists():
        raise RuntimeError("No Grok login found. Run grok login, then refresh.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Grok auth.json is not valid.")
    entries = [v for v in data.values() if isinstance(v, dict) and (v.get("key") or v.get("access_token"))]
    if not entries:
        raise RuntimeError("Grok auth.json has no session token. Run grok login.")
    now = datetime.now(timezone.utc)

    def sort_key(entry: dict) -> tuple[int, str]:
        exp = str(entry.get("expires_at") or "")
        try:
            expired = parse_time(exp) <= now
        except Exception:
            expired = True
        return (1 if expired else 0, exp)

    entries.sort(key=sort_key)
    token = entries[0].get("key") or entries[0].get("access_token")
    if not token:
        raise RuntimeError("Grok auth.json has no session token. Run grok login.")
    return str(token)


def parse_credits_payload(payload: dict[str, Any], fetched_at: datetime | None = None) -> SuperGrokSnapshot:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    used = config.get("creditUsagePercent")
    start = period.get("start") or config.get("billingPeriodStart")
    end = period.get("end") or config.get("billingPeriodEnd")
    if start is None or end is None:
        raise RuntimeError("Super Grok billing response is missing the weekly period")
    if used is None:
        cap = (config.get("onDemandCap") or {}).get("val") if isinstance(config.get("onDemandCap"), dict) else None
        spent = (config.get("onDemandUsed") or {}).get("val") if isinstance(config.get("onDemandUsed"), dict) else None
        if cap and cap > 0 and spent is not None:
            used = float(spent) / float(cap) * 100.0
        else:
            used = 0.0
    products: list[tuple[str, float]] = []
    for item in config.get("productUsage") or []:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("product") or "Other")
        pct = item.get("usagePercent")
        if pct is None:
            continue
        products.append((PRODUCT_LABELS.get(raw_name, raw_name), float(pct)))
    products.sort(key=lambda row: row[1], reverse=True)
    return SuperGrokSnapshot(
        used_percent=float(used),
        period_start=parse_time(start),
        period_end=parse_time(end),
        fetched_at=fetched_at or datetime.now(timezone.utc),
        products=products,
    )


def format_products(products: list[tuple[str, float]]) -> str:
    if not products:
        return ""
    return " · ".join(f"{name} {pct:.0f}%" for name, pct in products)


def fetch_supergrok() -> SuperGrokSnapshot:
    token = _read_cli_token()
    req = Request(CREDITS_URL, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-XAI-Token-Auth", "xai-grok-cli")
    req.add_header("Accept", "application/json")
    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("Super Grok session expired. Run grok login, then refresh.") from exc
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Super Grok HTTP {exc.code}: {body[:200]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Super Grok network error: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Super Grok billing response was not JSON")
    return parse_credits_payload(payload)
