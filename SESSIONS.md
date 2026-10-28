# CC session log

Newest first. Written by the `close-session` skill, read by `start-session`.

## 2026-10-28, third session (about 2:50 PM - 3:30 PM local)

**Summary:** Email alerts now need the email to name Joe; urgent people's mail still always alerts. Joe asked about ChatGPT voice control, heard the options and parked it. He restarted at 3:27 PM after the commit, so everything is loaded.

### Done
- `257545c` **Mail alerts only when Joe is named.**
  - A new email alerts only if its subject, or the new text above the quoted history, has his name (`followups.mention_names`, his Outlook name), his address or an @mention. It's the same check as Follow-ups' Needs your reply.
  - What it applies to:
    - **Needs his name to alert:** important senders and every `outlook.rules` match (VIP sender, High importance, Flagged, Keyword).
    - **Always alerts:** urgent people's mail.
    - **Never alerts:** automated mail (no-reply senders, list headers) and Teams' notification emails, so the "Teams activity" rule is now dead. Teams alerts come from the Teams reader.
  - Off switch: `outlook.alerts_require_mention: false`.
  - Tested by feeding `scan_new_mail` 11 fake mail items with no Outlook (scratchpad `mention_alert_test.py`), with the setting on and off. Documented in PROJECT.md and config.example.yaml.

### Decisions
- **Exceptions to the "names Joe" rule:** only urgent people's mail always alerts. Joe turned down:
  - no exceptions at all;
  - exempting important senders too.
- **Voice control (ChatGPT voice):** parked ("nevermind for now"). What was discussed, so it isn't re-researched:
  - The ChatGPT app's voice mode can't be embedded.
  - OpenAI's Realtime API could drive CC by push-to-talk. It needs an API key, billed separately from ChatGPT.
  - Sends would still need the review click.
  - The open question is whether Sonance allows work mail to go to OpenAI (or Claude).
  - Alternatives offered:
    - speech-to-text on the PC plus Claude;
    - fixed voice commands with Windows speech, entirely on the PC.

### Waiting on a restart
- Nothing. The 3:27 PM restart loaded `257545c`, with no errors in the log since and both docks back. The tree is clean and safe to restart on.

