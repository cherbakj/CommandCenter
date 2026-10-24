#!/bin/sh
# Command Center repo guard (installed as a LOCAL git hook; .git/hooks is never pushed).
# Blocks commits/pushes that would carry Command Center details or secrets into a shared repo.
# Usage from hooks:  guard.sh staged            (pre-commit: checks the staged changes)
#                    guard.sh commits <sha...>  (pre-push: checks each outgoing commit)
# Prints the file and the kind of problem, never the matching text itself.

mode="$1"; shift

# name|extended-regex  (checked against ADDED lines only)
PATTERNS='Command Center path|[Ss]onance[\\/]+internal[\\/]+[Cc]ommand[Cc]enter
Command Center name|[Cc]ommand ?[Cc]enter|commandcenter
Command Center session cookie / header|cc_session_|X-CommandCenter
Command Center keyring entry|CommandCenter-(SQL|Toolkit|Pulse|Asana)
JWT / API token|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}
Private key|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----'

# Local-only files that must never be committed.
BLOCKED_PATHS='(^|/)\.env($|\.local$|\.development$|\.production$)|(^|/)\.claude/settings\.local\.json$|(^|/)CLAUDE\.local\.md$|(^|/)\.vscode/|\.dpapi$'


check_diff() {  # $1 = label for messages; reads a unified diff on stdin
    awk '/^\+\+\+ b\//{f=substr($0,7)} /^\+[^+]/{print f "\t" substr($0,2)}' > "$tmp"
    echo "$PATTERNS" | while IFS='|' read -r name re; do
        [ -z "$name" ] && continue
        grep -E -- "$re" "$tmp" 2>/dev/null | cut -f1 | sort -u | while read -r file; do
            echo "$1|$file|$name"
        done
    done
}

tmp=$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/repo-guard.$$")
hits=$(mktemp 2>/dev/null || echo "${TMPDIR:-/tmp}/repo-guard-hits.$$")
: > "$hits"
trap 'rm -f "$tmp" "$hits"' EXIT

if [ "$mode" = "staged" ]; then
    git diff --cached --name-only --diff-filter=ACMR | grep -E -- "$BLOCKED_PATHS" | while read -r f; do echo "staged|$f|local-only file"; done >> "$hits"
    git diff --cached -U0 --diff-filter=ACMR --no-color | check_diff "staged" >> "$hits"
else
    for c in "$@"; do
        short=$(git rev-parse --short "$c")
        git show --format= --name-only --diff-filter=ACMR "$c" | grep -E -- "$BLOCKED_PATHS" | while read -r f; do echo "commit $short|$f|local-only file"; done >> "$hits"
        git show --format= -U0 --diff-filter=ACMR --no-color "$c" | check_diff "commit $short" >> "$hits"
    done
fi

if [ -s "$hits" ]; then
    echo "" >&2
    echo "Repo guard (Command Center): this would put Command Center details or secrets into a shared repo." >&2
    sort -u "$hits" | while IFS='|' read -r where file name; do echo "  BLOCKED: [$where] $file -> $name" >&2; done
    echo "" >&2
    echo "Remove those lines/files and try again. Nothing was committed or pushed." >&2
    echo "(If you are certain it is safe, a person can bypass once with --no-verify. Claude should never do that.)" >&2
    exit 1
fi
exit 0
