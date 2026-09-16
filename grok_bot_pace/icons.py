"""Tray and taskbar badge images."""

from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from grok_bot_pace.pace import Pace

COLORS = {
    "under": "#3dd68c",
    "on_track": "#7dd3fc",
    "over": "#f5a524",
    "critical": "#f31260",
    "unknown": "#9aa0a6",
}

_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\ARIALNB.TTF"),
    Path(r"C:\Windows\Fonts\impact.ttf"),
    Path(r"C:\Windows\Fonts\tahomabd.ttf"),
    Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
]

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64)


def status_color(status: str) -> str:
    return COLORS.get(status, COLORS["unknown"])


def badge_label(pace: Pace | None) -> str:
    """Absolute pts away from even weekly pace."""
    if pace is None:
        return "--"
    return str(abs(int(round(pace.delta))))


def badge_color(pace: Pace | None) -> str:
    if pace is None:
        return status_color("unknown")
    if pace.delta > 0:
        return status_color("over")
    return status_color("under")


def _best_font(text: str, max_w: int, max_h: int, stroke: int) -> ImageFont.ImageFont:
    best: ImageFont.ImageFont | None = None
    best_area = -1
    for path in _FONT_CANDIDATES:
        if not path.exists():
            continue
        lo, hi = 5, max_h + 10
        chosen: ImageFont.FreeTypeFont | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            font = ImageFont.truetype(str(path), size=mid)
            left, top, right, bottom = font.getbbox(text, stroke_width=stroke)
            tw, th = right - left, bottom - top
            if tw <= max_w and th <= max_h:
                chosen = font
                lo = mid + 1
            else:
                hi = mid - 1
        if chosen is None:
            continue
        left, top, right, bottom = chosen.getbbox(text, stroke_width=stroke)
        area = (right - left) * (bottom - top)
        if area > best_area:
            best = chosen
            best_area = area
    return best or ImageFont.load_default()


def badge_image(pace: Pace | None, size: int = 64) -> Image.Image:
    color = badge_color(pace)
    text = badge_label(pace)
    img = Image.new("RGBA", (size, size), color)
    draw = ImageDraw.Draw(img)
    stroke = 0 if size < 20 else (1 if size < 40 else 2)
    pad = 0 if size <= 20 else 1
    font = _best_font(text, size - pad * 2, size - pad * 2, stroke)
    left, top, right, bottom = font.getbbox(text, stroke_width=stroke)
    tw, th = right - left, bottom - top
    x = round((size - tw) / 2 - left)
    y = round((size - th) / 2 - top)
    draw.text(
        (x, y),
        text,
        font=font,
        fill="#ffffff",
        stroke_width=stroke,
        stroke_fill="#141414",
    )
    return img


def badge_png_bytes(pace: Pace | None, size: int = 64) -> bytes:
    buf = BytesIO()
    badge_image(pace, size=size).save(buf, format="PNG")
    return buf.getvalue()


def _pack_png_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    count = len(pngs)
    offset = 6 + 16 * count
    chunks = [struct.pack("<HHH", 0, 1, count)]
    payloads = []
    for size, png in pngs:
        chunks.append(
            struct.pack(
                "<BBBBHHII",
                0 if size >= 256 else size,
                0 if size >= 256 else size,
                0,
                0,
                1,
                32,
                len(png),
                offset,
            )
        )
        payloads.append(png)
        offset += len(png)
    return b"".join(chunks) + b"".join(payloads)


def badge_ico_bytes(pace: Pace | None) -> bytes:
    pngs: list[tuple[int, bytes]] = []
    for size in ICON_SIZES:
        buf = BytesIO()
        badge_image(pace, size=size).save(buf, format="PNG")
        pngs.append((size, buf.getvalue()))
    return _pack_png_ico(pngs)


def image_to_hicon(image: Image.Image) -> int:
    """Create a Windows HICON from an RGBA PIL image. Caller owns the handle."""
    import ctypes
    from ctypes import wintypes

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()
    raw = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            raw.extend((b, g, r, a))

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    bits = ctypes.c_void_p()
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    hdc = user32.GetDC(None)
    try:
        hbm_color = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
        )
    finally:
        user32.ReleaseDC(None, hdc)
    if not hbm_color or not bits:
        raise OSError("CreateDIBSection failed")
    ctypes.memmove(bits, bytes(raw), len(raw))
    hbm_mask = gdi32.CreateBitmap(width, height, 1, 1, None)

    info = ICONINFO(True, 0, 0, hbm_mask, hbm_color)
    user32.CreateIconIndirect.restype = wintypes.HICON
    hicon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(hbm_color)
    gdi32.DeleteObject(hbm_mask)
    if not hicon:
        raise OSError("CreateIconIndirect failed")
    return int(hicon)


def enable_dpi_awareness() -> None:
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def system_icon_sizes(hwnd: int | None = None) -> tuple[int, int]:
    import ctypes

    dpi = 96
    try:
        if hwnd:
            dpi = ctypes.windll.user32.GetDpiForWindow(int(hwnd)) or 96
        else:
            dpi = ctypes.windll.user32.GetDpiForSystem() or 96
    except Exception:
        pass
    scale = max(1.0, dpi / 96.0)
    # Taskbar icons are ~24px at 100% DPI; never hand Windows a 16px glyph to upscale.
    small = max(24, round(16 * scale))
    large = max(32, round(32 * scale))
    return small, large
