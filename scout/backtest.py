"""Backtester: replays the whole pipeline over past candles, honestly.

How a run works, one 4h candle at a time:
1. Stops first: if the candle's low touched a position's stop, it's sold at the stop
   (or at the candle's open, if the price gapped straight past it), minus slippage.
2. Funding is charged for the 4 hours each position was held.
3. At the candle's close, the same code the live bot uses (mood -> shortlist -> signals ->
   position sizing) decides what to do, seeing only candles that had closed by then.
4. The account is valued at closing prices.

The mood and shortlist are worked out once per day from finished daily candles (live: hourly).
The coin pool includes delisted coins, ranked by their volume on each day, so the test
isn't limited to today's survivors. Fees and slippage come from the risk settings.

Known limits: no past order books or open interest (those scanner filters are skipped),
no funding history (a flat rate is assumed), and no crowding in the past mood.
"""

from __future__ import annotations

import csv
import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from scout.config import Settings, SignalSettings
from scout.data import DAY_MS, INTERVAL_MS, MarketCoin, format_price
from scout.regime import Regime, RegimeReading, Volatility, history
from scout.scanner import CoinScan, evaluate_coin, on_shortlist, rank_scans
from scout.signals import (
    Account,
    Action,
    CoinContext,
    MarketContext,
    OpenPosition,
    Strategy,
    generate_signals,
    make_strategy,
)
from scout.version import APP_VERSION

Progress = Callable[[str], None]
DAILY_WINDOW = 70  # daily candles handed to the strategy (enough for a 50-day average)
BENCHMARK = "BTC"


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.timestamp() * 1000)


@dataclass
class History:
    """Past candles. daily: every coin in the pool; candles: signal-timeframe candles for tradeable coins."""

    daily: dict[str, pd.DataFrame]
    candles: dict[str, pd.DataFrame] = field(default_factory=dict)

    def first_signal_candle(self) -> pd.Timestamp:
        return self.candles[BENCHMARK].index[0]


# ------------------------------------------------------------ daily plans


@dataclass(frozen=True)
class DayPlan:
    """What the live bot would have known at 00:00 UTC on `day`: the mood and the shortlist."""

    day: pd.Timestamp
    market: MarketContext | None  # None = not enough history to judge the mood
    shortlist: tuple[str, ...]
    mood_summary: str = ""
    reading: RegimeReading | None = field(default=None, compare=False)
    scans: tuple[CoinScan, ...] = field(default=(), compare=False)


def _closes(daily: Mapping[str, pd.DataFrame], column: str) -> pd.DataFrame:
    return pd.DataFrame({coin: frame[column] for coin, frame in daily.items() if not frame.empty})


def build_plans(
    data: History, settings: Settings, start: pd.Timestamp, end: pd.Timestamp, progress: Progress | None = None
) -> dict[pd.Timestamp, DayPlan]:
    """One plan per UTC day in [start, end], each built only from daily candles closed before that day."""
    first_day, last_day = start.floor("D"), end.floor("D")
    btc = data.daily[BENCHMARK]
    closes, volumes = _closes(data.daily, "close"), _closes(data.daily, "dollar_volume")

    # Mood: regime.history gives one reading per finished day; the reading for day D-1 is used on day D.
    btc_until = btc[btc.index < last_day]
    days = int((last_day - first_day) / pd.Timedelta(days=1)) + 1
    readings = {pd.Timestamp(r.ts_ms, unit="ms", tz="UTC"): r
                for r in history(btc_until, closes, volumes, settings.regime, days)}

    scan_cfg = settings.scanner.model_copy(update={"min_open_interest_usd": 0.0})  # no past open interest
    positions = {coin: frame.index for coin, frame in data.daily.items()}
    plans: dict[pd.Timestamp, DayPlan] = {}
    for n, day in enumerate(pd.date_range(first_day, last_day, freq="D")):
        if progress and n % 60 == 0:
            progress(f"  planning {day:%Y-%m-%d}")
        reading = readings.get(day)
        market = MarketContext.from_reading(reading) if reading else None
        scans = _scan_on(day, data, positions, scan_cfg)
        chosen = tuple(s.coin for s in scans if on_shortlist(s, scan_cfg))
        plans[day] = DayPlan(day, market, chosen, reading.summary if reading else "", reading, tuple(scans))
    return plans


