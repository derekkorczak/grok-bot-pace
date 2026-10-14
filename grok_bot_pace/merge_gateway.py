"""Merge Gateway monthly Pro credit from the signed-in billing overview.

The public Management API reports spend, not the complimentary monthly grant.
That grant is on ``GET /api/billing/overview``, which accepts the dashboard
session cookie. A management-scoped CLI token is tried when one was saved,
then the cookie jar is used.

The session is kept in ``merge-session.dpapi`` next to the other app data, so
the dashboard cookie never has to be pasted by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from grok_bot_pace.api import UsageSnapshot, parse_time
from grok_bot_pace.crypto_win import dpapi_protect, dpapi_unprotect

OVERVIEW_URL = "https://api-gateway.merge.dev/api/billing/overview"
TOKEN_URL = "https://api-gateway.merge.dev/cli/auth/token"
AUTHORIZE_URL = "https://api-gateway.merge.dev/cli/auth/authorize"
CLIENT_ID = "mgw-cli"
SCOPE = "manage_keys"
SESSION_FILE = "merge-session.dpapi"
USER_AGENT = "grok-bot-pace"
EXPIRED = "Merge Gateway session expired. Choose Sign in to Merge Gateway from the menu."
NO_SESSION = "No Merge Gateway session. Choose Sign in to Merge Gateway from the menu."


class MergeAuthError(RuntimeError):
    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class MergeSnapshot:
    used_percent: float
    period_start: datetime
    period_end: datetime
    available: bool
    amount_usd: float
    spent_usd: float
    purchased_spent_usd: float
    fetched_at: datetime

    def as_usage(self) -> UsageSnapshot:
        return UsageSnapshot(
            used_percent=self.used_percent,
            period_start=self.period_start,
            period_end=self.period_end,
            available=self.available,
            plan_label="Pro",
            fetched_at=self.fetched_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "available": self.available,
            "amount_usd": self.amount_usd,
            "spent_usd": self.spent_usd,
            "purchased_spent_usd": self.purchased_spent_usd,
            "fetched_at": self.fetched_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MergeSnapshot:
        return cls(
            used_percent=float(data["used_percent"]),
            period_start=parse_time(data["period_start"]),
            period_end=parse_time(data["period_end"]),
            available=bool(data.get("available", True)),
            amount_usd=float(data["amount_usd"]),
            spent_usd=float(data["spent_usd"]),
            purchased_spent_usd=float(data.get("purchased_spent_usd") or 0),
            fetched_at=parse_time(data["fetched_at"]),
        )


def format_usd(amount: float) -> str:
    if abs(amount - round(amount)) < 0.005:
        return f"${amount:.0f}"
    return f"${amount:.2f}"


def month_window(resets_at: datetime) -> tuple[datetime, datetime]:
    """The grant month that ends at ``resets_at``.

    A reset exactly at 00:00 UTC on the 1st is the exclusive end of the
    previous month. Any other timestamp belongs to its own UTC month.
    """
    end = resets_at.astimezone(timezone.utc)
    boundary = (
        end.day == 1
        and end.hour == 0
        and end.minute == 0
        and end.second == 0
        and end.microsecond == 0
    )
    if boundary:
        if end.month == 1:
            start = datetime(end.year - 1, 12, 1, tzinfo=timezone.utc)
        else:
            start = datetime(end.year, end.month - 1, 1, tzinfo=timezone.utc)
        return start, end
    return datetime(end.year, end.month, 1, tzinfo=timezone.utc), end


def calendar_month(now: datetime) -> tuple[datetime, datetime]:
    current = now.astimezone(timezone.utc)
    start = datetime(current.year, current.month, 1, tzinfo=timezone.utc)
    if current.month == 12:
        end = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
    return start, end


def parse_overview(
    payload: dict[str, Any],
    fetched_at: datetime | None = None,
    now: datetime | None = None,
) -> MergeSnapshot:
    if not isinstance(payload, dict):
        raise RuntimeError("Merge billing response was not an object")
    fetched = fetched_at or datetime.now(timezone.utc)
    current = now or fetched
    starts = payload.get("plan_credit_starts_ym")
    if isinstance(starts, str) and _future_month(starts, current):
        raise RuntimeError(f"Monthly credits start {_month_label(starts)}.")
    amount = _number(payload.get("plan_credit_amount_usd"))
    if amount is None or amount <= 0:
        raise RuntimeError("No monthly Merge credit on this account.")
    remaining = _number(payload.get("plan_credit_remaining_usd"))
    spent = _number(payload.get("spend_from_plan_credit_usd"))
    if spent is None and remaining is not None:
        spent = max(0.0, amount - remaining)
    if spent is None:
        spent = 0.0
    purchased = _number(payload.get("spend_from_balance_usd")) or 0.0
    resets = payload.get("plan_credit_resets_at")
    if resets:
        start, end = month_window(parse_time(resets))
    else:
        start, end = calendar_month(current)
    used = max(0.0, min(100.0, spent / amount * 100.0))
    return MergeSnapshot(
        used_percent=used,
        period_start=start,
        period_end=end,
        available=used < 100.0,
        amount_usd=amount,
        spent_usd=spent,
        purchased_spent_usd=max(0.0, purchased),
        fetched_at=fetched,
    )


def session_path(data_dir: Path) -> Path:
    return data_dir / SESSION_FILE


def load_session(data_dir: Path) -> dict[str, Any] | None:
    path = session_path(data_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(dpapi_unprotect(path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return normalize_session(raw)
    except ValueError:
        return None


def save_session(data_dir: Path, session: dict[str, Any]) -> None:
    cleaned = normalize_session(session)
    path = session_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(cleaned).encode("utf-8")
    tmp = path.with_suffix(".dpapi.tmp")
    tmp.write_bytes(dpapi_protect(blob))
    tmp.replace(path)


def clear_session(data_dir: Path) -> None:
    session_path(data_dir).unlink(missing_ok=True)


def normalize_session(payload: dict[str, Any]) -> dict[str, Any]:
    raw_cookies = payload.get("cookies") or []
    if not isinstance(raw_cookies, list):
        raise ValueError("Merge session cookies are invalid.")
    if len(raw_cookies) > 40:
        raise ValueError("Merge session has too many cookies.")
    cookies: list[dict[str, Any]] = []
    for raw in raw_cookies:
        if not isinstance(raw, dict):
            raise ValueError("Merge session cookies are invalid.")
        domain = str(raw.get("domain") or "").strip()
        if not _merge_domain(domain):
            raise ValueError("Merge session cookies must be for merge.dev.")
        name = str(raw.get("name") or "").strip()
        value = str(raw.get("value") or "")
        path = str(raw.get("path") or "/").strip() or "/"
        if not name or any(ch in name + value + path for ch in "\r\n\x00"):
            raise ValueError("Merge session cookies are invalid.")
        if len(name) > 256 or len(value) > 8192 or len(path) > 256:
            raise ValueError("Merge session cookies are invalid.")
        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": bool(raw.get("secure", True)),
            "httpOnly": bool(raw.get("httpOnly", False)),
        }
        expires = raw.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            item["expires"] = float(expires)
        cookies.append(item)
    access = _secret(payload.get("access_token"))
    refresh = _secret(payload.get("refresh_token"))
    expires_at = _secret(payload.get("expires_at"))
    if not cookies and not access:
        raise ValueError("Sign-in did not include a Merge session.")
    session: dict[str, Any] = {"cookies": cookies}
    if access:
        session["access_token"] = access
    if refresh:
        session["refresh_token"] = refresh
    if expires_at:
        session["expires_at"] = expires_at
    return session


def authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": SCOPE,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_cli_code(redirect_uri: str, code: str, verifier: str) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
        }
    )


def refresh_cli_token(refresh_token: str) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    )


def fetch_overview(session: dict[str, Any]) -> tuple[MergeSnapshot, dict[str, Any]]:
    """Return the monthly grant and the session, including any rotated cookies."""
    current = normalize_session(session)
    bearer_error: MergeAuthError | None = None
    token = current.get("access_token")
    if token:
        try:
            snapshot = _overview_bearer(str(token))
            return snapshot, current
        except MergeAuthError as exc:
            bearer_error = exc
            refreshed = _try_refresh(current)
            if refreshed is not None:
                current = refreshed
                try:
                    snapshot = _overview_bearer(str(refreshed["access_token"]))
                    return snapshot, refreshed
                except MergeAuthError as retry_exc:
                    bearer_error = retry_exc
    if not current.get("cookies"):
        raise bearer_error or MergeAuthError(EXPIRED, status=401)
    jar = _jar(current["cookies"])
    status, raw = _overview_request(jar)
    if status in {401, 403}:
        raise MergeAuthError(EXPIRED, status=status)
    if status >= 400:
        raise RuntimeError(f"Merge billing failed: HTTP {status}")
    updated = dict(current)
    updated["cookies"] = _dump_jar(jar) or list(current["cookies"])
    return _parse_body(raw), updated


def _overview_bearer(token: str) -> MergeSnapshot:
    req = Request(
        OVERVIEW_URL,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        method="GET",
    )
    status, raw = _read(req)
    if status in {401, 403}:
        raise MergeAuthError(EXPIRED, status=status)
    if status >= 400:
        raise RuntimeError(f"Merge billing failed: HTTP {status}")
    return _parse_body(raw)


def _overview_request(jar: CookieJar) -> tuple[int, bytes]:
    req = Request(
        OVERVIEW_URL,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    return _read(req, jar=jar)


def _try_refresh(session: dict[str, Any]) -> dict[str, Any] | None:
    refresh = session.get("refresh_token")
    if not refresh:
        return None
    try:
        tokens = refresh_cli_token(str(refresh))
    except (MergeAuthError, RuntimeError):
        return None
    access = tokens.get("access_token")
    if not access:
        return None
    updated = dict(session)
    updated["access_token"] = str(access)
    if tokens.get("refresh_token"):
        updated["refresh_token"] = str(tokens["refresh_token"])
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expiry = datetime.now(timezone.utc).timestamp() + float(expires_in)
        updated["expires_at"] = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
    return updated


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    req = Request(
        TOKEN_URL,
        data=urlencode(form).encode(),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    status, raw = _read(req)
    if status in {401, 403}:
        raise MergeAuthError(EXPIRED, status=status)
    if status >= 400:
        raise RuntimeError(f"Merge token request failed: HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Merge token response was not JSON") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("Merge token response did not include an access token")
    return payload


def _read(req: Request, jar: CookieJar | None = None) -> tuple[int, bytes]:
    try:
        with _open(req, timeout=20, jar=jar) as resp:
            status = getattr(resp, "status", None) or resp.getcode() or 200
            return int(status), resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def _open(req: Request, timeout: int = 20, jar: CookieJar | None = None):
    if jar is None:
        return build_opener().open(req, timeout=timeout)
    return build_opener(HTTPCookieProcessor(jar)).open(req, timeout=timeout)


def _parse_body(raw: bytes) -> MergeSnapshot:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Merge billing response was not JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Merge billing response was not JSON")
    return parse_overview(payload)


def _jar(cookies: list[dict[str, Any]]) -> CookieJar:
    jar = CookieJar()
    for raw in cookies:
        expires = raw.get("expires")
        rest = {"HttpOnly": None} if raw.get("httpOnly") else {}
        domain = str(raw.get("domain") or "")
        jar.set_cookie(
            Cookie(
                version=0,
                name=str(raw["name"]),
                value=str(raw["value"]),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=str(raw.get("path") or "/"),
                path_specified=True,
                secure=bool(raw.get("secure", True)),
                expires=int(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
                discard=not isinstance(expires, (int, float)) or expires <= 0,
                comment=None,
                comment_url=None,
                rest=rest,
                rfc2109=False,
            )
        )
    return jar


def _dump_jar(jar: CookieJar) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for cookie in jar:
        item: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value or "",
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
        }
        if cookie.expires:
            item["expires"] = float(cookie.expires)
        if item["value"] and _merge_domain(cookie.domain):
            cookies.append(item)
    return cookies


def _merge_domain(domain: str) -> bool:
    host = domain.lstrip(".").lower().split(":", 1)[0]
    return host == "merge.dev" or host.endswith(".merge.dev")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _secret(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ch in text for ch in "\r\n\x00") or len(text) > 8000:
        return None
    return text


def _future_month(starts_ym: str, now: datetime) -> bool:
    parts = starts_ym.split("-")
    if len(parts) != 2:
        return False
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if month < 1 or month > 12:
        return False
    current = now.astimezone(timezone.utc)
    return (year, month) > (current.year, current.month)


def _month_label(starts_ym: str) -> str:
    year, month = (int(part) for part in starts_ym.split("-", 1))
    return datetime(year, month, 1, tzinfo=timezone.utc).strftime("%B %Y")
