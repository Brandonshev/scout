"""Replay: the live demo engine run over past data at high speed, for watching and showing.

The same DemoEngine as `scout demo` trades a fresh fake account in its own database
(data/replay.db), so the real demo account is never touched. The clock is simulated:
- Prices move within each 4h candle along a simple path (open -> dip/rally -> close),
  so the dashboard shows positions moving and stops can be hit mid-candle.
- At each 4h close the pipeline runs: the day's mood and shortlist (worked out from
  daily candles closed before that day), then signals, then the risk manager and fills.
- Messages are recorded but never sent (they show on the dashboard).

Speed, pause and single-step come from a small JSON control file, written by the dashboard
or by hand. The control file can only change the pace of the replay, never trades.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from scout.backtest import DAILY_WINDOW, DayPlan, History
from scout.data import DAY_MS, INTERVAL_MS, format_price
from scout.demo import DemoEngine
from scout.regime import Regime, Volatility, save_reading
from scout.scanner import save_scan
from scout.signals import (
    CoinContext,
    MarketContext,
    generate_signals,
    make_strategy,
    save_signals,
)

log = logging.getLogger(__name__)
BENCHMARK = "BTC"
REAL_TICK_SECONDS = 0.5  # how often the replay updates prices, in real time
MIN_SUB_MS = 5 * 60_000  # never move prices in steps finer than 5 simulated minutes


def parse_speed(text: str) -> float | None:
    """'500x' or '500' -> 500.0; 'max' -> None (as fast as possible)."""
    text = text.strip().lower()
    if text in ("max", "fastest"):
        return None
    value = float(text.rstrip("x"))
    if value <= 0:
        raise ValueError("speed must be positive")
    return value


# ------------------------------------------------------------ control file


@dataclass
class Control:
    paused: bool = False
    speed: float | None = 500.0  # None = max
    step: int = 0  # bump this while paused to advance one candle


class ReplayControl:
    """A tiny JSON file the dashboard writes and the replay reads. It controls pace only."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> Control:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return Control()
        speed = data.get("speed")
        return Control(bool(data.get("paused", False)), None if speed in (None, "max") else float(speed),
                       int(data.get("step", 0)))

    def write(self, control: Control) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"paused": control.paused, "speed": control.speed or "max",
                                   "step": control.step}))
        tmp.replace(self.path)  # atomic: the reader never sees half a file


class SimClock:
    """The replay's clock: simulated milliseconds, moved forward by the runner."""

    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


# ------------------------------------------------------------ price path