def _shortlist_on(day: pd.Timestamp, data: History, positions: Mapping, cfg) -> tuple[str, ...]:
    """The shortlist as the scanner would have made it at the start of `day`."""
    return tuple(s.coin for s in _scan_on(day, data, positions, cfg) if on_shortlist(s, cfg))


def _scan_on(day: pd.Timestamp, data: History, positions: Mapping, cfg) -> list[CoinScan]:
    """The scanner as it would have run at the start of `day` (volume ranking of that time)."""
    yesterday = day - pd.Timedelta(days=1)
    btc = data.daily[BENCHMARK]
    btc_before = btc.iloc[: positions[BENCHMARK].searchsorted(day)]
    if len(btc_before) == 0:
        return []
    listed = []
    for coin, frame in data.daily.items():
        end = positions[coin].searchsorted(day)
        if end == 0 or frame.index[end - 1] != yesterday:
            continue  # not trading yesterday (not listed yet, or delisted)
        listed.append((float(frame["dollar_volume"].iat[end - 1]), coin, end))
    listed.sort(reverse=True)
    scans = []
    for volume, coin, end in listed[: cfg.universe_size]:
        before = data.daily[coin].iloc[:end]
        price = float(before["close"].iat[-1])
        market_coin = MarketCoin(coin, False, 1, price, price, price, volume, 0.0, 0.0)
        scans.append(evaluate_coin(market_coin, before, btc_before, float(btc_before["close"].iat[-1]),
                                   None, cfg, _ms(day), check_book=False))
    return rank_scans(scans)


# -------------------------------------------------------------- portfolio


@dataclass
class Holding:
    coin: str
    side: str
    qty: float
    entry_price: float  # actual fill, after slippage
    entry_ts_ms: int
    stop_price: float
    initial_stop: float
    entry_reason: str
    regime: str
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    bars: int = 0

    @property
    def long(self) -> bool:
        return self.side == "long"


@dataclass(frozen=True)
class Trade:
    coin: str
    side: str
    entry_ts_ms: int
    entry_price: float
    qty: float
    initial_stop: float
    exit_ts_ms: int
    exit_price: float
    fees_usd: float
    funding_usd: float
    pnl_usd: float  # after fees and funding
    bars: int
    regime: str
    entry_reason: str
    exit_reason: str

    @property
    def notional_usd(self) -> float:
        return self.qty * self.entry_price

    @property
    def pnl_pct(self) -> float:
        return self.pnl_usd / self.notional_usd * 100 if self.notional_usd else 0.0


class Portfolio:
    """A fake account: cash plus open positions, paying fees, slippage and funding."""

    def __init__(self, cash: float, taker_fee_pct: float, slippage_pct: float, funding_hourly_pct: float) -> None:
        self.cash = cash
        self.fee = taker_fee_pct / 100
        self.slip = slippage_pct / 100
        self.funding = funding_hourly_pct / 100
        self.holdings: dict[str, Holding] = {}
        self.trades: list[Trade] = []

    def equity(self, prices: Mapping[str, float]) -> float:
        value = self.cash
        for h in self.holdings.values():
            price = prices.get(h.coin, h.entry_price)
            value += h.qty * price if h.long else h.qty * (h.entry_price - price)
        return value

    def free_cash(self) -> float:
        """Cash not tied up as collateral for shorts (longs already spent theirs)."""
        return self.cash - sum(h.qty * h.entry_price for h in self.holdings.values() if not h.long)

    def open(self, coin: str, side: str, qty: float, price: float, stop: float, ts_ms: int,
             reason: str, regime: str) -> Holding | None:
        fill = price * (1 + self.slip) if side == "long" else price * (1 - self.slip)
        affordable = self.free_cash() / (fill * (1 + self.fee))
        qty = min(qty, affordable)
        if qty <= 0:
            return None
        fee = qty * fill * self.fee
        self.cash -= fee + (qty * fill if side == "long" else 0.0)
        holding = Holding(coin, side, qty, fill, ts_ms, stop, stop, reason, regime, fees_usd=fee)
        self.holdings[coin] = holding
        return holding

    def close(self, coin: str, price: float, ts_ms: int, reason: str) -> Trade:
        h = self.holdings.pop(coin)
        fill = price * (1 - self.slip) if h.long else price * (1 + self.slip)
        fee = h.qty * fill * self.fee
        if h.long:
            self.cash += h.qty * fill - fee
            gross = h.qty * (fill - h.entry_price)
        else:
            gross = h.qty * (h.entry_price - fill)
            self.cash += gross - fee
        fees = h.fees_usd + fee
        trade = Trade(h.coin, h.side, h.entry_ts_ms, h.entry_price, h.qty, h.initial_stop, ts_ms, fill,
                      fees, h.funding_usd, gross - fees - h.funding_usd, h.bars, h.regime, h.entry_reason, reason)
        self.trades.append(trade)
        return trade

    def charge_funding(self, prices: Mapping[str, float], hours: float) -> None:
        """Longs pay the assumed funding rate; shorts receive it."""
        for h in self.holdings.values():
            amount = h.qty * prices.get(h.coin, h.entry_price) * self.funding * hours
            amount = amount if h.long else -amount
            self.cash -= amount
            h.funding_usd += amount


