"""Weekly included usage from the Grok billing endpoint, plus the combined pool."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from grok_bot_pace.api import PeriodUsage, UsageSnapshot, parse_time

CREDITS_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
DEFAULT_TOKEN_URL = "https://auth.x.ai/oauth2/token"

PRODUCT_LABELS = {
    "GrokBuild": "Build",
    "PRODUCT_GROK_BUILD": "Build",
    "GrokChat": "Chat",
    "PRODUCT_GROK_CHAT": "Chat",
    "GrokTasks": "Tasks",
    "GrokImagine": "Imagine",
    "GrokVoice": "Voice",
    "GrokBot": "Grok Bot",
    "PRODUCT_GROK_BOT": "Grok Bot",
    "GrokApi": "API",
    "Api": "API",
    "API": "API",
    "Cursor": "Cursor",
    "GrokCursor": "Cursor",
}

_GENERIC_PLANS = {"", "grok bot", "grok bot plan", "supergrok", "super grok", "included"}


@dataclass
class SuperGrokSnapshot:
    used_percent: float
    period_start: datetime
    period_end: datetime
    fetched_at: datetime
    products: list[tuple[str, float]]
    unified: bool = False
    plan_label: str = "Super Grok"
    additional_percent: float | None = None
    prepaid_cents: int | None = None
    on_demand_used_cents: int | None = None
    on_demand_cap_cents: int | None = None
    available: bool = True

    def as_usage(self) -> UsageSnapshot:
        return UsageSnapshot(
            used_percent=self.used_percent,
            period_start=self.period_start,
            period_end=self.period_end,
            available=self.available and self.used_percent < 100,
            plan_label=self.plan_label,
            fetched_at=self.fetched_at,
            unified=self.unified,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "products": [{"name": name, "percent": pct} for name, pct in self.products],
            "unified": self.unified,
            "plan_label": self.plan_label,
            "additional_percent": self.additional_percent,
            "prepaid_cents": self.prepaid_cents,
            "on_demand_used_cents": self.on_demand_used_cents,
            "on_demand_cap_cents": self.on_demand_cap_cents,
            "available": self.available,
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
            unified=bool(data.get("unified", False)),
            plan_label=str(data.get("plan_label") or "Super Grok"),
            additional_percent=_optional_float(data.get("additional_percent")),
            prepaid_cents=_optional_int(data.get("prepaid_cents")),
            on_demand_used_cents=_optional_int(data.get("on_demand_used_cents")),
            on_demand_cap_cents=_optional_int(data.get("on_demand_cap_cents")),
            available=bool(data.get("available", True)),
        )


def grok_home() -> Path:
    override = os.environ.get("GROK_HOME")
    if override:
        return Path(override)
    return Path.home() / ".grok"


def _auth_path() -> Path:
    return grok_home() / "auth.json"


def _load_auth_file() -> dict[str, Any]:
    path = _auth_path()
    if not path.exists():
        raise RuntimeError("No Grok login found. Run grok login, then refresh.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Grok auth.json is not valid.")
    return data


def _iter_entries(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, entry)
        for name, entry in data.items()
        if isinstance(entry, dict) and (entry.get("key") or entry.get("access_token"))
    ]


def _token_expired(entry: dict[str, Any], skew_seconds: int = 120) -> bool:
    exp = entry.get("expires_at")
    if not exp:
        return False
    try:
        return parse_time(exp).timestamp() <= datetime.now(timezone.utc).timestamp() + skew_seconds
    except Exception:
        return True


def _pick_entry(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    entries = _iter_entries(data)
    if not entries:
        raise RuntimeError("Grok auth.json has no session token. Run grok login.")
    entries.sort(key=lambda item: (1 if _token_expired(item[1]) else 0, str(item[1].get("expires_at") or "")))
    return entries[0]


def _access_token(entry: dict[str, Any]) -> str:
    token = entry.get("key") or entry.get("access_token")
    if not token:
        raise RuntimeError("Grok auth.json has no session token. Run grok login.")
    return str(token)


def _token_endpoint(entry: dict[str, Any]) -> str:
    issuer = str(entry.get("oidc_issuer") or "https://auth.x.ai").rstrip("/")
    if issuer == "https://auth.x.ai":
        return DEFAULT_TOKEN_URL
    return f"{issuer}/oauth2/token"


def _save_auth_file(data: dict[str, Any]) -> None:
    path = _auth_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _refresh_cli_entry(data: dict[str, Any], name: str, entry: dict[str, Any]) -> str:
    refresh_token = entry.get("refresh_token")
    client_id = entry.get("oidc_client_id")
    if not refresh_token or not client_id:
        raise RuntimeError("Grok session expired. Run grok login, then refresh.")
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
    ).encode("utf-8")
    req = Request(_token_endpoint(entry), data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError("Grok session expired. Run grok login, then refresh.") from exc
    access = payload.get("access_token")
    if not access:
        raise RuntimeError("Grok session expired. Run grok login, then refresh.")
    entry["key"] = access
    if payload.get("refresh_token"):
        entry["refresh_token"] = payload["refresh_token"]
    expires_in = payload.get("expires_in")
    if expires_in:
        expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        entry["expires_at"] = expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    data[name] = entry
    _save_auth_file(data)
    return str(access)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _cents(value: Any) -> int | None:
    if isinstance(value, dict):
        raw = value.get("val")
        if raw is None:
            return None
        return int(raw)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def product_label(raw: Any, *, unified: bool) -> str:
    if raw is None or raw == "" or raw == 0 or raw == "0":
        # proto3 omits the default product enum. On the combined plan that
        # unnamed slice is Cursor usage; older payloads name every product.
        return "Cursor" if unified else "Other"
    text = str(raw).strip()
    return PRODUCT_LABELS.get(text, text)


def display_plan(name: str | None, unified: bool) -> str:
    text = (name or "").strip()
    if text.lower() in _GENERIC_PLANS:
        return "Included" if unified else (text or "Super Grok")
    return text


def _merge_products(rows: list[tuple[str, float]]) -> list[tuple[str, float]]:
    totals: dict[str, float] = {}
    order: list[str] = []
    for name, pct in rows:
        if name not in totals:
            order.append(name)
            totals[name] = 0.0
        totals[name] += pct
    merged = [(name, totals[name]) for name in order if totals[name] >= 0.05]
    merged.sort(key=lambda row: row[1], reverse=True)
    return merged


def parse_credits_payload(payload: dict[str, Any], fetched_at: datetime | None = None) -> SuperGrokSnapshot:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    used = config.get("creditUsagePercent")
    start = period.get("start") or config.get("billingPeriodStart")
    end = period.get("end") or config.get("billingPeriodEnd")
    if start is None or end is None:
        raise RuntimeError("Usage response is missing the weekly period")
    if used is None:
        cap = (config.get("onDemandCap") or {}).get("val") if isinstance(config.get("onDemandCap"), dict) else None
        spent = (config.get("onDemandUsed") or {}).get("val") if isinstance(config.get("onDemandUsed"), dict) else None
        if cap and cap > 0 and spent is not None:
            used = float(spent) / float(cap) * 100.0
        else:
            used = 0.0
    unified = bool(config.get("isUnifiedBillingUser") or config.get("is_unified_billing_user"))
    products: list[tuple[str, float]] = []
    for item in config.get("productUsage") or []:
        if not isinstance(item, dict):
            continue
        pct = item.get("usagePercent")
        if pct is None:
            continue
        products.append((product_label(item.get("product"), unified=unified), float(pct)))
    return SuperGrokSnapshot(
        used_percent=float(used),
        period_start=parse_time(start),
        period_end=parse_time(end),
        fetched_at=fetched_at or datetime.now(timezone.utc),
        products=_merge_products(products),
        unified=unified,
        plan_label="Included" if unified else "Super Grok",
        prepaid_cents=_cents(config.get("prepaidBalance") or config.get("prepaid_balance")),
        on_demand_used_cents=_cents(config.get("onDemandUsed") or config.get("on_demand_used")),
        on_demand_cap_cents=_cents(config.get("onDemandCap") or config.get("on_demand_cap")),
    )


def format_percent(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 0.05:
        return f"{rounded:.0f}%"
    return f"{value:.1f}%"


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    amount = abs(int(cents))
    return f"{sign}${amount // 100}.{amount % 100:02d}"


def format_products(products: list[tuple[str, float]]) -> str:
    if not products:
        return ""
    return " · ".join(f"{name} {format_percent(pct)}" for name, pct in products)


def assemble_pool(
    sand: UsageSnapshot | None,
    period: PeriodUsage | None,
    credits: SuperGrokSnapshot | None,
) -> SuperGrokSnapshot | None:
    """One weekly included pool, with a product breakdown when billing has one."""
    unified = bool((sand and sand.unified) or (credits and credits.unified))
    precise: list[float] = []
    if unified:
        if period is not None and period.total_percent is not None:
            precise.append(float(period.total_percent))
        if sand is not None:
            precise.append(float(sand.used_percent))
    elif period is not None and period.total_percent is not None and credits is None:
        precise.append(float(period.total_percent))
    elif sand is not None and credits is None:
        precise.append(float(sand.used_percent))

    if precise:
        used = max(precise)
    elif credits is not None:
        used = float(credits.used_percent)
    else:
        return None

    if period is not None and period.period_start and period.period_end and (unified or credits is None):
        start, end = period.period_start, period.period_end
    elif sand is not None and (unified or credits is None):
        start, end = sand.period_start, sand.period_end
    elif credits is not None:
        start, end = credits.period_start, credits.period_end
    elif sand is not None:
        start, end = sand.period_start, sand.period_end
    else:
        return None

    stamps = [item.fetched_at for item in (sand, credits) if item is not None]
    fetched_at = max(stamps) if stamps else datetime.now(timezone.utc)

    plan_source = None
    if sand is not None and sand.cursor_plan_name:
        plan_source = sand.cursor_plan_name
    elif credits is not None:
        plan_source = credits.plan_label
    elif sand is not None:
        plan_source = sand.plan_label

    additional = None
    if period is not None and period.api_percent is not None and period.api_percent >= 0.5:
        additional = float(period.api_percent)

    available = True
    if sand is not None and (unified or credits is None):
        available = sand.available

    return SuperGrokSnapshot(
        used_percent=used,
        period_start=start,
        period_end=end,
        fetched_at=fetched_at,
        products=list(credits.products) if credits is not None else [],
        unified=unified,
        plan_label=display_plan(plan_source, unified),
        additional_percent=additional,
        prepaid_cents=credits.prepaid_cents if credits is not None else None,
        on_demand_used_cents=credits.on_demand_used_cents if credits is not None else None,
        on_demand_cap_cents=credits.on_demand_cap_cents if credits is not None else None,
        available=available,
    )


def _billing_request(token: str) -> SuperGrokSnapshot:
    req = Request(CREDITS_URL, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-XAI-Token-Auth", "xai-grok-cli")
    req.add_header("Accept", "application/json")
    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise PermissionError(f"HTTP {exc.code}") from exc
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Usage HTTP {exc.code}: {body[:200]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Usage network error: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Usage response was not JSON")
    return parse_credits_payload(payload)


def fetch_supergrok() -> SuperGrokSnapshot:
    data = _load_auth_file()
    name, entry = _pick_entry(data)
    token = _access_token(entry)
    if _token_expired(entry):
        token = _refresh_cli_entry(data, name, entry)
    try:
        return _billing_request(token)
    except PermissionError:
        data = _load_auth_file()
        name, entry = _pick_entry(data)
        token = _refresh_cli_entry(data, name, entry)
        try:
            return _billing_request(token)
        except PermissionError as exc:
            raise RuntimeError("Grok session expired. Run grok login, then refresh.") from exc
