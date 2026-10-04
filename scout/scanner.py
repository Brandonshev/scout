"""Coin scanner: builds the shortlist of coins worth trading right now.

1. Universe: the top coins by 24h volume.
2. Hard filters, each with a saved reason: minimum volume and open interest, not a
   stablecoin, listed long enough, tight spread and enough order-book depth.
3. Measurements for the rest: relative strength vs BTC (7 and 30 days), volume
   surge (today vs its 20-day average), trend (price vs 20/50-day averages) and
   volatility (ATR % of price).
4. Score = sum of points × weights (all in config.yaml), highest first.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

import pandas as pd

from scout.config import ScannerSettings
from scout.data import DAY_MS, L2Book, MarketCoin
from scout.indicators import average_true_range, moving_average
from scout.version import APP_VERSION

BENCHMARK = "BTC"


@dataclass(frozen=True)
class BookStats:
    """How easy it is to trade a coin right now, from its order book."""

    spread_pct: float  # gap between best buy and sell offers, as % of price
    bid_depth_usd: float  # buy orders within the band below the price
    ask_depth_usd: float  # sell orders within the band above the price

    @property
    def depth_usd(self) -> float:
        return min(self.bid_depth_usd, self.ask_depth_usd)


@dataclass(frozen=True)
class CoinScan:
    coin: str
    volume_24h_usd: float
    exclusions: list[str]
    points: dict[str, int]
    score: float
    metrics: dict[str, float | None]
    note: str
    rank: int | None = None
    breakdown: str = ""
    details: dict = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return not self.exclusions

    @property
    def reason(self) -> str:
        """The plain-English reason stored in scan_results."""
        if not self.eligible:
            return "Excluded: " + "; ".join(self.exclusions)
        return f"#{self.rank}, score {self.score:+.1f} ({self.breakdown}). {self.note}"


# ------------------------------------------------------------- helpers


def money(value: float) -> str:
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= size:
            return f"{value / size:,.1f}{suffix}"
    return f"{value:,.0f}"


def _period(days: int) -> str:
    return {7: "this week", 30: "this month"}.get(days, f"over {days} days")


def _sign_points(value: float, threshold: float) -> int:
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def book_stats(exact: L2Book, grouped: L2Book, band_pct: float) -> BookStats | None:
    """Spread from the full-precision book; depth from the grouped book, whose 20 levels
    reach far enough from the price to cover the band."""
    if not exact.bids or not exact.asks:
        return None
    bid, ask = exact.bids[0].price, exact.asks[0].price
    mid = (bid + ask) / 2
    low, high = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
    return BookStats(
        spread_pct=(ask - bid) / mid * 100,
        bid_depth_usd=sum(level.price * level.size for level in grouped.bids if level.price >= low),
        ask_depth_usd=sum(level.price * level.size for level in grouped.asks if level.price <= high),
    )


def select_universe(market: Sequence[MarketCoin], cfg: ScannerSettings) -> list[MarketCoin]:
    active = sorted((c for c in market if not c.is_delisted), key=lambda c: c.volume_24h_usd, reverse=True)
    return active[: cfg.universe_size]


def basic_exclusions(coin: MarketCoin, cfg: ScannerSettings) -> list[str]:
    """Filters that need only the market table (checked before spending requests on order books)."""
    reasons = []
    if coin.coin.upper() in {s.upper() for s in cfg.stablecoins}:
        reasons.append("a stablecoin (pegged to US$1, nothing to trade)")
    if coin.coin.upper() in {s.upper() for s in cfg.exclude_coins}:
        reasons.append("on your exclude list")
    if coin.volume_24h_usd < cfg.min_24h_volume_usd:
        reasons.append(f"24h volume US${money(coin.volume_24h_usd)} is below the US${money(cfg.min_24h_volume_usd)} minimum")
    if coin.open_interest_usd < cfg.min_open_interest_usd:
        reasons.append(
            f"open interest US${money(coin.open_interest_usd)} is below the US${money(cfg.min_open_interest_usd)} minimum"
        )
    return reasons


# ---------------------------------------------------------- evaluation


def _return(closes: pd.Series, price_now: float, days: int) -> float:
    """% change from the close `days` days ago to now."""
    return (price_now / closes.iloc[-days] - 1) * 100


def evaluate_coin(
    coin: MarketCoin,
    daily: pd.DataFrame,
    btc_daily: pd.DataFrame,
    btc_price: float,
    book: BookStats | None,
    cfg: ScannerSettings,
    now_ms: int,
    check_book: bool = True,
) -> CoinScan:
    """Check the filters and measure one coin. `daily` = finished daily candles, oldest first.

    check_book=False skips the order-book filters (the backtester has no past order books).
    """
    exclusions = basic_exclusions(coin, cfg)
    if exclusions:  # no candles or order book are fetched for these, so stop here
        return CoinScan(coin.coin, coin.volume_24h_usd, exclusions, {}, 0.0, {}, "")
    price = coin.mark_price

    # Listing age (Hyperliquid's first daily candle for the coin is its listing day)
    age_days = (now_ms - int(daily.index[0].timestamp() * 1000)) / DAY_MS if len(daily) else 0.0
    if age_days < cfg.min_listing_days:
        exclusions.append(f"listed only {age_days:.0f} days ago (minimum {cfg.min_listing_days})")

    # Order book
    if book is None:
        if check_book:
            exclusions.append("order book unavailable")
    else:
        if book.spread_pct > cfg.max_spread_pct:
            exclusions.append(f"spread {book.spread_pct:.2f}% is wider than {cfg.max_spread_pct:.2f}%")
        if book.depth_usd < cfg.min_depth_usd:
            exclusions.append(
                f"only US${money(book.depth_usd)} of orders within ±{cfg.depth_band_pct:g}% of the price "
                f"(minimum US${money(cfg.min_depth_usd)})"
            )

    needed = max(cfg.slow_ma_days - 1, cfg.rs_long_days, cfg.volume_avg_days, cfg.atr_days + 1)
    if len(daily) < needed or len(btc_daily) < cfg.rs_long_days:
        if not any(e.startswith("listed only") for e in exclusions):
            exclusions.append(f"not enough price history ({len(daily)} of {needed} days)")
        return CoinScan(coin.coin, coin.volume_24h_usd, exclusions, {}, 0.0, {}, "", details={"age_days": age_days})

    closes = pd.concat([daily["close"], pd.Series([price])], ignore_index=True)
    fast = float(moving_average(closes, cfg.fast_ma_days).iloc[-1])
    slow = float(moving_average(closes, cfg.slow_ma_days).iloc[-1])
    atr_pct = float(average_true_range(daily, cfg.atr_days).iloc[-1] / daily["close"].iloc[-1] * 100)
    normal_volume = float(daily["dollar_volume"].iloc[-cfg.volume_avg_days :].mean())
    metrics: dict[str, float | None] = {
        "price": price,
        "return_short_pct": _return(daily["close"], price, cfg.rs_short_days),
        "rs_short_pct": _return(daily["close"], price, cfg.rs_short_days)
        - _return(btc_daily["close"], btc_price, cfg.rs_short_days),
        "rs_long_pct": _return(daily["close"], price, cfg.rs_long_days)
        - _return(btc_daily["close"], btc_price, cfg.rs_long_days),
        "volume_ratio": coin.volume_24h_usd / normal_volume if normal_volume else None,
        "fast_ma": fast,
        "slow_ma": slow,
        "atr_pct": atr_pct,
        "spread_pct": book.spread_pct if book else None,
        "depth_usd": book.depth_usd if book else None,
        "age_days": age_days,
    }

    # Stablecoins not on the list: priced ~US$1 and barely moving
    if 0.97 <= price <= 1.03 and atr_pct < 0.5:
        exclusions.append("behaves like a stablecoin (stays near US$1)")

    is_benchmark = coin.coin == BENCHMARK
    volume_ratio = metrics["volume_ratio"] or 0.0
    points = {
        "rs_short": 0 if is_benchmark else _sign_points(metrics["rs_short_pct"], cfg.rs_threshold_pct),
        "rs_long": 0 if is_benchmark else _sign_points(metrics["rs_long_pct"], cfg.rs_threshold_pct),
        "volume": 1 if volume_ratio >= cfg.volume_surge_ratio else -1 if volume_ratio <= cfg.volume_dry_ratio else 0,
        "trend": (1 if price > fast else -1) + (1 if price > slow else -1),
        "volatility": -1 if atr_pct >= cfg.high_volatility_atr_pct else 0,
    }
    weights = cfg.weights.model_dump()
    score = sum(points[k] * weights[k] for k in points)
    breakdown = ", ".join(f"{k} {points[k]:+d}×{weights[k]:g}" for k in points)
    note = _note(coin.coin, points, metrics, cfg, is_benchmark, price > fast, price > slow)
    return CoinScan(
        coin.coin, coin.volume_24h_usd, exclusions, points, score, metrics, note, breakdown=breakdown,
        details={"points": points, "weights": weights, "metrics": metrics},
    )


def _note(
    coin: str,
    points: Mapping[str, int],
    m: Mapping[str, float | None],
    cfg: ScannerSettings,
    is_benchmark: bool,
    above_fast: bool,
    above_slow: bool,
) -> str:
    parts = []
    ratio = m["volume_ratio"] or 0.0
    if points["volume"] > 0:
        parts.append(f"volume {ratio:.1f}x normal")
    elif points["volume"] < 0:
        parts.append(f"volume drying up ({ratio:.1f}x normal)")
    else:
        parts.append(f"normal volume ({ratio:.1f}x)")

    if is_benchmark:
        parts.append("the benchmark every other coin is compared with")
    else:
        for key, days in (("rs_short", cfg.rs_short_days), ("rs_long", cfg.rs_long_days)):
            gap = m[f"{key}_pct"]
            if points[key] > 0:
                parts.append(f"outperforming BTC {_period(days)} ({gap:+.1f} pts)")
            elif points[key] < 0:
                parts.append(f"lagging BTC {_period(days)} ({gap:+.1f} pts)")
            else:
                parts.append(f"moving with BTC {_period(days)}")

    if above_fast and above_slow:
        parts.append("in an uptrend")
    elif not above_fast and not above_slow:
        parts.append("in a downtrend")
    elif above_fast:
        parts.append(f"recovering but still below its {cfg.slow_ma_days}-day average")
    else:
        parts.append(f"pulling back within an uptrend (below its {cfg.fast_ma_days}-day average)")

    if points["volatility"] < 0:
        parts.append(f"very jumpy (moves ~{m['atr_pct']:.0f}% a day)")
    return f"{coin}: {', '.join(parts)}."


def rank_scans(scans: Sequence[CoinScan]) -> list[CoinScan]:
    """Eligible coins ranked by score (ties: stronger vs BTC this week, then more volume), then excluded coins."""
    eligible = sorted(
        (s for s in scans if s.eligible),
        key=lambda s: (-s.score, -(s.metrics.get("rs_short_pct") or 0.0), -s.volume_24h_usd),
    )
    excluded = sorted((s for s in scans if not s.eligible), key=lambda s: -s.volume_24h_usd)
    return [replace(s, rank=i) for i, s in enumerate(eligible, start=1)] + excluded


def scan(
    universe: Sequence[MarketCoin],
    dailies: Mapping[str, pd.DataFrame],
    books: Mapping[str, BookStats | None],
    cfg: ScannerSettings,
    now_ms: int,
) -> list[CoinScan]:
    """Evaluate and rank every coin in the universe. Needs BTC's daily candles in `dailies`."""
    btc_daily = dailies[BENCHMARK]
    btc_price = next((c.mark_price for c in universe if c.coin == BENCHMARK), float(btc_daily["close"].iloc[-1]))
    empty = pd.DataFrame(columns=["high", "low", "close", "dollar_volume"], index=pd.DatetimeIndex([], tz="UTC"))
    scans = [
        evaluate_coin(c, dailies.get(c.coin, empty), btc_daily, btc_price, books.get(c.coin), cfg, now_ms)
        for c in universe
    ]
    return rank_scans(scans)


def on_shortlist(scan: CoinScan, cfg: ScannerSettings) -> bool:
    """Ranked in the top max_coins AND scoring at least min_score (weak coins stay off even on quiet days)."""
    return scan.rank is not None and scan.rank <= cfg.max_coins and scan.score >= cfg.min_score


def shortlist(scans: Sequence[CoinScan], cfg: ScannerSettings) -> list[CoinScan]:
    return [s for s in scans if on_shortlist(s, cfg)]


# ------------------------------------------------------------- storage


def save_scan(conn: sqlite3.Connection, scans: Sequence[CoinScan], ts_ms: int) -> None:
    """Store every coin of one scan (eligible and excluded) in scan_results. The caller commits."""
    conn.executemany(
        """INSERT INTO scan_results
               (ts_ms, coin, rank, volume_24h_usd, passed, reason, score, details_json, app_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                ts_ms, s.coin, s.rank, s.volume_24h_usd, int(s.eligible), s.reason,
                s.score if s.eligible else None, json.dumps(s.details | {"exclusions": s.exclusions}), APP_VERSION,
            )
            for s in scans
        ],
    )
