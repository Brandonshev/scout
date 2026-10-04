"""Copy trading: find wallets whose own recent trades were consistently profitable, then follow them.

Picking wallets (once a day):
1. The public leaderboard gives thousands of wallets, but its returns can mislead (a wallet that
   started nearly empty shows +3,000,000%). So it's only a first filter: big enough account,
   profitable month and all-time, and trading this week.
2. For the best candidates, their actual trades over the last 30 days decide: profit after fees
   relative to account size, profitable in at least 2 of 3 ten-day stretches, enough closed trades,
   profit factor (money won ÷ money lost). Market-making bots (thousands of trades) are skipped:
   a copier can't keep up with them.

Following (every poll): compare each wallet's positions with last time. A NEW position is copied
(existing ones are ignored: we'd be late); when the wallet closes or flips it, our copy is closed.
Only main-market perps are copied ("dex:COIN" builder markets can't be traded here).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from scout.config import CopySettings
from scout.data import WalletPosition

DAY_MS = 86_400_000


def _window(row: dict, name: str) -> dict:
    return dict(row["windowPerformances"])[name]


def leaderboard_candidates(rows: Sequence[dict], cfg: CopySettings) -> list[tuple[str, float]]:
    """(wallet, account value) of the most promising wallets to check in detail."""
    picked = []
    for row in rows:
        try:
            account = float(row["accountValue"])
            month, all_time, week = _window(row, "month"), _window(row, "allTime"), _window(row, "week")
            if (account >= cfg.min_account_usd and float(month["pnl"]) > 0 and float(all_time["pnl"]) > 0
                    and float(week["vlm"]) > 0):
                picked.append((float(month["pnl"]) / account, row["ethAddress"], account))
        except (KeyError, TypeError, ValueError):
            continue
    picked.sort(reverse=True)
    return [(wallet, account) for _, wallet, account in picked[: cfg.candidates]]


@dataclass(frozen=True)
class WalletScore:
    wallet: str
    account_usd: float
    pnl_usd: float  # realised over the lookback, after fees (main-market trades only)
    return_pct: float
    closing_trades: int
    win_rate_pct: float
    profit_factor: float
    good_windows: int  # profitable ten-day stretches out of 3
    fills: int
    score: float
    excluded: str = ""  # why it isn't followed, if it isn't
    open_pnl_usd: float = 0.0  # profit/loss sitting in positions it hasn't closed yet

    @property
    def short(self) -> str:
        return f"{self.wallet[:6]}…{self.wallet[-4:]}"

    @property
    def summary(self) -> str:
        return (f"{self.short} (30d {self.return_pct:+.1f}% on US${self.account_usd:,.0f}, {self.closing_trades} "
                f"trades, {self.win_rate_pct:.0f}% won)")


def score_wallet(wallet: str, account_usd: float, fills: Sequence[dict], now_ms: int, cfg: CopySettings,
                 open_pnl_usd: float = 0.0) -> WalletScore:
    """Judge a wallet on its real trades (fills) over the lookback, plus its open positions.

    Closed trades alone can flatter: a wallet that sells winners but never closes losers shows a near-100%
    win rate. So losses still sitting in open positions count too, and big ones rule the wallet out.
    """
    start = now_ms - cfg.lookback_days * DAY_MS
    main = [f for f in fills if ":" not in f.get("coin", "") and int(f.get("time", 0)) >= start]
    closing = [f for f in main if float(f.get("closedPnl") or 0) != 0]
    results = [float(f["closedPnl"]) for f in closing]
    fees = sum(float(f.get("fee") or 0) for f in main)
    pnl = sum(results) - fees
    won = sum(r for r in results if r > 0)
    lost = -sum(r for r in results if r < 0)
    profit_factor = min(won / lost, 10.0) if lost > 0 else (10.0 if won > 0 else 0.0)
    third = cfg.lookback_days * DAY_MS / 3
    windows = [0.0, 0.0, 0.0]
    for f in closing:
        windows[min(2, int((int(f["time"]) - start) // third))] += float(f["closedPnl"])
    good = sum(w > 0 for w in windows)
    return_pct = (pnl + min(open_pnl_usd, 0.0)) / account_usd * 100 if account_usd else 0.0
    open_loss_pct = -open_pnl_usd / account_usd * 100 if account_usd and open_pnl_usd < 0 else 0.0

    excluded = ""
    if len(fills) >= cfg.max_fills:
        excluded = f"{len(fills)}+ trades: a market-making bot, too fast to copy"
    elif len(closing) < cfg.min_closing_trades:
        excluded = f"only {len(closing)} closed trades on the main market (need {cfg.min_closing_trades})"
    elif open_loss_pct > cfg.max_open_loss_pct:
        excluded = (f"sitting on open losses of {open_loss_pct:.0f}% of its account (it may be holding losers "
                    "rather than closing them)")
    elif pnl + min(open_pnl_usd, 0.0) <= 0:
        excluded = "lost money over the period after fees (including open losses)"
    elif profit_factor < cfg.min_profit_factor:
        excluded = f"profit factor {profit_factor:.2f} (need {cfg.min_profit_factor:g})"
    elif good < 2:
        excluded = f"profitable in only {good} of 3 ten-day stretches (inconsistent)"
    score = 0.0 if excluded else return_pct * good / 3
    return WalletScore(wallet, account_usd, pnl, return_pct, len(closing),
                       100 * sum(r > 0 for r in results) / len(results) if results else 0.0,
                       profit_factor, good, len(fills), score, excluded, open_pnl_usd)


def pick_wallets(scores: Sequence[WalletScore], count: int) -> list[WalletScore]:
    return sorted((s for s in scores if not s.excluded), key=lambda s: -s.score)[:count]


def wallets_to_json(scores: Sequence[WalletScore]) -> str:
    return json.dumps([asdict(s) for s in scores])


def wallets_from_json(text: str | None) -> list[WalletScore]:
    return [WalletScore(**row) for row in json.loads(text)] if text else []


# ------------------------------------------------------------ following


@dataclass(frozen=True)
class Change:
    wallet: str
    coin: str
    action: str  # "open" or "close"
    side: str
    value_usd: float = 0.0
    entry_price: float = 0.0


def copyable(coin: str) -> bool:
    return ":" not in coin  # "dex:COIN" = a builder market we can't trade


def diff_positions(wallet: str, previous: Mapping[str, WalletPosition] | None, current: Sequence[WalletPosition],
                   account_usd: float, cfg: CopySettings) -> list[Change]:
    """What changed since last time. The first look at a wallet (previous=None) copies nothing:
    positions it already had were opened before we were watching, so we'd be buying late."""
    now = {p.coin: p for p in current if copyable(p.coin) and p.size != 0}
    if previous is None:
        return []
    changes = []
    for coin, old in previous.items():
        new = now.get(coin)
        if new is None or new.side != old.side:
            changes.append(Change(wallet, coin, "close", old.side))
    for coin, new in now.items():
        old = previous.get(coin)
        if old is not None and old.side == new.side:
            continue
        big_enough = account_usd <= 0 or new.value_usd >= account_usd * cfg.min_wallet_position_pct / 100
        if big_enough and (new.side == "long" or cfg.allow_shorts):
            changes.append(Change(wallet, coin, "open", new.side, new.value_usd, new.entry_price))
    return changes


def snapshot(current: Sequence[WalletPosition]) -> dict[str, WalletPosition]:
    return {p.coin: p for p in current if copyable(p.coin) and p.size != 0}
