"""Trade signals: when to buy, when to sell, and why.

A Strategy looks at one coin (and the market mood) and returns a Decision.
The engine (generate_signals) applies the rules every strategy shares:
entries only for shortlisted coins, a limit on open positions, and position
sizing from risk.py. It then writes the plain-English reason for each signal.

Strategies are pluggable: subclass Strategy and add it to STRATEGIES.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

import pandas as pd

from scout.config import Settings, SignalSettings
from scout.data import INTERVAL_MS, format_price
from scout.indicators import average_true_range, moving_average, rsi
from scout.regime import Regime, RegimeReading, Volatility
from scout.risk import size_position
from scout.version import APP_VERSION


class Action(StrEnum):
    ENTER_LONG = "ENTER_LONG"  # buy, betting the price rises
    ENTER_SHORT = "ENTER_SHORT"  # sell first, betting the price falls
    EXIT = "EXIT"  # close an open position
    MOVE_STOP = "MOVE_STOP"  # tighten the stop loss of an open position
    SKIP = "SKIP"  # a setup appeared but a rule said no
    HOLD = "HOLD"  # nothing to do (not saved)


ENTRIES = (Action.ENTER_LONG, Action.ENTER_SHORT)


@dataclass(frozen=True)
class MarketContext:
    regime: Regime
    volatility: Volatility
    size_multiplier: float

    @classmethod
    def from_reading(cls, reading: RegimeReading) -> MarketContext:
        return cls(reading.regime, reading.volatility, reading.size_multiplier)


@dataclass(frozen=True)
class CoinContext:
    coin: str
    candles: pd.DataFrame  # finished candles on the signal timeframe, oldest first
    daily: pd.DataFrame  # finished daily candles, oldest first
    price: float  # price now
    sz_decimals: int = 4


@dataclass(frozen=True)
class OpenPosition:
    coin: str
    side: str  # "long" or "short"
    qty: float
    entry_price: float
    stop_price: float
    opened_ts_ms: int
    id: int | None = None


@dataclass(frozen=True)
class Account:
    equity_usd: float
    cash_usd: float
    usd_to_aud: float  # for showing amounts in AUD


@dataclass(frozen=True)
class Decision:
    """What a strategy wants to do with one coin, and its explanation."""

    coin: str
    action: Action
    why: str
    price: float
    candle_ts_ms: int | None = None
    stop_price: float | None = None
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    coin: str
    action: Action
    ts_ms: int
    candle_ts_ms: int | None
    timeframe: str
    price: float
    stop_price: float | None
    regime: str
    reason: str
    qty: float | None = None
    notional_usd: float | None = None
    risk_usd: float | None = None
    details: dict = field(default_factory=dict)


# --------------------------------------------------------------- strategies


class Strategy(ABC):
    """Base class for trading strategies. Subclasses decide; the engine sizes and records."""

    name: ClassVar[str]

    def __init__(self, cfg: SignalSettings) -> None:
        self.cfg = cfg

    @abstractmethod
    def entry(self, coin: CoinContext, market: MarketContext) -> Decision:
        """Should we open a position in this coin now? (Return HOLD for no.)"""

    @abstractmethod
    def manage(self, position: OpenPosition, coin: CoinContext, market: MarketContext) -> Decision:
        """For an open position: EXIT, MOVE_STOP or HOLD."""


def _span(periods: int, timeframe: str) -> str:
    """20 × 4h -> '3.3-day'; 12 × 1h -> '12-hour'."""
    hours = INTERVAL_MS[timeframe] / 3_600_000 * periods
    return f"{hours:g}-hour" if hours < 48 else f"{hours / 24:.1f}".rstrip("0").rstrip(".") + "-day"


def _usd(value: float) -> str:
    return f"${format_price(value)}"


class BreakoutStrategy(Strategy):
    """Buy strength: an uptrending coin closes above its recent high on above-average volume.

    Stop: stop_atr_multiple × ATR below entry, then trailing trail_atr_multiple × ATR below the
    highest price since entry. Exit early if a candle closes below its exit_ma_periods average
    or the market mood turns RISK_OFF. Shorts (if enabled) mirror everything.
    """

    name = "breakout"

    def _needed(self) -> int:
        c = self.cfg
        return max(c.breakout_periods, c.volume_avg_periods, c.rsi_periods, c.atr_periods, c.exit_ma_periods) + 1

    def _indicators(self, coin: CoinContext) -> dict:
        c, candles = self.cfg, coin.candles
        prior = candles.iloc[-(c.breakout_periods + 1) : -1]
        daily = pd.concat([coin.daily["close"], pd.Series([coin.price])], ignore_index=True)
        normal_volume = candles["dollar_volume"].iloc[-(c.volume_avg_periods + 1) : -1].mean()
        return {
            "close": float(candles["close"].iloc[-1]),
            "recent_high": float(prior["high"].max()),
            "recent_low": float(prior["low"].min()),
            "volume_ratio": float(candles["dollar_volume"].iloc[-1] / normal_volume) if normal_volume else 0.0,
            "rsi": float(rsi(candles["close"], c.rsi_periods).iloc[-1]),
            "atr": float(average_true_range(candles, c.atr_periods).iloc[-1]),
            "exit_ma": float(moving_average(candles["close"], c.exit_ma_periods).iloc[-1]),
            "trend_fast": float(moving_average(daily, c.trend_fast_ma_days).iloc[-1]),
            "trend_slow": float(moving_average(daily, c.trend_slow_ma_days).iloc[-1]),
        }

    def entry(self, coin: CoinContext, market: MarketContext) -> Decision:
        c = self.cfg
        if len(coin.candles) < self._needed() or len(coin.daily) < c.trend_slow_ma_days - 1:
            return Decision(coin.coin, Action.HOLD, "not enough price history yet", coin.price)
        candle_ts = int(coin.candles.index[-1].timestamp() * 1000)
        span = _span(c.breakout_periods, c.timeframe)
        m: dict = {}

        def hold(why: str) -> Decision:
            return Decision(coin.coin, Action.HOLD, why, coin.price, candle_ts, metrics=m)

        def skip(why: str) -> Decision:
            return Decision(coin.coin, Action.SKIP, why, coin.price, candle_ts, metrics=m)

        if market.regime is Regime.RISK_OFF and not c.allow_shorts:
            return hold("market mood is RISK_OFF, so no new long trades")
        if market.regime is not Regime.RISK_OFF:
            # Cheap check first: most of the time there's no breakout, so skip the heavier indicators.
            close = float(coin.candles["close"].iat[-1])
            recent_high = float(coin.candles["high"].iloc[-(c.breakout_periods + 1) : -1].max())
            if close <= recent_high:
                m = {"close": close, "recent_high": recent_high}
                gap = (recent_high / close - 1) * 100
                return hold(f"no breakout yet: last {c.timeframe} close {_usd(close)} is {gap:.1f}% below "
                            f"its {span} high {_usd(recent_high)}")

        m = self._indicators(coin)
        if market.regime is Regime.RISK_OFF:
            return self._short_entry(coin, m, candle_ts, hold, skip)
        if not (coin.price > m["trend_fast"] and coin.price > m["trend_slow"]):
            return hold(
                f"not in an uptrend (price {_usd(coin.price)} vs its {c.trend_fast_ma_days}-day average "
                f"{_usd(m['trend_fast'])} and {c.trend_slow_ma_days}-day {_usd(m['trend_slow'])})"
            )
        broke = f"broke above its {span} high of {_usd(m['recent_high'])}"
        if m["volume_ratio"] < c.min_volume_ratio:
            return skip(f"{broke} but on only {m['volume_ratio']:.1f}x normal volume (need {c.min_volume_ratio:g}x): "
                        "weak breakouts often fail")
        if m["rsi"] > c.max_rsi:
            return skip(f"{broke} but RSI is {m['rsi']:.0f} (above {c.max_rsi:g}): too stretched, "
                        "likely to pull back first")
        if coin.price <= m["recent_high"]:
            return skip(f"{broke} at the {c.timeframe} close but has since fallen back to {_usd(coin.price)}")
        stop = coin.price - c.stop_atr_multiple * m["atr"]
        stop_pct = (coin.price - stop) / coin.price * 100
        if stop <= 0 or stop_pct > c.max_stop_pct:
            return skip(f"{broke} but a safe stop would be {stop_pct:.1f}% away (max {c.max_stop_pct:g}%)")
        return Decision(
            coin.coin, Action.ENTER_LONG,
            f"{coin.coin} {broke} (last {c.timeframe} close {_usd(m['close'])}) on {m['volume_ratio']:.1f}x normal "
            f"volume, RSI {m['rsi']:.0f}",
            coin.price, candle_ts, stop, m,
        )

    def _short_entry(self, coin: CoinContext, m: dict, candle_ts: int, hold, skip) -> Decision:
        c = self.cfg
        span = _span(c.breakout_periods, c.timeframe)
        if not (coin.price < m["trend_fast"] and coin.price < m["trend_slow"]):
            return hold("market mood is RISK_OFF but this coin isn't in a downtrend")
        if m["close"] >= m["recent_low"]:
            return hold(f"no breakdown: last {c.timeframe} close {_usd(m['close'])} is above its {span} low "
                        f"{_usd(m['recent_low'])}")
        broke = f"broke below its {span} low of {_usd(m['recent_low'])}"
        if m["volume_ratio"] < c.min_volume_ratio:
            return skip(f"{broke} but on only {m['volume_ratio']:.1f}x normal volume (need {c.min_volume_ratio:g}x)")
        if m["rsi"] < c.min_rsi_short:
            return skip(f"{broke} but RSI is {m['rsi']:.0f} (below {c.min_rsi_short:g}): already oversold")
        if coin.price >= m["recent_low"]:
            return skip(f"{broke} at the close but has since bounced to {_usd(coin.price)}")
        stop = coin.price + c.stop_atr_multiple * m["atr"]
        stop_pct = (stop - coin.price) / coin.price * 100
        if stop_pct > c.max_stop_pct:
            return skip(f"{broke} but a safe stop would be {stop_pct:.1f}% away (max {c.max_stop_pct:g}%)")
        return Decision(
            coin.coin, Action.ENTER_SHORT,
            f"{coin.coin} is in a downtrend and {broke} on {m['volume_ratio']:.1f}x normal volume, RSI {m['rsi']:.0f}",
            coin.price, candle_ts, stop, m,
        )

    def manage(self, position: OpenPosition, coin: CoinContext, market: MarketContext) -> Decision:
        c = self.cfg
        long = position.side == "long"
        price = coin.price
        if len(coin.candles) < self._needed():
            return Decision(coin.coin, Action.HOLD, "not enough price history to manage the position", price)
        candle_ts = int(coin.candles.index[-1].timestamp() * 1000)
        m = self._indicators(coin)

        def decide(action: Action, why: str, stop: float | None = None) -> Decision:
            return Decision(coin.coin, action, why, price, candle_ts, stop, m)

        # 1. Stop loss hit
        if (long and price <= position.stop_price) or (not long and price >= position.stop_price):
            return decide(Action.EXIT, f"price {_usd(price)} hit the stop loss at {_usd(position.stop_price)}")
        # 2. Market mood turned against the position
        if long and market.regime is Regime.RISK_OFF:
            return decide(Action.EXIT, "the market mood turned RISK_OFF")
        if not long and market.regime is Regime.RISK_ON:
            return decide(Action.EXIT, "the market mood turned RISK_ON")
        # 3. Trend broke
        if long and m["close"] < m["exit_ma"]:
            return decide(Action.EXIT, f"the trend broke: a {c.timeframe} candle closed at {_usd(m['close'])}, below "
                                       f"its {c.exit_ma_periods}-candle average {_usd(m['exit_ma'])}")
        if not long and m["close"] > m["exit_ma"]:
            return decide(Action.EXIT, f"the downtrend broke: a {c.timeframe} candle closed at {_usd(m['close'])}, "
                                       f"above its {c.exit_ma_periods}-candle average {_usd(m['exit_ma'])}")
        # 4. Trailing stop: follows the best price since entry, never moves backwards
        since = coin.candles[coin.candles.index >= pd.Timestamp(position.opened_ts_ms, unit="ms", tz="UTC")
                             - pd.Timedelta(milliseconds=INTERVAL_MS[c.timeframe])]
        distance = c.trail_atr_multiple * m["atr"]
        if long:
            best = max([price, *since["high"]])
            trail = best - distance
            if trail > position.stop_price:
                return decide(Action.MOVE_STOP, f"the price reached {_usd(best)}, and the trailing stop follows "
                                                f"{c.trail_atr_multiple:g}×ATR ({_usd(distance)}) below the highest price",
                              trail)
        else:
            best = min([price, *since["low"]])
            trail = best + distance
            if trail < position.stop_price:
                return decide(Action.MOVE_STOP, f"the price fell to {_usd(best)}, and the trailing stop follows "
                                                f"{c.trail_atr_multiple:g}×ATR ({_usd(distance)}) above the lowest price",
                              trail)
        gap = abs(price - position.stop_price) / price * 100
        return decide(Action.HOLD, f"holding: price {_usd(price)}, stop {_usd(position.stop_price)} ({gap:.1f}% away)")


STRATEGIES: dict[str, type[Strategy]] = {BreakoutStrategy.name: BreakoutStrategy}


def make_strategy(cfg: SignalSettings) -> Strategy:
    return STRATEGIES[cfg.strategy](cfg)


# ------------------------------------------------------------------- engine

MOOD_WORDS = {Regime.RISK_ON: "positive", Regime.NEUTRAL: "mixed", Regime.RISK_OFF: "negative"}


def generate_signals(
    strategy: Strategy,
    market: MarketContext,
    coins: Mapping[str, CoinContext],
    shortlist: Sequence[str],
    positions: Sequence[OpenPosition],
    account: Account,
    settings: Settings,
    ts_ms: int,
) -> list[Signal]:
    """Manage open positions first (exits free up room), then look for entries in shortlisted coins."""
    signals: list[Signal] = []
    tf = settings.signals.timeframe

    def make(decision: Decision, reason: str, **extra) -> Signal:
        return Signal(
            decision.coin, decision.action, ts_ms, decision.candle_ts_ms, tf, decision.price,
            decision.stop_price, market.regime.value, reason,
            details={"metrics": decision.metrics, "strategy": strategy.name, "why": decision.why}, **extra,
        )

    open_count = len(positions)
    for position in positions:
        coin = coins.get(position.coin)
        if coin is None:
            signals.append(Signal(position.coin, Action.HOLD, ts_ms, None, tf, 0.0, position.stop_price,
                                  market.regime.value, f"No price data for {position.coin}: can't manage it this round"))
            continue
        decision = strategy.manage(position, coin, market)
        signals.append(make(decision, _position_reason(decision, position)))
        if decision.action is Action.EXIT:
            open_count -= 1

    held = {p.coin for p in positions}
    for name in shortlist:
        if name in held:
            continue
        coin = coins.get(name)
        if coin is None:
            continue
        decision = strategy.entry(coin, market)
        if decision.action not in ENTRIES:
            prefix = "Not trading" if decision.action is Action.SKIP else "Watching"
            signals.append(make(decision, f"{prefix} {name}: {decision.why}."))
            continue
        verb = "buying" if decision.action is Action.ENTER_LONG else "shorting"
        if open_count >= settings.risk.max_open_positions:
            skipped = Decision(name, Action.SKIP, decision.why, decision.price, decision.candle_ts_ms,
                               decision.stop_price, decision.metrics)
            signals.append(make(skipped, f"Not {verb} {name}: already holding {open_count} positions "
                                         f"(the maximum is {settings.risk.max_open_positions}). Setup: {decision.why}."))
            continue
        size = size_position(account.equity_usd, account.cash_usd, decision.price, decision.stop_price,
                             settings.risk, market.size_multiplier, coin.sz_decimals)
        if not size.ok:
            skipped = Decision(name, Action.SKIP, decision.why, decision.price, decision.candle_ts_ms,
                               decision.stop_price, decision.metrics)
            signals.append(make(skipped, f"Not {verb} {name}: {size.problem}. Setup: {decision.why}."))
            continue
        signals.append(make(decision, _entry_reason(decision, size, market, account),
                            qty=size.qty, notional_usd=size.notional_usd, risk_usd=size.risk_usd))
        open_count += 1
    return signals


def _entry_reason(decision: Decision, size, market: MarketContext, account: Account) -> str:
    long = decision.action is Action.ENTER_LONG
    distance = abs(decision.price - decision.stop_price) / decision.price * 100
    aud = account.usd_to_aud
    text = (
        f"{'Buying' if long else 'Shorting'} {size.qty:g} {decision.coin} (≈US${size.notional_usd:,.2f}) at "
        f"{_usd(decision.price)}: market mood is {MOOD_WORDS[market.regime]} ({market.regime}), {decision.why}. "
        f"Stop at {_usd(decision.stop_price)} ({distance:.1f}% {'below' if long else 'above'}). "
        f"Risking A${size.risk_usd * aud:,.2f} (US${size.risk_usd:,.2f}, including fees) of the "
        f"A${account.equity_usd * aud:,.0f} account"
    )
    notes = []
    if market.size_multiplier < 1:
        notes.append(f"{market.size_multiplier:.0%} of the usual size because volatility is wild")
    if size.capped_by:
        notes.append(f"size capped at {size.capped_by}")
    return text + (f" ({'; '.join(notes)})." if notes else ".")


def _position_reason(decision: Decision, position: OpenPosition) -> str:
    long = position.side == "long"
    coin = decision.coin
    if decision.action is Action.EXIT:
        change = (decision.price / position.entry_price - 1) * 100 * (1 if long else -1)
        verb = f"Selling {coin}" if long else f"Closing the {coin} short"
        return (f"{verb}: {decision.why}. Entered at {_usd(position.entry_price)}, now {_usd(decision.price)} "
                f"({change:+.1f}%).")
    if decision.action is Action.MOVE_STOP:
        direction = "Raising" if long else "Lowering"
        protects = (decision.stop_price > position.entry_price) if long else (decision.stop_price < position.entry_price)
        effect = "locking in part of the gain" if protects else "shrinking the possible loss"
        return (f"{direction} {coin}'s stop to {_usd(decision.stop_price)} (was {_usd(position.stop_price)}), "
                f"{effect}: {decision.why}.")
    return f"{coin}: {decision.why}."


# ------------------------------------------------------------------ storage


def save_signals(conn: sqlite3.Connection, signals: Sequence[Signal]) -> int:
    """Store actionable and skipped signals (not HOLDs). The same coin/action/candle is stored once.
    Returns how many were new. The caller commits."""
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO signals
               (ts_ms, coin, timeframe, action, entry_price, stop_price, regime, reason,
                candle_ts_ms, qty, risk_usd, details_json, app_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (s.ts_ms, s.coin, s.timeframe, s.action.value, s.price, s.stop_price, s.regime, s.reason,
             s.candle_ts_ms, s.qty, s.risk_usd, json.dumps(s.details, default=float), APP_VERSION)
            for s in signals
            if s.action is not Action.HOLD
        ],
    )
    return conn.total_changes - before


def load_open_positions(conn: sqlite3.Connection) -> list[OpenPosition]:
    rows = conn.execute(
        """SELECT id, coin, side, qty, entry_price, stop_price, opened_ts_ms
           FROM demo_positions WHERE status = 'open' ORDER BY opened_ts_ms"""
    ).fetchall()
    return [
        OpenPosition(r["coin"], r["side"], r["qty"], r["entry_price"], r["stop_price"], r["opened_ts_ms"], r["id"])
        for r in rows
    ]
