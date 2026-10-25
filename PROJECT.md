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
- **Dependencies:** `requirements.txt` pins the top-level packages; `requirements.lock` pins everything (pip freeze). Set up or repair `.venv` with `.venv\Scripts\python.exe -m pip install -r requirements.lock`. After an intended upgrade, regenerate it with `pip freeze` (keep its header) and update the pins in `requirements.txt`.

## Configure it
`config.yaml` (created from `config.example.yaml`; every setting is commented). Secrets are never in config: work secrets live in
Windows Credential Manager (services `CommandCenter-SQL`, `CommandCenter-Toolkit`, `CommandCenter-Pulse`, `CommandCenter-Asana`); personal ones in the personal vault.
Appearance (colors, text size, density, effects) lives in the in-app **Profile** (avatar at top right), stored in SQLite (DPAPI-encrypted with the name and initials).

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
- **Overlay calendars (`feeds.py`):** read-only iCal feeds (Google Calendar "Secret address in iCal format", iCloud public calendar links; Skylight's source calendars), color-coded on the 10-day calendar with a legend to show/hide each. Manage via Calendar > Calendars.
  - Links live in the **personal vault**.
  - Hosts allowed: calendar.google.com, `*.google.com/calendar/` and `*.icloud.com` only. https only, global (public) addresses only (`is_global`), the connection goes to the address that was checked (no DNS rebinding; TLS still checks the name), no proxy, redirects re-checked, 10 MB cap.
  - Parsing is bounded: single events far from the window are dropped as text first, at most 5000 VEVENTs and 1000 events per calendar, SECONDLY/MINUTELY rules (and dense rules running since long ago) are skipped, 3 s budget per parse, one download + parse in flight per calendar.
  - Per-calendar detail level: full / titles (default) / busy. Fields are stripped before they're cached or returned.
  - Events are held in memory for 15 minutes, only while the vault is unlocked. Never written to Outlook or disk.
- **Calendar (`planner.py`):**
  - Outlook meetings for the next 10 days, including all-day events.
  - Dated follow-ups: tasks, flags due, snoozes, reply checks.
  - **Personal entries**, kept in the personal vault (encrypted with Joe's own passphrase), never sent to Outlook.
- **Alerts:** toast + sound + feed (toasts say only "<Source>: <category>" unless `alerts.toast_detail: full`, because Windows keeps toast text unencrypted); focus mode (manual, tray, or calendar events containing "focus"); morning briefing and end-of-day wrap-up; meeting reminders with a **Prep** card (attendees, recent mail, open follow-ups).
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
- **Verified sending:** every reply is reviewed before it sends.
  - `POST /api/mail/reply/preview` builds the reply in Outlook, reads its recipients and subject, discards it, and issues a single-use `approval_id` (5 min).
  - `POST /api/mail/reply` with `send:true` needs that approval for the exact same text, and Outlook re-checks the recipients right before Send.
  - The UI (`CC.replyDialog`, triage) shows recipients, flagging anyone outside Sonance, then a deliberate Send click. Enter never sends; Ctrl+Enter only opens the review.
  - No recipients in the preview: no Send button, only "open a draft in Outlook". A failed send goes back to the text ("Not sent: <reason>"), so the next Review gets a fresh approval. Unsent text is kept per message in memory until it's sent or opened as a draft.
  - Sent replies are logged locally in kv `mail:sent_log` (subject and recipients only, no bodies), encrypted.
- **Workday (`daily.py`):** Top 3 for today (carry-over; with more unfinished items than free slots Joe picks which, `pick` on `/api/daily/top3/carry`; briefing/wrap-up counts), uncommitted/unpushed work reminder (after `daily.uncommitted_after`), yesterday recap (commits, meetings, sent mail, tasks, Pulse closed; copy for stand-up), meeting notes + action items (→ To Do / Asana; "capture notes?" after each meeting). Top 3 and notes are DPAPI-encrypted work data: shown in Privacy mode and labelled "encrypted", not "private".
- **Triage (`triage.js`, Shift+T):** one-at-a-time queue with single-key actions; replies use the verified-send flow. An item cleared this session keeps Reply/Snooze/Done/Task off when revisited (a snooze can be undone).
- **Quick capture (`capture.js`, Ctrl+Alt+Space):** a global hotkey (`ui.capture_hotkey`) registered in `desktop.py`. Captures a task, or a personal note when the vault is unlocked; Task / Personal by click, Alt+1 / Alt+2 or the arrow keys on the choice (Tab just moves focus). It respects the app lock. In Privacy mode a saved Personal choice is never turned into a task: it says "Personal is off (Privacy mode)" and saves (Enter included) only after Task is picked.
- **Pulse aging:** business days a ticket has waited on Joe, warn/overdue buckets (`pulse.aging_*`), a daily alert, and a Today tile (only while something is aging or overdue).
- **Mail / Teams / Websites / SQL tabs:** as before. Teams unread count comes from a background browser profile once Joe logs in; chats open through `msteams:` links.

## Security model (security.py, desktop.py)
- **Loopback and same user:** binds 127.0.0.1 with SO_EXCLUSIVEADDRUSE. Each connection is checked against the owning process's Windows user (always on; the result is cached per PID + process creation time, so a recycled PID is checked again).
- **Request checks:** Host check; `Sec-Fetch-Site` cross-site/same-site refused; non-GET requests need the `X-CommandCenter` header plus our Origin; WebSockets need our exact Origin.
- **Session cookie:** per-launch secret held in memory, set by a one-time `/auth?k=` bootstrap (HttpOnly, SameSite=Strict). Every route needs it.
- **Response headers:** strict CSP, `frame-ancestors 'self'`, nosniff, no-referrer, `no-store`.
- **Window:** WebView2 with devtools, remote debugging, `file://` access, downloads, form autofill and password saving off (saved autofill data is cleared at each launch); no pywebview `js_api`. When a page's session ends (401), privacy.js removes personal items from the page.
- **App folder:** ACL limited to Joe, SYSTEM and Administrators, enforced at start. The folders above it (up to the drive root) are checked read-only at start; if another account can write, delete or re-permission one, a high-priority Security alert gives the `icacls ... /inheritance:r /grant:r` fix, and `GET /api/status` reports `folder_security`.
- **Project folders:** a project that other accounts can modify (folder, `.claude`, `.git`, settings/hook files) is flagged `writable_by_others` in `/api/projects` and shown in red; starting Claude (embedded or New window) or opening it in VS Code needs an explicit confirmation, sent as `allow_unsafe` / `unsafe=1`. Folders with `;` or `"` in the path are never listed. Git runs with fsmonitor, hooks, untracked cache, signature verification (`log.showSignature`, `gpg.*program`), pager, external diff and `core.sshCommand` neutralized (`projects.GIT_SAFE`; every `git log` gets `--no-show-signature`). A repo whose own config sets a program or include key (`gpg.*`, `log.showSignature`, `include*`, `core.sshCommand`, `diff.external`, textconv, credential helpers, filters, pager...) isn't run through git at all (`projects.RISKY_CONFIG`; only its config is read).
- **Hotbar:** only web, `msteams:`, folders and documents (.pdf .docx .xlsx .pptx .csv .txt .png .jpg .md); UNC only to NetBIOS names or `*.sonance.com`. Checked on save and on every open.
- **VS Code server:** archive paths must stay inside the build folder; node.exe and every .exe/.node/.dll must be validly Authenticode-signed (Microsoft; GitHub, Inc. for its bundled tools) before the server starts.
- **Lock screen:** its Quit/Hide/Unlock links work only while the lock page is showing.
- **Personal vault (`vault.py`, `privacy.py`):** personal data (entries, calendar links, quick-capture notes) is encrypted with AES-256-GCM, using a key derived from Joe's own passphrase (scrypt n=2^17). The passphrase is never stored, and it is not tied to company/domain accounts: DPAPI is avoided here because domain backup keys can recover it.
  - The key lives in memory only. Unlock is rate-limited and the counter persists.
  - It locks on Command Center Lock, on Windows lock / session switch, on system sleep and hibernate (power notification, plus a clock-gap check on resume), on restart and in Privacy mode (Ctrl+Shift+P, eye icon). No idle auto-lock unless `privacy.auto_lock_minutes` is set (default 0).
  - A lock or Privacy mode during an unlock (while scrypt runs) wins: the new key is discarded. Unlocking turns off only a Privacy mode that was already on when the unlock started.
  - Changing the passphrase re-encrypts under the same locks as every write, and stops (changing nothing) if a record can't be decrypted. Derived key bytes are wiped once the AES-GCM object exists (best effort).
  - The Privacy & Data panel lists every source (Work/Personal) with what's stored, how it's protected and how long it's kept.
  - Forgotten passphrase: "Delete all personal data" is the only option.
- **Work data at rest (`db.py`):** alert text, the sent log, follow-up subjects/names, settings with names (including the Profile), and the Toolkit change log (pk, before, after, error) are DPAPI-encrypted (`ccsealed:v1:`). A startup + daily migration encrypts any plaintext. The connection uses `secure_delete=ON` with regular VACUUM. Sealing, VACUUM and moves of old copies run with the rollback journal in PERSIST mode, and the journal is overwritten with zeros afterwards (it holds the old plaintext pages; `db.scrubbed_journal`). Pulse ticket state is sealed too. What stays in clear (dates, ids, counts) is listed in Privacy & Data under "Unencrypted metadata".
- **Retention (`retention:` in config):** alerts 30 days, sent log 90, Toolkit change log 365, `seen` 30, dismissed follow-ups 60.
- **Secrets:** only in Credential Manager, the personal vault or memory. Logs, the database and API responses were swept for secrets.
  Logi passwords never come back in lists (`has_password` only); one user's password is fetched on an explicit Show/Clipboard click (POST `/api/toolkit/logi/password`), logged as a `reveal` in `toolkit_changes` without the value. New Logi passwords: 16 random characters (crypto.getRandomValues), upper/lower/digit/symbol, no look-alikes.
- **Log:** `data/commandcenter.log`, rotated at 5 MB (3 old files). Alerts are logged by id, source and priority only, never titles or bodies. Request errors are logged as type, path and code locations only, never the exception message (it can quote row values). At startup, older lines carrying work data are rewritten as `[redacted]`.
- **Fonts and network:** fonts are self-hosted (`static/fonts`, CSP `font-src 'self'`). The page makes no requests beyond 127.0.0.1.
- **Dev bypass:** `CC_DEV_NO_AUTH=1` works in `serve` mode only and logs a warning.

## Code layout
- `commandcenter/app.py`: core routes. Auto-loads the feature modules security, toolkit, pulse, terminal, vscode, planner, profile, feeds, daily, vault and privacy (each exposes `router` and optionally `background`).
- `commandcenter/connectors/`: outlook, web, sql, asana, weather, teams. `people.py` handles internal domains and urgent people.
- `commandcenter/static/`:
  - Core UI: `app.js`, `style.css`, `index.html`.
  - Feature modules: `projects`, `vscode`, `toolkit`, `pulse`, `planner`, `profile`, `splash`, `daily`, `triage`, `capture`, `privacy` (`.js`/`.css`).
  - Shared assets: `vendor/` (xterm), `jc-logo.svg` and `icon.ico` (the JC monogram).
- `data/` (git-ignored, private ACL): the SQLite DB, log, browser profiles, `vscode/`, and the ACL backup.

## Constraints
- Cylance blocks `.ps1` files, so never create them. Run everything through `run.cmd` or `.venv\Scripts\python.exe`.
- **Outlook COM** calls must stay on one thread (`outlook.call()`). Use `_table_rows()`/`_mail_rows()` for bulk reads; the To-Do folder ignores GetTable filters.
- **Tests:** run a second copy with `CC_PORT=<port> CC_DEV_NO_AUTH=1 python -m commandcenter serve --no-browser`. Never use 8765 for tests.

## Repo guard (protects shared repos such as Pulse)
`tools/repo-guard/` holds local-only git hooks, installed in `C:\Sonance\internal\Pulse\pulse-nextjs\.git\hooks` (which covers its worktrees).
They block commits and pushes that would carry Command Center details (paths, names, cookie and keyring names), JWTs or private keys,
or local-only files (`.env`, `.vscode/`, `.claude/settings.local.json`, `*.dpapi`). Hooks live in `.git` and are never pushed.
Command Center's own repo has no remote. To guard another repo, copy `guard.sh` to `<repo>/.git/hooks/command-center-guard.sh` along with `pre-commit` and `pre-push`.