def candle_path(row: pd.Series, open_ms: int, length_ms: int) -> list[tuple[int, float]]:
    """A simple path through a candle: open, the dip (or rally) first, then the other extreme, then close."""
    up = row["close"] >= row["open"]
    first, second = (row["low"], row["high"]) if up else (row["high"], row["low"])
    return [(open_ms, row["open"]), (open_ms + length_ms // 3, first),
            (open_ms + 2 * length_ms // 3, second), (open_ms + length_ms, row["close"])]


def price_at(path: list[tuple[int, float]], ts: int) -> float:
    if ts <= path[0][0]:
        return path[0][1]
    for (t0, p0), (t1, p1) in zip(path, path[1:]):
        if ts <= t1:
            return p0 + (p1 - p0) * (ts - t0) / (t1 - t0)
    return path[-1][1]


def range_between(path: list[tuple[int, float]], a: int, b: int) -> tuple[float, float]:
    """Lowest and highest price on the path between times a and b."""
    points = [price_at(path, a), price_at(path, b)] + [p for t, p in path if a < t < b]
    return min(points), max(points)


# ------------------------------------------------------------ the runner


Sleep = Callable[[float], Awaitable[None]]


class ReplayRunner:
    def __init__(self, engine: DemoEngine, sim: SimClock, data: History, plans: Mapping[pd.Timestamp, DayPlan],
                 start: pd.Timestamp, end: pd.Timestamp, control: ReplayControl,
                 sleep: Sleep = asyncio.sleep, wall_clock: Callable[[], float] = time.monotonic,
                 echo: Callable[[str], None] = print) -> None:
        self.engine = engine
        self.sim = sim  # the engine must have been created with clock=sim
        self.settings = engine.settings
        self.data = data
        self.plans = plans
        self.start, self.end = start, end
        self.control = control
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.echo = echo
        self.strategy = make_strategy(self.settings.signals)
        self.step_ms = INTERVAL_MS[self.settings.signals.timeframe]
        self.window = int(self.settings.signals.candles_needed_days * DAY_MS / self.step_ms)
        self.index = {coin: frame.index for coin, frame in data.candles.items()}
        self.daily_index = {coin: frame.index for coin, frame in data.daily.items()}
        self.last_close: dict[str, float] = {}
        self.day_saved: pd.Timestamp | None = None
        self._last_step = control.read().step
        self._step_until: int | None = None
        self._seen_message = 0

    def steps(self) -> pd.DatetimeIndex:
        btc = self.data.candles[BENCHMARK].index
        return btc[(btc >= self.start) & (btc < self.end)]

    async def run(self) -> None:
        steps = self.steps()
        state = self.engine.state
        with self.engine.conn:
            state.set("replay_from_ms", int(self.start.timestamp() * 1000))
            state.set("replay_to_ms", int(self.end.timestamp() * 1000))
            state.set("replay_status", "running")
        for n, t in enumerate(steps, start=1):
            await self._candle(t)
            if n % 30 == 0 or n == len(steps):
                equity = self.engine.account.equity(self.engine.prices.prices)
                self.echo(f"  {pd.Timestamp(self.sim.ms, unit='ms', tz='UTC').tz_convert(self.settings.app.tz):%d %b %Y %H:%M} "
                          f"· A${equity / self.settings.demo.aud_to_usdc_rate:,.2f} · "
                          f"{len(self.engine.account.positions())} open · {n}/{len(steps)} candles")
        with self.engine.conn:
            state.set("replay_status", "finished")

    # ---- one 4h candle

    def _coins_now(self, plan: DayPlan | None) -> set[str]:
        return {BENCHMARK, *(p.coin for p in self.engine.account.positions()), *(plan.shortlist if plan else ())}

    def _row(self, coin: str, t: pd.Timestamp) -> int | None:
        index = self.index.get(coin)
        if index is None:
            return None
        pos = index.searchsorted(t)
        return pos if pos < len(index) and index[pos] == t else None

    async def _candle(self, t: pd.Timestamp) -> None:
        engine = self.engine
        open_ms = int(t.timestamp() * 1000)
        close_ms = open_ms + self.step_ms
        day = pd.Timestamp(close_ms, unit="ms", tz="UTC").floor("D")
        plan = self.plans.get(day)
        rows = {coin: pos for coin in self._coins_now(plan) if (pos := self._row(coin, t)) is not None}
        paths = {coin: candle_path(self.data.candles[coin].iloc[pos], open_ms, self.step_ms) for coin, pos in rows.items()}

        # Coins that stopped trading (delisted) are sold at their last price
        for p in engine.account.positions():
            index = self.index.get(p.coin)
            if p.coin not in rows and index is not None and index[-1] < t:
                self.sim.ms = open_ms
                await engine.close(p, f"Selling {p.coin}: the coin stopped trading (delisted).",
                                   self.last_close.get(p.coin, p.entry_price))

        # Move through the candle in small steps, like a live price feed
        control = self.control.read()
        sub = self._sub_ms(control)
        previous = open_ms
        ts = open_ms
        while ts < close_ms:
            ts = min(close_ms, ts + sub)
            await self._pace(sub, close_ms)
            self.sim.ms = ts
            engine.prices.update({coin: price_at(path, ts) for coin, path in paths.items()}, ts)
            await self._stops(paths, previous, ts, open_ms)
            await engine.housekeeping()  # snapshots, daily limit, kill switch, daily summary (messages are only recorded)
            previous = ts

        # At the close: the day's mood and shortlist, then signals
        closes = {coin: float(self.data.candles[coin]["close"].iat[pos]) for coin, pos in rows.items()}
        self.last_close.update(closes)
        if plan is not None and plan.day != self.day_saved:
            self._save_day(plan, close_ms)
        market = plan.market if plan and plan.market else MarketContext(Regime.NEUTRAL, Volatility.NORMAL, 1.0)
        shortlist = [c for c in (plan.shortlist if plan and plan.market else ()) if c in rows]
        contexts = {}
        for coin, pos in rows.items():
            day_end = self.daily_index[coin].searchsorted(day) if coin in self.daily_index else 0
            contexts[coin] = CoinContext(
                coin, self.data.candles[coin].iloc[max(0, pos + 1 - self.window): pos + 1],
                self.data.daily[coin].iloc[max(0, day_end - DAILY_WINDOW): day_end] if coin in self.data.daily
                else pd.DataFrame(), closes[coin],
            )
        signals = generate_signals(self.strategy, market, contexts, shortlist, engine.positions_for_signals(),
                                   engine.account_for_signals(), self.settings, close_ms)
        with engine.conn:
            save_signals(engine.conn, signals)
        await engine.execute_signals(signals)
        with engine.conn:
            engine.state.set("sim_now_ms", close_ms)
        self._echo_messages()
        if self._step_until == close_ms:
            self._step_until = None

    def _echo_messages(self) -> None:
        """Print what Scout would have texted (trades, mood changes, risk events) as it happens."""
        rows = self.engine.conn.execute(
            "SELECT id, created_ms, text FROM notifications WHERE id > ? AND category != 'summary' "
            "AND priority > 0 ORDER BY id",  # skip the minor, batched ones (stop moves)
            (self._seen_message,),
        ).fetchall()
        for row in rows:
            self._seen_message = row["id"]
            stamp = pd.Timestamp(row["created_ms"], unit="ms", tz="UTC").tz_convert(self.settings.app.tz)
            first, *rest = row["text"].splitlines()
            self.echo(f"[{stamp:%d %b %H:%M}] {first}" + "".join(f"\n    {line}" for line in rest[:1]))

    def _save_day(self, plan: DayPlan, ts: int) -> None:
        """What the live bot would have recorded that day: the mood reading and the scan."""
        engine = self.engine
        with engine.conn:
            if plan.reading is not None:
                save_reading(engine.conn, plan.reading)
            if plan.scans:
                save_scan(engine.conn, list(plan.scans), int(plan.day.timestamp() * 1000))
        if plan.reading is not None:
            engine.on_mood(plan.reading)
        self.day_saved = plan.day

    async def _stops(self, paths: Mapping[str, list], a: int, b: int, candle_open: int) -> None:
        """Close positions whose stop was crossed between a and b, at the stop (or at the open if it gapped)."""
        for p in self.engine.account.positions():
            path = paths.get(p.coin)
            if path is None:
                continue
            low, high = range_between(path, a, b)
            hit = low <= p.stop_price if p.long else high >= p.stop_price
            if not hit:
                continue
            fill = p.stop_price
            opening = path[0][1]
            if a == candle_open and ((p.long and opening < p.stop_price) or (not p.long and opening > p.stop_price)):
                fill = opening  # the price gapped past the stop
            gap = " (the price gapped past it)" if fill != p.stop_price else ""
            await self.engine.close(p, f"{'Selling' if p.long else 'Closing the short in'} {p.coin}: the stop loss at "
                                       f"${format_price(p.stop_price)} was hit{gap}.", fill)

    # ---- pace, pause and step

    def _sub_ms(self, control: Control) -> int:
        if control.speed is None:
            return self.step_ms
        return int(min(self.step_ms, max(MIN_SUB_MS, control.speed * REAL_TICK_SECONDS * 1000)))

    async def _pace(self, sub_ms: int, close_ms: int) -> None:
        """Wait the right amount of real time; while paused, wait for resume or a step request."""
        control = self.control.read()
        stepping = self._step_until is not None
        if control.paused and not stepping:
            with self.engine.conn:
                self.engine.state.set("replay_paused", "1")  # so the dashboard shows it straight away
        while control.paused and not stepping:
            if control.step != self._last_step:  # step: play the rest of this candle, then pause again
                self._last_step = control.step
                self._step_until = close_ms
                stepping = True
                break
            await self.sleep(0.2)
            control = self.control.read()
        with self.engine.conn:
            self.engine.state.set("replay_speed", control.speed or "max")
            self.engine.state.set("replay_paused", "1" if control.paused else "0")
        if control.speed is None or stepping:
            await self.sleep(0)
        else:
            await self.sleep(sub_ms / 1000 / control.speed)
