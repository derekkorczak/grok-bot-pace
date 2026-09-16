"""Load a Cursor/Grok Bot session from local app data. Tokens stay in memory."""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from grok_bot_pace.crypto_win import decrypt_chromium_v10, dpapi_protect, dpapi_unprotect

CURSOR_OAUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
OAUTH_TOKEN_URL = "https://api2.cursor.sh/oauth/token"
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None = None
    source: str = ""


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    path = base / "GrokBotPace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _roaming() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def _jwt_exp(token: str) -> float | None:
    try:
        payload = json.loads(_b64url(token.split(".")[1]))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def _b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _extract_jwt(raw: str) -> str:
    match = JWT_RE.search(raw)
    if not match:
        raise ValueError("decrypted value is not a JWT")
    return match.group(0)


def _token_usable(token: str, skew_seconds: int = 120) -> bool:
    exp = _jwt_exp(token)
    if exp is None:
        return bool(token)
    return exp - time.time() > skew_seconds


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:240]}") from exc
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def refresh_access_token(refresh_token: str) -> Tokens:
    body = _post_json(
        OAUTH_TOKEN_URL,
        {
            "client_id": CURSOR_OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    access = body.get("access_token")
    if not access:
        raise RuntimeError("token refresh returned no access_token")
    return Tokens(
        access_token=access,
        refresh_token=body.get("refresh_token") or refresh_token,
        source="refresh",
    )


def _load_cached() -> Tokens | None:
    path = app_data_dir() / "tokens.dpapi"
    if not path.exists():
        return None
    try:
        payload = json.loads(dpapi_unprotect(path.read_bytes()).decode("utf-8"))
        access = payload.get("access_token")
        if not access:
            return None
        return Tokens(
            access_token=access,
            refresh_token=payload.get("refresh_token"),
            source="cache",
        )
    except Exception:
        return None


def save_cached(tokens: Tokens) -> None:
    blob = json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
        }
    ).encode("utf-8")
    (app_data_dir() / "tokens.dpapi").write_bytes(dpapi_protect(blob))


def _decrypt_electron_string(b64_value: str, aes_key: bytes) -> str:
    raw = base64.b64decode(b64_value)
    return decrypt_chromium_v10(raw, aes_key).decode("utf-8")


def load_grok_bot_tokens() -> Tokens | None:
    roaming = _roaming() / "Grok Bot"
    secrets_path = roaming / "sand-secrets.json"
    local_state_path = roaming / "Local State"
    if not secrets_path.exists() or not local_state_path.exists():
        return None
    local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
    enc_key = base64.b64decode(local_state.get("os_crypt", {}).get("encrypted_key") or "")
    if not enc_key:
        return None
    if enc_key.startswith(b"DPAPI"):
        enc_key = enc_key[5:]
    aes_key = dpapi_unprotect(enc_key)
    secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
    accounts = json.loads(secrets["cursor-accounts"])
    active = accounts.get("active")
    acct = (accounts.get("accounts") or {}).get(active) if active else None
    if not isinstance(acct, dict):
        return None
    access_raw = _decrypt_electron_string(acct["cursor-access-token"], aes_key)
    refresh_raw = None
    if acct.get("cursor-refresh-token"):
        try:
            refresh_raw = _decrypt_electron_string(acct["cursor-refresh-token"], aes_key)
        except Exception:
            refresh_raw = None
    access = _extract_jwt(access_raw)
    refresh = None
    if refresh_raw:
        try:
            refresh = _extract_jwt(refresh_raw)
        except ValueError:
            refresh = refresh_raw.strip() or None
    return Tokens(access_token=access, refresh_token=refresh, source="grok-bot")


def load_cursor_tokens() -> Tokens | None:
    db_path = _roaming() / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not db_path.exists():
        return None
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT key, value FROM ItemTable WHERE key IN (?, ?)",
            ("cursorAuth/accessToken", "cursorAuth/refreshToken"),
        )
        rows = {k: v.decode("utf-8") if isinstance(v, bytes) else v for k, v in cur.fetchall()}
    finally:
        con.close()
    access = (rows.get("cursorAuth/accessToken") or "").strip().strip('"')
    refresh = (rows.get("cursorAuth/refreshToken") or "").strip().strip('"')
    if not access and not refresh:
        return None
    return Tokens(access_token=access, refresh_token=refresh or None, source="cursor")


def load_tokens() -> Tokens:
    errors: list[str] = []
    candidates = [load_grok_bot_tokens(), _load_cached(), load_cursor_tokens()]
    seen: list[Tokens] = [c for c in candidates if c is not None]

    for tokens in seen:
        if _token_usable(tokens.access_token):
            return tokens

    for tokens in seen:
        if not tokens.refresh_token:
            continue
        try:
            refreshed = refresh_access_token(tokens.refresh_token)
            refreshed.source = tokens.source + "+refresh"
            save_cached(refreshed)
            return refreshed
        except Exception as exc:
            errors.append(f"{tokens.source}: {exc}")

    detail = "; ".join(errors) if errors else "no local Grok Bot or Cursor session found"
    raise RuntimeError(
        "Could not sign in to Grok Bot usage. Open Grok Bot and sign in, then retry. "
        + detail
    )