# ------------------------------------------------------------------ stats


@dataclass(frozen=True)
class Stats:
    start_equity: float
    end_equity: float
    total_return_pct: float
    annual_return_pct: float
    max_drawdown_pct: float
    max_drawdown_start: pd.Timestamp | None
    max_drawdown_end: pd.Timestamp | None
    trades: int = 0
    win_rate_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    profit_factor: float | None = None
    time_in_market_pct: float = 100.0
    fees_usd: float = 0.0
    funding_usd: float = 0.0


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp | None, pd.Timestamp | None]:
    """Biggest fall from a peak, in %, with when the peak and the bottom happened."""
    if equity.empty:
        return 0.0, None, None
    peaks = equity.cummax()
    drawdowns = (peaks - equity) / peaks * 100
    bottom = drawdowns.idxmax()
    peak = equity.loc[:bottom].idxmax()
    return float(drawdowns.max()), peak, bottom


def compute_stats(equity: pd.Series, trades: Sequence[Trade] = (), in_market: pd.Series | None = None) -> Stats:
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    years = max((equity.index[-1] - equity.index[0]) / pd.Timedelta(days=365), 1 / 365)
    total = (end / start - 1) * 100
    annual = ((end / start) ** (1 / years) - 1) * 100 if end > 0 else -100.0
    dd, dd_start, dd_end = max_drawdown(equity)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    gross_loss = -sum(t.pnl_usd for t in losses)
    return Stats(
        start, end, total, annual, dd, dd_start, dd_end,
        trades=len(trades),
        win_rate_pct=100 * len(wins) / len(trades) if trades else 0.0,
        avg_win_pct=float(np.mean([t.pnl_pct for t in wins])) if wins else 0.0,
        avg_loss_pct=float(np.mean([t.pnl_pct for t in losses])) if losses else 0.0,
        avg_win_usd=float(np.mean([t.pnl_usd for t in wins])) if wins else 0.0,
        avg_loss_usd=float(np.mean([t.pnl_usd for t in losses])) if losses else 0.0,
        profit_factor=(sum(t.pnl_usd for t in wins) / gross_loss) if gross_loss > 0 else None,
        time_in_market_pct=float(in_market.mean() * 100) if in_market is not None and len(in_market) else 100.0,
        fees_usd=sum(t.fees_usd for t in trades),
        funding_usd=sum(t.funding_usd for t in trades),
    )


# --------------------------------------------------------------- simulate


def stop_fill(long: bool, stop: float, open_: float, high: float, low: float) -> float | None:
    """The price a stop order fills at during a candle, or None if it wasn't touched.
    If the candle opened beyond the stop (a gap), the fill is the worse opening price."""
    if long and low <= stop:
        return min(stop, open_)
    if not long and high >= stop:
        return max(stop, open_)
    return None


