"""Market mood (regime) detector.

Answers "is crypto overall going up, down or sideways, and how risky is it?"
using four simple, explainable checks on daily candles:

1. Trend      Bitcoin's price vs its 50- and 200-day moving averages, and their slopes.
2. Breadth    % of the top coins trading above their own 50-day average.
3. Volatility Bitcoin's average true range (ATR) as a % of price, vs its own past year.
4. Crowding   average funding rate of the top coins.

Trend, breadth and crowding add up to a points score -> RISK_ON / NEUTRAL / RISK_OFF.
Volatility gives a separate label -> CALM / NORMAL / WILD.

Rules for later steps: RISK_OFF = no new long trades; WILD = smaller positions.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

import pandas as pd

from scout.config import RegimeSettings
from scout.data import DAY_MS
from scout.indicators import (
    average_true_range,
    moving_average,
    percent_change,
    percentile_rank,
)
from scout.version import APP_VERSION

MIN_VOLATILITY_HISTORY = 60  # days of ATR needed before "calm"/"wild" means anything


class Regime(StrEnum):
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"


class Volatility(StrEnum):
    CALM = "CALM"
    NORMAL = "NORMAL"
    WILD = "WILD"


class NotEnoughData(ValueError):
    """Not enough candle history to judge the market."""


@dataclass(frozen=True)
class RegimeReading:
    ts_ms: int
    regime: Regime
    volatility: Volatility
    score: int
    points: dict[str, int]
    btc_price: float
    fast_ma: float
    slow_ma: float
    fast_slope_pct: float
    slow_slope_pct: float
    breadth_pct: float | None
    breadth_coins: int
    atr_pct: float
    atr_percentile: float | None
    funding_annual_pct: float | None
    size_multiplier: float
    summary: str
    reasons: list[str] = field(default_factory=list)

    @property
    def allows_new_longs(self) -> bool:
        return self.regime is not Regime.RISK_OFF

    def details(self) -> dict:
        data = asdict(self)
        data.pop("summary")
        return data


# ----------------------------------------------------------------- checks


def _slope_word(slope: float, flat: float) -> tuple[int, str]:
    if slope > flat:
        return 1, "rising"
    if slope < -flat:
        return -1, "falling"
    return 0, "flat"


def breadth(coin_closes: pd.DataFrame, coin_prices: Mapping[str, float], ma_days: int) -> tuple[float | None, int]:
    """% of coins whose price now is above their own moving average, and how many coins were counted."""
    above = counted = 0
    for coin in coin_closes.columns:
        price = coin_prices.get(coin)
        history = coin_closes[coin].dropna()
        if price is None or len(history) < ma_days - 1:
            continue
        average = (history.iloc[-(ma_days - 1) :].sum() + price) / ma_days
        counted += 1
        above += price > average
    return (100 * above / counted if counted else None), counted


def classify(score: int, btc_above_slow_ma: bool, cfg: RegimeSettings) -> Regime:
    if score >= cfg.risk_on_min_score and (btc_above_slow_ma or not cfg.require_btc_above_slow_ma_for_risk_on):
        return Regime.RISK_ON
    if score <= cfg.risk_off_max_score:
        return Regime.RISK_OFF
    return Regime.NEUTRAL


def detect(
    btc: pd.DataFrame,
    btc_price: float,
    coin_closes: pd.DataFrame,
    coin_prices: Mapping[str, float],
    funding_rates: Sequence[float] | None,
    cfg: RegimeSettings,
    ts_ms: int,
) -> RegimeReading:
    """Judge the market.

    btc          finished daily candles (high/low/close), oldest first
    btc_price    Bitcoin's price now (today's unfinished day)
    coin_closes  finished daily closes of the top coins, one column per coin
    coin_prices  those coins' prices now
    funding_rates hourly funding rates of the top coins, or None if unknown
    """
    need = cfg.slow_ma_days + cfg.slope_days
    if len(btc) < need:
        raise NotEnoughData(f"need {need} daily candles for {cfg.benchmark_coin}, have {len(btc)}")
    reasons: list[str] = []

    # 1. Trend
    closes = pd.concat([btc["close"], pd.Series([btc_price])], ignore_index=True)
    fast = moving_average(closes, cfg.fast_ma_days)
    slow = moving_average(closes, cfg.slow_ma_days)
    fast_now, slow_now = float(fast.iloc[-1]), float(slow.iloc[-1])
    fast_slope = percent_change(fast, cfg.slope_days)
    slow_slope = percent_change(slow, cfg.slope_days)
    above_fast, above_slow = btc_price > fast_now, btc_price > slow_now
    fast_pts, fast_word = _slope_word(fast_slope, cfg.slope_flat_pct)
    slow_pts, slow_word = _slope_word(slow_slope, cfg.slope_flat_pct)
    trend = (1 if above_fast else -1) + (1 if above_slow else -1) + fast_pts + slow_pts
    reasons.append(
        f"Trend {trend:+d}: {cfg.benchmark_coin} ${btc_price:,.0f} is "
        f"{'above' if above_fast else 'below'} its {cfg.fast_ma_days}-day average ${fast_now:,.0f} "
        f"({1 if above_fast else -1:+d}), {'above' if above_slow else 'below'} its {cfg.slow_ma_days}-day "
        f"${slow_now:,.0f} ({1 if above_slow else -1:+d}); {cfg.fast_ma_days}-day {fast_word} "
        f"{fast_slope:+.1f}% ({fast_pts:+d}), {cfg.slow_ma_days}-day {slow_word} {slow_slope:+.1f}% "
        f"({slow_pts:+d}) over {cfg.slope_days} days"
    )

    # 2. Breadth
    breadth_pct, breadth_coins = breadth(coin_closes, coin_prices, cfg.breadth_ma_days)
    if breadth_pct is None or breadth_coins < cfg.breadth_min_coins:
        breadth_pct, breadth_pts = None, 0
        reasons.append(f"Breadth 0: unknown (only {breadth_coins} coins have {cfg.breadth_ma_days} days of history)")
    else:
        if breadth_pct >= cfg.breadth_strong_pct:
            breadth_pts, word = cfg.breadth_weight, "broad strength"
        elif breadth_pct <= cfg.breadth_weak_pct:
            breadth_pts, word = -cfg.breadth_weight, "weak, only a few coins rising"
        else:
            breadth_pts, word = 0, "mixed"
        reasons.append(
            f"Breadth {breadth_pts:+d}: {breadth_pct:.0f}% of the top {breadth_coins} coins are above their own "
            f"{cfg.breadth_ma_days}-day average ({word}; strong ≥ {cfg.breadth_strong_pct:.0f}%, "
            f"weak ≤ {cfg.breadth_weak_pct:.0f}%)"
        )

    # 3. Crowding
    funding_annual = None
    crowding_pts = 0
    if funding_rates:
        funding_annual = sum(funding_rates) / len(funding_rates) * 24 * 365 * 100
        crowded = funding_annual > cfg.crowded_funding_annual_pct
        crowding_pts = -1 if crowded else 0
        reasons.append(
            f"Crowding {crowding_pts:+d}: average funding {funding_annual:+.0f}% a year "
            f"({'crowded' if crowded else 'not crowded'}; crowded above {cfg.crowded_funding_annual_pct:.0f}%)"
        )
    else:
        reasons.append("Crowding 0: funding rates not available")

    # 4. Volatility
    atr_pct_series = (average_true_range(btc, cfg.atr_days) / btc["close"] * 100).dropna()
    window = atr_pct_series.iloc[-cfg.volatility_history_days :]
    atr_pct = float(window.iloc[-1])
    if len(window) < MIN_VOLATILITY_HISTORY:
        atr_percentile, volatility = None, Volatility.NORMAL
        reasons.append(f"Volatility NORMAL: {cfg.benchmark_coin} moves ~{atr_pct:.1f}% a day (too little history to compare)")
    else:
        atr_percentile = percentile_rank(window, atr_pct)
        if atr_percentile >= cfg.wild_percentile:
            volatility = Volatility.WILD
        elif atr_percentile <= cfg.calm_percentile:
            volatility = Volatility.CALM
        else:
            volatility = Volatility.NORMAL
        reasons.append(
            f"Volatility {volatility}: {cfg.benchmark_coin} moves ~{atr_pct:.1f}% a day, more than on "
            f"{atr_percentile:.0f}% of days in the past {len(window)} (calm ≤ {cfg.calm_percentile:.0f}%, "
            f"wild ≥ {cfg.wild_percentile:.0f}%)"
        )

    points = {"trend": trend, "breadth": breadth_pts, "crowding": crowding_pts}
    score = sum(points.values())
    regime = classify(score, above_slow, cfg)
    if score >= cfg.risk_on_min_score and regime is not Regime.RISK_ON:
        reasons.append(f"Held back from RISK_ON: {cfg.benchmark_coin} is below its {cfg.slow_ma_days}-day average")
    size_multiplier = cfg.wild_volatility_size_multiplier if volatility is Volatility.WILD else 1.0

    return RegimeReading(
        ts_ms=ts_ms,
        regime=regime,
        volatility=volatility,
        score=score,
        points=points,
        btc_price=btc_price,
        fast_ma=fast_now,
        slow_ma=slow_now,
        fast_slope_pct=fast_slope,
        slow_slope_pct=slow_slope,
        breadth_pct=breadth_pct,
        breadth_coins=breadth_coins,
        atr_pct=atr_pct,
        atr_percentile=atr_percentile,
        funding_annual_pct=funding_annual,
        size_multiplier=size_multiplier,
        summary=_summary(cfg, regime, volatility, above_fast, above_slow, slow_word, breadth_pct,
                         breadth_coins, funding_annual, size_multiplier),
        reasons=reasons,
    )


def _summary(
    cfg: RegimeSettings,
    regime: Regime,
    volatility: Volatility,
    above_fast: bool,
    above_slow: bool,
    slow_word: str,
    breadth_pct: float | None,
    breadth_coins: int,
    funding_annual: float | None,
    size_multiplier: float,
) -> str:
    coin = "Bitcoin" if cfg.benchmark_coin == "BTC" else cfg.benchmark_coin
    if above_fast and above_slow:
        trend = f"{coin} is above its short- and long-term averages"
    elif above_slow:
        trend = f"{coin} is above its long-term average but has dipped below its short-term one"
    elif above_fast:
        trend = f"{coin} is below its long-term average but back above its short-term one"
    else:
        trend = f"{coin} is below its short- and long-term averages"
    parts = [trend, f"its long-term average is {slow_word}"]
    if breadth_pct is not None:
        parts.append(f"{breadth_pct:.0f}% of the top {breadth_coins} coins are above their own {cfg.breadth_ma_days}-day average")
    parts[-1] = "and " + parts[-1]
    verdict = {
        Regime.RISK_ON: "the market is in an uptrend. New long trades allowed.",
        Regime.NEUTRAL: "the market has no clear direction. Trades allowed, with extra care.",
        Regime.RISK_OFF: "the market is weak or falling. No new long trades: Scout sits in cash.",
    }[regime]
    text = f"{', '.join(parts)}: {verdict}"
    text += f" Volatility is {volatility.lower()}"
    text += f", so position sizes are cut to {size_multiplier:.0%}." if size_multiplier < 1 else "."
    if funding_annual is not None and funding_annual > cfg.crowded_funding_annual_pct:
        text += (
            f" Funding is high ({funding_annual:+.0f}% a year): lots of traders are betting on a rise,"
            " which raises the risk of a sharp drop."
        )
    return text[0].upper() + text[1:]


# ---------------------------------------------------------------- history


def top_coins_at(dollar_volume: pd.DataFrame, day: pd.Timestamp, n: int, lookback_days: int = 30) -> list[str]:
    """The n coins with the highest average daily trading volume in the `lookback_days` before `day`.

    Ranking by volume *at the time* (not today's) avoids quietly picking today's winners.
    """
    window = dollar_volume.loc[(dollar_volume.index < day) & (dollar_volume.index >= day - pd.Timedelta(days=lookback_days))]
    return window.mean().dropna().nlargest(n).index.tolist()


def history(
    btc: pd.DataFrame,
    coin_closes: pd.DataFrame,
    coin_dollar_volume: pd.DataFrame,
    cfg: RegimeSettings,
    days: int,
) -> list[RegimeReading]:
    """One reading per day for the last `days` days, using only data available on that day.

    Funding history isn't included (crowding scores 0).
    """
    first = max(len(btc) - days, cfg.slow_ma_days + cfg.slope_days)
    readings = []
    for i in range(first, len(btc)):
        day = btc.index[i]
        universe = top_coins_at(coin_dollar_volume, day, cfg.breadth_top_coins)
        before = coin_closes.loc[coin_closes.index < day, universe]
        prices = coin_closes.loc[day, universe].dropna().to_dict() if day in coin_closes.index else {}
        ts_ms = int(day.timestamp() * 1000) + DAY_MS
        readings.append(detect(btc.iloc[:i], float(btc["close"].iloc[i]), before, prices, None, cfg, ts_ms))
    return readings


@dataclass(frozen=True)
class RegimeStats:
    days: int
    share_pct: float
    avg_next_day_pct: float | None  # BTC's average move on the day *after* each reading


def regime_stats(readings: Sequence[RegimeReading]) -> dict[Regime, RegimeStats]:
    """How often each mood occurred, and what Bitcoin did next. A first hint only, not a backtest."""
    next_moves: dict[Regime, list[float]] = {r: [] for r in Regime}
    counts = dict.fromkeys(Regime, 0)
    for i, reading in enumerate(readings):
        counts[reading.regime] += 1
        if i + 1 < len(readings):
            next_moves[reading.regime].append((readings[i + 1].btc_price / reading.btc_price - 1) * 100)
    total = len(readings) or 1
    return {
        r: RegimeStats(
            days=counts[r],
            share_pct=100 * counts[r] / total,
            avg_next_day_pct=sum(next_moves[r]) / len(next_moves[r]) if next_moves[r] else None,
        )
        for r in Regime
    }


# ---------------------------------------------------------------- storage


def save_reading(conn: sqlite3.Connection, reading: RegimeReading) -> None:
    """Store a reading in regime_history. The caller commits."""
    conn.execute(
        """INSERT INTO regime_history (ts_ms, regime, risk_level, score, reason, details_json, app_version)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            reading.ts_ms,
            reading.regime.value,
            reading.volatility.value,
            reading.score,
            reading.summary,
            json.dumps(reading.details()),
            APP_VERSION,
        ),
    )


