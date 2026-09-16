# Grok Bot Pace

A small Windows tray app that shows how your **Grok Bot** weekly usage compares to where you should be if you spent the week evenly.

The taskbar / tray badge is **actual % used**. The window also shows **should be %** — the even-pace target for the current reset window — and a marker on the bar so you can see whether you are under, on, or over pace.

## What you see

- **Used** — live Grok Bot weekly usage from your signed-in Grok Bot session
- **Should be** — elapsed time in this reset week ÷ week length × 100
- **Super Grok** — the separate grok.com weekly pool (Chat, Build, Imagine, and the rest), shown in the window only. The taskbar badge stays Grok Bot pace.
- **Bar** — fill is used; the white line is the even-pace target
- **Color**
  - Green: under pace (more quota left than the clock would suggest)
  - Blue: on track
  - Amber: over pace
  - Red: nearly empty or exhausted

The reset window comes from Grok Bot itself (start + next reset), not a guessed Monday–Sunday week.

## Install

1. Stay signed in to the **Grok Bot** desktop app.
2. From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe -m grok_bot_pace
```

Or double-click `run.bat`.

Optional: in the app menu, turn on **Start with Windows**.

## How it authenticates

The app reads the session already stored by Grok Bot (`%APPDATA%\Grok Bot`) and calls the same usage endpoint the Grok Bot / Cursor dashboard uses. Tokens are kept in memory (and, after a refresh, in a DPAPI-protected local file). Nothing is sent anywhere except `api2.cursor.sh`.

This is unofficial and not affiliated with xAI or Cursor. That usage endpoint is undocumented and can change.

## Controls

- Close the window to hide it to the tray (it keeps running)
- Tray icon click: show the window
- Tray / window **Quit**: exit
- **Refresh**: fetch usage now (usage auto-refreshes every 5 minutes; the “should be” figure recomputes at least once a minute)
