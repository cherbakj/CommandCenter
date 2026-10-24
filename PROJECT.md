# Command Center

A local dashboard that pulls Outlook, Teams (through Outlook), websites and SQL Server
into one place. It sends alerts and lets you act on them. It runs on this PC only,
at http://127.0.0.1:8765, and uses your existing sign-ins.

## Start it
It's a desktop app: its own window (Edge WebView2 through pywebview) plus a tray icon.
- **Start menu / desktop shortcut "Command Center"**, or double-click `run.cmd`
- It starts automatically at sign-in, minimized to the tray (shortcut in shell:startup)
- Closing the window hides it to the tray so alerts keep running. Use **tray icon → Quit** to exit.
- Launching it again while it's running just brings the window forward.

Commands (`run.cmd <command>`): `install` / `uninstall` (shortcuts; `install --no-startup` skips sign-in),
`serve` (server only, dashboard in a browser), `login <profile> <url>`, `setpass <connection>`.

## Configure it
Edit `config.yaml`. It is created from `config.example.yaml` on first run, and every
setting in it has a comment. Click **Reload config** in the dashboard afterwards.
(Changing `poll_seconds` or the port needs a restart.)

## Today tab (home page)
- **Clock and date**, with a workday progress bar (8 AM to 5 PM, set by `WORKDAY` in `static/app.js`).
- **Weather** for `today.zip`, from zippopotam.us (zip to coordinates) and Open-Meteo. Free, no key, cached for 10 minutes.
- **Agenda:** today's Outlook meetings, with Join buttons.
- **Tasks:** open Outlook/To Do tasks plus Asana tasks assigned to you. **+ New task** creates a task in either one. There's also a **+ Task** button on any email.
- **Follow-ups** (`today.py`, cached for 2 minutes; **Rescan** forces a fresh scan):
  - *Needs your reply:* mail sent to you in the last few days with no reply and no bulk-mail headers
  - *Awaiting their reply:* mail you sent with no later message in that conversation. **Nudge** opens a draft.
  - *Flagged:* flagged mail from people in the last 30 days. Automated senders (noreply etc.) are hidden. The user has about 7,000 auto-flagged NoReply@sonance.com emails.
  - *Unread from VIPs:* senders listed in the Outlook rules
  - **Dismiss** hides one item and **Mute** hides a sender. Both are stored in SQLite and can be undone under *Muted senders*.
- **Daily briefing** at `today.briefing_time` and **wrap-up** at `today.wrapup_time` (tomorrow's first meeting and what's still open). Weekdays only by default. A late start still catches up within 4 hours.

## Other features
- **Ctrl+K palette:** commands (tabs, new task, focus, join next meeting, Teams), today's items, saved SQL queries, and live Outlook search of Inbox and Sent (3+ letters).
- **Focus mode:** header chip, tray menu or Ctrl+K. Only high-priority alerts pop up or play a sound; everything still goes to the feed. A calendar event whose title contains a `outlook.focus_keywords` word turns it on for that event.
- **Outlook watchdog:** if Outlook is closed it's restarted (`outlook.auto_start`). The app never Dispatches a hidden Outlook (`_ns()` only attaches to a running one).
- **Teams tab:** unread count from a background browser on Teams web (profile `teams`, read from the page title), the missed-activity emails, and "Open chat in Teams" through `msteams:` deep links (opened with `/api/open` → `os.startfile`). Chat buttons also appear on emails and needs-reply follow-ups. Polling only starts after you log in on the Teams tab.
- **Layout:** fluid rem sizing (root font scales with the window). Three columns that fill the screen with no page scroll at ≥2200px wide and ≥2:1 aspect (the user's 34" ultrawide). Two columns on standard monitors, one on narrow. Lists use container queries so buttons wrap below the text in narrow columns.
- **Theme:** dark HUD with neon green accent (`ui.accent` in config; any hex), cyan secondary.

## How each connection works
| Source | How | Notes |
|---|---|---|
| Asana | REST API with a Personal Access Token | The token is stored in Windows Credential Manager (`CommandCenter-Asana`). Connect it on the Today tab. |
| Weather | Open-Meteo and zippopotam.us | No key needed |
| Outlook mail and calendar | COM automation of classic Outlook (`pywin32`) | Outlook must be running. Reply, flag, mark read and open all run as you. |
| Teams | Teams' missed-activity emails, picked up by an Outlook rule | Power Automate is blocked in this tenant. Turn on Teams > Settings > Notifications > email for missed activity. |
| Websites | Playwright driving the installed Edge, one saved profile per site | Sessions live in `data/browser/<profile>`. Log in again when you get a "login needed" alert. |
| SQL Server | pyodbc with ODBC Driver 18, SQL login | The VPN must be up. Only saved SELECT queries run, each in a transaction that is rolled back. |

## Layout
- `commandcenter/app.py`: FastAPI routes. A middleware blocks requests with a foreign Host header and any POST without the `X-CommandCenter` header.
- `commandcenter/alerts.py`: alert feed, Windows toast notifications (`windows-toasts`) and sound
- `commandcenter/desktop.py`: window, tray, single instance, shortcuts
- `commandcenter/today.py`: assembles the Today view, follow-up filtering and the daily briefing
- `commandcenter/connectors/`: `outlook.py`, `web.py`, `sql.py`, `asana.py`, `weather.py`, `teams.py`
- `commandcenter/static/`: dashboard (plain JS, no build step)
- `data/`: SQLite database (alerts and state), log file, browser profiles (all git-ignored)

## Constraints
- Cylance blocks `.ps1` files, so nothing here uses PowerShell scripts. Run everything through `run.cmd` or `.venv\Scripts\python.exe`.
- Outlook COM calls must stay on one thread. Always go through `outlook.call()`.
- For bulk Outlook reads, use `_table_rows()` (Folder.GetTable). Walking Items took 38 s for this mailbox. The To-Do folder ignores GetTable filters, so filter in Python.
- The UI uses a dark HUD theme: Orbitron, Rajdhani and JetBrains Mono from Google Fonts, falling back to system fonts when offline.

## Ideas for later
- SQL watches: run a saved query on a schedule and alert when it returns rows
- Order numbers to SQL: find order numbers in emails and show the order's status (needs SQL connected)
- Teams: after login, check that the title-count approach holds up. Reading chat previews would need DOM selectors calibrated on a live session.
- AI summaries and drafts: on hold until company policy on sending email content to an outside AI service is checked