@dataclass(frozen=True)
class BacktestResult:
    start: pd.Timestamp
    end: pd.Timestamp
    equity: pd.DataFrame  # index: time; columns: strategy, btc_hold, in_market
    trades: list[Trade]
    stats: Stats
    btc_stats: Stats
    params: dict = field(default_factory=dict)


def simulate(
    data: History,
    plans: Mapping[pd.Timestamp, DayPlan],
    settings: Settings,
    start: pd.Timestamp,
    end: pd.Timestamp,
    start_equity: float | None = None,
    strategy: Strategy | None = None,
    close_at_end: bool = True,
) -> BacktestResult:
    """Trade candle by candle from `start` to `end` (candle open times)."""
    cfg = settings.signals
    strategy = strategy or make_strategy(cfg)
    step = pd.Timedelta(milliseconds=INTERVAL_MS[cfg.timeframe])
    hours = step / pd.Timedelta(hours=1)
    usd_to_aud = 1 / settings.demo.aud_to_usdc_rate
    start_equity = settings.demo.starting_balance_usdc if start_equity is None else start_equity
    book = Portfolio(start_equity, settings.risk.taker_fee_pct, settings.risk.slippage_pct,
                     settings.backtest.funding_rate_hourly_pct)
    window = int(cfg.candles_needed_days * DAY_MS / INTERVAL_MS[cfg.timeframe])

    btc = data.candles[BENCHMARK]
    steps = btc.index[(btc.index >= start) & (btc.index < end)]
    index = {coin: frame.index for coin, frame in data.candles.items()}
    daily_index = {coin: frame.index for coin, frame in data.daily.items()}
    last_close: dict[str, float] = {}
    rows: list[tuple] = []
    btc_qty = btc_fill = None

    for t in steps:
        close_time = t + step
        close_ms = _ms(close_time)
        candle: dict[str, int] = {}  # coin -> row position of the candle opening at t
        for coin in {*book.holdings, *plans.get(close_time.floor("D"), DayPlan(t, None, ())).shortlist}:
            if coin in index:
                pos = index[coin].searchsorted(t)
                if pos < len(index[coin]) and index[coin][pos] == t:
                    candle[coin] = pos

        # 1. Stops touched during this candle; delisted coins are closed at their last price
        for coin, h in list(book.holdings.items()):
            if coin not in candle:
                if index.get(coin) is not None and index[coin][-1] < t:
                    book.close(coin, last_close.get(coin, h.entry_price), close_ms,
                               f"Selling {coin}: the coin stopped trading (delisted).")
                continue
            row = data.candles[coin].iloc[candle[coin]]
            h.bars += 1
            fill = stop_fill(h.long, h.stop_price, row["open"], row["high"], row["low"])
            if fill is not None:
                gap = " (the price gapped past it)" if fill != h.stop_price else ""
                book.close(coin, fill, close_ms, f"{'Selling' if h.long else 'Closing short in'} {coin}: the stop "
                                                 f"loss at ${format_price(h.stop_price)} was hit{gap}.")

        prices = {coin: float(data.candles[coin]["close"].iat[pos]) for coin, pos in candle.items()}
        last_close.update(prices)

        # 2. Funding for the hours just held
        book.charge_funding(prices, hours)

        # 3. Decisions at the close, using only candles closed by now
        plan = plans.get(close_time.floor("D"))
        market = plan.market if plan and plan.market else MarketContext(Regime.NEUTRAL, Volatility.NORMAL, 1.0)
        shortlist = plan.shortlist if plan and plan.market else ()
        contexts = {}
        for coin, pos in candle.items():
            day_end = daily_index[coin].searchsorted(close_time.floor("D")) if coin in daily_index else 0
            contexts[coin] = CoinContext(
                coin,
                data.candles[coin].iloc[max(0, pos + 1 - window) : pos + 1],
                data.daily[coin].iloc[max(0, day_end - DAILY_WINDOW) : day_end] if coin in data.daily else pd.DataFrame(),
                prices[coin],
            )
        positions = [OpenPosition(h.coin, h.side, h.qty, h.entry_price, h.stop_price, h.entry_ts_ms)
                     for h in book.holdings.values()]
        account = Account(book.equity(last_close), book.free_cash(), usd_to_aud)
        signals = generate_signals(strategy, market, contexts, [c for c in shortlist if c in contexts],
                                   positions, account, settings, close_ms)
        for s in signals:
            if s.action is Action.EXIT and s.coin in book.holdings:
                book.close(s.coin, s.price, close_ms, s.reason)
            elif s.action is Action.MOVE_STOP and s.coin in book.holdings:
                book.holdings[s.coin].stop_price = s.stop_price
        for s in signals:
            if s.action in (Action.ENTER_LONG, Action.ENTER_SHORT) and s.coin not in book.holdings:
                side = "long" if s.action is Action.ENTER_LONG else "short"
                book.open(s.coin, side, s.qty, s.price, s.stop_price, close_ms, s.reason, market.regime.value)

        # 4. Value the account; BTC buy-and-hold for comparison (bought at the first close)
        btc_price = float(btc["close"].loc[t])
        if btc_qty is None:
            btc_fill = btc_price * (1 + book.slip)
            btc_qty = start_equity / (btc_fill * (1 + book.fee))
        rows.append((close_time, book.equity(last_close), btc_qty * btc_price, bool(book.holdings)))

    if close_at_end:
        final_ms = rows[-1][0] if rows else start
        for coin in list(book.holdings):
            book.close(coin, last_close[coin], _ms(final_ms) if rows else 0,
                       f"Closed {coin} at the end of the test period.")
        if rows:
            rows[-1] = (rows[-1][0], book.equity(last_close), rows[-1][2], rows[-1][3])
    if rows and btc_qty:
        # Selling the BTC at the end costs a fee and slippage too
        last = rows[-1]
        rows[-1] = (last[0], last[1], last[2] * (1 - book.slip) * (1 - book.fee), last[3])

    curve = pd.DataFrame(rows, columns=["time", "strategy", "btc_hold", "in_market"]).set_index("time")
    if curve.empty:
        raise ValueError("no candles in the requested period")
    return BacktestResult(
        start, end, curve, book.trades,
        compute_stats(curve["strategy"], book.trades, curve["in_market"]),
        compute_stats(curve["btc_hold"]),
        params={k: getattr(cfg, k) for k in settings.backtest.walk_forward.grid},
    )


