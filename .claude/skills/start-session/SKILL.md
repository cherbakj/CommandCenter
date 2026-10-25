---
name: start-session
description: Start a CC (Command Center) work session - read CLAUDE.md, PROJECT.md and the latest SESSIONS.md entry, check git and the live app, and brief Joe on where things stand. Use when Joe says "start session", "start a session", "pick up where we left off", or at the start of a new day's CC work.
---

# Start a CC session

Get fully up to speed before doing any work, then give Joe a short briefing. Read-only: change nothing, start nothing, commit nothing.

## 1. Read the docs (in full, not skimmed)
- `CLAUDE.md`: working rules (it loads PROJECT.md).
- `PROJECT.md`: what CC is and how it's built.
- `SESSIONS.md`: read the **newest entry** completely (newest is at the top), plus any older entry it says is still open. These are the hand-off notes from the last session: what was done, what's in flight, what's waiting on Joe.

## 2. Check the actual state (don't trust the log blindly)
- `git status --short` and `git log --oneline -10`. Uncommitted changes that the last entry doesn't explain: say so, don't touch them.
- The live app: who listens on 127.0.0.1:8765 and when that process started (PowerShell `Get-NetTCPConnection -LocalPort 8765 -State Listen`, then `Get-Process -Id <pid>`). Compare with the time of the newest commit (`git log -1 --format=%cd`): if CC started before it, the commits since then are **waiting on a restart**. List them by name.
- Agents the log lists as still running are gone after a session ends. Check whether their work landed (git log / files) or needs redoing.
- If the log says a test server or port was left running, check for it; never stop the live app on 8765.

## 3. Brief Joe (short, plain language)
1. **Where we left off**: one or two lines.
2. **Waiting on a restart**: what will load, and whether it's safe to restart now (clean tree, or only finished and tested work).
3. **Waiting on you**: Joe's to-dos from the log (sign-ins, settings, decisions).
4. **Open work**: unfinished items and known issues, most important first.
5. **Suggested next step.**

Then wait for Joe's direction. If something in the log contradicts what you found, say which and why.
