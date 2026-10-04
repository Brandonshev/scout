"""Demo trading: the live pipeline on real prices, with a fake A$1,000 account.

- DemoAccount keeps the account in SQLite (cash and bot state in bot_state, positions in
  demo_positions, orders in demo_orders), so `scout status`, `scout pause` and `scout kill`
  in another Terminal window see and change the same account.
- DemoEngine sends every order through the RiskManager, then to an OrderExecutor. The only
  executor is DemoExecutor, which pretends to fill at the live price ± slippage and charges the
  taker fee. There is no exchange key or order-signing code anywhere in Scout.
- DemoRunner is the loop: live prices from the websocket, stops and account checks every few
  seconds, and mood -> scan -> signals every hour. After the Mac sleeps it waits for fresh
  prices before doing anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scout import notify as messages
from scout.backup import backup_db
from scout.config import Settings
from scout.data import format_price
from scout.db import log_event, now_ms
from scout.notify import Notifier, Priority
from scout.reports import report_message, save_report, weekly_report
from scout.risk import Alert, BotState, HeldPosition, Order, RiskContext, RiskManager
from scout.service import heartbeat_path, write_heartbeat
from scout.signals import Account, Action, OpenPosition, Signal
from scout.version import APP_VERSION

log = logging.getLogger(__name__)
MINUTE_MS = 60_000
# Positions without a stop loss (the experiment's) store a stop that can never be reached.
NO_STOP_LONG = 0.0
NO_STOP_SHORT = 1e18


# --------------------------------------------------------------- executors


@dataclass(frozen=True)
class Fill:
    coin: str
    side: str  # "buy" or "sell"
    qty: float
    price: float  # after slippage
    fee_usd: float
    ts_ms: int


class OrderExecutor(ABC):
    """Turns an approved order into a fill. Live trading (v1.0.0) would add an exchange executor."""

    name: str

    @abstractmethod
    async def execute(self, order: Order) -> Fill:
        ...


class DemoExecutor(OrderExecutor):
    """Pretend fills at the order's (live) price ± slippage, paying the taker fee. Never contacts an exchange."""

    name = "demo"

    def __init__(self, taker_fee_pct: float, slippage_pct: float, clock: Callable[[], int] = now_ms) -> None:
        self.fee = taker_fee_pct / 100
        self.slip = slippage_pct / 100
        self.clock = clock

    async def execute(self, order: Order) -> Fill:
        price = order.price * (1 + self.slip) if order.side == "buy" else order.price * (1 - self.slip)
        return Fill(order.coin, order.side, order.qty, price, order.qty * price * self.fee, self.clock())


# ------------------------------------------------------------------ state


class StateStore:
    """Small key/value store in the bot_state table."""

    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], int] = now_ms) -> None:
        self.conn = conn
        self.clock = clock

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return default if row is None or row["value"] is None else row["value"]

    def get_float(self, key: str, default: float = 0.0) -> float:
        value = self.get(key)
        return default if value is None else float(value)

    def set(self, key: str, value: object) -> None:
        self.conn.execute(
            "INSERT INTO bot_state (key, value, updated_ms) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (key, None if value is None else str(value), self.clock()),
        )

    @property
    def state(self) -> BotState:
        return BotState(self.get("state", BotState.RUNNING))


# ---------------------------------------------------------------- account


@dataclass(frozen=True)
class DemoPosition:
    id: int
    coin: str
    side: str
    qty: float
    entry_price: float
    stop_price: float
    opened_ts_ms: int
    fees_usd: float
    funding_usd: float
    open_reason: str
    strategy: str = "core"
    source: str | None = None

    @property
    def long(self) -> bool:
        return self.side == "long"

    @property
    def has_stop(self) -> bool:
        return self.stop_price != (NO_STOP_LONG if self.long else NO_STOP_SHORT)

    def value(self, price: float) -> float:
        """What the position adds to the account: coins held (long), or the gain/loss so far (short)."""
        return self.qty * price if self.long else self.qty * (self.entry_price - price)

    def unrealised_usd(self, price: float) -> float:
        return self.qty * (price - self.entry_price) * (1 if self.long else -1)


