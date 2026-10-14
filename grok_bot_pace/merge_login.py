"""Sign in to Merge Gateway in a local Chrome window and store that session.

The tray app runs on this computer, so there is no server to push to. This
command opens Chrome or Edge, waits for the Google sign-in to finish, then
keeps the dashboard session in the app data folder (DPAPI-protected). The
browser profile stays on this computer, so the next run is usually a short
Google confirm.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, build_opener

from grok_bot_pace.auth import app_data_dir
from grok_bot_pace.merge_gateway import (
    MergeAuthError,
    authorize_url,
    exchange_cli_code,
    fetch_overview,
    refresh_cli_token,
    save_session,
)

LOGIN_URL = "https://api-gateway.merge.dev/sso/auth/google/login"
SIGN_IN_SECONDS = 300
TOKEN_SECONDS = 20
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def profile_dir() -> Path:
    return app_data_dir() / "merge-browser"


def _browser_candidates() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    program_files = Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files")
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
    return [
        program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
        program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        local / "Google" / "Chrome" / "Application" / "chrome.exe",
        program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]


def find_browser(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise RuntimeError(f"Browser not found: {explicit}")
        return str(path)
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _browser_candidates():
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("Chrome or Edge was not found. Install one, or pass --browser.")


def run(*, browser: str | None = None, data_dir: Path | None = None) -> None:
    binary = find_browser(browser)
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    print("Opening the browser. Sign in to Merge Gateway with Google.", flush=True)
    proc = subprocess.Popen(
        [
            binary,
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            LOGIN_URL,
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        port = _debugging_port(profile, proc)
        session = _wait_for_session(proc, port)
        tokens = _try_cli_token(port)
        if tokens:
            session.update(tokens)
            print("Merge accepted a refreshable sign-in.", flush=True)
        else:
            print("Using the browser session.", flush=True)
    finally:
        _stop(proc)
    save_session(data_dir or app_data_dir(), session)
    print("Merge Gateway session stored.", flush=True)


def _wait_for_session(proc: subprocess.Popen, port: int) -> dict:
    deadline = time.monotonic() + SIGN_IN_SECONDS
    last = "Timed out waiting for Google sign-in."
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("The sign-in window closed before Merge accepted the login.")
        try:
            cookies = _browser_cookies(port)
            if cookies:
                _snapshot, updated = fetch_overview({"cookies": cookies})
                return updated
        except MergeAuthError as exc:
            last = str(exc)
        except (OSError, RuntimeError) as exc:
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(last)


def _try_cli_token(port: int) -> dict | None:
    """Ask Merge's CLI login for a refresh token. Billing usually rejects it."""
    verifier = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    try:
        with _Loopback() as receiver:
            _navigate(port, authorize_url(receiver.redirect_uri, state, challenge))
            result = receiver.wait(TOKEN_SECONDS)
        if result is None or result.get("state") != state or not result.get("code"):
            return None
        tokens = exchange_cli_code(receiver.redirect_uri, str(result["code"]), verifier)
        refresh = tokens.get("refresh_token")
        if not refresh:
            return None
        refreshed = refresh_cli_token(str(refresh))
        access = refreshed.get("access_token")
        if not access:
            return None
        fetch_overview({"access_token": str(access)})
    except Exception:
        return None
    stored = {
        "access_token": str(access),
        "refresh_token": str(refreshed.get("refresh_token") or refresh),
    }
    expires_in = refreshed.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        from datetime import datetime, timedelta, timezone

        stored["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))).isoformat()
    return stored


def _browser_cookies(port: int) -> list[dict]:
    version = _debug_json(port, "/json/version")
    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("The browser did not offer a debugging connection.")
    cdp = CDP(ws_url)
    try:
        result = cdp.call("Network.getAllCookies")
    finally:
        cdp.close()
    return _pick_cookies(result.get("cookies") or [])


def _navigate(port: int, url: str) -> None:
    targets = _debug_json(port, "/json/list")
    page = next((item for item in targets if item.get("type") == "page" and item.get("webSocketDebuggerUrl")), None)
    if page is None:
        raise RuntimeError("The browser has no page to continue sign-in.")
    cdp = CDP(str(page["webSocketDebuggerUrl"]))
    try:
        cdp.call("Page.navigate", {"url": url})
    finally:
        cdp.close()


def _pick_cookies(raw: list) -> list[dict]:
    chosen: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if not name or not value or "merge.dev" not in domain.lower():
            continue
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(item.get("path") or "/"),
            "secure": bool(item.get("secure", True)),
            "httpOnly": bool(item.get("httpOnly", False)),
        }
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = float(expires)
        chosen.append(cookie)
    chosen.sort(key=lambda cookie: (not cookie["httpOnly"], cookie["name"]))
    return chosen[:40]


