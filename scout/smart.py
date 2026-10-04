"""SMART:3: a researched, two-part strategy in its own fake account.

1. Bitcoin trend (half the account): hold BTC while its price is above its 50-day average, cash otherwise.
2. Long/short ranking (a quarter long, a quarter short): every day, the 40 most-traded coins (not BTC) are
   ranked on six measures that predicted the next days' returns in 2023-25:
   - 1-month and 2-month trend (strength, scaled by how jumpy the coin is),
   - how close the price is to its 20-day high,
   - calm: smaller daily moves,
   - no lottery spikes: no huge single-day jumps lately (those coins tend to fade),
   - moving less in step with Bitcoin.
   It buys the best 6 and shorts the worst 6, and only swaps a coin out when it falls out of the best
   (or worst) 12, which keeps trading costs down. Each position is sized so calmer coins get more money.

Why this design (see `scout smart backtest`): simply buying the best-ranked altcoins LOST money in 2023-25,
because altcoins as a group fell behind Bitcoin. Buying the best AND shorting the worst cancels that out,
and it barely moves with Bitcoin, so it pairs well with the Bitcoin trend half. A machine-learning version
(weights re-learnt from the past every month) was tested too and lost money: the weights chased noise.

Everything here is decided on finished daily candles (00:00 UTC = 10 or 11am Sydney); the backtest and the
live account call the same `plan()` so they can't drift apart.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from scout.backtest import BENCHMARK, Portfolio, Stats, Trade, compute_stats, stop_fill
from scout.config import Settings, SmartSettings
from scout.data import DAY_MS

BTC = BENCHMARK
FACTORS = ("mom28", "mom56", "near_high", "calm", "no_spikes", "low_beta")


# ------------------------------------------------------------ the measures


@dataclass(frozen=True)
class Panels:
    """Every measure for every coin and day (rows: days, columns: coins). Only uses the past at each row."""

    open: pd.DataFrame
    close: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    raw: dict[str, pd.DataFrame]  # the measures in their own units, for explanations
    signed: dict[str, pd.DataFrame]  # the measures as ranked (higher = better), e.g. trend ÷ jumpiness
    score: pd.DataFrame  # the combined score (higher = better to own), NaN outside the universe
    vol: pd.DataFrame  # typical daily move (standard deviation of daily returns)
    atr: pd.DataFrame  # average true range, for emergency stops
    btc_trend: pd.Series  # True when BTC closed above its moving average


def build_panels(daily: Mapping[str, pd.DataFrame], cfg: SmartSettings) -> Panels:
    """`daily`: coin -> DataFrame indexed by day (UTC) with open/high/low/close/volume (finished candles)."""
    frames = {c: f for c, f in daily.items() if not f.empty}
    close = pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()
    open_ = pd.DataFrame({c: f["open"] for c, f in frames.items()}).reindex(close.index)
    high = pd.DataFrame({c: f["high"] for c, f in frames.items()}).reindex(close.index)
    low = pd.DataFrame({c: f["low"] for c, f in frames.items()}).reindex(close.index)
    dollar_volume = pd.DataFrame({c: f["volume"] * f["close"] for c, f in frames.items()}).reindex(close.index)

    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.log(close).diff()
    vol = ret.rolling(cfg.vol_days, min_periods=cfg.vol_days).std()
    btc = ret[BTC]
    beta = ret.rolling(60, min_periods=40).cov(btc).div(btc.rolling(60, min_periods=40).var(), axis=0)
    raw = {
        "mom28": ret.rolling(28).sum(),
        "mom56": ret.rolling(56).sum(),
        "near_high": np.log(close / close.rolling(20).max()),
        "calm": vol,
        "no_spikes": ret.rolling(20).max(),
        "low_beta": beta,
    }
    signed = {  # higher = better
        "mom28": raw["mom28"] / vol, "mom56": raw["mom56"] / vol, "near_high": raw["near_high"],
        "calm": -vol, "no_spikes": -raw["no_spikes"], "low_beta": -beta,
    }
    # The universe: the most-traded coins over the last 30 days, with enough history, never BTC itself
    # (BTC has its own half of the account).
    adv = dollar_volume.rolling(30, min_periods=20).mean()
    old_enough = close.notna().cumsum() >= cfg.min_history_days
    eligible = adv.where(old_enough & close.notna()).drop(columns=[BTC], errors="ignore")
    universe = eligible.rank(axis=1, ascending=False) <= cfg.universe
    universe = universe.reindex(columns=close.columns, fill_value=False)

    def zscore(frame: pd.DataFrame) -> pd.DataFrame:
        f = frame.where(universe)
        return f.sub(f.mean(axis=1), axis=0).div(f.std(axis=1), axis=0).clip(-3, 3)

    score = sum(zscore(signed[k]) for k in FACTORS) / len(FACTORS)  # NaN if any measure is missing
    prev_close = close.shift()
    true_range = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()]).groupby(level=0).max()
    atr = true_range.reindex(close.index).rolling(14, min_periods=10).mean()
    btc_trend = close[BTC] > close[BTC].rolling(cfg.btc_ma_days, min_periods=cfg.btc_ma_days).mean()
    return Panels(open_, close, high, low, raw, signed, score, vol, atr, btc_trend)


# ------------------------------------------------------------ the decision


@dataclass(frozen=True)
class Pick:
    coin: str
    side: str
    rank: int  # 1 = best (longs) or worst (shorts)
    score: float
    weight: float  # share of its half/quarter of the account
    reason: str


@dataclass(frozen=True)
class Plan:
    day: pd.Timestamp
    btc_on: bool
    btc_price: float
    btc_ma: float
    picks: list[Pick] = field(default_factory=list)
    ranked: int = 0

    @property
    def longs(self) -> list[Pick]:
        return [p for p in self.picks if p.side == "long"]

    @property
    def shorts(self) -> list[Pick]:
        return [p for p in self.picks if p.side == "short"]

    def wants(self, coin: str, side: str) -> bool:
        if coin == BTC:
            return self.btc_on and side == "long"
        return any(p.coin == coin and p.side == side for p in self.picks)


def choose(scores: pd.Series, held: set[str], k: int, keep_within: int) -> list[str]:
    """The best `k`, keeping held coins while they stay within the best `keep_within` (fewer swaps)."""
    ranks = scores.rank(ascending=False, method="first")
    kept = [c for c in ranks.sort_values().index if c in held and ranks[c] <= keep_within]
    for coin in ranks.sort_values().index:
        if len(kept) >= k:
            break
        if coin not in kept:
            kept.append(coin)
    return kept[:k]


def _describe(panels: Panels, day: pd.Timestamp, coin: str, side: str, rank: int, ranked: int) -> str:
    r = {k: float(panels.raw[k].at[day, coin]) for k in FACTORS}
    facts = {
        "mom28": f"1-month trend {math.expm1(r['mom28']) * 100:+.0f}%",
        "mom56": f"2-month trend {math.expm1(r['mom56']) * 100:+.0f}%",
        "near_high": ("at its 20-day high" if r["near_high"] > -0.005
                      else f"{-math.expm1(r['near_high']) * 100:.0f}% below its 20-day high"),
        "calm": f"typical daily move {r['calm'] * 100:.1f}%",
        "no_spikes": f"biggest day lately {math.expm1(r['no_spikes']) * 100:+.0f}%",
        "low_beta": f"moves {r['low_beta']:.1f}x as much as Bitcoin",
    }
    # The measures that pushed it furthest in its direction, judged exactly as the ranking judges them.
    in_universe = panels.score.loc[day].notna()
    zs = {}
    for k in FACTORS:
        values = panels.signed[k].loc[day][in_universe]
        zs[k] = float((values[coin] - values.mean()) / values.std()) if values.std() else 0.0
    order = sorted(zs, key=lambda k: -zs[k] if side == "long" else zs[k])
    where, compared = ("best", "strongest") if side == "long" else ("worst", "weakest")
    return (f"#{rank} {where} of {ranked} ranked coins; {compared} next to the others on: "
            + "; ".join(facts[k] for k in order[:3]))


def plan(panels: Panels, day: pd.Timestamp, held: Mapping[str, str], cfg: SmartSettings) -> Plan:
    """What SMART:3 wants to hold after the daily candle of `day` closed. `held`: coin -> side."""
    btc_price = float(panels.close.at[day, BTC])
    ma = float(panels.close[BTC].loc[:day].tail(cfg.btc_ma_days).mean())
    scores = panels.score.loc[day].dropna()
    if len(scores) < 2 * cfg.picks + 2:
        return Plan(day, bool(panels.btc_trend.get(day, False)), btc_price, ma, [], len(scores))
    longs = choose(scores, {c for c, s in held.items() if s == "long"}, cfg.picks, cfg.keep_within)
    shorts = choose(-scores.drop(longs), {c for c, s in held.items() if s == "short"}, cfg.picks, cfg.keep_within)
    picks = []
    for side, coins in (("long", longs), ("short", shorts)):
        inverse_vol = {c: 1 / float(panels.vol.at[day, c]) for c in coins}
        total = sum(inverse_vol.values())
        order = (scores if side == "long" else -scores).rank(ascending=False, method="first")
        for c in coins:
            rank = int(order[c])
            picks.append(Pick(c, side, rank, float(scores[c]), inverse_vol[c] / total,
                              _describe(panels, day, c, side, rank, len(scores))))
    return Plan(day, bool(panels.btc_trend.get(day, False)), btc_price, ma, picks, len(scores))


CASH_BUFFER = 0.98  # size off 98% of the account, so fees never make the last order unaffordable


def target_usd(plan_: Plan, pick: Pick | None, equity: float, cfg: SmartSettings) -> float:
    """How big a new position should be, in US$."""
    if pick is None:  # the Bitcoin half
        return equity * CASH_BUFFER * cfg.btc_pct / 100
    sleeve = cfg.long_pct if pick.side == "long" else cfg.short_pct
    return equity * CASH_BUFFER * sleeve / 100 * pick.weight


def emergency_stop(entry: float, atr: float, side: str, cfg: SmartSettings) -> float | None:
    """A wide safety stop (several days' typical range away), or None when switched off."""
    if cfg.stop_atr <= 0 or not atr or math.isnan(atr):
        return None
    return entry - cfg.stop_atr * atr if side == "long" else entry + cfg.stop_atr * atr


def btc_reason(plan_: Plan, cfg: SmartSettings) -> str:
    word = "above" if plan_.btc_on else "below"
    return (f"Bitcoin closed at ${plan_.btc_price:,.0f}, {word} its {cfg.btc_ma_days}-day average of "
            f"${plan_.btc_ma:,.0f}: its trend is {'up' if plan_.btc_on else 'down'}")


# ------------------------------------------------------------ backtest


@dataclass(frozen=True)
class SmartBacktest:
    equity: pd.DataFrame  # strategy, btc_hold
    trades: list[Trade]
    stats: Stats
    btc_stats: Stats


def backtest(daily: Mapping[str, pd.DataFrame], settings: Settings, start: pd.Timestamp, end: pd.Timestamp,
             cfg: SmartSettings | None = None, progress: Callable[[str], None] | None = None) -> SmartBacktest:
    """Replay SMART:3 day by day, as the live account trades: decide at each daily close and trade at that
    price (plus slippage and fees), keep positions at their entry size until they're dropped, pay funding,
    and check the emergency stops against each day's high and low."""
    cfg = cfg or settings.smart
    panels = build_panels(daily, cfg)
    risk = settings.risk
    start_cash = settings.smart.starting_balance_aud * settings.demo.aud_to_usdc_rate
    book = Portfolio(start_cash, risk.taker_fee_pct, risk.slippage_pct, settings.backtest.funding_rate_hourly_pct)
    days = panels.close.index[(panels.close.index >= start) & (panels.close.index <= end)]
    curve, btc_curve = [], []
    btc_qty = start_cash / float(panels.close.at[days[0], BTC])
    for n, day in enumerate(days):
        if progress and n % 90 == 0:
            progress(f"  {day:%Y-%m-%d}")
        ts = int(day.timestamp() * 1000) + DAY_MS  # the candle for `day` closes at the end of the day
        prices = {c: float(v) for c, v in panels.close.loc[day].dropna().items()}
        # During the day: emergency stops, then a day of funding.
        for coin, h in list(book.holdings.items()):
            o, hi, lo = (float(panels.open.at[day, coin]), float(panels.high.at[day, coin]),
                         float(panels.low.at[day, coin]))
            if math.isnan(o) or math.isnan(hi) or math.isnan(lo):
                continue
            fill = stop_fill(h.long, h.stop_price, o, hi, lo)
            if fill is not None:
                book.close(coin, fill, ts, "emergency stop")
        for coin in list(book.holdings):
            if coin not in prices:  # delisted: sold at its last price
                book.close(coin, book.holdings[coin].entry_price, ts, "no longer traded")
        book.charge_funding(prices, 24)
        # At the close: the same decision as live.
        held = {c: h.side for c, h in book.holdings.items()}
        p = plan(panels, day, held, cfg)
        for coin, h in list(book.holdings.items()):
            if not p.wants(coin, h.side):
                book.close(coin, prices[coin], ts, "dropped from the plan")
        equity = book.equity(prices)
        wanted: list[tuple[str, str, Pick | None]] = [(BTC, "long", None)] if p.btc_on else []
        wanted += [(pk.coin, pk.side, pk) for pk in p.picks]
        for coin, side, pk in wanted:
            if coin in book.holdings or coin not in prices:
                continue
            size = target_usd(p, pk, equity, cfg)
            if size < risk.min_order_usd:
                continue
            stop = emergency_stop(prices[coin], float(panels.atr.at[day, coin]), side, cfg)
            book.open(coin, side, size / prices[coin], prices[coin],
                      stop if stop is not None else (0.0 if side == "long" else 1e18), ts,
                      pk.reason if pk else btc_reason(p, cfg), "")
        curve.append((day, book.equity(prices)))
        btc_curve.append(btc_qty * prices[BTC])
    last = {c: float(v) for c, v in panels.close.loc[days[-1]].dropna().items()}
    for coin in list(book.holdings):
        book.close(coin, last.get(coin, book.holdings[coin].entry_price), int(days[-1].timestamp() * 1000) + DAY_MS,
                   "end of the test")
    index = pd.DatetimeIndex([d for d, _ in curve])
    equity = pd.DataFrame({"strategy": [v for _, v in curve], "btc_hold": btc_curve}, index=index)
    equity.iloc[-1, 0] = book.cash
    return SmartBacktest(equity, book.trades, compute_stats(equity["strategy"], book.trades),
                         compute_stats(equity["btc_hold"]))


def sharpe(equity: pd.Series) -> float:
    daily = equity.pct_change().dropna()
    return float(daily.mean() / daily.std() * math.sqrt(365)) if daily.std() else 0.0


def monthly_returns(equity: pd.Series) -> pd.Series:
    return equity.resample("ME").last().pct_change().dropna() * 100


def frames_from_candles(rows: Sequence) -> pd.DataFrame:
    """Candles (scout.data.Candle) -> a daily DataFrame indexed by the UTC day."""
    frame = pd.DataFrame([{"day": pd.Timestamp(c.open_time_ms, unit="ms"), "open": c.open, "high": c.high,
                           "low": c.low, "close": c.close, "volume": c.volume} for c in rows])
    return frame.set_index("day").sort_index() if not frame.empty else frame
