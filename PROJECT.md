# Command Center

A local dashboard that pulls Outlook, Teams (through Outlook), websites and SQL Server
into one place. It sends alerts and lets you act on them. It runs on this PC only,
at http://127.0.0.1:8765, and uses your existing sign-ins.

## Start it
Double-click `run.cmd`. It opens the dashboard in your browser. Close the console window to stop it.

Other commands:
- `run.cmd login <profile> <url>`: log in to a website once, in a saved browser profile
- `run.cmd setpass <connection>`: save a SQL password in Windows Credential Manager
- `run.cmd serve --no-browser`: start without opening the dashboard

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
- **Daily briefing** pop-up at `today.briefing_time`.

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
- `commandcenter/today.py`: assembles the Today view, follow-up filtering and the daily briefing
- `commandcenter/connectors/`: `outlook.py`, `web.py`, `sql.py`, `asana.py`, `weather.py`
- `commandcenter/static/`: dashboard (plain JS, no build step)
- `data/`: SQLite database (alerts and state), log file, browser profiles (all git-ignored)

## Constraints
- Cylance blocks `.ps1` files, so nothing here uses PowerShell scripts. Run everything through `run.cmd` or `.venv\Scripts\python.exe`.
- Outlook COM calls must stay on one thread. Always go through `outlook.call()`.
- For bulk Outlook reads, use `_table_rows()` (Folder.GetTable). Walking Items took 38 s for this mailbox. The To-Do folder ignores GetTable filters, so filter in Python.
- The UI uses a dark HUD theme: Orbitron, Rajdhani and JetBrains Mono from Google Fonts, falling back to system fonts when offline.

## Ideas for later
- SQL watches: run a saved query on a schedule and alert when it returns rows
- Combined actions: find an order number in an email, look it up in SQL, show the result in the alert
- Teams in the browser: drive teams.microsoft.com with a saved profile to read and reply directly
- Start automatically at Windows sign-in (shortcut in shell:startup)
