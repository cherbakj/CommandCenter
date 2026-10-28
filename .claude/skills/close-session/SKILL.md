---
name: close-session
description: Close a CC (Command Center) work session - log everything done this session to SESSIONS.md (changes, commits, decisions, in-flight work, what's waiting on a restart and on Joe), keep PROJECT.md current, commit the log, and push the day's backup to the private GitHub repo. Use when Joe says "close session", "end session", "wrap up", "log everything", or is done for the day.
---

# Close a CC session

Write the hand-off that the next session's `start-session` reads. Someone with no memory of today must be able to continue from it alone.

## 1. Gather the facts (check, don't recall)
- `git log --oneline` since the previous SESSIONS.md entry's last commit, and `git status --short`.
- The whole conversation: every request Joe made, what was built, decisions he made (and the options he turned down), bugs found and fixed, incidents (anything that touched his screen, data or accounts).
- Background agents: which finished (and landed in a commit), which are still running or were stopped, and what each was doing.
- The live app: start time of the process on 127.0.0.1:8765 vs the newest commit, so you can say what is **waiting on a restart**. Is the tree safe to restart on (no half-finished edits)?
- Settings changed in the git-ignored `config.yaml`: note the key and new value (never secrets).

## 2. Write the entry at the top of SESSIONS.md
Newest first, under the title line. Use this shape; leave a section out only if it's truly empty:

```markdown
## YYYY-MM-DD (start-end, local time)

**Summary:** one or two sentences.

### Done
- Feature or fix, in plain words, with the commit hash. One line each; group related ones.

### Decisions
- What Joe chose, and the alternative he didn't (so it isn't re-asked).

### Settings changed (config.yaml, not in git)
- `key: value`: why.

### Waiting on a restart
- What loads on Joe's next CC restart; whether the tree is safe to restart on right now.

### Waiting on Joe
- [ ] Sign-ins, settings, checks only he can do.

### In flight / not finished
- Work started but not done (agents still running, uncommitted edits and whose they are), with enough detail to resume.

### Known issues
- Bugs or risks found and not fixed yet.

### Incidents
- Anything unexpected that touched Joe's screen, mailbox, accounts or data, and what was done about it.
```

Keep it factual and short: no transcripts, no tool output, no secrets, no email or chat content, no real customer data. Names of Joe's coworkers only if needed to make sense of a decision.

## 3. Keep the docs current
- If this session changed how CC works and PROJECT.md doesn't say so yet, update PROJECT.md too.
- If Joe stated a lasting preference, save it to memory as well.

## 4. Commit
Commit SESSIONS.md (and PROJECT.md if changed) **on its own**: `git add SESSIONS.md PROJECT.md` then commit with the paths named (`git commit -m "Session log: <date>" -- SESSIONS.md PROJECT.md`), so nobody's in-progress edits get swept in. If PROJECT.md has someone else's uncommitted edits in it, commit only SESSIONS.md and say so in the entry.

## 5. Back up to GitHub (every close)
Joe wants each day's work backed up to the private GitHub repo `origin` (github.com/cherbakj/CommandCenter; the app itself stays local).
- Only finished, committed work goes: never commit half-finished edits to get them into the backup (say what was left out).
- Run exactly `git push origin main` (allowed in `.claude/settings.local.json`; any other push form will ask). Never force-push.
- Check it landed: `git ls-remote origin main` must print the same hash as `git rev-parse HEAD`.
- If the push is refused or fails (sign-in needed, network, rejected), don't retry another way: tell Joe to run `git push origin main` in `C:\Sonance\internal\CommandCenter`, and note it under "Waiting on Joe".

## 6. Tell Joe
In a few lines: what's logged, whether the backup reached GitHub (the hash), what's waiting on a restart (and whether it's safe now), and his to-dos.