def _debugging_port(profile: Path, proc: subprocess.Popen) -> int:
    path = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("The browser exited before the sign-in window opened.")
        if path.is_file():
            line = path.read_text(encoding="utf-8").splitlines()
            if line and line[0].isdigit():
                return int(line[0])
        time.sleep(0.2)
    raise RuntimeError("The browser did not open a debugging port.")


def _debug_json(port: int, path: str):
    req = Request(f"http://127.0.0.1:{port}{path}")
    try:
        with build_opener().open(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "null")
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not reach the browser.") from exc
    if not isinstance(payload, (dict, list)):
        raise RuntimeError("The browser returned an unexpected debugging response.")
    return payload


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


class CDP:
    def __init__(self, ws_url: str) -> None:
        parsed = urlparse(ws_url)
        if parsed.hostname is None or parsed.port is None:
            raise RuntimeError("The browser returned an invalid debugging address.")
        self._sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        self._sock.settimeout(10)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("The browser closed the debugging connection.")
            buf += chunk
        header, rest = buf.split(b"\r\n\r\n", 1)
        status_line = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise RuntimeError("Could not connect to the browser.")
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if accept.encode() not in header:
            raise RuntimeError("The browser debugging handshake did not match.")
        self._buf = rest
        self._id = 0

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        ident = self._id
        payload = json.dumps({"id": ident, "method": method, "params": params or {}}).encode()
        self._sock.sendall(_client_frame(payload))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = self._read_json()
            if message.get("id") != ident:
                continue
            if "error" in message:
                error = message["error"]
                text = error.get("message") if isinstance(error, dict) else "Browser debugging call failed."
                raise RuntimeError(text or "Browser debugging call failed.")
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        raise RuntimeError("Browser debugging timed out.")

    def _read_json(self) -> dict:
        while True:
            opcode, data = self._read_frame()
            if opcode == 9:
                self._sock.sendall(_client_frame(data, opcode=0xA))
                continue
            if opcode == 8:
                raise RuntimeError("The browser closed the debugging connection.")
            if opcode == 1:
                parsed = json.loads(data.decode("utf-8") or "{}")
                if not isinstance(parsed, dict):
                    raise RuntimeError("The browser returned an unexpected debugging message.")
                return parsed

    def _read_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > 8_000_000:
            raise RuntimeError("Browser debugging response was too large.")
        mask_key = self._read_exact(4) if second & 0x80 else b""
        payload = self._read_exact(length)
        if mask_key:
            payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise RuntimeError("Browser debugging timed out.") from exc
            if not chunk:
                raise RuntimeError("The browser closed the debugging connection.")
            self._buf += chunk
        data, self._buf = self._buf[:size], self._buf[size:]
        return data


def _client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    header.extend(masked)
    return bytes(header)


class _Loopback:
    def __init__(self) -> None:
        self._server = HTTPServer(("127.0.0.1", 0), _Callback)
        self._server.done = threading.Event()  # type: ignore[attr-defined]
        self._server.result = {}  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def redirect_uri(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/callback"

    def __enter__(self) -> _Loopback:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait(self, timeout: float) -> dict | None:
        if not self._server.done.wait(timeout):  # type: ignore[attr-defined]
            return None
        return dict(self._server.result)  # type: ignore[attr-defined]


class _Callback(BaseHTTPRequestHandler):
    server: HTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = parse_qs(parsed.query)
        self.server.result = {  # type: ignore[attr-defined]
            "code": (query.get("code") or [None])[0],
            "state": (query.get("state") or [None])[0],
            "error": (query.get("error") or [None])[0],
        }
        body = b"<p>Merge sign-in finished. You can close this window.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.done.set()  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        return


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sign in to Merge Gateway and store the session.")
    parser.add_argument("--browser", help="Chrome or Edge executable. The default is the installed one.")
    args = parser.parse_args()
    try:
        run(browser=args.browser)
    except Exception as exc:
        text = str(exc).strip() or exc.__class__.__name__
        print(text, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
