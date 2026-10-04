# How Scout keeps GitHub up to date

Scout runs on Brandon's Mac. Two automatic jobs keep this public repository current. **No passwords or tokens
are stored in this repository.** GitHub sign-in is handled by GitHub's official `gh` tool and kept in the
Mac's keychain. The `.env` file (alert settings), `data/` (the accounts) and `logs/` are never uploaded
(see `.gitignore`).

## 1. Code: uploaded after every change

When Brandon and Claude Code change Scout, the change is tested (`uv run pytest`) and saved as a git commit.
A git **post-commit hook** then sends it here in the background. Hooks live in `.git/hooks/`, which git never
uploads, so here is the exact script:

```sh
#!/bin/sh
# Scout: send every new commit on main to GitHub, in the background so committing stays fast.
# Only main and the version tags that are part of it are sent: the private history before
# GitHub (branch history-before-github and its old tags) never leaves this Mac.
# Does nothing until a GitHub copy ("origin") is set up. Results: logs/git-push.log
[ "$(git branch --show-current)" = "main" ] || exit 0
git remote get-url origin >/dev/null 2>&1 || exit 0
(
  export GIT_TERMINAL_PROMPT=0
  {
    date
    git push --quiet origin main
    for tag in $(git tag --merged main); do git push --quiet origin "refs/tags/$tag"; done
    echo "exit $?"
  } >> logs/git-push.log 2>&1
) &
```

The private history from before GitHub (branch `history-before-github`) is deliberately never sent.

## 2. Status page: rebuilt and published every hour

`uv run scout status-page make --publish` (code: `scout/status_page.py`, command in `scout/cli.py`):
1. fetches live prices from Hyperliquid's public API;
2. opens each account's SQLite database **read-only** (it cannot change anything);
3. writes `STATUS.md`: balance vs holding BTC, open positions with reasons, the last 10 closed trades, and warnings;
4. pushes it as the only file on the [`status` branch](https://github.com/Brandonshev/scout/blob/status/STATUS.md)
   as a single commit that replaces the previous one, so `main`'s history isn't cluttered.

macOS runs it every hour with this LaunchAgent (`~/Library/LaunchAgents/au.scout.demo.status.plist`),
installed by `uv run scout status-page install-service`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>EnvironmentVariables</key>
	<dict>
		<key>LANG</key>
		<string>en_AU.UTF-8</string>
		<key>LAUNCHED_BY_SCOUT_SERVICE</key>
		<string>1</string>
		<key>PATH</key>
		<string>/usr/bin:/bin:/usr/sbin:/sbin</string>
		<key>PYTHONUNBUFFERED</key>
		<string>1</string>
	</dict>
	<key>Label</key>
	<string>au.scout.demo.status</string>
	<key>ProgramArguments</key>
	<array>
		<string>/Users/brandonshev/Scout/.venv/bin/scout</string>
		<string>status-page</string>
		<string>make</string>
		<string>--publish</string>
		<string>--config</string>
		<string>/Users/brandonshev/Scout/config.yaml</string>
		<string>--env-file</string>
		<string>/Users/brandonshev/Scout/.env</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>/Users/brandonshev/Scout/logs/status.err.log</string>
	<key>StandardOutPath</key>
	<string>/Users/brandonshev/Scout/logs/status.out.log</string>
	<key>StartInterval</key>
	<integer>3600</integer>
	<key>ThrottleInterval</key>
	<integer>30</integer>
	<key>WorkingDirectory</key>
	<string>/Users/brandonshev/Scout</string>
</dict>
</plist>
```

(`--env-file` only tells Scout where its settings file is. Nothing from it goes on the status page.)

## 3. The trading accounts themselves

Three more LaunchAgents keep the accounts running 24/7 (`au.scout.demo`, `.experiment`, `.smart`). They restart
after a crash and pause while the Mac sleeps. When the Mac is asleep or off, nothing updates: check the
"Updated" time at the top of STATUS.md.

## Who can change what

Only Brandon can upload, from his Mac. AI helpers reading this repository can't change it. Improvements go
through Brandon and Claude Code, and are tested before they run.
