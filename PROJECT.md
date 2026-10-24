# Command Center

Joe's local desktop hub. It pulls Outlook, Teams, Pulse tickets, Asana and SQL Server tools into
one window, sends alerts, and hosts Claude Code and VS Code for his projects. It runs on this PC
and for this Windows user only (127.0.0.1:8765 behind a per-launch session cookie). It lives in
`C:\Sonance\internal\CommandCenter`, next to Pulse, for corporate/VPN folder access.

## Start it
- Use the **Command Center** shortcut (desktop / Start menu), or `run.cmd`. It also starts at sign-in, minimized to the tray.
- Opens **borderless full screen** (`ui.start_fullscreen`). The header has Lock, Restart, Minimize, Full screen (F11) and Close-to-tray buttons.
- **Windows Hello / the Windows password** is asked when the window is first shown. There's no idle re-lock; **Lock** in the header logs out of Command Center.
- A puzzle-piece launch animation with live sync checks plays on each load. Turn it off under Profile.
- Commands: `run.cmd install|uninstall|serve|login <profile> <url>|setpass <connection>`. `serve` = server only, with a one-time browser sign-in link.

## Configure it
`config.yaml` (created from `config.example.yaml`; every setting is commented). Secrets are never in config: they live in
Windows Credential Manager (services `CommandCenter-SQL`, `CommandCenter-Toolkit`, `CommandCenter-Pulse`, `CommandCenter-Asana`).
Appearance (colors, text size, density, effects) lives in the in-app **Profile** (avatar at top right), stored in SQLite.

## Layout of the window
- **Split workspace** (>= 1600px wide; toggle Split/Single in the header):
  - **COMMS** (left): Today, Alerts, Mail, Calendar, Teams.
  - **WORK** (right): Claude, VS Code, Toolkit, SQL, Pulse, Websites, with a **Working on:** project selector that drives the Claude and VS Code tabs.
- **Splitter:** draggable and keyboard-movable; its position is saved per viewer in localStorage.
- **Sizing:** layouts use container queries on the pane (not the window) and rem units scaled by Profile text size.
- **Front-end plug-ins:** feature modules register through `window.CC` (`CC.registerTab({id, label, render, onShow, pane})`, `CC.addPaletteSource`, `CC.activeProject`, events `cc:project` / `cc:layout`).

## Features
- **Today:** compact clock / weather (Lake Forest | San Clemente toggle) / stats strip and hotbar (Alt+1-9), follow-ups, agenda (with free slots you can drag tasks onto), recent alerts, tasks.
- **Follow-ups** (`today.py`, cached 2 min):
  - **Urgent:** reply needed from Joe's urgent people (edited in the app or `followups.urgent_people`).
  - **Needs your reply:** internal @sonance.com senders only, excluding auto-replies and sharing notices.
  - **Teams:** reply-needed from Teams missed-activity emails.
  - **Awaiting their reply:** sent mail with no answer yet.
  - **Flagged:** recent flags from people; automated NoReply@sonance.com flags are hidden.
  - **VIP unread.**
  - **Actions:** Snooze, Dismiss, Mute, Mark urgent. No-reply reminders, and urgent escalation after `urgent_escalate_hours`.
- **Source tags:** every item shows where it came from (Outlook / Sent / Flagged / Teams / Calendar / To Do / Asana / Pulse / SQL / Website / Personal), with the sender address.
- **Calendar (`planner.py`):**
  - Outlook meetings for the next 10 days, including all-day events.
  - Dated follow-ups: tasks, flags due, snoozes, reply checks.
  - **Personal entries**, stored only in SQLite, title and notes encrypted with DPAPI, never sent to Outlook.