### Waiting on Joe
- [ ] Watch whether any wanted email stops alerting (one that is meant for him but doesn't use his name). The fix is to add names to `followups.mention_names`, add the sender to urgent people, or set `outlook.alerts_require_mention: false`.
- [ ] From the second session: check the new Today with real data, and share the demo page if he wants.

## 2026-10-28, second session (about 11:30 AM - 2:50 PM local)

**Summary:** Built a shareable CC demo page in the Sonance skin. Today is now a one-screen overview: no tabs, laid out for the screen size. Empty follow-up groups are hidden. Joe restarted at 2:46 PM after the last code commit, so everything is loaded and checked in the log.

### Done
- **Demo artifact** (not in the repo): https://claude.ai/artifact/JeSJGvmLKRc6Ukuukxuu4t, private until Joe shares it.
  - A clickable mock of CC in the Sonance / Pulse skin, with sample data only. No real mail, chats, tickets or server names.
  - Covers: Today (Block time, Teams Recent DMs), the Alerts table, Teams with the hideable list, docked VS Code / SSMS, and short notes on features and privacy.
  - The source is in the session scratchpad only, so a later change means rebuilding it from the published page (Artifact read).
- `2446ebc` **Follow-ups:** groups with nothing in them are hidden (no "0" headings). With nothing at all, the card says "No follow-ups."
- `d0427c4` **Today redesign:** every card shows at once instead of the Agenda / Tasks / Follow-ups tabs.
  - Cards: Needs you (follow-ups), Schedule, Tasks, Teams Recent DMs and the hour grid.
  - Columns follow the pane width:
    - 1 column under 44rem;
    - 2 columns at 44-72rem (the laptop's split COMMS pane);
    - 3 columns at 72rem and up, where the hour grid gets its own column.
  - Detail follows each card's width:
    - narrow cards are summaries: one line a row, the main buttons only, and the first 3 rows of each follow-up group plus "N more" (Urgent always shows all);
    - wider cards add location, organizer, due text, project and the weather detail.
  - Other changes:
    - a slim clock / weather strip, with the stat tiles as one-line chips;
    - task Snooze / Open moved into a ⋯ menu;
    - the hour grid fetches nothing while it's hidden;
    - `CC.todayTab(name)` and the tiles now scroll to a card.
  - Tested on a scratch copy at 560 / 800 / 1280×720 / 1920 split and single / 2560 split and single / 3840, in both skins. There was no sideways scroll at any size, and no page errors.

### Decisions
- **Today layout:** Joe chose "everything at once", with the meeting list as the Schedule and the hour grid only as a third column on a wide pane. He turned down:
  - the hour grid as the Schedule;
  - keeping the tabs and only tightening the top.
- **Screen sizes:** Joe wants Today to suit the laptop (1920×1080 at 100%) and bigger screens: summaries or short text when small, more detail when large.
- **Demo page:** the Sonance skin and sample data only.

### Waiting on a restart
- Nothing. Joe's 2:46 PM restart loaded `2446ebc` and `d0427c4`, and SSMS and VS Code docked again. The tree is clean and safe to restart on.

### Waiting on Joe
- [ ] Look at the new Today with real mail and calendar on the laptop, and say what feels cramped or sparse. It was only tested with sample data.
- [ ] Share the demo page from its Share menu if he wants others to see it. It's private until then.

### Known issues
- Every restart logs "VS Code dock: check failed (ObjectDisposedException)" once, while the window closes. It has done so since 10/26 and it's harmless: the windows dock again. Fix it only if it starts to matter.

## 2026-10-28 (about 8:35 AM - 11:30 AM local)

**Summary:** Fixed the morning freeze (a deadlock when a second launch or the tray showed CC) and the misplaced native views on Joe's 150% displays. Then a run of UI changes: Alerts as an aligned table, Close that always ends the process, block time and Teams DMs on Today, an SSMS-only SQL tab, SSMS re-docking after restarts and a hideable Teams list. CC now has a private GitHub backup, pushed by `close-session`. Everything is committed and loaded: Joe restarted at 10:54 after the last commit, and the restart was verified in the log.

### Done
- `a930364` **Freeze fix.** CC hung (pythonw "not responding" on 10/25, 10/26, 10/27 and 10/28) whenever a second launch or the tray "Open" showed the already-running copy.
  - Cause: `DesktopApp.show` set pywebview's `window.on_top` from the server thread. pythonnet keeps the GIL through a .NET property set, and `TopMost` waits on the UI thread, which was itself waiting for the GIL.
  - Fix: bring the window forward with ctypes `SetWindowPos`, and run `/api/window/show` off the event loop.
  - Diagnosed with a py-spy dump of the hung process. A stand-alone repro deadlocked every run on the old code; the new code passed 3 × 1000 rounds. PROJECT.md Constraints now has a "window from other threads" rule.
- `064b350` **Native views at the real DPI.** The Pulse and Teams views and the docked VS Code / SSMS were drawn at 2/3 size on Joe's 4K monitors at 150%. WinForms' `DeviceDpi` stays 96; `overlay._geometry` now uses `GetDpiForWindow`. Verified live after the restart.
- `ed69401`, `aa33866` **Alerts tab.**
  - Layout: All | Unread, a kind filter (Mail / Teams / Calendar / Pulse / Other), both remembered; grouped by day with sticky headers.
  - A table with column headings (Time · Source · Alert · Action): the list is one CSS grid and every row a subgrid, so the columns line up.
  - Clear and Reload config moved into a ⋯ menu. The list fills the tab (it was capped at 60vh in split view).
- `188d502` **⏻ is just Close** (confirm, then quit); its menu duplicated header buttons.
  - Closing always ends the process: `desktop.exit_process` gives atexit hooks 10 s, then `os._exit`, and a 45 s watchdog runs from Close / Restart.
  - Tested end to end on a desktop-mode scratch copy driven over CDP: a normal close was gone in 4.7 s, one with a stuck Outlook call in 25 s, and the old code was still running after 75 s.
- `9c4d9a7`, `4e1693e` **Private GitHub backup.** `origin` = github.com/cherbakj/CommandCenter. Joe did the first push himself.
  - `close-session` now pushes the day's commits with `git push origin main`, which is allowed in the git-ignored `.claude/settings.local.json`.
  - CLAUDE.md and PROJECT.md updated.
- `3965131` **Today.**
  - **＋ Block time** (Agenda toolbar), **Block** on free slots and Ctrl+K add a busy Outlook appointment (existing `/api/calendar/block`). "Blocked" / "Lunch" titles are refused.
  - No meeting notes on Today: no Notes button and no per-meeting Notes. The Calendar tab, Prep and Ctrl+K keep them.
  - A **Teams · Recent DMs** panel under the main panel: the newest 6, served by `GET /api/teams/recent`, which skips Outlook. A row opens that chat in the Teams tab.
  - Teams tab: Recent DMs and mentions is collapsible, collapsed by default and remembered.
- `f961260` **SQL tab is the docked SSMS only.**
  - Removed: the CC queries view, its switch, the `/api/sql*` routes and the palette's "Run query".
  - The order lookup still works; its password is set via `run.cmd setpass main`.
- `2b03ccf` **SSMS docked again after Restart / Close / Lock**, like VS Code.
  - The docked window is recorded in sealed kv `ssms:dock`, and the next start re-docks it once unlocked. Undock clears it.
  - Privacy & Data has a "Docked windows" entry (VS Code's record was missing too).
  - Tested with a throwaway SSMS in a scratch copy, plus a stand-in window for the record logic. Verified live at the 10:54 restart (pid 43312 re-docked).
- `220e219` **Teams tab:** ⟨ on the Teams bar hides the status / Recent column so Teams on the web gets the whole tab, and ⟩ List brings it back (remembered). Wide panes only.
- `8599513` Docs: the launch sign-in is off in Joe's config.

### Decisions
- GitHub backup: private repo under cherbakj, everything tracked (SESSIONS.md included). Joe created the repo on the web rather than installing the GitHub CLI.
- No launch sign-in: the Windows sign-in is enough. Lock still asks, and the personal vault keeps its passphrase.
- Meeting notes: not on Today, and no "capture notes?" prompts. They stay available on the Calendar tab and in Ctrl+K.
- SQL: Joe only ever docks SSMS, so the CC queries view is gone.
- Teams tab: Recent collapsed by default; the list can be hidden.
- Per-monitor DPI ("adapt to the screen it's on") wasn't built. Both of Joe's monitors are 150% and fine after the DPI fix; offered if he uses the laptop screen or another monitor with different scaling.

### Settings changed (config.yaml, not in git)
- `daily.meeting_notes_prompt: false`: no "capture notes?" alert after meetings.
- `security.unlock_on_start: false`: no Windows Hello / password at launch.

### Waiting on a restart
- Nothing. The last commit was `220e219` (10:33); CC restarted at 10:43 and 10:54. The tree is clean and safe to restart on.

### Waiting on Joe
- [ ] Optional: try Profile > Skin > Pulse and say what to adjust.
- [ ] Still open from earlier: Toolkit → Connection → Test; SSMS AutoRecover on; sign in to the Pulse view; mark important senders; Google/iCloud calendar links; urgent people, Asana token.
- Done today: yesterday's leftover test processes are gone, and a personal reminder was seen "brought forward" live at 10:30.

### In flight / not finished
- Nothing uncommitted. No agents and no test servers running. The scratch copy lives in the session scratchpad (`cctest`, made-up data only).

### Known issues
- Windows Hello reports DeviceNotPresent on this PC, so Lock falls back to the Windows password.
- The Today Teams panel and the Teams tab's Recent list stay empty until the first background Teams read after a start (names are memory-only).
- Carried over:
  - `config.yaml` line 39 comment on `teams.poll_minutes`;
  - the start-up page always plays the puzzle;
  - a force-kill or crash of CC still closes docked VS Code / SSMS;
  - the web VS Code terminal fails (0xC0000022);
  - a hung docked SSMS or VS Code slows CC;
  - the keyboard follows only a mouse click into docked VS Code.

### Incidents
- Joe force-closed the hung CC this morning (the freeze above). Before he did, its stacks were dumped read-only with py-spy, installed in the scratchpad (not the venv).
- Test windows appeared on Joe's screen: a small CC test copy several times, a pywebview repro window and a throwaway SSMS. Joe accidentally closed one CC test copy mid-test. No harm: the remaining check was done in code. Only processes the tests started were ended; Joe's CC and his SSMS (43312) were never touched.
- The first GitHub push from a session was blocked by the permission check. Joe pushed himself, then allowed exactly `git push origin main` for `close-session`.

## 2026-10-27 (about 8:40 AM - 5:15 PM local)

**Summary:** A long day of CC changes: a header Power menu, Teams fixes (signing in with the Windows work account, checks every minute, opening a chat in place), alerts that go away once answered, and a Pulse skin. The work ended on performance: CC's Teams reader no longer loads Teams from scratch every minute. Everything is committed and loaded (CC restarted at 1:52 PM); the tree is clean.

### Done
- `090822f` Header: a ⏻ Power menu replaces the ✕ close-to-tray button. It has Minimize, Windowed / Full screen, Restart and Close Command Center; Close is a real quit after a confirm and leaves docked VS Code / SSMS running. Lock gets a padlock.
- `97544b2` Teams view signs in with the Windows work account (OS single sign-on). Before, Sign in did nothing, because Teams wanted a pop-up and the view sent it to the browser. Pulse keeps SSO off.
- `7f1721f` After an update, the window never runs a cached older script. Before this, a stale app.js left `window.CC` undefined: no WORK tabs and no window buttons. `index.html` now stamps `/static/` links with `?v=<mtime>`, and `/static/` is sent with no-cache.
- `a1bbec1` Teams checks run every `poll_minutes` from the start of one check to the next. They had been about every 80 s.
- `b90d74d` Teams view: opening a chat while Teams is up changes only the `#/l/chat/…` part of the address, so the chat opens in about a second without reloading Teams.
- `78264d3` The window is only ever open, minimized or closed. Alt+F4 minimizes, and the "Hide to tray" links are gone.
- `2694381` Alerts go away once answered:
  - A reply sent from CC deletes that email's alerts.
  - Teams alerts are deleted once their chat is no longer unread.
  - Pulse no longer alerts on "Waiting on reporter".
- `81de205` Profile > Skin: **Pulse** (`static/skin-pulse.css`). It uses Pulse's dark-theme tokens, self-hosted Montserrat, Sonance Blue, underline tabs, KPI-card tiles and a flat look (glow, glass, motion and dot grid are off). Command Center stays the default skin.
- `4a1fd03` Teams reader keeps its hidden Edge open between checks:
  - Before, each check cold-booted Teams (about 20 s, bursts of 3–8 cores), every minute.
  - It starts afresh every `teams.keep_open_minutes` (60), after a bad read and at sign-in; it closes on quit.
  - Verified live after the 1:52 PM restart: one open, then a read every minute, using 0.004 cores over a minute and holding about 2 GB of memory.
- PC load check (not CC code). The main load was two Pulse `next dev` servers (about 28 GB of RAM, about 2.6 cores) and a stuck `check-site-fingerprint.mjs`. Joe stopped them. RAM use went down to about 20 / 32 GB.
- Gave Joe a list of what not to keep running:
  - stop: dev servers and leftovers from old Claude sessions, Teams / Outlook / Pulse open in browser tabs, idle SSMS, VS Code windows for projects not in use;
  - optional: the Teams app (needed for calls), the Claude desktop app, the Asana app;
  - keep: Outlook desktop, CC.

### Decisions
- Teams view: OS single sign-on on (Joe's call). Pulse view stays off.
- Window: open, minimized or closed only. Nothing hides to the tray.
- No Pulse alert when a ticket moves to "Waiting on reporter". Coming back to Open still alerts.
- Kept `teams.poll_minutes: 1`. After the reader fix each check is cheap. Setting `keep_open_minutes: 0` (frees about 2 GB but brings back the minute-by-minute cold boots) was advised against.

### Waiting on a restart
- Nothing. CC started at 1:52 PM, after the last commit (`4a1fd03`, 12:26 PM). The tree is clean and safe to restart.

### Waiting on Joe
- [ ] OK to stop two leftover test processes from Oct 24 (python PIDs 25240 and 16088, a few MB, harmless). Stopping them was refused by the permission check without his OK.
- [ ] Optional: try Profile > Skin > Pulse and say what to adjust. A Pulse light skin was offered, not asked for.
- [ ] Still open from earlier: watch for a personal reminder actually being "brought forward" (see Known issues); Toolkit → Connection → Test; SSMS AutoRecover on; sign in to the Pulse view; mark important senders; Google/iCloud calendar links; urgent people, Asana token; optionally move blocked-sender mail to Junk via Spam.

### In flight / not finished
- Nothing uncommitted. No agents and no test servers running. The scratch test copy and its data were deleted.

### Known issues
- Personal reminders fired today (10:00, 10:15, 10:30, 12:00) and showed in CC. Three found CC "already in front". The 10:00 one was "kept back (full-screen app)", which could be a false positive (CC is full screen itself); not looked into. "Brought forward" hasn't been seen live yet.
- The Teams reader now holds about 2 GB at all times (the trade for the CPU saving).
- `config.yaml` line 39: the comment on `teams.poll_minutes` still says each check loads Teams. `config.example.yaml` is updated.
- Carried over:
  - the start-up page always plays the puzzle;
  - a force-kill or crash of CC closes docked VS Code / SSMS;
  - the web VS Code terminal fails (0xC0000022);
  - a hung docked SSMS or VS Code slows CC;
  - the keyboard follows only a mouse click into docked VS Code.

### Incidents
- A throwaway test copy on port 8817 read Joe's real Outlook (read-only) for its Today tab during the skin check. Its screenshot, which showed real data, and its data folder were deleted, and the server was stopped. Nothing was written.
- Performance check: read-only process and counter sampling only. No processes were stopped; Joe stopped the Pulse dev servers himself.

## 2026-10-26, third session (about 6:40 PM - 8:00 PM local)

**Summary:** Joe had never seen a personal reminder: none had been saved, and the pop-up only ever showed inside CC's window. Due reminders now bring CC to the front (not while it's locked or the screen may be shared). Joe saved his reminders for tomorrow; the bring-to-front hasn't been seen firing live yet.

### Done
- `6f8da62` Personal reminders come to the front: a due reminder (or a snooze running out) restores CC from the tray / minimized and brings it in front of other apps, with the pop-up centered (`DesktopApp.raise_for_reminder`, `reminders.front_hook`, `CC.reminderPoll`). Kept back (toast + sound only, pop-up waits in CC) while CC is locked or when `reminders.share_risk` finds presentation mode / a full-screen app other than CC, the mic or camera in use (Windows' CapabilityAccessManager records), or a duplicated display. Logs "brought forward" / "kept back (<reason>)". Tests: 12/12 logic checks with a fake window and user32, real detection read-only on this PC (all clear), preview page check (hook present, no errors). PROJECT.md updated.

### Decisions
- Bring to front, but not while sharing (chosen over "always jump to front" and "a small always-on-top window").
- Vault across restarts: **leave as is**. It stays unlocked all day but a CC restart still locks it (the key is never written to disk). Turned down: keeping the key until midnight sealed by a Windows Hello/TPM key, or by DPAPI.

### Waiting on a restart
- Nothing: CC started at 7:00 PM, after `6f8da62` (6:52 PM). Tree clean, safe to restart.

### Waiting on Joe
- [ ] Tomorrow: watch for the first personal reminder (four saved at 7:55-7:56 PM: ids fb79b6, 6112d0, dc3a5c, 9a80e3) and say whether CC came forward; the log line "Personal reminders: window …" says what happened.
- [ ] Optional: blocked-sender mail still in the Inbox via Spam → "Also move matching emails to Junk".
- [ ] Still open from earlier: Toolkit → Connection → Test; SSMS AutoRecover on; sign in to the Pulse and Teams views; mark important senders; Google/iCloud calendar links; urgent people, Asana token.

### In flight / not finished
- Nothing uncommitted, no agents or test servers running. A scratch test copy (code only) was made and deleted without being started.

### Known issues
- Bring-to-front not yet seen live (only tested with fakes). Two demo attempts at 7:05 and 7:10 didn't happen because the reminders were never saved (the vault was locked after the 7:00 restart, then not re-saved); saving itself works (4 saves logged).
- If CC is already in front while Joe shares his screen, the pop-up still shows its text (as before). Offered to hide it during calls; not asked for.
- Ad-hoc screen shares without the mic or camera on aren't detected (only the mic/camera, presentation/full-screen and duplicated-display signals).
- Carried over: start-up page always plays the puzzle; a force-kill or crash of CC closes docked VS Code / SSMS; web VS Code terminal fails (0xC0000022); a hung docked SSMS or VS Code slows CC; the keyboard follows only a mouse click into docked VS Code.

### Incidents
- None. Only read-only checks on the PC: microphone/camera privacy registry keys, the notification state and display topology, and the CC log.

## 2026-10-26, second session (about 2:25 PM - 4:30 PM local)

**Summary:** Fixed typing in the docked VS Code (the keyboard stayed on CC's dashboard) and added an Inbox | Sent switch to the Mail tab. Both are committed and loaded (CC restarted at 3:06 PM); the tree is clean.

### Done
- `917ea5d` Docked VS Code: a click inside it now gives it the keyboard. Cause: as a child window VS Code (Chromium) is never the active window, so it never took the focus and keys went to the dashboard's WebView2. The panel now gets `WM_PARENTNOTIFY` for button presses through a window subclass (`SetWindowSubclass`) and calls `SetFocus` on the docked window. Tested with a stand-in window + throwaway VS Code profile and real input: 3/8 checks without the fix (bug reproduced), 8/8 with it. Confirmed on the live app with a read-only focus watcher (a click on VS Code moved focus to it in 7 ms); Joe confirmed he can type.
- `c738cc1` Mail tab: **Inbox | Sent** switch (`folder=inbox|sent` on `/api/mail`, `outlook.sent_mail`, localStorage `cc.mail.folder`). Sent shows the newest 50 sent emails as "To: …" rows; All | Unread hides there; picking a folder ends a search (search still covers both); sent mail gets no Spam / ☆ actions (`get_message` returns `folder: "Sent"`). Preview mock serves Sent mail. Tests: read-only Outlook check (50 rows, newest first, ~1.2 s warm, reader detects Sent) and a Playwright preview check at 1920 / 1280 / 390 (no page errors, no sideways scroll; the only 3 fails were the test's case-sensitive "Sent" vs the CSS-uppercased "SENT").

### Decisions
- Joe changed the password he had pasted in the earlier session, so redacting the old transcript is no longer needed.
- Inbox | Sent: search keeps covering both folders rather than only the chosen one.

### Waiting on a restart
- Nothing: CC started at 3:06 PM, after the last commit (2:45 PM). Tree clean.

### Waiting on Joe
- [ ] Add the three weekday personal reminders (Personal tab); none saved yet.
- [ ] Optional: move blocked-sender mail still in the Inbox via Spam → "Also move matching emails to Junk".
- [ ] Still open from earlier: Toolkit → Connection → Test; SSMS AutoRecover on; sign in to the Pulse and Teams views; mark important senders; Google/iCloud calendar links; urgent people, Asana token.

### In flight / not finished
- Nothing uncommitted, no agents, no test servers left running.

### Known issues
- Docked VS Code: the keyboard follows a mouse click only. Choosing a project's window in the tab bar doesn't move the keyboard into VS Code (click inside it).
- Carried over: start-up page always plays the puzzle; a force-kill or crash of CC closes docked VS Code / SSMS windows; web VS Code terminal fails (0xC0000022); a hung docked SSMS or VS Code slows CC.

### Incidents
- The focus tests (with Joe's OK) took over the mouse and keyboard for about two minutes and showed a stand-in window and a throwaway VS Code (temp profile in the session scratchpad); those were closed by command line afterwards and the cursor put back. Joe's own VS Code windows were only read (focus probes), never changed.
- Read-only Outlook reads for checks: Sent Items and Inbox lists (counts and timings only printed).

## 2026-10-26 (about 11 AM - 2:40 PM local)

**Summary:** Nine of Joe's requests built, tested and committed. The biggest: desktop VS Code docked in the VS Code tab, so a CC restart keeps VS Code and its Claude chats running (it already worked live in Joe's 2:20 PM restart). Everything is live; nothing is waiting on a restart.

### Done
- `62041fc` Spam: a block deletes the alerts for that email, the previewed matching Inbox mail and other alerted emails it matches (Outlook lookup, newest 300), then Mail, Today, the alert feed and badge refresh. Today's **Recent alerts** column replaced by **Today's calendar** (`daycal.js`): a time grid of the day from `/api/planner?days=1` (Outlook, overlay calendars, personal entries; now line, all-day strip, overlaps side by side).
- `d50e07e` Start-up: the launch puzzle (splash.js in gate mode, inlined by `desktop._gate_page`) plays behind Windows Hello instead of the padlock page; Unlock/Quit appear only if Hello fails. The dashboard then opens at `/#signed-in` and skips the fly-in. Lock still shows the padlock page.
- `7f60cb4` Snip: ✂ Snip in the header and Ctrl+K open Windows' snip overlay (`ms-screenclip:`), which copies to the clipboard (`snip.py`, `snip.js`).
- `ecd7bfa` Lunch blocks: Joe's recurring "Blocked" 12:00-1:30 (any plain appointment named Blocked/Lunch inside 11:00-14:30, `outlook.meetings.lunch`) shows as 🍽 Lunch: not counted, warned, prepped or summarized as a meeting.
- `2c71c60` Vault stays unlocked for the day (`privacy.stay_unlocked_today`, default on): survives Windows lock, sleep and CC Lock; locks at midnight, with Lock vault, in Privacy mode and on restart.
- `045dca7` + `44ccf58` **Docked desktop VS Code** (`codedock.py`, `codedock.js`): one window per project, the WORKING ON one shows. Restart/Quit hand the windows back (never closed) and record them (kv `vscode:dock`); the next start docks them again after unlock. Fix in `44ccf58`: later clean-up passes no longer wipe that record. CLAUDE.md rule updated (restarts must keep Joe's VS Code + Claude sessions alive). Web VS Code stays for serve mode.
- `045dca7` Alerts: any action button marks the alert read; reading an email in CC marks its alerts read.
- `be98df3` Mail tab: legible two-line rows, an **Unread** section on top with every unread Inbox email (not just the newest 50), All | Unread, and search across Inbox + Sent (subject, people, text via Outlook's index; ~3 s). Narrow pane: list and reader take turns.
- Tests: preview check at 3 widths after each change (no page errors, no sideways scroll); unit tests with fakes (spam alert removal, vault lock rules 6/6, alert/mail read); a stand-in window + throwaway VS Code profile for the dock (16/16, then 9/9 for restart/quit incl. same VS Code processes before and after); read-only checks on Joe's real Outlook (lunch rule, mail view and search timings).

### Decisions
- Sign-in: keep Windows Hello at start, with the loading screen behind it (not "skip Hello at start").
- VS Code: dock the real desktop VS Code (not "offer both" or "leave as is"). Running chats can't survive a web-VS-Code reload, which is why.
- Vault: unlocked for the whole day; a CC restart still locks it (the key is never written to disk).
- Snip: Windows' own snip overlay, not a CC-built tool.
- Spam-blocked alerts are deleted (Undo doesn't bring them back). Mail rows drop the "OUTLOOK" source tag (address on hover and in the reader).
- "Ledgeable" was taken as "legible".

### Waiting on a restart
- Nothing: CC restarted at 2:20 PM, after the last commit (2:18 PM). The tree is clean.

### Waiting on Joe
- [x] (Done another way: Joe changed the password, 2026-10-26 second session.) **Remove the password he pasted into this chat** from the session transcript: run `C:\sonance\internal\CommandCenter\.venv\Scripts\python.exe -I "<scratchpad>\redact.py" fix` (the command is in the last message of the 2026-10-26 session; the script finds the message by its shape and overwrites it in place). Claude was blocked from editing its own transcript. Change that password if it's a real one (a copy went to Anthropic with the conversation).
- [ ] Add the three weekday personal reminders (Personal tab). No reminder has ever been saved, which is why no pop-up has appeared.
- [ ] Optional: blocked-sender mail still sitting in the Inbox (e.g. 23 matching "pricing"; CC hides it) can be moved with Spam → "Also move matching emails to Junk".
- [ ] Still open from earlier: Toolkit → Connection → Test; SSMS AutoRecover on; sign in to the Pulse and Teams views; mark important senders; Google/iCloud calendar links; urgent people, Asana token.

### In flight / not finished
- Nothing uncommitted, no agents running. `redact.py` lives in the session's temp scratchpad; if it's gone, recreate it: find the single password-shaped user message in the transcript and overwrite it with `*` of the same length, in place.

### Known issues
- The start-up page always plays the puzzle, even with the launch animation turned off in Profile (it can't read the Profile before sign-in).
- Docked VS Code: a force-kill or crash of CC closes docked VS Code windows (same as SSMS). Only tested at 100% display scaling.
- After a crash or reboot the recorded VS Code windows are gone, so nothing re-docks (by design).
- Carried over: the web VS Code terminal fails (0xC0000022); a hung docked SSMS or VS Code slows CC.

### Incidents
- Joe pasted what looks like a password into the chat. It was not used or stored by Claude; a scan found it in exactly one file on the PC (this session's transcript). Redaction was blocked by CC's safety rules, so Joe has to run the script himself (see Waiting on Joe).
- The VS Code dock tests briefly showed a test window and a throwaway VS Code instance (temp profile, Joe's installed extensions read-only) on Joe's screen; all were closed by process command line. Joe's own VS Code windows were never targeted.
- Read-only Outlook reads for checks: today's midday calendar item, this week's calendar (lunch rule), Inbox unread counts and search timings (counts only printed).

## 2026-10-25 (about 9 AM - 3:40 PM local)

**Summary:** Fixed the boot error Joe hit ("Cannot read properties of null (reading 'replaceChildren')"). Then did the four open items from the last log: dead code removed, Today tools on one row, Pulse alerts open in the Pulse tab, and the preview artifact republished. Everything is committed and tested, and the tree is clean.

### Done
- `b95cd4a` Boot error fix. It happened whenever Calendar was the open COMMS tab at startup: app.js's old 24-hour calendar loader started, planner.js then replaced the tab, and the loader filled a list that no longer existed. Reproduced in the preview by pausing on caught exceptions over CDP, across all 70 tab and focus-layout combinations.
- `6303c1f`:
  - Removed the old 24-hour calendar list (markup, loader, `calMeetings`), the unused `.teams-grid` CSS and `static/vendor/` (xterm).
  - Today's Follow-ups tools: Urgent people, Important senders, Snoozed and Muted senders now sit behind one **Manage ▾** menu (the buttons are kept hidden in `#fu-manage`; important.js still adds its button there).
  - A narrow Today head tightens the tabs ("Agenda", Rescan as ↻; container queries on `.tw-head`), so the tools stay on one row at 1200 / 1600 / 1920 / 2560 px.
  - Pulse alerts use a new action type `pulse` (pulse.py), and pulse.js opens them in the Pulse tab (the browser when there's no view).
  - Preview mock data: Important senders (GET/POST, stars, follow-up group), `/api/window/teams`, the Pulse alert action.
  - PROJECT.md updated.
- `39f36a6` The preview build now swaps out the weather places, the surname and "Cherbak's Toolkit". The Urgent people placeholder uses a generic name instead of a coworker's.
- Preview artifact "Command Center" republished (version 2, private): https://claude.ai/artifact/4nkSbtnowfNmg2ukvSqN2Q. The whole build was read before publishing. The old version had shown a real city and ZIP.
- Tests: the preview check at 3 widths (no page errors, no blocked requests, no sideways scroll), the 70 startup layouts, and the Manage menu items and keyboard, stars, Important senders group and Pulse alert routing (browser and faked desktop). No test servers were started, and the live app wasn't touched.

### Decisions
- Joe asked for items 1-4 from the open list: preview artifact, dead code, Today wrap, Pulse alerts.
- Today wrap: fixed with a Manage menu and compact tabs, not by letting the tools wrap.

### Waiting on a restart
- CC started at 9:00 AM, before all three commits. The next restart loads the Calendar boot fix, the Manage menu, the compact Today head, Pulse alerts opening in the Pulse tab, and the dead-code removal.
- **Safe to restart now**: the tree is clean, and everything is committed and tested.

### Waiting on Joe
- [ ] Restart CC to load today's fixes.
- [ ] Sharing: the shared-by-link page is still "JC Command Center" (https://claude.ai/artifact/AGg3kutmutf5r6UtVEpAAk, not updated today). The new preview is private; share it from its page's Share menu if wanted. Code comments in it still mention Sonance and Joe (offered to remove; not done).
- [ ] Still open from 2026-10-24:
  - Toolkit → Connection → Test.
  - SSMS AutoRecover on.
  - Sign in to the Pulse view (Prod and Dev) and the Teams web view.
  - Mark important senders.
  - Trust a folder in VS Code · Claude.
  - Add the three weekday reminders.
  - Google / iCloud calendar links.
  - Urgent people, Asana token, Windows Hello.

### In flight / not finished
Nothing running or uncommitted. Optional next steps:
- The Walkthrough artifact (https://claude.ai/artifact/NRrSk4oqSEfpQLZnLu5not) doesn't cover today's changes yet (Manage menu, compact Today head, Pulse alerts); offered to update it.
- Remove the Sonance and Joe mentions from the preview's code comments (add them to `SCRUB` in `tools/preview/build.py`) before sharing it.

### Known issues
- Pulse alerts raised before today carry the old `url` action and still open in the browser. They age out within 30 days.
- In a pane under about 40rem, the Follow-ups tools can still wrap when counts get long (3-digit numbers).
- Carried over: the embedded VS Code terminal fails (0xC0000022); "Git Base not loaded" until the VS Code window reloads; a hard kill of CC takes a docked SSMS with it; the Okta / Teams sign-ins in the views are untested; a hung docked SSMS slows CC.

## 2026-10-24 (about 6 PM - 11 PM local; the first session crashed at 9 PM and was resumed)

**Summary:** Big build day. Glass restyle, spam blocks, SQL certificate pin, Pulse site inside CC with a Pulse | Code focus layout, SSMS docked in the SQL tab, VS Code · Claude replacing the terminal Claude tab, a reworked Today, a Personal tab with pop-up reminders, Teams unread counts plus a full Teams chat view, Important senders, an off-hours log, meeting warnings at 15 / 5 / now, and many fixes. Everything is committed and tested. The tree is clean, so it's safe to restart.

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
- `2f248cb`:
  - Teams chat view: Teams on the web in a native view beside the counts and Recent list (`siteview.py`, `teamsview.js`, profile `data/webview-teams`). "Open chat" opens the full conversation.
  - Important senders: kv `people:important`, ★ Mark important, high-priority alerts, "Important senders" follow-up group, manage dialog. Spam blocks refuse important senders.
  - Meeting reminders 15 / 5 / 0.
- `d649d4b` Preview artifact build tooling moved into the repo (`tools/preview/`; output in the git-ignored `tools/preview/out/`).
- Skills `start-session` / `close-session` and this log (`.claude/skills/`, `SESSIONS.md`); CLAUDE.md points to them.
- Artifacts republished:
  - "JC Command Center" (shared by link): glass look, generic wording only. https://claude.ai/artifact/AGg3kutmutf5r6UtVEpAAk
  - "Command Center Walkthrough" (private): 18 chapters covering everything through today. https://claude.ai/artifact/NRrSk4oqSEfpQLZnLu5not
- Final test pass on `2f248cb`: compile and import, spam suite, reminders suite, Important senders suite (57), and the all-tab click-through. All pass, no page errors.

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
- CC was last restarted at 10:20 PM, before `08c3b60` landed. The next restart loads `08c3b60` and `2f248cb`: Pulse / SSMS / VS Code mirror finishing touches, Teams chat view, Important senders, meeting warnings, Teams every 1 min, and the Alerts badge fix.
- **Safe to restart now**: the tree is clean and everything is committed and tested. The first start takes about 30 s longer while VS Code copies extensions.

### Waiting on Joe
- [ ] Restart CC once the in-flight work is committed; the first start takes about 30 s longer while it copies VS Code extensions.
- [ ] Toolkit → Connection → **Test** (certificate is pinned and the password is saved, but no successful test yet).
- [ ] SSMS: check Tools → Options → Environment → **AutoRecover** is on (1-5 min). In the connect dialog: Encryption Mandatory + Trust server certificate (his saved connections already have both).
- [ ] Sign in to Pulse in the Pulse tab (Okta, Prod and Dev), and to the Teams web view on the right of the Teams tab (Microsoft, then Okta).
- [ ] Mark a few important senders (☆ Mark important on their email, or Ctrl+K → Important senders). The old config VIP list only held placeholders.
- [ ] VS Code · Claude: open a project and **Trust** the folder.
- [ ] Personal tab: add the three weekday reminders he described. Do it after the restart; the vault is already set up.
- [ ] Calendar → Calendars: add the Google / iCloud links (he was doing iCloud).
- [ ] Still open from the account setup list: urgent people, Asana token, Windows Hello.

### In flight / not finished
Nothing is running or uncommitted. Open items:
- **Interactive preview artifact** ("Command Center", private, https://claude.ai/artifact/4nkSbtnowfNmg2ukvSqN2Q): **not republished yet**; the live page is still the old HUD-era preview.
  - Build with `.venv\Scripts\python.exe tools\preview\build.py`, then check with `tools\preview\check.py`.
  - The build of `2f248cb` is 675 KB and clean: no errors, all 12 tabs and 10 tiles at 3 widths, no outside requests except Google Fonts.
  - To do first: add sample data in `tools/preview/mock.js` for the 3 new endpoints (`/api/people/important` and the Teams view's `/api/window/teams`).
  - Then rebuild, **read the whole output** (the publish rule), and publish to the URL above. The page still has "Sonance" / "Joe" in code comments; it's private.
- Small follow-ups: remove the unused `.teams-grid` rules in style.css and `static/vendor/` (xterm).

### Known issues
- The terminal inside CC's embedded VS Code fails to start (PowerShell exits with 0xC0000022, access denied). This predates today; cause unknown (not the copied extensions).
- "Git Base not loaded" errors in embedded VS Code after trusting a folder, until the window reloads.
- A hard crash or kill of CC takes a docked SSMS down with it. Normal exits hand SSMS back.
- Untested until Joe signs in: real Okta sign-in in the Pulse and Teams views (and whether Teams sign-in uses any host beyond the allowlist; add it under `teams.view_hosts` if a link opens in the browser instead); a deep link opening the chat itself; Ctrl+Shift+P while SSMS has focus; Teams channel-message links may not open.
- "Open in Pulse" on alerts still opens the default browser.
- A docked SSMS that hangs can make CC feel slow until it recovers. Undock ends it.
- The Today tools row wraps under the tabs in the narrow split COMMS pane.

### Incidents
- The first session crashed at about 9 PM. It was resumed from its saved transcript in `~\.claude\projects\C--Users-JoeC\`.
- An agent test popped a CC "can't use port 8819" error box on Joe's screen. Tests now check ports and suppress dialogs.
- An agent probe briefly opened an SSMS window on Joe's screen (a dropdown and a "Change Password" box appeared) and killed it after about 45 s. Nothing was connected or saved. Later SSMS tests ran on a hidden desktop.
- A test keystroke sent a stray prompt ("pen User Settings (JSON)") to a real Claude session in the CommandCenter project. Text reply only, no tools, stopped within seconds. Transcript `947c2514-…jsonl`; safe to delete.
- Joe restarted CC at 9:47 PM and 10:20 PM while agents were mid-edit. Both starts loaded without errors (checked with a test copy).