def earliest_start(data: History, settings: Settings) -> pd.Timestamp:
    return (data.first_signal_candle() + pd.Timedelta(days=settings.backtest.warmup_days)).ceil("D")


# ------------------------------------------------------------ walk-forward


@dataclass(frozen=True)
class Window:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def make_windows(start: pd.Timestamp, end: pd.Timestamp, train_days: int, test_days: int) -> list[Window]:
    """Train on [a, a+train), test on the next [a+train, a+train+test), then slide forward by test_days.
    The final test window may be shorter (at least half a window)."""
    windows = []
    train, test = pd.Timedelta(days=train_days), pd.Timedelta(days=test_days)
    a = start
    while a + train + test / 2 <= end:
        test_end = min(a + train + test, end)
        windows.append(Window(a, a + train, a + train, test_end))
        a += test
    return windows


def settings_with(settings: Settings, params: Mapping[str, float]) -> Settings:
    signals = SignalSettings.model_validate(settings.signals.model_dump() | dict(params))
    return settings.model_copy(update={"signals": signals})


def grid_combinations(grid: Mapping[str, Sequence[float]]) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]


def score(result: BacktestResult, min_trades: int) -> float:
    """How good the training result was: return per unit of drawdown ('reward for the pain')."""
    if result.stats.trades < min_trades:
        return -math.inf
    return result.stats.total_return_pct / max(result.stats.max_drawdown_pct, 5.0)


@dataclass(frozen=True)
class WindowResult:
    window: Window
    params: dict
    train: BacktestResult | None
    test: BacktestResult
    note: str = ""


@dataclass(frozen=True)
class WalkForwardResult:
    windows: list[WindowResult]
    equity: pd.DataFrame  # stitched out-of-sample (test) equity, and BTC held over the same span
    trades: list[Trade]
    stats: Stats
    btc_stats: Stats


