import json
from datetime import datetime, timezone
from http.cookiejar import Cookie
from io import BytesIO
from urllib.error import HTTPError

import pytest

from grok_bot_pace import merge_gateway
from grok_bot_pace.merge_gateway import (
    EXPIRED,
    NO_SESSION,
    MergeAuthError,
    calendar_month,
    clear_session,
    fetch_overview,
    format_usd,
    load_session,
    month_window,
    normalize_session,
    parse_overview,
    save_session,
)
from grok_bot_pace.pace import compute_pace, status_label

OVERVIEW = {
    "tier": "pro",
    "plan_credit_amount_usd": 10,
    "spend_from_plan_credit_usd": 4.2,
    "plan_credit_remaining_usd": 5.8,
    "spend_from_balance_usd": 1.25,
    "plan_credit_resets_at": "2026-11-01T00:00:00Z",
}

NOW = datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)


def _cookie(**overrides) -> dict:
    item = {
        "name": "session",
        "value": "super-secret",
        "domain": ".merge.dev",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    }
    item.update(overrides)
    return item


class _Body:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._raw = json.dumps(payload).encode()
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_parse_monthly_grant() -> None:
    snap = parse_overview(OVERVIEW, fetched_at=NOW, now=NOW)
    assert snap.used_percent == pytest.approx(42.0)
    assert snap.amount_usd == 10
    assert snap.spent_usd == pytest.approx(4.2)
    assert snap.purchased_spent_usd == pytest.approx(1.25)
    assert snap.period_start == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert snap.period_end == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert snap.available is True
    assert format_usd(snap.spent_usd) == "$4.20"
    assert format_usd(snap.amount_usd) == "$10"


def test_month_window_treats_midnight_on_the_first_as_the_previous_month() -> None:
    start, end = month_window(datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert start == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 1, tzinfo=timezone.utc)
    start, end = month_window(datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc))
    assert start == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)
    start, end = calendar_month(datetime(2026, 12, 2, tzinfo=timezone.utc))
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_future_grant_is_not_a_pace_window() -> None:
    with pytest.raises(RuntimeError, match="start November 2026"):
        parse_overview(
            {"plan_credit_amount_usd": 10, "plan_credit_starts_ym": "2026-11"},
            now=NOW,
        )


def test_missing_grant_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="No monthly Merge credit"):
        parse_overview({"tier": "free"}, now=NOW)


def test_expired_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req, timeout=20, jar=None):
        raise HTTPError(req.full_url, 401, "unauthorized", hdrs=None, fp=BytesIO(b"{}"))

    monkeypatch.setattr(merge_gateway, "_open", boom)
    with pytest.raises(MergeAuthError) as caught:
        fetch_overview({"cookies": [_cookie()]})
    assert caught.value.status == 401
    assert "expired" in str(caught.value)


def test_rotated_cookie_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok(req, timeout=20, jar=None):
        assert jar is not None
        jar.set_cookie(
            Cookie(
                version=0,
                name="session",
                value="rotated",
                port=None,
                port_specified=False,
                domain=".merge.dev",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None},
                rfc2109=False,
            )
        )
        return _Body(OVERVIEW)

    monkeypatch.setattr(merge_gateway, "_open", ok)
    snap, updated = fetch_overview({"cookies": [_cookie()]})
    assert snap.spent_usd == pytest.approx(4.2)
    assert updated["cookies"][0]["value"] == "rotated"
    assert updated["cookies"][0]["httpOnly"] is True


def test_bearer_is_used_when_billing_accepts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok(req, timeout=20, jar=None):
        assert req.get_header("Authorization") == "Bearer tok"
        assert jar is None
        return _Body(OVERVIEW)

    monkeypatch.setattr(merge_gateway, "_open", ok)
    snap, updated = fetch_overview({"access_token": "tok", "cookies": [_cookie()]})
    assert snap.used_percent == pytest.approx(42.0)
    assert updated["access_token"] == "tok"
    assert updated["cookies"][0]["value"] == "super-secret"


def test_session_rejects_other_domains() -> None:
    with pytest.raises(ValueError, match="merge.dev"):
        normalize_session({"cookies": [_cookie(domain="example.com")]})


def test_merge_pace_uses_the_month(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(merge_gateway, "dpapi_protect", lambda data: data)
    monkeypatch.setattr(merge_gateway, "dpapi_unprotect", lambda data: data)
    save_session(tmp_path, {"cookies": [_cookie()]})
    stored = load_session(tmp_path)
    assert stored is not None
    assert stored["cookies"][0]["value"] == "super-secret"

    snap = parse_overview(OVERVIEW, fetched_at=NOW, now=NOW)
    pace = compute_pace(snap.as_usage(), now=NOW)
    assert pace.plan_label == "Pro"
    assert status_label(pace, period="month") != "On track for the week"
    assert status_label(pace) == "On track for the week" or pace.status != "on_track"

    clear_session(tmp_path)
    assert load_session(tmp_path) is None


def test_no_session_constant() -> None:
    assert "No Merge Gateway session" in NO_SESSION
    assert "expired" in EXPIRED.lower()


def test_pick_cookies_keeps_only_merge_dev() -> None:
    from grok_bot_pace.merge_login import _pick_cookies

    picked = _pick_cookies(
        [
            {"name": "session", "value": "v", "domain": ".merge.dev", "httpOnly": True},
            {"name": "session", "value": "v", "domain": "example.com"},
            {"name": "empty", "value": "", "domain": ".merge.dev"},
            {"name": "token", "value": "t", "domain": "api-gateway.merge.dev", "expires": 1_800_000_000.0},
        ]
    )
    assert [c["name"] for c in picked] == ["session", "token"]
    assert picked[1]["expires"] == 1_800_000_000.0
    assert picked[0]["httpOnly"] is True


def test_read_session_errors_without_cookies(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from grok_bot_pace import merge_login

    class _Proc:
        pid = 1

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(merge_login, "_debugging_port", lambda profile, proc: 1)
    monkeypatch.setattr(merge_login, "_browser_cookies", lambda port: [])
    monkeypatch.setattr(merge_login.subprocess, "Popen", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError, match="No Merge Gateway session was found"):
        merge_login._read_session("chrome.exe", tmp_path)
