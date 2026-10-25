# CC session log

Newest first. Written by the `close-session` skill, read by `start-session`.

## 2026-10-24 (about 6 PM - 11 PM local; the first session crashed at 9 PM and was resumed)

**Summary:** Big build day. Glass restyle, spam blocks, SQL certificate pin, Pulse site inside CC with a Pulse | Code focus layout, SSMS docked in the SQL tab, VS Code · Claude replacing the terminal Claude tab, a reworked Today, a Personal tab with pop-up reminders, Teams unread counts, an off-hours log, and many fixes. Three pieces of work were still being built when Joe left (see In flight).

### Done
- `25b4121` Glass launcher restyle (Inter + Orbitron, frosted panels, Profile switches); spam blocks (sender / domain / subject phrase, preview, Junk Email via two Outlook rules plus an app filter, Undo, Spam blocks list); Toolkit SQL certificate pin (`connectors/sqlcert.py`); "Needs your reply" only when the email names Joe outside the quoted history; Open in Outlook brings the window forward; View on flagged opens that email; Teams sign-in startup retry.
- `2875d3f` CLAUDE.md: CC working rules (loads PROJECT.md).
- `08c3b60`:
  - Pulse (WORK) as a native WebView2 view of pulse.sonance.com with Prod | Dev; Pulse Feedback ticket list in COMMS; Focus layout "⧉ Pulse | Code".
  - SSMS docked in the SQL tab with a "CC queries" toggle for the old saved queries; shared native-view code in `overlay.py` / `nativestage.js`.
  - VS Code · Claude tab: desktop extensions and settings mirrored on every start; Claude panel opens by itself; Sync from desktop; project picker only on that tab.
  - Today:
    - layout: clock and weather in one panel; Agenda / Tasks / Follow-ups tabs; follow-ups as a list;
    - KPI tiles open and highlight what they count;
    - Recent alerts show unread only;
    - tasks get a priority pill (Outlook Importance, or local for Asana); Unflag on flagged emails;
    - flags set before `flagged_since` are ignored;
    - off-hours tile;
    - hotbar and Top 3 removed.
  - Personal tab: reminders (text in the vault, times-only DPAPI schedule), pop-ups that stay until Done, Snooze 15 min, missed reminders batched, generic toasts.
  - Teams: unread DMs + mentions badge, counts and Recent list, an alert per new DM or mention.
  - Fixes from the security review (no high-severity findings): reminder off/on no false "missed"; VS Code mirror can't delete outside `data/vscode` or wipe copies on an empty desktop list, copies Marketplace extensions only, skips secret-like settings; Asana priorities past 100 tasks; Pulse view links need a real click, rate-limited; Ctrl+Shift+P forwarded; `.xls` downloads removed; Alerts badge no longer shows 0.
- Pulse read-only DB login (`commandcenter_ro`) created in production Pulse through Cortex, verified read-only, TLS `verify-full` with `data/certs/supabase-prod-ca-2021.crt`.
- Skills `start-session` / `close-session` and this log (`.claude/skills/`, `SESSIONS.md`).
- Artifact "JC Command Center" (shared by link) republished: glass look, generic wording only.

### Decisions
- SQL server's self-signed certificate: **pin it** (not "trust without verifying", not "wait for IT").
- Pulse: real site embedded, **one tab with a Prod | Dev switch**, keep background ticket alerts. Ticket list moved to a COMMS **Pulse Feedback** tab; WORK Pulse is the site only.
- Claude: **replace** the terminal tab with embedded VS Code + the Claude Code extension.
- SQL tab: **dock real SSMS** inside CC, accepting the risks below (not MSSQL-in-VS-Code, not a launch button).
- Off-hours: count **active-at-PC** time; show a **Today tile + weekly total** (no Friday summary, no CSV).
- Spam: Joe picks sender / domain / phrase each time; blocked mail goes to **Junk Email**; enforced by **Outlook rule + app filter**; existing matches are moved only after a preview.
- Flagged mail: Joe has 7,766 flagged emails. **Hide flags set before today in CC; don't clear them in Outlook.** The bulk unflag was counted only, nothing changed.
- Task priority on Outlook tasks is stored in **Outlook's own Importance**, so it shows on his phone.
- Personal reminders: the pop-up closes **only with Done** (no Dismiss); **Snooze on by default, 15 min**; **Weekdays** is the default repeat.
- Teams: **full chat embedded in CC** (a native Teams web view), not just previews.
- Important senders: a separate tier from Urgent people.
- Pulse Supabase `service_role` key (was readable by other local accounts before the lock-down; all copies on this PC were removed): Joe decided **not to rotate it** and not to track it. Don't raise it again or list it as a to-do. It's still recorded in `C:\Sonance\internal\Security\Pulse-security-findings-2026-10-24.md`.
- Today: hotbar not needed (alerts have "open with" actions, sites are embedded); Top 3 strip not needed.