- **Alerts:** toast + sound + feed; focus mode (manual, tray, or calendar events containing "focus"); morning briefing and end-of-day wrap-up; meeting reminders with a **Prep** card (attendees, recent mail, open follow-ups).
- **Orders (`orders.py`):** copying an order number (7-10 digits) or opening an email that contains one looks it up with the saved SQL query `Find order` (needs SQL set up).
- **Claude tab (`terminal.py`, `projects.py`):**
  - Runs the real Claude Code CLI in a ConPTY, streamed to xterm.js over a WebSocket.
  - Sessions persist per project until **End session**; max 6. Each runs in a kill-on-close job object.
  - Projects come from `C:\Sonance\internal\*`, `~\source\*` and `projects.extra`.
- **VS Code tab (`vscode.py`):**
  - Runs the VS Code Server build matching desktop VS Code (bundled node, not `code serve-web`, which fails here). Includes the Claude Code extension.
  - Listens on 127.0.0.1 only, with a random port reused while free and a per-launch token passed over a named pipe.
  - Data in `data/vscode` (about 600 MB).
- **Toolkit tab (`toolkit.py`):** Cherbak's Toolkit (Shipping, Logi, Customers) against SQL Server.
  - Every write is a single-row, whitelisted, parameterized transaction, confirmed in the UI with a before/after diff.
  - Every attempt is logged in SQLite `toolkit_changes`.
  - TLS verifies the server certificate by default; this PC trusts Sonance's corp CAs.
- **Pulse tab (`pulse.py`):** read-only Postgres access to `public.data_reviews` through a least-privilege role `commandcenter_ro` (setup SQL on the tab, run by Joe in Supabase). Polls every 2 min for new or changed tickets on Joe.
- **Mail / Teams / Websites / SQL tabs:** as before. Teams unread count comes from a background browser profile once Joe logs in; chats open through `msteams:` links.

## Security model (security.py, desktop.py)
- **Loopback and same user:** binds 127.0.0.1 with SO_EXCLUSIVEADDRUSE. Each connection is checked against the owning process's Windows user.
- **Request checks:** Host check; `Sec-Fetch-Site` cross-site/same-site refused; non-GET requests need the `X-CommandCenter` header plus our Origin; WebSockets need our exact Origin.
- **Session cookie:** per-launch secret held in memory, set by a one-time `/auth?k=` bootstrap (HttpOnly, SameSite=Strict). Every route needs it.
- **Response headers:** strict CSP, `frame-ancestors 'self'`, nosniff, no-referrer, `no-store`.
- **Window:** WebView2 with devtools, remote debugging, `file://` access and downloads off; no pywebview `js_api`.
- **App folder:** ACL limited to Joe, SYSTEM and Administrators, enforced at start.
- **Secrets:** only in Credential Manager or memory. Logs, the database and API responses were swept for secrets.
- **Dev bypass:** `CC_DEV_NO_AUTH=1` works in `serve` mode only and logs a warning.

## Code layout
- `commandcenter/app.py`: core routes. Auto-loads the feature modules security, toolkit, pulse, terminal, vscode, planner and profile (each exposes `router` and optionally `background`).
- `commandcenter/connectors/`: outlook, web, sql, asana, weather, teams. `people.py` handles internal domains and urgent people.
- `commandcenter/static/`:
  - Core UI: `app.js`, `style.css`, `index.html`.
  - Feature modules: `projects`, `vscode`, `toolkit`, `pulse`, `planner`, `profile`, `splash` (`.js`/`.css`).
  - Shared assets: `vendor/` (xterm), `jc-logo.svg` and `icon.ico` (the JC monogram).
- `data/` (git-ignored, private ACL): the SQLite DB, log, browser profiles, `vscode/`, and the ACL backup.

## Constraints
- Cylance blocks `.ps1` files, so never create them. Run everything through `run.cmd` or `.venv\Scripts\python.exe`.
- **Outlook COM** calls must stay on one thread (`outlook.call()`). Use `_table_rows()`/`_mail_rows()` for bulk reads; the To-Do folder ignores GetTable filters.
- **Tests:** run a second copy with `CC_PORT=<port> CC_DEV_NO_AUTH=1 python -m commandcenter serve --no-browser`. Never use 8765 for tests.
