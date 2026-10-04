"""A public status page (STATUS.md) for all three accounts, refreshed hourly, so outside helpers can follow along.

It reads each account's database READ-ONLY (it can't change anything) plus live prices, and writes plain
markdown. `publish` pushes it to the `status` branch of the GitHub copy: one commit, replaced every hour, so
the code's history isn't cluttered. Nothing secret goes in: balances and trades of fake accounts only.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scout import service
from scout.config import Settings
from scout.data import format_price
from scout.demo import DemoAccount, StateStore
from scout.version import APP_VERSION

STATUS_BRANCH = "status"


@dataclass(frozen=True)
class AccountInfo:
    name: str
    what: str  # the strategy in one line
    settings: Settings  # that account's own settings (database, starting balance)


def _when(ms: float, settings: Settings) -> str:
    return datetime.fromtimestamp(ms / 1000, settings.app.tz).strftime("%a %d %b %H:%M")


def _aud(usd: float, settings: Settings, signed: bool = False) -> str:
    value = usd / settings.demo.aud_to_usdc_rate
    sign = ("+" if value >= 0 else "−") if signed else ("−" if value < 0 else "")
    return f"{sign}A${abs(value):,.2f}"


def _clean(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def account_section(info: AccountInfo, prices: Mapping[str, float], now_ms: int) -> list[str]:
    s = info.settings
    db = s.app.db_path
    lines = [f"## {info.name}", f"*{info.what}*", ""]
    if not db.exists():
        return [*lines, "Not started yet.", ""]
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        account = DemoAccount(conn, s)
        state = StateStore(conn)
        held = account.positions()
        equity = account.equity(prices)
        start = state.get_float("initial_equity_usd", equity) or equity
        btc_start = state.get_float("btc_start_price", 0)
        btc_now = prices.get("BTC")
        opened = state.get_float("opened_ms", 0)
        closed = conn.execute("SELECT coin, side, entry_price, exit_price, pnl_usd, closed_ts_ms, close_reason, strategy "
                              "FROM demo_positions WHERE status = 'closed' ORDER BY closed_ts_ms DESC").fetchall()
        events = conn.execute("SELECT ts_ms, level, message FROM events_log WHERE level IN ('WARNING', 'ERROR') "
                              "AND ts_ms >= ? ORDER BY ts_ms DESC LIMIT 5", (now_ms - 86_400_000,)).fetchall()
        bot_state = state.get("state", "?")
    beat = service.read_heartbeat(service.heartbeat_path(s))
    alive = "no heartbeat" if not beat else f"heartbeat {(now_ms - beat['ts_ms']) / 1000:.0f}s ago, v{beat['version']}"
    btc_line = (f" · holding BTC instead: {(btc_now / btc_start - 1) * 100:+.2f}%" if btc_now and btc_start else "")
    wins = sum(r["pnl_usd"] > 0 for r in closed)
    lines += [
        f"- **Balance:** {_aud(equity, s)} ({(equity / start - 1) * 100:+.2f}% since {_when(opened, s) if opened else 'the start'})"
        + btc_line,
        f"- **State:** {bot_state} · loop {alive}",
        f"- **Closed trades:** {len(closed)} ({wins} won), total {_aud(sum(r['pnl_usd'] for r in closed), s, True)}",
        "",
    ]
    if held:
        lines += ["| Coin | Side | Entry | Now | Profit/loss | Opened | Why |", "|---|---|---|---|---|---|---|"]
        for p in held:
            price = prices.get(p.coin, p.entry_price)
            pnl = p.unrealised_usd(price) - p.fees_usd - p.funding_usd
            name = p.source if p.strategy == "high_risk" and p.source else p.coin
            lines.append(f"| {name} | {p.side} | ${format_price(p.entry_price)} | ${format_price(price)} | "
                         f"{_aud(pnl, s, True)} ({pnl / (p.qty * p.entry_price) * 100:+.1f}%) | "
                         f"{_when(p.opened_ts_ms, s)} | {_clean(p.open_reason)} |")
        lines.append("")
    else:
        lines += ["No open positions.", ""]
    if closed:
        lines += ["Last 10 closed trades:", "", "| Closed | Coin | Side | Result | Why it closed |", "|---|---|---|---|---|"]
        for r in closed[:10]:
            lines.append(f"| {_when(r['closed_ts_ms'], s)} | {r['coin']} | {r['side']} | {_aud(r['pnl_usd'], s, True)} | "
                         f"{_clean(r['close_reason'])} |")
        lines.append("")
    if events:
        lines += ["Warnings in the last 24h:", ""] + [f"- {_when(e['ts_ms'], s)} {e['level']}: {_clean(e['message'], 160)}"
                                                       for e in events] + [""]
    return lines


def build_page(accounts: list[AccountInfo], prices: Mapping[str, float], now_ms: int, settings: Settings,
               commit: str | None = None) -> str:
    lines = [
        "# Scout status",
        "",
        f"Updated **{_when(now_ms, settings)} Sydney time**, refreshed every hour. Scout v{APP_VERSION}"
        + (f", code version `{commit}`" if commit else "") + ".",
        "",
        "All three accounts trade **fake money** (each started with A$1,000) on real live Hyperliquid prices. "
        "Nothing here is financial advice. The code is on the `main` branch of this repository.",
        "",
    ]
    for info in accounts:
        lines += account_section(info, prices, now_ms)
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------ publishing


def _git(repo: Path, *args: str, input_text: str | None = None, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], input=input_text, capture_output=True, text=True,
                            env={**os.environ, **(env or {})}, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def current_commit(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "--short", "HEAD")
    except (RuntimeError, OSError):
        return None


def publish(repo: Path, text: str, remote: str = "origin", branch: str = STATUS_BRANCH) -> str:
    """Push STATUS.md as the only file on `branch`, replacing what was there (one commit, no history).
    Uses git's plumbing, so the working folder and the main branch are never touched."""
    blob = _git(repo, "hash-object", "-w", "--stdin", input_text=text)
    tree = _git(repo, "mktree", input_text=f"100644 blob {blob}\tSTATUS.md\n")
    commit = _git(repo, "commit-tree", tree, "-m", f"Status update (Scout v{APP_VERSION})")
    _git(repo, "push", "--force", "--quiet", remote, f"{commit}:refs/heads/{branch}",
         env={"GIT_TERMINAL_PROMPT": "0"})  # never sit waiting for a password in the background
    return commit