### Settings changed (config.yaml, not in git)
- `followups.needs_reply_requires_mention: true`, `followups.mention_names: ["Joe", "Joe Cherbak"]`
- `followups.flagged_since: "2026-10-24"`
- `teams.poll_minutes: 1` (was 3)
- `outlook.meetings.remind_minutes: [15, 5, 0]` (was [10, 1]; 0 = "starts now")
- New sections `spam:` and `offhours:` (work hours 08:00-17:00 Mon-Fri)

### Waiting on a restart
- CC was last restarted at 10:20 PM, before `08c3b60` (10:29 PM) finished. The next restart loads all of `08c3b60` plus the in-flight work below once it's committed.
- **Not safe to restart right now**: agents were mid-edit when Joe left. Restart only after a commit with a clean tree (`git status` empty apart from `config.yaml`).
- Uncommitted main-session edits that go with it: meeting reminders 15/5/now plus a fix so "now" can't be skipped (`connectors/outlook.py`, `config.example.yaml`, `PROJECT.md`).

### Waiting on Joe
- [ ] Restart CC once the in-flight work is committed; the first start takes about 30 s longer while it copies VS Code extensions.
- [ ] Toolkit → Connection → **Test** (certificate is pinned and the password is saved, but no successful test yet).
- [ ] SSMS: check Tools → Options → Environment → **AutoRecover** is on (1-5 min). In the connect dialog: Encryption Mandatory + Trust server certificate (his saved connections already have both).
- [ ] Sign in to Pulse in the Pulse tab (Okta), and to the Teams chat view once it's built.
- [ ] VS Code · Claude: open a project and **Trust** the folder.
- [ ] Personal tab: add the three weekday reminders he described. Do it after the restart; the vault is already set up.
- [ ] Calendar → Calendars: add the Google / iCloud links (he was doing iCloud).
- [ ] Still open from the account setup list: urgent people, Asana token, Windows Hello.

### In flight / not finished
These were all running when Joe left. If this session ended, they stopped too: check what landed and resume or redo.
- **Important senders**: in-app VIP list (kv `people:important`), "★ Mark important" on mail / follow-ups (sender or `@domain`, never shared providers or internal domains), high-priority alerts, "Important senders" follow-up group, manage dialog, `/api/people/important`. Files: people.py, outlook.py, today.py, app.py, app.js, privacy.py.
- **Teams chat view**: native Teams web view in the Teams tab (own profile `data/webview-teams`); clicking a Recent DM or mention opens the full chat; sign-in allowlist; no camera or mic. Files: desktop.py, security.py, overlay.py / new `siteview.py`, nativestage.js, app.js Teams section, teams.css.
- **Interactive preview artifact** ("Command Center", private): rebuilt and passing every check (651 KB; all tabs, KPI tiles, palette, triage and capture at 3 screen sizes; no outside requests except Google Fonts). Not published yet. Rebuild it after the Teams view and Important senders land, then read the whole file and publish it to https://claude.ai/artifact/4nkSbtnowfNmg2ukvSqN2Q. Rebuild command: `.venv\Scripts\python.exe <scratchpad>\preview\build.py`. The scratchpad is `C:\Users\JoeC\AppData\Local\Temp\claude\c--sonance-internal-CommandCenter\f5ff0096-e873-4797-8626-cbcf06267293\scratchpad`; it is temporary, so copy `preview\` into the repo if it should outlive this session. The page still contains the word "Sonance" and "Joe" in code comments; it's private.
- **Walkthrough artifact** ("Command Center Walkthrough", private): draft written (`scratchpad\artifacts\walkthrough.html`, 18 chapters). Held back so the Teams chat view and Important senders can be added first. Not published yet.
- After these land: review, run the test suites (spam, reminders, the all-tab smoke test), commit, then tell Joe to restart.

### Known issues
- The terminal inside CC's embedded VS Code fails to start (PowerShell exits with 0xC0000022, access denied). This predates today; cause unknown (not the copied extensions).
- "Git Base not loaded" errors in embedded VS Code after trusting a folder, until the window reloads.
- A hard crash or kill of CC takes a docked SSMS down with it. Normal exits hand SSMS back.
- Untested: real Okta sign-in in the Pulse view; Ctrl+Shift+P while SSMS has focus; Teams "Open" deep links on an actually-unread chat; Teams channel-message links may not open.
- "Open in Pulse" on alerts still opens the default browser.
- `static/vendor/` (xterm) is now unused and can be deleted.
- The Today tools row wraps under the tabs in the narrow split COMMS pane.

### Incidents
- The first session crashed at about 9 PM. It was resumed from its saved transcript in `~\.claude\projects\C--Users-JoeC\`.
- An agent test popped a CC "can't use port 8819" error box on Joe's screen. Tests now check ports and suppress dialogs.
- An agent probe briefly opened an SSMS window on Joe's screen (a dropdown and a "Change Password" box appeared) and killed it after about 45 s. Nothing was connected or saved. Later SSMS tests ran on a hidden desktop.
- A test keystroke sent a stray prompt ("pen User Settings (JSON)") to a real Claude session in the CommandCenter project. Text reply only, no tools, stopped within seconds. Transcript `947c2514-…jsonl`; safe to delete.
- Joe restarted CC at 9:47 PM and 10:20 PM while agents were mid-edit. Both starts loaded without errors (checked with a test copy).
