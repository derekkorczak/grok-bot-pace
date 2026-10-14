# Grok Bot Pace

A small Windows tray app that shows how your weekly included usage compares to an even pace across the week.

The X Subscription uses one weekly pool for Grok, Grok Bot, and Cursor. The taskbar / tray badge is how many points you are from an even pace. The window shows **used** against **should be**, and which products are drawing from that same pool.

## What you see

- **Used** — the shared weekly pool
- **Should be** — elapsed time in this reset week ÷ week length × 100
- **What's using it** — each product's share of that pool (Cursor, Grok Bot, Chat, Build, and the rest). Other-model usage is called out only when it is billed outside the pool.
- **Bar** — fill is used; the white line is the even-pace target
- **Color**
  - Green: under pace (more quota left than the clock would suggest)
  - Blue: on track
  - Amber: over pace
  - Red: nearly empty or exhausted

The reset window comes from the plan (start + next reset), not a guessed Monday–Sunday week.

## Merge Gateway

Under the weekly pool, the window shows the complimentary Pro credits ($10 each month). **Used** is how much of that grant is spent. **Should be** is how far through the month you are. The session is stored on this computer (DPAPI-protected), and the app never shows the cookie.

Merge has no public credit-balance API. Sign in from this computer, which has Chrome or Edge. Open the app menu and choose **Sign in to Merge Gateway**. A browser window opens for the Google sign-in, then closes and the app picks up the session. Run it again when the bar says the session expired. The browser keeps its own profile (`%LOCALAPPDATA%\GrokBotPace\merge-browser`), so the next sign-in is usually a short Google confirm. **Sign out of Merge Gateway** in the menu forgets the session.

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

The app reads the session already stored by Grok Bot (`%APPDATA%\Grok Bot`) and the Grok CLI login (`%USERPROFILE%\.grok`). It calls the same usage endpoints those apps use. Tokens stay on this machine (and, after a refresh, in a DPAPI-protected local file). Requests go to `api2.cursor.sh` and `cli-chat-proxy.grok.com`.

This is unofficial and not affiliated with xAI or Cursor. Those usage endpoints are undocumented and can change.

## Controls

- Close the window to hide it to the tray (it keeps running)
- Tray icon click: show the window
- Tray / window **Quit**: exit
- **Refresh**: fetch usage now (usage auto-refreshes every 5 minutes; the “should be” figure recomputes about every 15 seconds)
- **Menu → Sign in to Merge Gateway** / **Sign out of Merge Gateway**: manage the monthly credit session
