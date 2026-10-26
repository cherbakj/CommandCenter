# CC (Command Center)

Joe calls this app **CC**. What it is, how it's built, and its security model are all in PROJECT.md. Read it first and keep it current: every feature change updates PROJECT.md in the same change.

@PROJECT.md

**Session hand-off:** `SESSIONS.md` (newest entry first) says what the last session did, what's in flight, what's waiting on a restart and what's waiting on Joe. Joe starts with the `start-session` skill and ends with `close-session`.

## Working rules
- **The live app is Joe's.** It runs on 127.0.0.1:8765. Never restart, stop or reconfigure it, and never touch its embedded VS Code server or the VS Code windows docked in it. Joe restarts only when told it's safe. When telling him, list what the restart will load.
- **Restarts and VS Code:** Joe's desktop VS Code docked in the VS Code tab (`codedock.py`), and the Claude chats in it, survive a CC Restart or Quit: the windows step out and the next start docks them again. Keep it that way: any change to `codedock.py`, `desktop.py` quit/restart or the lock flow must keep his VS Code windows and Claude sessions alive across a restart (test it with a stand-in window and a throwaway VS Code profile, never his windows). What still ends them: the web VS Code (serve mode, `vscode.py`), a force-kill or crash of CC, and a reboot.
- **Don't leave the tree half-done for a restart.** If work is in progress (yours or an agent's), say so before Joe restarts. Commit finished, tested work on `main` (no remote). Don't commit half-finished work.
- **Testing:** use a second copy on another port, e.g. `CC_PORT=8817 CC_DEV_NO_AUTH=1 PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m commandcenter serve --no-browser`. Use a temp data dir when the test writes data. Drive the UI with Playwright and installed Edge (`channel="msedge"`), and stop test servers afterwards. Never use the real Outlook for writes in tests; use a fake COM object or read-only checks.
- **Python:** always `.venv/Scripts/python.exe`. **Never create `.ps1` files** (Cylance blocks them).
- **Parallel agents:** re-read a file right before editing it. If an Edit fails because the file changed, re-read and re-apply; never write back a stale copy. Give each agent its own files and test port.

## Conventions for new features
- **Feature module:** `commandcenter/<name>.py` exposes `router` and optionally `background`, and is added to the tuple in `app.py` `_load_feature_modules`. The front end goes in `static/<name>.js` (+ `.css`), registered in `index.html` and wired through `window.CC` (`registerTab`, `addPaletteSource`, `addMailAction`, ...).
- **Front-end text:** build it with `CC.el` only, never as HTML, from data or email text.
- **Data protection, every time:**
  - secrets go in Windows Credential Manager or the personal vault, never in config, the DB or logs;
  - work data at rest is DPAPI-sealed through `db.py`;
  - personal data goes in the vault;
  - add a `retention:` default and a Privacy & Data entry (`privacy.py`) for any new stored data;
  - logs carry ids, counts and types, never subjects, bodies, addresses or row values.
- **Outlook COM:** only on the COM thread via `outlook.call()`. Never change Joe's own Outlook rules.
- **Destructive or outward actions** (sending mail, SQL writes, moving mail, production Pulse/Supabase changes): preview first, then an explicit confirm, as in the existing verified-send and Toolkit flows.
- **Pulse** (`C:\Sonance\internal\Pulse`) is a shared production repo with repo-guard hooks. Don't change its headers or config to suit CC.

## Joe
- Work hours: 08:00–17:00 Mon–Fri (the off-hours log counts time outside them).
- He asks for features in short messages while work is running. Fold each into the plan and confirm briefly.
- Earlier long CC build sessions were started from the home folder: their transcripts are in `C:\Users\JoeC\.claude\projects\C--Users-JoeC\`. Look there after a crash ("continue where it left off").
