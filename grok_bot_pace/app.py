"""Tk + tray UI for Grok Bot weekly pace."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tempfile
import winreg
from pathlib import Path
from datetime import datetime, timezone

import tkinter as tk
from tkinter import font as tkfont
from PIL import ImageTk

from grok_bot_pace.api import UsageSnapshot, fetch_usage
from grok_bot_pace.auth import Tokens, app_data_dir, load_tokens, refresh_access_token, save_cached
from grok_bot_pace.icons import (
    badge_ico_bytes,
    badge_image,
    badge_label,
    enable_dpi_awareness,
    image_to_hicon,
    status_color,
    system_icon_sizes,
)
from grok_bot_pace.pace import Pace, compute_pace, format_hours, status_label, tooltip_text
from grok_bot_pace.supergrok import SuperGrokSnapshot, fetch_supergrok, format_products

BG = "#0e0f12"
CARD = "#17181c"
TEXT = "#f2f2f2"
MUTED = "#9aa0a6"
BAR_BG = "#2a2d33"
REFRESH_MS = 5 * 60 * 1000
TICK_MS = 15_000
WINDOW_W, WINDOW_H = 340, 420


def _settings_path() -> Path:
    return app_data_dir() / "settings.json"


def _cache_path() -> Path:
    return app_data_dir() / "last_usage.json"


def _sg_cache_path() -> Path:
    return app_data_dir() / "last_supergrok.json"


def load_settings() -> dict:
    path = _settings_path()
    if not path.exists():
        return {"always_on_top": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"always_on_top": True}
    except Exception:
        return {"always_on_top": True}


def save_settings(data: dict) -> None:
    _settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_cached_snapshot() -> UsageSnapshot | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        return UsageSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_cached_snapshot(snapshot: UsageSnapshot) -> None:
    _cache_path().write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")


def load_cached_supergrok() -> SuperGrokSnapshot | None:
    path = _sg_cache_path()
    if not path.exists():
        return None
    try:
        return SuperGrokSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_cached_supergrok(snapshot: SuperGrokSnapshot) -> None:
    _sg_cache_path().write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def pythonw_path() -> Path:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        sibling = exe.with_name("pythonw.exe")
        if sibling.exists():
            return sibling
    return exe


STARTUP_RUN_NAME = "Grok Bot Pace"
STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_shortcut_path() -> Path:
    roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Grok Bot Pace.lnk"


def launcher_vbs_path() -> Path:
    return app_data_dir() / "launch.vbs"


def _vbs_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _run_key(access: int):
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, access)


def is_startup_enabled() -> bool:
    if startup_shortcut_path().exists():
        return True
    try:
        with _run_key(winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, STARTUP_RUN_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _write_launcher() -> Path:
    pythonw = pythonw_path()
    workdir = project_root()
    run_cmd = f'"{pythonw}" -m grok_bot_pace'
    script = (
        "Set sh = CreateObject(\"WScript.Shell\")\r\n"
        f"sh.CurrentDirectory = {_vbs_quote(str(workdir))}\r\n"
        f"sh.Run {_vbs_quote(run_cmd)}, 0, False\r\n"
    )
    path = launcher_vbs_path()
    path.write_text(script, encoding="utf-8")
    return path


def _create_startup_shortcut() -> None:
    lnk = startup_shortcut_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    pythonw = pythonw_path()
    workdir = project_root()
    maker = app_data_dir() / "_mkshortcut.vbs"
    maker.write_text(
        "Set sh = CreateObject(\"WScript.Shell\")\r\n"
        f"Set l = sh.CreateShortcut({_vbs_quote(str(lnk))})\r\n"
        f"l.TargetPath = {_vbs_quote(str(pythonw))}\r\n"
        "l.Arguments = \"-m grok_bot_pace\"\r\n"
        f"l.WorkingDirectory = {_vbs_quote(str(workdir))}\r\n"
        "l.WindowStyle = 7\r\n"
        "l.Description = \"Grok Bot weekly pace\"\r\n"
        "l.Save\r\n",
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            ["wscript.exe", "//B", "//Nologo", str(maker)],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not lnk.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(detail or "could not create Startup shortcut")
    finally:
        maker.unlink(missing_ok=True)


def set_startup(enabled: bool) -> None:
    lnk = startup_shortcut_path()
    if not enabled:
        if lnk.exists():
            lnk.unlink()
        try:
            with _run_key(winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, STARTUP_RUN_NAME)
        except FileNotFoundError:
            pass
        return

    launcher = _write_launcher()
    command = f'wscript.exe //B //Nologo "{launcher}"'
    with _run_key(winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, STARTUP_RUN_NAME, 0, winreg.REG_SZ, command)
    _create_startup_shortcut()


def grok_bot_exe() -> Path | None:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    candidate = local / "Programs" / "Grok Bot" / "Grok Bot.exe"
    return candidate if candidate.exists() else None


def set_startup(enabled: bool) -> None:
    path = startup_shortcut_path()
    if not enabled:
        if path.exists():
            path.unlink()
        return
    target = pythonw_path()
    workdir = project_root()
    script = (
        f'$s = New-Object -ComObject WScript.Shell; '
        f'$l = $s.CreateShortcut({str(path)!r}); '
        f'$l.TargetPath = {str(target)!r}; '
        f'$l.Arguments = "-m grok_bot_pace"; '
        f'$l.WorkingDirectory = {str(workdir)!r}; '
        f'$l.WindowStyle = 7; '
        f'$l.Description = "Grok Bot weekly pace"; '
        f'$l.Save()'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


def work_area() -> tuple[int, int, int, int]:
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


def enable_dark_titlebar(hwnd: int) -> None:
    import ctypes

    value = ctypes.c_int(1)
    for attr in (20, 19):
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            )
        except Exception:
            pass


class PaceApp:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.tokens: Tokens | None = None
        self.snapshot: UsageSnapshot | None = load_cached_snapshot()
        self.pace: Pace | None = (
            compute_pace(self.snapshot, stale=True) if self.snapshot is not None else None
        )
        self.sg_snapshot: SuperGrokSnapshot | None = load_cached_supergrok()
        self.sg_pace: Pace | None = (
            compute_pace(self.sg_snapshot.as_usage(), stale=True)
            if self.sg_snapshot is not None
            else None
        )
        self.error: str | None = None
        self.sg_error: str | None = None
        self._refreshing = False
        self._icon = None
        self._icon_photos: list[ImageTk.PhotoImage] = []
        self._hicons: list[int] = []
        self._ico_path = Path(tempfile.gettempdir()) / "grok-bot-pace.ico"

        self.root = tk.Tk()
        self.root.title("Grok Bot Pace")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        if self.settings.get("always_on_top", True):
            self.root.attributes("-topmost", True)

        self._build()
        self._place_window()
        self.root.after(80, self._after_map)
        self.root.after(200, self.refresh_async)
        self.root.after(TICK_MS, self._tick)
        self.root.after(REFRESH_MS, self._auto_refresh)

    def _build(self) -> None:
        pad = tk.Frame(self.root, bg=BG)
        pad.pack(fill="both", expand=True, padx=16, pady=(16, 18))

        header = tk.Frame(pad, bg=BG)
        header.pack(fill="x")
        self.brand = tk.Label(header, text="Grok Bot", fg=TEXT, bg=BG, font=_ui_font(11, bold=True))
        self.brand.pack(side="left")
        self.plan = tk.Label(header, text="", fg=MUTED, bg=BG, font=_ui_font(9))
        self.plan.pack(side="right")

        numbers = tk.Frame(pad, bg=BG)
        numbers.pack(fill="x", pady=(12, 6))
        left = tk.Frame(numbers, bg=BG)
        left.pack(side="left")
        self.used_value = tk.Label(left, text="--%", fg=TEXT, bg=BG, font=_ui_font(28, bold=True))
        self.used_value.pack(anchor="w")
        tk.Label(left, text="used", fg=MUTED, bg=BG, font=_ui_font(9)).pack(anchor="w")

        right = tk.Frame(numbers, bg=BG)
        right.pack(side="right")
        self.expected_value = tk.Label(right, text="--%", fg=TEXT, bg=BG, font=_ui_font(28, bold=True))
        self.expected_value.pack(anchor="e")
        tk.Label(right, text="should be", fg=MUTED, bg=BG, font=_ui_font(9)).pack(anchor="e")

        self.bar = tk.Canvas(pad, width=308, height=16, bg=BAR_BG, highlightthickness=0, bd=0)
        self.bar.pack(fill="x", pady=(4, 6))
        tk.Label(
            pad,
            text="Fill is actual usage.  The line is an even weekly pace.",
            fg=MUTED,
            bg=BG,
            font=_ui_font(8),
        ).pack(anchor="w")

        meta = tk.Frame(pad, bg=BG)
        meta.pack(fill="x", pady=(10, 8))
        self.status = tk.Label(meta, text="Loading…", fg=TEXT, bg=BG, font=_ui_font(10, bold=True))
        self.status.pack(side="left")
        self.reset = tk.Label(meta, text="", fg=MUTED, bg=BG, font=_ui_font(9))
        self.reset.pack(side="right")

        tk.Frame(pad, bg="#2a2d33", height=1).pack(fill="x", pady=(4, 12))

        sg_header = tk.Frame(pad, bg=BG)
        sg_header.pack(fill="x")
        tk.Label(sg_header, text="Super Grok", fg=TEXT, bg=BG, font=_ui_font(11, bold=True)).pack(
            side="left"
        )
        self.sg_plan = tk.Label(sg_header, text="weekly pool", fg=MUTED, bg=BG, font=_ui_font(9))
        self.sg_plan.pack(side="right")

        sg_numbers = tk.Frame(pad, bg=BG)
        sg_numbers.pack(fill="x", pady=(8, 4))
        sg_left = tk.Frame(sg_numbers, bg=BG)
        sg_left.pack(side="left")
        self.sg_used_value = tk.Label(sg_left, text="--%", fg=TEXT, bg=BG, font=_ui_font(18, bold=True))
        self.sg_used_value.pack(anchor="w")
        tk.Label(sg_left, text="used", fg=MUTED, bg=BG, font=_ui_font(8)).pack(anchor="w")
        sg_right = tk.Frame(sg_numbers, bg=BG)
        sg_right.pack(side="right")
        self.sg_expected_value = tk.Label(
            sg_right, text="--%", fg=TEXT, bg=BG, font=_ui_font(18, bold=True)
        )
        self.sg_expected_value.pack(anchor="e")
        tk.Label(sg_right, text="should be", fg=MUTED, bg=BG, font=_ui_font(8)).pack(anchor="e")

        self.sg_bar = tk.Canvas(pad, width=308, height=12, bg=BAR_BG, highlightthickness=0, bd=0)
        self.sg_bar.pack(fill="x", pady=(2, 4))

        sg_meta = tk.Frame(pad, bg=BG)
        sg_meta.pack(fill="x", pady=(4, 10))
        self.sg_products = tk.Label(sg_meta, text="", fg=MUTED, bg=BG, font=_ui_font(8))
        self.sg_products.pack(side="left")
        self.sg_reset = tk.Label(sg_meta, text="", fg=MUTED, bg=BG, font=_ui_font(8))
        self.sg_reset.pack(side="right")

        buttons = tk.Frame(pad, bg=BG)
        buttons.pack(fill="x")
        self._btn(buttons, "Refresh", self.refresh_async).pack(side="left")
        self._btn(buttons, "Open Grok Bot", self.open_grok_bot).pack(side="left", padx=(8, 0))
        self._btn(buttons, "Menu", self._show_menu).pack(side="right")

        self.footer = tk.Label(
            pad,
            text="",
            fg=MUTED,
            bg=BG,
            font=_ui_font(8),
            wraplength=WINDOW_W - 40,
            justify="left",
            anchor="w",
        )
        self.footer.pack(fill="x", pady=(10, 2))

        self.menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=CARD,
            fg=TEXT,
            activebackground="#2a2d33",
            postcommand=self._sync_menu_checks,
        )
        self.menu.add_command(label="Refresh now", command=self.refresh_async)
        self.menu.add_separator()
        self._always_var = tk.BooleanVar(value=bool(self.settings.get("always_on_top", True)))
        self._startup_var = tk.BooleanVar(value=is_startup_enabled())
        self.menu.bind("<Unmap>", self._on_menu_unmap, add="+")
        self.menu.add_checkbutton(
            label="Always on top",
            variable=self._always_var,
            command=self._toggle_topmost,
        )
        self.menu.add_checkbutton(
            label="Start with Windows",
            variable=self._startup_var,
            command=self._toggle_startup,
        )
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        self.render()

    def _btn(self, parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#23262c",
            fg=TEXT,
            activebackground="#2e323a",
            activeforeground=TEXT,
            relief="flat",
            padx=10,
            pady=4,
            font=_ui_font(9),
            cursor="hand2",
        )

    def _content_size(self) -> tuple[int, int]:
        self.root.update_idletasks()
        width = max(WINDOW_W, int(self.root.winfo_reqwidth()))
        height = max(WINDOW_H, int(self.root.winfo_reqheight()))
        return width, height

    def _place_window(self) -> None:
        width, height = self._content_size()
        x, y = self._saved_position()
        if x is None or y is None:
            try:
                left, top, right, bottom = work_area()
            except Exception:
                left, top, right, bottom = 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            x = max(left + 8, right - width - 16)
            y = max(top + 8, bottom - height - 16)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _saved_position(self) -> tuple[int | None, int | None]:
        import re

        raw = str(self.settings.get("geometry") or "")
        match = re.search(r"([+-]\d+)([+-]\d+)$", raw)
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def _after_map(self) -> None:
        try:
            enable_dark_titlebar(self.root.winfo_id())
        except Exception:
            pass
        self._place_window()
        self._start_tray()

    def _show_menu(self) -> None:
        self._sync_menu_checks()
        self.menu.tk_popup(self.root.winfo_rootx() + WINDOW_W - 24, self.root.winfo_rooty() + 36)

    def _sync_menu_checks(self) -> None:
        self._startup_var.set(is_startup_enabled())
        self._always_var.set(bool(self.settings.get("always_on_top", True)))

    def _on_menu_unmap(self, _event=None) -> None:
        want = bool(self._startup_var.get())
        if want == is_startup_enabled():
            return
        self._apply_startup(want)

    def _toggle_topmost(self) -> None:
        value = bool(self._always_var.get())
        self.root.attributes("-topmost", value)
        self.settings["always_on_top"] = value
        save_settings(self.settings)

    def _toggle_startup(self) -> None:
        self._apply_startup(bool(self._startup_var.get()))

    def _apply_startup(self, enabled: bool) -> None:
        try:
            set_startup(enabled)
        except Exception as exc:
            self.error = f"Could not update startup: {exc}"
            self.render()
        self._startup_var.set(is_startup_enabled())
        if enabled and not is_startup_enabled():
            self.error = "Could not register Start with Windows"
            self.render()

    def open_grok_bot(self) -> None:
        exe = grok_bot_exe()
        if exe is None:
            self.error = "Grok Bot.exe was not found"
            self.render()
            return
        os.startfile(str(exe))  # type: ignore[attr-defined]

    def hide_to_tray(self) -> None:
        self.settings["geometry"] = self.root.geometry()
        save_settings(self.settings)
        self.root.withdraw()

    def show_window(self, *_args) -> None:
        self.root.deiconify()
        self.root.after(40, lambda: self.root.lift())
        self.root.after(40, lambda: self.root.focus_force())

    def quit(self, *_args) -> None:
        self.settings["geometry"] = self.root.geometry()
        save_settings(self.settings)
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
        self.root.destroy()

    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception:
            return
        image = badge_image(self.pace, size=32)
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show, default=True),
            pystray.MenuItem("Refresh", self._tray_refresh),
            pystray.MenuItem("Open Grok Bot", self._tray_open),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._icon = pystray.Icon("GrokBotPace", image, "Grok Bot Pace", menu)
        self._icon.run_detached()
        self.root.after(400, self._sync_icon)

    def _tray_show(self, *_args) -> None:
        self.root.after(0, self.show_window)

    def _tray_refresh(self, *_args) -> None:
        self.root.after(0, self.refresh_async)

    def _tray_open(self, *_args) -> None:
        self.root.after(0, self.open_grok_bot)

    def _tray_quit(self, *_args) -> None:
        self.root.after(0, self.quit)

    def _tick(self) -> None:
        if self.snapshot is not None:
            self.pace = compute_pace(self.snapshot, stale=bool(self.error))
        if self.sg_snapshot is not None:
            self.sg_pace = compute_pace(self.sg_snapshot.as_usage(), stale=bool(self.sg_error))
        if self.snapshot is not None or self.sg_snapshot is not None:
            self.render()
        self.root.after(TICK_MS, self._tick)

    def refresh_async(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.footer.configure(text="Refreshing…")

        def work() -> None:
            error = None
            snapshot = None
            sg_snapshot = None
            sg_error = None
            try:
                if self.tokens is None:
                    self.tokens = load_tokens()
                try:
                    snapshot = fetch_usage(self.tokens.access_token)
                except RuntimeError as exc:
                    if "401" in str(exc) and self.tokens.refresh_token:
                        self.tokens = refresh_access_token(self.tokens.refresh_token)
                        save_cached(self.tokens)
                        snapshot = fetch_usage(self.tokens.access_token)
                    else:
                        raise
            except Exception as exc:
                error = str(exc)
            try:
                sg_snapshot = fetch_supergrok()
            except Exception as exc:
                sg_error = str(exc)
            self.root.after(0, lambda: self._on_refresh(snapshot, error, sg_snapshot, sg_error))

        threading.Thread(target=work, daemon=True).start()

    def _on_refresh(
        self,
        snapshot: UsageSnapshot | None,
        error: str | None,
        sg_snapshot: SuperGrokSnapshot | None,
        sg_error: str | None,
    ) -> None:
        self._refreshing = False
        if snapshot is not None:
            self.snapshot = snapshot
            self.error = None
            save_cached_snapshot(snapshot)
        else:
            self.error = error
        if sg_snapshot is not None:
            self.sg_snapshot = sg_snapshot
            self.sg_error = None
            save_cached_supergrok(sg_snapshot)
        else:
            self.sg_error = sg_error
        if self.snapshot is not None:
            self.pace = compute_pace(self.snapshot, stale=self.error is not None)
        if self.sg_snapshot is not None:
            self.sg_pace = compute_pace(self.sg_snapshot.as_usage(), stale=self.sg_error is not None)
        self.render()

    def _auto_refresh(self) -> None:
        self.refresh_async()
        self.root.after(REFRESH_MS, self._auto_refresh)

    def render(self) -> None:
        pace = self.pace
        if pace is None:
            self.used_value.configure(text="--%")
            self.expected_value.configure(text="--%")
            self.status.configure(text=self.error or "Waiting for Grok Bot usage…")
            self._draw_bar(self.bar, 0, 0, status_color("unknown"))
        else:
            color = status_color(pace.status)
            self.used_value.configure(text=f"{pace.used_percent:.0f}%", fg=color)
            self.expected_value.configure(text=f"{pace.expected_percent:.0f}%")
            self.plan.configure(text=pace.plan_label)
            self.status.configure(text=status_label(pace), fg=color)
            self.reset.configure(text=f"resets in {format_hours(pace.resets_in)}")
            self._draw_bar(self.bar, pace.used_percent, pace.expected_percent, color)
            self.root.title(f"{badge_label(pace)} pts vs pace")

        sg = self.sg_pace
        if sg is None:
            self.sg_used_value.configure(text="--%", fg=TEXT)
            self.sg_expected_value.configure(text="--%")
            self.sg_products.configure(text=self.sg_error or "Waiting for Super Grok usage…")
            self.sg_reset.configure(text="")
            self._draw_bar(self.sg_bar, 0, 0, status_color("unknown"))
        else:
            sg_color = status_color(sg.status)
            self.sg_used_value.configure(text=f"{sg.used_percent:.0f}%", fg=sg_color)
            self.sg_expected_value.configure(text=f"{sg.expected_percent:.0f}%")
            products = format_products(self.sg_snapshot.products) if self.sg_snapshot else ""
            self.sg_products.configure(text=products or (self.sg_error or ""))
            self.sg_reset.configure(text=f"resets in {format_hours(sg.resets_in)}")
            self._draw_bar(self.sg_bar, sg.used_percent, sg.expected_percent, sg_color)

        fetched = None
        if pace and pace.fetched_at:
            fetched = pace.fetched_at
        if sg and sg.fetched_at and (fetched is None or sg.fetched_at > fetched):
            fetched = sg.fetched_at
        extra_bits = [bit for bit in (self.error, self.sg_error) if bit]
        extra = f"  ·  {extra_bits[0]}" if extra_bits else ""
        if fetched:
            stamp = fetched.astimezone().strftime("%I:%M %p").lstrip("0")
            self.footer.configure(text=f"Updated {stamp}{extra}")
        else:
            self.footer.configure(text=extra_bits[0] if extra_bits else "")
        self._sync_icon()

    def _draw_bar(self, canvas: tk.Canvas, used: float, expected: float, color: str) -> None:
        canvas.delete("all")
        width = int(canvas.cget("width"))
        height = int(canvas.cget("height"))
        canvas.create_rectangle(0, 0, width, height, fill=BAR_BG, outline="")
        used_x = max(0, min(width, used / 100.0 * width))
        if used_x > 0:
            canvas.create_rectangle(0, 0, used_x, height, fill=color, outline="")
        exp_x = max(1, min(width - 1, expected / 100.0 * width))
        canvas.create_line(exp_x, 1, exp_x, height - 1, fill="#f7f7f7", width=2)

    def _sync_icon(self) -> None:
        small_n, large_n = system_icon_sizes(self.root.winfo_id())
        small = badge_image(self.pace, size=small_n)
        large = badge_image(self.pace, size=large_n)
        tray = badge_image(self.pace, size=max(32, small_n))
        try:
            self._ico_path.write_bytes(badge_ico_bytes(self.pace))
            self.root.iconbitmap(str(self._ico_path))
        except Exception:
            pass
        try:
            photos = [ImageTk.PhotoImage(badge_image(self.pace, size=s)) for s in (16, 24, 32, 48, 64)]
            self._icon_photos = photos
            self.root.iconphoto(True, *photos)
        except Exception:
            pass
        self._apply_taskbar_icons(small, large)
        if self._icon is not None:
            try:
                self._icon.icon = tray
                if self.pace is not None:
                    self._icon.title = tooltip_text(self.pace)
            except Exception:
                pass

    def _apply_taskbar_icons(self, small: object, large: object) -> None:
        import ctypes

        try:
            hwnd = int(self.root.winfo_id())
            parent = ctypes.windll.user32.GetParent(hwnd)
            targets = [hwnd]
            if parent:
                targets.append(parent)
            h_small = image_to_hicon(small)
            h_large = image_to_hicon(large)
            for target in targets:
                ctypes.windll.user32.SendMessageW(target, 0x0080, 0, h_small)
                ctypes.windll.user32.SendMessageW(target, 0x0080, 1, h_large)
            for old in self._hicons:
                ctypes.windll.user32.DestroyIcon(old)
            self._hicons = [h_small, h_large]
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


_FONTS: dict[tuple[int, bool], tkfont.Font] = {}


def _ui_font(size: int, bold: bool = False) -> tkfont.Font:
    key = (size, bold)
    cached = _FONTS.get(key)
    if cached is None:
        cached = tkfont.Font(family="Segoe UI", size=size, weight="bold" if bold else "normal")
        _FONTS[key] = cached
    return cached


def _single_instance_mutex():
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, True, "Local\\GrokBotPaceSingleton")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return None
    return handle


def main() -> None:
    enable_dpi_awareness()
    mutex = _single_instance_mutex()
    if mutex is None:
        # An existing instance is already in the tray / taskbar.
        return
    PaceApp().run()