def walk_forward(
    data: History,
    plans: Mapping[pd.Timestamp, DayPlan],
    settings: Settings,
    start: pd.Timestamp,
    end: pd.Timestamp,
    progress: Progress | None = None,
) -> WalkForwardResult:
    wf = settings.backtest.walk_forward
    windows = make_windows(start, end, wf.train_days, wf.test_days)
    if not windows:
        raise ValueError(f"need at least {wf.train_days + wf.test_days // 2} days for one walk-forward window")
    combos = grid_combinations(wf.grid)
    equity = settings.demo.starting_balance_usdc
    results: list[WindowResult] = []
    for n, w in enumerate(windows, start=1):
        best, best_score, best_train = None, -math.inf, None
        for combo in combos:
            train = simulate(data, plans, settings_with(settings, combo), w.train_start, w.train_end)
            if (s := score(train, wf.min_trades)) > best_score:
                best, best_score, best_train = combo, s, train
        note = ""
        if best is None:
            best = {k: getattr(settings.signals, k) for k in wf.grid}
            note = f"no settings made {wf.min_trades}+ trades in training; used the config defaults"
        test = simulate(data, plans, settings_with(settings, best), w.test_start, w.test_end, start_equity=equity)
        equity = float(test.equity["strategy"].iloc[-1])
        results.append(WindowResult(w, best, best_train, test, note))
        if progress:
            progress(f"  window {n}/{len(windows)}: trained {w.train_start:%Y-%m-%d}→{w.train_end:%Y-%m-%d}, "
                     f"picked {best}, tested {w.test_start:%Y-%m-%d}→{w.test_end:%Y-%m-%d}: "
                     f"{test.stats.total_return_pct:+.1f}% (BTC {test.btc_stats.total_return_pct:+.1f}%)")

    stitched = pd.concat([r.test.equity for r in results])
    stitched = stitched[~stitched.index.duplicated(keep="last")]
    # BTC held continuously across all test periods, for a fair comparison
    btc = data.candles[BENCHMARK]["close"]
    span = btc[(btc.index >= windows[0].test_start - pd.Timedelta(milliseconds=INTERVAL_MS[settings.signals.timeframe]))]
    btc_prices = span.reindex(stitched.index - pd.Timedelta(milliseconds=INTERVAL_MS[settings.signals.timeframe]))
    first = float(btc_prices.iloc[0])
    cost = (1 + settings.risk.slippage_pct / 100) * (1 + settings.risk.taker_fee_pct / 100)
    start_equity = settings.demo.starting_balance_usdc
    stitched["btc_hold"] = (start_equity / (first * cost) * btc_prices).to_numpy()
    stitched.iloc[-1, stitched.columns.get_loc("btc_hold")] /= cost
    trades = [t for r in results for t in r.test.trades]
    return WalkForwardResult(
        results, stitched, trades,
        compute_stats(stitched["strategy"], trades, stitched["in_market"]),
        compute_stats(stitched["btc_hold"]),
    )


# ---------------------------------------------------------------- reports


def verdict(stats: Stats, btc: Stats) -> list[str]:
    """Plain, honest conclusions."""
    lines = []
    beat = stats.total_return_pct > btc.total_return_pct
    lines.append(
        f"{'BEAT' if beat else 'DID NOT BEAT'} holding Bitcoin after costs: "
        f"{stats.total_return_pct:+.1f}% vs {btc.total_return_pct:+.1f}%."
    )
    if stats.max_drawdown_pct < btc.max_drawdown_pct:
        lines.append(f"Its worst fall ({stats.max_drawdown_pct:.1f}%) was smaller than Bitcoin's "
                     f"({btc.max_drawdown_pct:.1f}%).")
    else:
        lines.append(f"Its worst fall ({stats.max_drawdown_pct:.1f}%) was NOT smaller than Bitcoin's "
                     f"({btc.max_drawdown_pct:.1f}%).")
    if stats.total_return_pct <= 0:
        lines.append("It lost money overall.")
    if stats.trades < 30:
        lines.append(f"Only {stats.trades} trades: too few to be confident either way.")
    if stats.trades and stats.fees_usd + stats.funding_usd > abs(stats.end_equity - stats.start_equity):
        lines.append("Fees and funding were larger than the overall profit or loss: costs matter a lot here.")
    return lines