# ------------------------------------------------------------------ chart

REGIME_COLOURS = {Regime.RISK_ON: "#2e9e5b", Regime.NEUTRAL: "#b0b0b0", Regime.RISK_OFF: "#d9534f"}
VOLATILITY_COLOURS = {Volatility.CALM: "#5b8def", Volatility.NORMAL: "#b0b0b0", Volatility.WILD: "#e8a33d"}


def plot_history(readings: Sequence[RegimeReading], cfg: RegimeSettings, path: Path, note: str = "") -> Path:
    """Save a PNG: BTC price with the mood shaded behind it, breadth, and volatility."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    times = pd.to_datetime([r.ts_ms for r in readings], unit="ms", utc=True)
    fig, (ax_price, ax_breadth, ax_vol) = plt.subplots(
        3, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1, 1]}
    )

    # Shade each day by mood
    day = pd.Timedelta(days=1)
    for t, r in zip(times, readings):
        ax_price.axvspan(t - day, t, color=REGIME_COLOURS[r.regime], alpha=0.25, linewidth=0)
        ax_vol.axvspan(t - day, t, color=VOLATILITY_COLOURS[r.volatility], alpha=0.35, linewidth=0)

    ax_price.plot(times, [r.btc_price for r in readings], color="black", linewidth=1.2, label=f"{cfg.benchmark_coin} price")
    ax_price.plot(times, [r.fast_ma for r in readings], color="#1f77b4", linewidth=1, label=f"{cfg.fast_ma_days}-day average")
    ax_price.plot(times, [r.slow_ma for r in readings], color="#9467bd", linewidth=1, label=f"{cfg.slow_ma_days}-day average")
    ax_price.set_yscale("log")
    ax_price.set_ylabel("US$ (log scale)")
    handles = ax_price.get_legend_handles_labels()[0] + [
        Patch(color=colour, alpha=0.4, label=regime.value) for regime, colour in REGIME_COLOURS.items()
    ]
    ax_price.legend(handles=handles, loc="lower left", fontsize=8, ncol=2)

    ax_breadth.plot(times, [r.breadth_pct if r.breadth_pct is not None else float("nan") for r in readings], color="#2c7fb8")
    ax_breadth.axhline(cfg.breadth_strong_pct, color="#2e9e5b", linestyle="--", linewidth=0.8)
    ax_breadth.axhline(cfg.breadth_weak_pct, color="#d9534f", linestyle="--", linewidth=0.8)
    ax_breadth.set_ylim(0, 100)
    ax_breadth.set_ylabel("Breadth %")

    ax_vol.plot(times, [r.atr_pct for r in readings], color="black", linewidth=1)
    ax_vol.set_ylabel("ATR % of price")
    ax_vol.legend(
        handles=[Patch(color=c, alpha=0.5, label=v.value) for v, c in VOLATILITY_COLOURS.items()],
        loc="upper left", fontsize=8, ncol=3,
    )

    fig.suptitle(f"Scout v{APP_VERSION} — market mood, last {len(readings)} days", fontsize=13)
    if note:
        fig.text(0.5, 0.005, note, ha="center", fontsize=8, color="#555555")
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
