# Scout

An explainable, risk-first crypto trading bot. Scout:
1. reads the overall market mood,
2. scans only high-volume coins,
3. trades only when a coin and the market agree,
4. manages risk strictly,
5. explains every trade in plain English.

**Scout runs in DEMO mode by default: fake money, real prices.** Live trading isn't built
(v1.0.0 at the earliest) and can't be turned on from config.

Current version: see `scout/version.py` (also shown by `uv run scout version`).

## Setup on macOS

1. **Install uv** (manages Python and packages):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   Then open a new Terminal window so the `uv` command is found.

2. **Install Scout's dependencies** (uv also downloads Python 3.12 if needed):
   ```bash
   cd ~/Desktop/Scout
   uv sync
   ```

3. **Create your secrets file** (empty for now, but git-ignored):
   ```bash
   cp .env.example .env
   ```

4. **Check the config and create the database:**
   ```bash
   uv run scout config-check
   uv run scout init-db
   ```

5. **Run the tests:**
   ```bash
   uv run pytest
   ```

## Commands

| Command | What it does |
|---|---|
| `uv run scout version` | Print the version |
| `uv run scout config-check` | Validate `config.yaml` + `.env` and summarise settings |
| `uv run scout init-db` | Create `data/scout.db` and any missing tables (keeps existing data) |
| `uv run scout market` | Coins sorted by 24h volume: price, 24h change, funding, open interest (`-n 50` for more) |
| `uv run scout fetch --coin BTC --interval 4h --days 365` | Download past candles (only what's missing) |
| `uv run scout fetch --top 10` | Download the default intervals/days for the 10 highest-volume coins |
| `uv run scout watch --coin BTC --coin SOL` | Live prices from the websocket (reconnects automatically) |
| `uv run scout mood` | Current market mood (RISK_ON / NEUTRAL / RISK_OFF + volatility) and why; saved to the database |
| `uv run scout mood --watch` | Re-check the mood every hour (refreshes data first, including after the Mac wakes) |
| `uv run scout mood-history --days 365` | Mood for each past day, saved as a PNG chart in `reports/` |
| `uv run scout demo` | Start demo trading: fake A$1,000, real live prices (Ctrl+C to stop; positions persist) |
| `uv run scout status` | Demo account: state, value, today's result vs the daily limit, drawdown vs the kill switch |
| `uv run scout positions --closed 10` | Open demo positions with live profit/loss, plus recent closed trades |
| `uv run scout pause` / `resume` | Stop / restart opening new trades (open positions are still managed) |
| `uv run scout kill` | KILL SWITCH: close every demo position now; nothing new until `scout reset-kill` |
| `uv run scout dashboard` | Open the read-only dashboard in your browser (`--replay` to open on the replay) |
| `uv run scout replay --from 2025-01-01 --to 2025-03-31 --speed 20000x` | Run the demo trader over past prices at high speed (watch it in the dashboard) |
| `uv run scout explain` / `scout explain 12` | List trades / walk through why trade #12 happened and how it ended (`--replay` for replays) |
| `uv run scout service install` | Run `scout demo` in the background: starts at login, restarts after a crash (`uninstall`, `stop`, `start`, `status`) |
| `uv run scout doctor` | Check config, secrets, database, disk, logs, backups, the service, sleep, network, rates and iMessage |
| `uv run scout report weekly` | Performance vs BTC, best/worst trades, market moods and an honest verdict (also sent Sundays 19:00) |
| `uv run scout tax-export --fy 2026` | CSV of every trade in Sydney time, USD and AUD (RBA daily rate) + notes for your accountant |
| `uv run scout backup` | Copy the database to `data/backups` now (the demo does this daily, keeping 14) |
| `uv run scout ntfy-setup` | Set up push alerts through the free ntfy iPhone app (makes a secret topic, switches alerts on) |
| `uv run scout notify-test` | Send one test iMessage to your phone (`--dry-run` shows it without sending) |
| `uv run scout backtest --from 2024-07-01 --to 2026-09-01` | Replay the whole strategy over past candles vs holding BTC; saves `equity.png`, `trades.csv`, `summary.txt` to `reports/` |
| `uv run scout backtest --walk-forward` | Tune settings on each 180-day period, test them on the next unseen 90 days |
| `uv run scout signals` | Mood + scan + trade signals (buy / sell / move stop / skipped / watching), each with a reason |
| `uv run scout scan` | Rank the top 25 coins by volume and shortlist the best 10, with a reason for each (`--all`, `--watch`) |

All commands except `version` accept `--config path/to/config.yaml` and `--env-file path/to/.env`.

## Market data notes

- Source: Hyperliquid's public info API (no account needed). Prices and money are in US$/USDC.
- Hyperliquid keeps only the **latest 5000 candles** per coin and size: a year of 4h/1d,
  but only ≈208 days of 1h and ≈52 days of 15m. Running `scout fetch` regularly keeps
  adding new candles, so the local history grows beyond that over time.
- Only finished candles are stored, so strategies can never "see" an unfinished one.
- Refresh the offline test fixtures with `uv run python scripts/record_fixtures.py`.

## Market mood (v0.3.0)

Scored on daily candles, all thresholds in `config.yaml` → `regime`:

| Check | Points |
|---|---|
| Trend: BTC above/below its 50- and 200-day averages; each average rising/flat/falling | −4 … +4 |
| Breadth: % of the top 30 coins above their own 50-day average (≥60% / ≤40%) | −2, 0, +2 |
| Crowding: average funding of the top 30 above 30%/yr | −1 or 0 |

≥ +3 = RISK_ON (only if BTC is above its 200-day), ≤ −3 = RISK_OFF, otherwise NEUTRAL.
Volatility (BTC's ATR % vs its past year) is labelled CALM / NORMAL / WILD separately.
Rules for later steps: RISK_OFF = no new long trades; WILD = half-size positions.

## Coin scanner (v0.4.0)

Settings in `config.yaml` → `scanner`. Every coin checked is saved to `scan_results` with its reason.

- **Filters** (fail any = excluded): 24h volume, open interest, stablecoins, listed < 60 days,
  spread > 0.10%, < US$100k of orders within ±1% of the price (from the `l2Book` order book).
- **Points** (+1 / 0 / −1 each, × weight): beating BTC over 7 days and 30 days, volume vs its
  20-day average, price vs 20- and 50-day averages (−2 … +2), very high volatility (−1).

## Trade signals (v0.5.0)

Settings in `config.yaml` → `signals` (strategy) and `risk` (sizing). Strategy: `breakout`.

- **Buy** a shortlisted coin when the mood is RISK_ON or NEUTRAL, the coin is above its 20- and
  50-day averages, and a 4h candle closes above the high of the previous 20 candles (~3.3 days)
  on ≥ 1.2× average volume with RSI ≤ 75.
- **Stop loss** 2 × ATR below entry, then trailing 3 × ATR below the highest price since entry.
- **Sell** when the stop is hit, a 4h candle closes below its 20-candle average, or the mood turns RISK_OFF.
- **Size**: lose at most 1% of the account if stopped out (fees included), capped at 25% of the
  account per position; half size when volatility is wild.
- **Shorts**: off by default, only in RISK_OFF, and refused in live mode.
- New strategies: subclass `Strategy` in `scout/signals.py` and register it in `STRATEGIES`.

## Backtester (v0.6.0)

- Replays mood → shortlist → signals → exits one 4h candle at a time, using only candles that
  had closed by then (`tests/test_backtest.py::test_no_look_ahead` scrambles the future to prove it).
- Pays taker fees and slippage (from `risk`) and a flat funding rate (`backtest.funding_rate_hourly_pct`).
  Stops fill at the stop price, or worse if the price gapped past it.
- The coin pool includes delisted coins, ranked by their volume on each day (no survivorship bias).
- Limits: Hyperliquid keeps only 5000 4h candles (back to mid-2024), so tests cover about two years;
  the mood and shortlist are updated daily; order-book, open-interest and crowding filters can't be
  replayed; funding is a flat assumption.

## Demo trading and the risk manager (v0.7.0)

- `scout demo` runs the live pipeline every hour (just after the hour, when 4h candles close),
  checks stops and the account every 5 seconds on websocket prices, and saves the account value
  every 5 minutes. Fills are simulated at the live price ± slippage, with fees and live funding rates.
- The account lives in `data/scout.db`, so `status`, `pause`, `kill` etc. work from another window.
- **Every order passes the risk manager** (`scout/risk.py`). Closing is always allowed. Opening
  needs: state RUNNING, a price younger than 60 s, today's loss under 3% (resets at midnight
  Sydney), drawdown under 15%, a stop loss on the right side, one position per coin, at most 3
  positions, ≤ 25% of the account per coin, ≤ 75% in total, never more than 1x (no borrowing),
  and enough cash. Every rejection is logged with its reason.
- **Kill switch**: if the account falls 15% from its peak (or you run `scout kill`), everything is
  closed and nothing new is traded until `scout reset-kill`.
- Demo stops are watched by Scout, so they can fill late if the Mac sleeps. Live trading (v1.0)
  will place stops on the exchange. There is no exchange key or signing code in Scout.

## Phone alerts: ntfy (v0.11.0) or iMessage (v0.8.0)

**ntfy (recommended):** free push notifications through the official ntfy iPhone app; your
personal Messages are untouched. `uv run scout ntfy-setup` makes a long random topic (stored in
`.env`; anyone who knows it can read your alerts) and prints what to tap on your iPhone. The kill
switch arrives as an urgent notification; bundled minor updates arrive silently.

## iMessage updates (v0.8.0)

One-way messages from this Mac's Messages app to your iPhone. Every message starts with
`Scout v… · DEMO` (or LIVE).

1. Put your number in `.env`: `SCOUT_NOTIFY__IMESSAGE_RECIPIENT="+614XXXXXXXX"`
2. `uv run scout notify-test` — the first time, macOS asks to let Terminal control Messages: allow it.
3. Set `notify.imessage_enabled: true` in `config.yaml`, then run `scout demo`.

What you get: every trade opened/closed (reason and result in AUD), market mood changes,
risk events (daily loss limit, kill switch, price feed down for 2+ minutes) and an update at
8am and 8pm from every account (`notify.update_times`; one missed while the Mac slept is sent on
waking if under 3 hours late, otherwise skipped). Quiet hours 23:00–07:00: everything waits except the kill switch, then arrives as one
"While you were asleep" message. Minor updates (stop moves) are bundled hourly.
Messages queue in the `notifications` table, so nothing is lost while the Mac sleeps; failures
are retried and logged. Another channel (e.g. ntfy.sh) can be added as a `NotifyBackend`.

## Dashboard, replay and explain (v0.9.0)

- **Dashboard** (`scout dashboard`): mode, version, bot state, balance, today/total vs holding BTC,
  the market mood with its explanation and history, the coin shortlist, open positions with their
  reasons, trade history, the equity curve, messages, and a plain-English glossary. It only listens
  on this Mac (127.0.0.1) and opens the database **read-only** (SQLite `mode=ro`): it cannot trade.
- **Replay** (`scout replay`): the same demo engine over past candles on a simulated clock, in its
  own fresh `data/replay.db` (the real demo account is never touched; no messages are sent, but the
  dashboard shows what would have been). Speed: `500x` (3 months ≈ 4 hours), `20000x` (≈ 6 minutes)
  or `max`. Pause / step one 4h candle / speed are in the dashboard sidebar; they write only
  `data/replay_control.json`, which controls pace, never trades.
- **Explain** (`scout explain <id>`): the market mood, why the coin was shortlisted, the signal, the
  order and its costs, stop moves, how it ended (with the result broken down), and the lesson.

## Running unattended (v0.10.0)

1. `uv run scout doctor` — fix anything marked ✗ (and ideally ⚠).
2. `chmod 600 .env` — so other users on the Mac can't read your secrets.
3. `uv run scout service install` — a LaunchAgent starts `scout demo` at login and restarts it after a
   crash (a clean stop isn't restarted). Check with `uv run scout service status`.

While running, Scout keeps the Mac awake on mains power (`caffeinate -s`, `service.keep_awake`).
A closed MacBook lid still sleeps. After sleep or an internet drop, Scout waits for fresh prices,
checks stops, then re-runs the hourly check before doing anything else; failed checks retry in 5 minutes.
Every start after an unexpected stop sends an iMessage ("🔄 Scout restarted…", with the reason);
5 starts within an hour pause it for 15 minutes and tell you. `data/heartbeat.json` is refreshed every
30 seconds; the database is backed up daily; logs rotate (including launchd's own at each start).

## Experiment: copy trading + high-risk coins (v0.12.0)

Names (in alerts, scorecards and the dashboard): the main demo is **Breakout:1**, the experiment is
**70/30:2**. Change them with `demo.name` / `experiment.name` in `config.yaml`. Commands are unchanged
(`scout status`, `scout experiment ...`).

A **separate** fake A$1,000 account (`data/experiment.db`) beside the main demo, so the main 6-week
test stays clean. Settings in `config.yaml` → `experiment`.

- **Copy trading (70%)**: each day, wallets from Hyperliquid's public leaderboard are judged on their
  real trades over 30 days (profit after fees and open losses, consistency, enough trades; market-making
  bots and wallets hiding open losses are skipped). The top 10 are checked every 20 s; each NEW
  position is copied with 7% of the account and closed when the wallet closes it.
- **High-risk coins (30%)**: every 15 minutes, small Hyperliquid coins with a 24h jump ≥ 15% and a volume
  surge are bought with 6% of the account, filled against the real order book; sold at +50% or after
  48 hours. Expect most to lose.
- No per-trade stop losses; leverage 1x; ≤ 10% of the account per position; kill switch at −50%.
- `uv run scout experiment run` / `status` / `wallets` / `install-service`. Trade alerts are bundled
  hourly; a per-strategy scorecard arrives at 08:00 and 20:00 (`notify.update_times`).
- **News check (v0.13.0)**: every 10 minutes it reads free crypto news headlines (CoinDesk,
  Cointelegraph, Decrypt, The Block, Bitcoin Magazine). A serious headline about a smaller coin (hack,
  exploit, rug pull, scam, delisting, insolvency...) in the last 48 h blocks buying it and sells a long
  we hold, with an immediate alert. Big established coins (BTC, ETH, XRP...) are exempt: they show up in
  hack stories as the stolen money. Other headlines are only quoted in trade reasons. Each news decision
  is recorded, and the scorecard shows whether those coins then fell. `uv run scout news [--coin SOL]`
  shows what it sees. Keyword matching is crude, so treat it as a tripwire rather than analysis.

## SMART:3: Bitcoin trend + long/short ranking (v0.14.0)

A third fake A$1,000 account (`data/smart.db`), built from research on 3 years of daily candles:
2023-09 to 2025-06 to design it, then one test on the unseen 2025-07 to 2026-09.

- **Bitcoin trend (50%)**: hold BTC while it closes above its 50-day average, cash otherwise.
- **Long/short ranking (25% + 25%)**: every day after 00:10 UTC, the 40 most-traded coins (not BTC) are
  ranked on 1- and 2-month trend, closeness to their 20-day high, calm, no recent lottery-style spikes,
  and low sensitivity to Bitcoin. It buys the best 6 and shorts the worst 6 (sized so calmer coins get
  more), keeping each while it stays in the best/worst 12. Wide safety stops (5 daily ranges away).
- What the research found: buying the best altcoins alone lost money (altcoins trailed Bitcoin), and a
  machine-learning version lost money too. Long/short was what held up.
- Backtest after fees, slippage and funding: 2023-09 to 2025-06 about +49%/yr (worst fall 22%) vs BTC
  +118%/yr (28%); unseen 2025-07 to 2026-09 about +2%/yr (worst fall 16%) vs BTC −17%/yr (53%).
  So it gave up return in Bitcoin's boom and protected capital in its slump.
- `uv run scout smart run` / `status` / `backtest` / `install-service`. A rebalance message arrives each
  morning, and an update at 08:00 and 20:00 (with the other two accounts for comparison).

## Public status page and GitHub (v0.16.0)

- `uv run scout status-page make` writes `reports/STATUS.md`: all three accounts (balance vs holding BTC,
  open positions with reasons, last 10 closed trades, warnings), read-only from their databases.
- `--publish` puts it on the `status` branch of the GitHub copy as a single commit replaced each time, so the
  code's history stays clean. `uv run scout status-page install-service` does that every hour.
- A local git hook (`.git/hooks/post-commit`) sends every new commit and version tag to GitHub, so helpers
  reading the repository always see the latest code. `.env`, `data/`, `logs/` and `reports/` are never uploaded.
- `handoffs/`: briefings for the AI helpers following the project (no secrets). How the automatic uploads work: `AUTOMATION.md`.

## Configuration

- `config.yaml`: all normal settings, commented. Put times in quotes (`"20:00"`).
- `.env`: secrets only. Never commit or share it.
- Any setting can be overridden by an environment variable: `SCOUT_<SECTION>__<KEY>`,
  e.g. `SCOUT_RISK__RISK_PER_TRADE_PCT=0.5`.

Safety rules enforced by config validation: `mode: live` is refused, leverage can't exceed 1x,
and `long_only` must stay `true`.

## Layout

```
scout/          the Python package (one module per part of the bot)
tests/          pytest tests
config.yaml     settings
logs/           rotating log files (git-ignored)
data/scout.db   SQLite database (git-ignored)
```

## Conventions

- Times are stored in UTC and shown in Australia/Sydney time.
- Money is stored in USD/USDC (Hyperliquid's currency) and shown in AUD where useful.
- Every build bumps `APP_VERSION`. It appears at startup, on every log line and in every message.