def stats_table(stats: Stats, btc: Stats, usd_to_aud: float) -> list[tuple[str, str, str]]:
    def money(v: float) -> str:
        return f"A${v * usd_to_aud:,.2f}"

    pf = "n/a" if stats.profit_factor is None else f"{stats.profit_factor:.2f}"
    return [
        ("Start → end", f"{money(stats.start_equity)} → {money(stats.end_equity)}",
         f"{money(btc.start_equity)} → {money(btc.end_equity)}"),
        ("Total return", f"{stats.total_return_pct:+.1f}%", f"{btc.total_return_pct:+.1f}%"),
        ("Per year", f"{stats.annual_return_pct:+.1f}%", f"{btc.annual_return_pct:+.1f}%"),
        ("Max drawdown", f"{stats.max_drawdown_pct:.1f}%", f"{btc.max_drawdown_pct:.1f}%"),
        ("Trades", str(stats.trades), "1"),
        ("Win rate", f"{stats.win_rate_pct:.0f}%", "—"),
        ("Average win", f"{stats.avg_win_pct:+.1f}% ({money(stats.avg_win_usd)})", "—"),
        ("Average loss", f"{stats.avg_loss_pct:+.1f}% ({money(stats.avg_loss_usd)})", "—"),
        ("Profit factor", pf, "—"),
        ("Time in market", f"{stats.time_in_market_pct:.0f}%", "100%"),
        ("Fees + funding paid", money(stats.fees_usd + stats.funding_usd), "—"),
    ]


def write_trades_csv(trades: Sequence[Trade], path: Path, tz: ZoneInfo, usd_to_aud: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    def when(ms: int) -> str:
        return pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(tz).strftime("%Y-%m-%d %H:%M")

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["coin", "side", "entry_time_sydney", "entry_price", "qty", "size_usd", "initial_stop",
                         "exit_time_sydney", "exit_price", "pnl_usd", "pnl_aud", "pnl_pct", "fees_usd",
                         "funding_usd", "candles_held", "mood_at_entry", "entry_reason", "exit_reason"])
        for t in trades:
            writer.writerow([t.coin, t.side, when(t.entry_ts_ms), f"{t.entry_price:.6g}", f"{t.qty:.6g}",
                             f"{t.notional_usd:.2f}", f"{t.initial_stop:.6g}", when(t.exit_ts_ms),
                             f"{t.exit_price:.6g}", f"{t.pnl_usd:.2f}", f"{t.pnl_usd * usd_to_aud:.2f}",
                             f"{t.pnl_pct:.2f}", f"{t.fees_usd:.2f}", f"{t.funding_usd:.2f}", t.bars, t.regime,
                             t.entry_reason, t.exit_reason])
    return path


def plot_equity(equity: pd.DataFrame, stats: Stats, btc: Stats, path: Path, title: str, usd_to_aud: float,
                shade: Sequence[tuple[pd.Timestamp, pd.Timestamp]] = ()) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax, ax_dd) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax.plot(equity.index, equity["strategy"] * usd_to_aud, color="#1f77b4", linewidth=1.4,
            label=f"Scout ({stats.total_return_pct:+.1f}%, worst fall {stats.max_drawdown_pct:.0f}%)")
    ax.plot(equity.index, equity["btc_hold"] * usd_to_aud, color="#f7931a", linewidth=1.2,
            label=f"Just holding BTC ({btc.total_return_pct:+.1f}%, worst fall {btc.max_drawdown_pct:.0f}%)")
    for i, (a, b) in enumerate(shade):
        ax.axvspan(a, b, color="#999999", alpha=0.08 if i % 2 else 0.16, linewidth=0)
    ax.set_ylabel("Account value (A$)")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)

    for column, colour in (("strategy", "#1f77b4"), ("btc_hold", "#f7931a")):
        peaks = equity[column].cummax()
        ax_dd.fill_between(equity.index, -(peaks - equity[column]) / peaks * 100, 0, color=colour, alpha=0.3)
    ax_dd.set_ylabel("Drawdown %")
    ax_dd.grid(alpha=0.3)

    fig.suptitle(f"Scout v{APP_VERSION} — {title}", fontsize=13)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