class DemoAccount:
    """The fake account, stored in SQLite."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, clock: Callable[[], int] = now_ms) -> None:
        self.conn = conn
        self.settings = settings
        self.clock = clock
        self.state = StateStore(conn, clock)

    def ensure_started(self) -> None:
        if self.state.get("cash_usd") is None:
            start = self.settings.demo.starting_balance_usdc
            with self.conn:
                for key, value in {"cash_usd": start, "initial_equity_usd": start, "peak_equity_usd": start,
                                   "day_start_equity_usd": start, "state": BotState.RUNNING,
                                   "opened_ms": self.clock(), "mode": self.settings.mode.value}.items():
                    self.state.set(key, value)
                log_event(self.conn, "INFO", "demo", f"demo account opened with US${start:,.2f} "
                          f"(A${self.settings.demo.starting_balance_aud:,.2f})", ts_ms=self.clock())

    @property
    def cash(self) -> float:
        return self.state.get_float("cash_usd", self.settings.demo.starting_balance_usdc)

    def positions(self) -> list[DemoPosition]:
        rows = self.conn.execute(
            """SELECT id, coin, side, qty, entry_price, stop_price, opened_ts_ms, fees_usd, funding_usd, open_reason,
                      strategy, source
               FROM demo_positions WHERE status = 'open' ORDER BY opened_ts_ms"""
        ).fetchall()
        return [DemoPosition(**dict(r)) for r in rows]

    def equity(self, prices: Mapping[str, float]) -> float:
        return self.cash + sum(p.value(prices.get(p.coin, p.entry_price)) for p in self.positions())

    def free_cash(self) -> float:
        """Cash not set aside as cover for shorts."""
        return self.cash - sum(p.qty * p.entry_price for p in self.positions() if not p.long)

    def record_order(self, order: Order, status: str, reason: str, fill: Fill | None = None,
                     position_id: int | None = None) -> None:
        self.conn.execute(
            """INSERT INTO demo_orders (ts_ms, coin, side, order_type, qty, price, fill_price, fee_usd, status,
                                        reason, position_id, app_version)
               VALUES (?, ?, ?, 'market', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fill.ts_ms if fill else self.clock(), order.coin, order.side, order.qty, order.price,
             fill.price if fill else None, fill.fee_usd if fill else 0.0, status, reason, position_id, APP_VERSION),
        )

    def open(self, order: Order, fill: Fill, strategy: str = "core", source: str | None = None) -> int:
        side = order.position_side
        cost = fill.qty * fill.price if side == "long" else 0.0
        self.state.set("cash_usd", self.cash - cost - fill.fee_usd)
        stop = order.stop_price if order.stop_price is not None else (NO_STOP_LONG if side == "long" else NO_STOP_SHORT)
        cursor = self.conn.execute(
            """INSERT INTO demo_positions (coin, side, status, opened_ts_ms, qty, entry_price, stop_price, fees_usd,
                                           open_reason, strategy, source, app_version)
               VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order.coin, side, fill.ts_ms, fill.qty, fill.price, stop, fill.fee_usd, order.reason, strategy, source,
             APP_VERSION),
        )
        return int(cursor.lastrowid)

    def close(self, position: DemoPosition, fill: Fill, reason: str) -> float | None:
        """Close a position. Returns the realised profit/loss, or None if it was already closed
        (e.g. `scout kill` in another window got there first)."""
        gross = position.qty * (fill.price - position.entry_price) * (1 if position.long else -1)
        fees = position.fees_usd + fill.fee_usd
        pnl = gross - fees - position.funding_usd
        claimed = self.conn.execute(
            """UPDATE demo_positions SET status = 'closed', closed_ts_ms = ?, exit_price = ?, fees_usd = ?,
                   pnl_usd = ?, close_reason = ? WHERE id = ? AND status = 'open'""",
            (fill.ts_ms, fill.price, fees, pnl, reason, position.id),
        ).rowcount
        if not claimed:
            return None
        returned = fill.qty * fill.price if position.long else gross
        self.state.set("cash_usd", self.cash + returned - fill.fee_usd)
        return pnl

    def move_stop(self, position: DemoPosition, stop: float) -> bool:
        """Tighten a stop. Stops only ever move in the position's favour."""
        tighter = stop > position.stop_price if position.long else stop < position.stop_price
        if tighter:
            self.conn.execute("UPDATE demo_positions SET stop_price = ? WHERE id = ?", (stop, position.id))
        return tighter

    def charge_funding(self, position: DemoPosition, amount_usd: float) -> None:
        """Funding paid (positive) or received (negative) for holding the position."""
        self.conn.execute("UPDATE demo_positions SET funding_usd = funding_usd + ? WHERE id = ?",
                          (amount_usd, position.id))
        self.state.set("cash_usd", self.cash - amount_usd)

    def snapshot(self, prices: Mapping[str, float], ts_ms: int) -> float:
        positions = self.positions()
        value = sum(p.value(prices.get(p.coin, p.entry_price)) for p in positions)
        equity = self.cash + value
        self.conn.execute(
            """INSERT OR REPLACE INTO equity_snapshots
                   (ts_ms, equity_usd, cash_usd, positions_value_usd, aud_to_usd_rate, btc_price, mode, app_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts_ms, equity, self.cash, value, self.settings.demo.aud_to_usdc_rate, prices.get("BTC"),
             self.settings.mode.value, APP_VERSION),
        )
        # The latest price of each open position, so the (offline, read-only) dashboard can show it
        self.conn.executemany(
            "UPDATE demo_positions SET last_price = ?, last_price_ms = ? WHERE id = ?",
            [(prices[p.coin], ts_ms, p.id) for p in positions if p.coin in prices],
        )
        return equity


def demo_account_snapshot(settings: Settings, conn: sqlite3.Connection,
                          prices: Mapping[str, float] | None = None) -> Account:
    """The demo account as the signal engine sees it (for sizing new trades)."""
    account = DemoAccount(conn, settings)
    prices = prices or {}
    return Account(account.equity(prices), account.free_cash(), 1 / settings.demo.aud_to_usdc_rate)


# ------------------------------------------------------------------ prices


class PriceBook:
    """The latest live price of each coin and when it arrived."""

    def __init__(self) -> None:
        self.prices: dict[str, float] = {}
        self.updated_ms: dict[str, int] = {}
        self.last_update_ms = 0

    def update(self, mids: Mapping[str, float], ts_ms: int) -> None:
        self.prices.update(mids)
        self.updated_ms.update(dict.fromkeys(mids, ts_ms))
        self.last_update_ms = ts_ms

    def age_seconds(self, coin: str, now: int) -> float | None:
        ts = self.updated_ms.get(coin)
        return None if ts is None else (now - ts) / 1000

    def fresh(self, now: int, limit_seconds: float) -> bool:
        return bool(self.last_update_ms) and (now - self.last_update_ms) / 1000 <= limit_seconds


# ------------------------------------------------------------------ engine


def sydney_day(ts_ms: int, settings: Settings) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, settings.app.tz).strftime("%Y-%m-%d")


class DemoEngine:
    """Executes orders for the demo account, enforcing every risk rule. No network access."""

    def __init__(self, settings: Settings, conn: sqlite3.Connection, executor: OrderExecutor | None = None,
                 clock: Callable[[], int] = now_ms, notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.notifier = notifier
        self.conn = conn
        self.clock = clock
        self.risk = RiskManager(settings.risk)
        self.executor = executor or DemoExecutor(settings.risk.taker_fee_pct, settings.risk.slippage_pct, clock)
        self.account = DemoAccount(conn, settings, clock)
        self.state = self.account.state
        self.prices = PriceBook()
        self.funding_rates: dict[str, float] = {}  # hourly rates from the latest market data
        self.summaries = True  # daily summary and weekly report (the experiment sends its own)
        self.account.ensure_started()

    # ---- state and context

    @property
    def bot_state(self) -> BotState:
        return self.state.state

    def event(self, level: str, category: str, message: str, data: dict | None = None) -> None:
        log.log(logging.getLevelName(level), message)
        log_event(self.conn, level, category, message, data, ts_ms=self.clock())

    def notify(self, text: str | None, category: str, priority: Priority = Priority.NORMAL) -> None:
        if text and self.notifier is not None:
            self.notifier.notify(text, category, priority)

    @property
    def usd_per_aud(self) -> float:
        return self.settings.demo.aud_to_usdc_rate

    def risk_context(self, coin: str | None = None) -> RiskContext:
        now = self.clock()
        positions = self.account.positions()
        prices = self.prices.prices
        return RiskContext(
            state=self.bot_state,
            equity_usd=self.account.equity(prices),
            free_cash_usd=self.account.free_cash(),
            peak_equity_usd=self.state.get_float("peak_equity_usd", self.settings.demo.starting_balance_usdc),
            day_start_equity_usd=self.state.get_float("day_start_equity_usd", self.settings.demo.starting_balance_usdc),
            daily_limit_hit=self.state.get("daily_limit_hit") == "1",
            positions=tuple(HeldPosition(p.coin, p.side, p.qty * prices.get(p.coin, p.entry_price)) for p in positions),
            price_age_seconds=self.prices.age_seconds(coin, now) if coin else None,
            allow_shorts=self.settings.signals.allow_shorts,
        )

    # ---- orders

    async def submit(self, order: Order) -> Fill | None:
        """Risk check, then execute. Rejections are recorded with their reason."""
        decision = self.risk.check(order, self.risk_context(order.coin))
        if not decision.approved:
            with self.conn:
                self.account.record_order(order, "rejected", decision.reason)
                self.event("WARNING", "risk", decision.reason, {"coin": order.coin, "side": order.side})
            return None
        return await self.executor.execute(order)

    async def open_from_signal(self, signal: Signal) -> int | None:
        price = self.prices.prices.get(signal.coin, signal.price)
        order = Order(signal.coin, "buy" if signal.action is Action.ENTER_LONG else "sell", signal.qty or 0.0,
                      price, reduce_only=False, reason=signal.reason, stop_price=signal.stop_price)
        fill = await self.submit(order)
        if fill is None:
            return None
        with self.conn:
            position_id = self.account.open(order, fill)
            self.account.record_order(order, "filled", signal.reason, fill, position_id)
            self._mark_acted(signal)
            self.event("INFO", "trade", f"{signal.reason} Filled at ${format_price(fill.price)}.",
                       {"coin": order.coin, "qty": fill.qty, "price": fill.price})
        self.notify(messages.trade_opened(
            order.coin, order.position_side, fill.qty, fill.price, order.stop_price, signal.risk_usd or 0.0,
            fill.qty * fill.price, signal.details.get("why") or signal.reason, signal.regime, self.usd_per_aud,
            wild=self.state.get("last_volatility") == "WILD",
        ), "trade")
        return position_id

    async def open_position(self, coin: str, side: str, qty: float, reason: str, *, strategy: str,
                            source: str | None = None, stop: float | None = None, fill: Fill | None = None,
                            message: str | None = None) -> int | None:
        """Open a position that doesn't come from a signal (copy trading, high-risk coins).
        The risk manager checks it first. `fill` lets the caller supply an order-book fill price."""
        price = self.prices.prices.get(coin)
        if price is None:
            self.event("WARNING", "trade", f"Can't open {coin}: no live price for it.")
            return None
        order = Order(coin, "buy" if side == "long" else "sell", qty, price, reduce_only=False, reason=reason,
                      stop_price=stop)
        executed = await self.submit(order)
        if executed is None:
            return None
        fill = fill or executed
        with self.conn:
            position_id = self.account.open(order, fill, strategy, source)
            self.account.record_order(order, "filled", reason, fill, position_id)
            self.event("INFO", "trade", f"{reason} Filled at ${format_price(fill.price)}.",
                       {"coin": coin, "qty": fill.qty, "price": fill.price, "strategy": strategy})
        self.notify(message, "trade", Priority.BATCH)
        return position_id

    async def close(self, position: DemoPosition, reason: str, price: float | None = None,
                    fill_price: float | None = None) -> float | None:
        price = price if price is not None else self.prices.prices.get(position.coin)
        if price is None:
            self.event("WARNING", "trade", f"Can't close {position.coin} yet: no price for it.")
            return None
        order = Order(position.coin, "sell" if position.long else "buy", position.qty, price,
                      reduce_only=True, reason=reason)
        fill = await self.submit(order)
        if fill_price is not None:  # e.g. a price worked out from the real order book
            fill = Fill(fill.coin, fill.side, fill.qty, fill_price, fill.qty * fill_price * self.settings.risk.taker_fee_pct
                        / 100, fill.ts_ms)
        with self.conn:
            pnl = self.account.close(position, fill, reason)
            if pnl is None:
                return None
            self.account.record_order(order, "filled", reason, fill, position.id)
            aud = pnl / self.settings.demo.aud_to_usdc_rate
            self.event("INFO", "trade", f"{reason} Filled at ${format_price(fill.price)}; result "
                       f"{'+' if pnl >= 0 else '-'}A${abs(aud):,.2f} after fees and funding.",
                       {"coin": position.coin, "pnl_usd": pnl})
        if not reason.startswith("Kill switch"):  # the kill switch sends one message for everything
            self.notify(messages.trade_closed(position.coin, position.side, position.entry_price, fill.price,
                                              position.qty, pnl, reason, fill.ts_ms - position.opened_ts_ms,
                                              self.usd_per_aud), "trade",
                        Priority.NORMAL if position.strategy == "core" else Priority.BATCH)
        return pnl

    def _mark_acted(self, signal: Signal) -> None:
        self.conn.execute("UPDATE signals SET acted = 1 WHERE coin = ? AND action = ? AND candle_ts_ms IS ?",
                          (signal.coin, signal.action.value, signal.candle_ts_ms))

    async def execute_signals(self, signals: Sequence[Signal]) -> None:
        """Exits and stop moves first (they free up room and cash), then entries."""
        held = {p.coin: p for p in self.account.positions()}
        for s in signals:
            position = held.get(s.coin)
            if position is None:
                continue
            if s.action is Action.EXIT:
                if await self.close(position, s.reason) is not None:
                    with self.conn:
                        self._mark_acted(s)
            elif s.action is Action.MOVE_STOP and s.stop_price is not None:
                with self.conn:
                    if self.account.move_stop(position, s.stop_price):
                        self._mark_acted(s)
                        self.event("INFO", "trade", s.reason, {"coin": s.coin, "stop": s.stop_price})
                        self.notify(messages.stop_moved(s.coin, position.stop_price, s.stop_price), "trade",
                                    Priority.BATCH)
        for s in signals:
            if s.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
                await self.open_from_signal(s)

    # ---- the checks that run every few seconds

    async def check_stops(self, late: bool = False) -> None:
        for p in self.account.positions():
            price = self.prices.prices.get(p.coin)
            if price is None:
                continue
            if (p.long and price <= p.stop_price) or (not p.long and price >= p.stop_price):
                verb = "Selling" if p.long else "Closing the short in"
                note = " It was hit while the Mac was asleep, so it filled late at a worse price." if late else ""
                await self.close(p, f"{verb} {p.coin}: the stop loss at ${format_price(p.stop_price)} was hit "
                                    f"(price ${format_price(price)}).{note}", price)

    async def kill(self, reason: str) -> None:
        """KILL SWITCH: close everything at the latest prices and stop until manually reset."""
        with self.conn:
            self.state.set("state", BotState.KILLED)
            self.state.set("killed_reason", reason)
            self.state.set("killed_ms", self.clock())
            self.state.set("kill_requested", None)
            self.event("CRITICAL", "risk", f"KILL SWITCH: {reason} Closing every position; no new trades until "
                                           "`scout reset-kill`.")
        closed, result = 0, 0.0
        for p in self.account.positions():
            pnl = await self.close(p, f"Kill switch: closing {p.coin}. {reason}")
            if pnl is not None:
                closed, result = closed + 1, result + pnl
        self.notify(messages.kill_switch(reason, closed, result, self.usd_per_aud), "risk", Priority.CRITICAL)

    def reset_kill(self) -> None:
        """Manual reset: start again from the current account value as the new peak."""
        equity = self.account.equity(self.prices.prices)
        with self.conn:
            self.state.set("state", BotState.RUNNING)
            self.state.set("peak_equity_usd", equity)
            self.state.set("killed_reason", None)
            self.event("WARNING", "risk", f"Kill switch reset by you. Trading resumes; the new peak is "
                                          f"US${equity:,.2f}.")

    def set_paused(self, paused: bool) -> None:
        if self.bot_state is BotState.KILLED:
            raise ValueError("the kill switch is on: use `scout reset-kill`, not resume/pause")
        with self.conn:
            self.state.set("state", BotState.PAUSED if paused else BotState.RUNNING)
            self.event("INFO", "control", "Paused by you: no new trades; open positions are still managed."
                       if paused else "Resumed by you: new trades allowed again.")

    async def housekeeping(self) -> None:
        """Commands from other windows, the new day, funding, snapshots, the daily limit and the kill switch."""
        now = self.clock()
        if self.state.get("kill_requested") and self.bot_state is not BotState.KILLED:
            await self.kill("You pressed the kill switch.")
        elif self.bot_state is BotState.KILLED and self.account.positions():
            for p in self.account.positions():  # e.g. killed while this loop wasn't running
                await self.close(p, f"Kill switch: closing {p.coin}.")

        with self.conn:
            today = sydney_day(now, self.settings)
            if self.state.get("day") != today:
                equity = self.account.equity(self.prices.prices)
                had_limit = self.state.get("daily_limit_hit") == "1"
                self.state.set("day", today)
                self.state.set("day_start_equity_usd", equity)
                self.state.set("day_start_ms", now)
                self.state.set("btc_day_start_price", self.prices.prices.get("BTC"))
                self.state.set("daily_limit_hit", "0")
                self.event("INFO", "demo", f"New trading day ({today}, Sydney): starting value US${equity:,.2f}."
                           + (" The daily loss limit is reset." if had_limit else ""))

            last = int(self.state.get_float("last_funding_ms", now))
            hours = (now - last) / 3_600_000
            if hours > 0:
                for p in self.account.positions():
                    rate = self.funding_rates.get(p.coin, 0.0)
                    price = self.prices.prices.get(p.coin, p.entry_price)
                    amount = p.qty * price * rate * hours * (1 if p.long else -1)
                    if amount:
                        self.account.charge_funding(p, amount)
            self.state.set("last_funding_ms", now)
            if self.state.get("btc_start_price") is None and "BTC" in self.prices.prices:
                self.state.set("btc_start_price", self.prices.prices["BTC"])  # for "vs holding BTC"

            every = self.settings.demo.snapshot_minutes * MINUTE_MS
            if now - int(self.state.get_float("last_snapshot_ms", 0)) >= every:
                equity = self.account.snapshot(self.prices.prices, now)
                self.state.set("last_snapshot_ms", now)
                if equity > self.state.get_float("peak_equity_usd", equity):
                    self.state.set("peak_equity_usd", equity)

        ctx = self.risk_context()
        for alert in self.risk.alerts(ctx):
            if alert is Alert.KILL:
                await self.kill(f"The account fell {ctx.drawdown_pct:.1f}% from its peak (limit "
                                f"{self.settings.risk.kill_switch_drawdown_pct:g}%).")
            elif alert is Alert.DAILY_LIMIT:
                with self.conn:
                    self.state.set("daily_limit_hit", "1")
                    self.event("WARNING", "risk", f"Daily loss limit hit: down {ctx.daily_loss_pct:.1f}% since "
                               f"midnight Sydney (limit {self.settings.risk.daily_loss_limit_pct:g}%). No new trades "
                               "until tomorrow; open positions are still managed.")
                self.notify(messages.daily_limit_hit(ctx.daily_loss_pct, self.settings.risk.daily_loss_limit_pct),
                            "risk")
        self.maybe_daily_summary(now)
        self.maybe_weekly_report(now)

    # ---- starting up

    def on_start(self, by_service: bool) -> str:
        """Record this start and send a restart alert. Returns a line for the console."""
        now = self.clock()
        last_tick = int(self.state.get_float("last_tick_ms", 0))
        stopped = int(self.state.get_float("loop_stopped_ms", 0))
        crash = self.state.get("crash_reason")
        with self.conn:
            self.state.set("crash_reason", None)
        if not last_tick:
            return "First start."
        down = messages.duration(now - last_tick)
        when = datetime.fromtimestamp(last_tick / 1000, self.settings.app.tz).strftime("%a %d %b %H:%M")
        if stopped >= last_tick and not crash:
            line = f"Scout started{' (service)' if by_service else ''}. Last stopped cleanly {when}, {down} ago."
            priority = Priority.BATCH
        else:
            reason = crash or "unknown (power loss, a Mac restart, or the process was killed)"
            line = (f"🔄 Scout restarted after an unexpected stop at {when} ({down} ago). Reason: {reason}. "
                    "Checking prices and positions before trading.")
            priority = Priority.NORMAL
        with self.conn:
            self.event("WARNING" if priority is Priority.NORMAL else "INFO", "demo", line)
        if self.settings.service.restart_alert:
            self.notify(line, "service", priority)
        return line

    def recent_starts(self, window_ms: int = 3_600_000) -> int:
        """Count starts in the last hour (to spot a crash loop), recording this one."""
        now = self.clock()
        starts = [int(t) for t in (self.state.get("start_times") or "").split(",") if t]
        starts = [t for t in starts if now - t < window_ms] + [now]
        with self.conn:
            self.state.set("start_times", ",".join(map(str, starts)))
        return len(starts)

    def record_crash(self, reason: str) -> None:
        with self.conn:
            self.state.set("crash_reason", reason[:300])
            self.event("CRITICAL", "demo", f"Scout crashed: {reason[:300]}")

    # ---- messages that depend on the market

    def on_mood(self, reading) -> None:
        """Called after each hourly mood check: message if the mood (or wild volatility) changed."""
        text = messages.mood_changed(self.state.get("last_regime"), self.state.get("last_volatility"), reading,
                                     self.settings.regime.slow_ma_days)
        with self.conn:
            self.state.set("last_regime", reading.regime.value)
            self.state.set("last_volatility", reading.volatility.value)
        self.notify(text, "mood")

    def maybe_weekly_report(self, now: int) -> None:
        """The weekly report, once a week on the configured day and time (Sydney)."""
        if not self.summaries:
            return
        cfg = self.settings.notify
        local = datetime.fromtimestamp(now / 1000, self.settings.app.tz)
        today = local.strftime("%Y-%m-%d")
        if (local.strftime("%A").lower() != cfg.weekly_report_day or local.time() < cfg.weekly_report_time
                or self.state.get("last_weekly_day") == today):
            return
        if now - self.state.get_float("opened_ms", now) < 24 * 3_600_000:
            return  # the account is less than a day old: nothing to report yet
        report = weekly_report(self.conn, now)
        if self.settings.mode.value != "replay":  # replays show the message on the dashboard, no files
            try:
                path = save_report(report, self.settings.app.reports_dir / "weekly", self.usd_per_aud,
                                   self.settings.app.tz)
                self.event("INFO", "demo", f"Weekly report saved to {path.name}.")
            except OSError as exc:
                log.warning("couldn't save the weekly report: %s", exc)
        with self.conn:
            self.state.set("last_weekly_day", today)
        self.notify(report_message(report, self.usd_per_aud, self.settings.app.tz), "report")

    def maybe_daily_summary(self, now: int) -> None:
        """Queue an update at each of notify.update_times (Sydney), e.g. 8am and 8pm."""
        if not self.summaries:
            return
        local = datetime.fromtimestamp(now / 1000, self.settings.app.tz)
        due = messages.due_update(now, self.settings.app.tz, self.settings.notify.update_times,
                                  self.state.get("last_update_slot"), int(self.state.get_float("opened_ms", 0)))
        if due is None:
            return
        slot, send = due
        if not send:  # too late (asleep) or the account opened after it: skip quietly
            with self.conn:
                self.state.set("last_update_slot", slot)
            return
        prices = self.prices.prices
        equity = self.account.equity(prices)
        day_start_ms = int(self.state.get_float("day_start_ms", 0))
        trades = self.conn.execute(
            "SELECT COUNT(*) FROM demo_orders WHERE status = 'filled' AND ts_ms >= ?", (day_start_ms,)
        ).fetchone()[0]
        positions = []
        for p in self.account.positions():
            price = prices.get(p.coin, p.entry_price)
            pnl = p.unrealised_usd(price) - p.fees_usd - p.funding_usd
            positions.append((p.coin, pnl, pnl / (p.qty * p.entry_price) * 100))
        btc = prices.get("BTC")
        btc_day, btc_start = self.state.get_float("btc_day_start_price", 0), self.state.get_float("btc_start_price", 0)
        mood = self.state.get("last_regime")
        text = messages.daily_summary(
            local.strftime("%a %d %b, %-I%p").replace("AM", "am").replace("PM", "pm"), equity, self.state.get_float("initial_equity_usd", equity),
            self.state.get_float("day_start_equity_usd", equity), trades, positions,
            f"{mood}, volatility {self.state.get('last_volatility', '?')}" if mood else None,
            (btc / btc_day - 1) * 100 if btc and btc_day else None,
            (btc / btc_start - 1) * 100 if btc and btc_start else None,
            self.usd_per_aud,
        )
        with self.conn:
            self.state.set("last_update_slot", slot)
        self.notify(text, "summary")

    def positions_for_signals(self) -> list[OpenPosition]:
        return [OpenPosition(p.coin, p.side, p.qty, p.entry_price, p.stop_price, p.opened_ts_ms, p.id)
                for p in self.account.positions()]

    def account_for_signals(self) -> Account:
        return Account(self.account.equity(self.prices.prices), self.account.free_cash(),
                       1 / self.settings.demo.aud_to_usdc_rate)


# ------------------------------------------------------------------ runner


@contextlib.contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    """Only one demo loop at a time (two would trade the same account twice)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("the demo is already running in another window") from None
        yield


def next_cycle_ms(now: int, cycle_minutes: int) -> int:
    """Just after the next cycle boundary (e.g. 1 minute past each hour, when 4h candles have closed)."""
    period = cycle_minutes * MINUTE_MS
    return (now // period + 1) * period + MINUTE_MS


Cycle = Callable[[DemoEngine], Awaitable[None]]


@dataclass(frozen=True)
class Job:
    """Extra work for the loop, e.g. the experiment's wallet checks: run every `every_seconds`."""

    name: str
    every_seconds: float
    run: Cycle
PriceStream = Callable[[], AsyncIterator[dict[str, float]]]


class DemoRunner:
    """The demo loop. `cycle` runs mood -> scan -> signals and executes them; `stream` yields live prices."""

    def __init__(self, engine: DemoEngine, cycle: Cycle, stream: PriceStream,
                 wall_clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 echo: Callable[[str], None] = print, jobs: Sequence[Job] = ()) -> None:
        self.jobs = list(jobs)
        self._job_due: dict[str, int] = {}
        self.engine = engine
        self.cycle = cycle
        self.stream = stream
        self.wall_clock = wall_clock
        self.sleep = sleep
        self.echo = echo
        self.settings = engine.settings
        self._last_heartbeat = 0
        self._awake_since: int | None = None  # when this run started or last woke from sleep

    async def _consume_prices(self) -> None:
        async for mids in self.stream():
            self.engine.prices.update(mids, self.engine.clock())

    async def _wait_for_fresh_prices(self, timeout: float = 60.0) -> bool:
        started = self.wall_clock()
        limit = self.settings.risk.stale_price_seconds
        while not self.engine.prices.fresh(self.engine.clock(), min(limit, 15)):
            if self.wall_clock() - started > timeout:
                return False
            await self.sleep(0.5)
        return True

    async def _cycle_unless_stopped(self, stop: asyncio.Event, work: Cycle | None = None) -> bool:
        """Run the hourly check (or `work`), but abandon it if a stop is requested. Returns False if stopped."""
        cycle = asyncio.create_task((work or self.cycle)(self.engine))
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({cycle, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if cycle not in done:
            cycle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cycle
            return False
        cycle.result()  # re-raise its error, if any
        return True

    async def _run_jobs(self, stop: asyncio.Event) -> bool:
        """Run any extra jobs that are due. Returns False if a stop was requested meanwhile."""
        engine = self.engine
        for job in self.jobs:
            now = engine.clock()
            if now < self._job_due.get(job.name, 0) or engine.bot_state is BotState.KILLED:
                continue
            if not engine.prices.fresh(now, self.settings.risk.stale_price_seconds):
                continue
            self._job_due[job.name] = now + int(job.every_seconds * 1000)
            try:
                if not await self._cycle_unless_stopped(stop, job.run):
                    return False
            except Exception as exc:  # keep running: the next round may work
                engine.event("ERROR", "demo", f"{job.name} failed ({exc}); trying again next round.")
        return True

    def _heartbeat(self) -> None:
        """Rewrite data/heartbeat.json about every 30 seconds so others can see the loop is alive."""
        now = self.engine.clock()
        if now - self._last_heartbeat < 30_000:
            return
        self._last_heartbeat = now
        try:
            write_heartbeat(heartbeat_path(self.settings), pid=os.getpid(), version=APP_VERSION,
                            state=self.engine.bot_state.value, ts_ms=now,
                            positions=len(self.engine.account.positions()))
        except OSError as exc:
            log.warning("couldn't write the heartbeat file: %s", exc)

    def _daily_backup(self) -> None:
        """Copy the database once a day (Sydney time); keep the last backup_keep_days copies."""
        engine = self.engine
        today = sydney_day(engine.clock(), self.settings)
        if engine.state.get("last_backup_day") == today:
            return
        try:
            path = backup_db(self.settings.app.db_path, self.settings.app.backup_dir, engine.clock(),
                             self.settings.app.tz, self.settings.app.backup_keep_days)
        except (OSError, sqlite3.Error) as exc:
            engine.event("ERROR", "demo", f"Daily database backup failed: {exc}")
            return
        with engine.conn:
            engine.state.set("last_backup_day", today)
            engine.event("INFO", "demo", f"Database backed up to {path.name}.")

    def _watch_feed(self, woke: bool) -> None:
        """Warn once if live prices stop arriving while Scout is running, and again when they're back.

        After a start or a wake-up the countdown starts from that moment, not from the last price
        (which may be from before the Mac slept): a sleeping Mac isn't a connection problem.
        """
        engine = self.engine
        now = engine.clock()
        if woke or self._awake_since is None:
            self._awake_since = now
        age = (now - engine.prices.last_update_ms) / 1000 if engine.prices.last_update_ms else 0
        age = min(age, (now - self._awake_since) / 1000)
        down = engine.state.get("feed_down_since")
        if age > self.settings.notify.feed_down_seconds and down is None:
            with engine.conn:
                engine.state.set("feed_down_since", now - int(age * 1000))
                engine.event("WARNING", "demo", f"No live prices for {age:.0f} seconds.")
            engine.notify(messages.feed_down(age), "risk")
        elif down is not None and age <= self.settings.risk.stale_price_seconds:
            with engine.conn:
                engine.state.set("feed_down_since", None)
                engine.event("INFO", "demo", "Live prices are back.")
            engine.notify(messages.feed_back((now - float(down)) / 60_000), "risk")

    async def _deliver(self) -> None:
        """Send waiting messages. A messaging problem must never stop the trading loop."""
        if self.engine.notifier is None:
            return
        try:
            await self.engine.notifier.deliver()
        except Exception as exc:  # noqa: BLE001
            log.error("sending messages failed: %s", exc)

    async def run(self, stop_after_seconds: float | None = None, stop: asyncio.Event | None = None) -> None:
        """Run until stop_after_seconds pass or `stop` is set (e.g. by SIGTERM from launchd)."""
        engine = self.engine
        stop = stop or asyncio.Event()
        prices = asyncio.create_task(self._consume_prices())
        started = self.wall_clock()
        try:
            self.echo("Waiting for live prices…")
            await self._wait_for_fresh_prices()
            next_cycle = 0  # run the pipeline straight away
            last_tick = self.wall_clock()
            while not stop.is_set() and (stop_after_seconds is None or self.wall_clock() - started < stop_after_seconds):
                now = self.wall_clock()
                woke = now - last_tick > self.settings.data.stale_after_seconds
                if woke:
                    minutes = (now - last_tick) / 60
                    engine.event("WARNING", "demo", f"Scout was not running for {minutes:.0f} minutes (Mac asleep?). "
                                                    "Waiting for fresh prices before doing anything.")
                    await self._wait_for_fresh_prices()
                    next_cycle = 0  # re-check the mood, shortlist and signals now
                last_tick = self.wall_clock()
                with engine.conn:
                    engine.state.set("last_tick_ms", engine.clock())

                await engine.check_stops(late=woke)
                await engine.housekeeping()
                self._watch_feed(woke)
                if engine.clock() >= next_cycle and engine.bot_state is not BotState.KILLED:
                    if engine.prices.fresh(engine.clock(), self.settings.risk.stale_price_seconds):
                        try:
                            if not await self._cycle_unless_stopped(stop):
                                break  # asked to stop: don't wait for a slow check to finish
                            next_cycle = next_cycle_ms(engine.clock(), self.settings.demo.cycle_minutes)
                        except Exception as exc:  # keep running: e.g. the internet dropped
                            engine.event("ERROR", "demo", f"The hourly check failed ({exc}). Trying again in "
                                                          "5 minutes.")
                            next_cycle = engine.clock() + 5 * MINUTE_MS
                    else:
                        engine.event("WARNING", "demo", "Prices are stale, so the hourly check is postponed.")
                        next_cycle = engine.clock() + MINUTE_MS
                if not await self._run_jobs(stop):
                    break
                await self._deliver()
                self._heartbeat()
                self._daily_backup()
                await self.sleep(self.settings.demo.tick_seconds)
        finally:
            prices.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prices
            with engine.conn:
                engine.state.set("loop_stopped_ms", engine.clock())
