"""The live pipeline: market mood -> coin scan -> trade signals.

Shared by the CLI commands and the demo trader, so both run exactly the same steps.
Each function takes an open API client and database connection.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pandas as pd

from scout.config import Settings
from scout.data import (
    INTERVAL_MS,
    HyperliquidClient,
    HyperliquidError,
    MarketCoin,
    backfill,
    load_candles,
)
from scout.db import log_event, now_ms
from scout.indicators import candle_frame
from scout.regime import NotEnoughData, RegimeReading, detect, save_reading
from scout.scanner import (
    BENCHMARK,
    CoinScan,
    basic_exclusions,
    book_stats,
    save_scan,
    select_universe,
    shortlist,
)
from scout.scanner import scan as run_scanner
from scout.signals import (
    Account,
    Action,
    CoinContext,
    MarketContext,
    OpenPosition,
    Signal,
    generate_signals,
    make_strategy,
    save_signals,
)

log = logging.getLogger(__name__)
Echo = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


def by_volume(coins: Sequence[MarketCoin]) -> list[MarketCoin]:
    return sorted((c for c in coins if not c.is_delisted), key=lambda c: c.volume_24h_usd, reverse=True)


def btc_history_days(settings: Settings) -> int:
    cfg = settings.regime
    return max(cfg.slow_ma_days + cfg.slope_days, cfg.volatility_history_days + cfg.atr_days) + 5


async def refresh_daily(client: HyperliquidClient, conn: sqlite3.Connection, coins: dict[str, int],
                        echo: Echo = _quiet) -> None:
    """Make sure each coin has `days` of daily candles stored (downloads only what's missing)."""
    echo(f"Refreshing daily candles for {len(coins)} coins…")
    for coin, days in coins.items():
        await backfill(client, conn, coin, "1d", days)


def closes(frames: dict[str, pd.DataFrame], coins: Sequence[str], column: str = "close") -> pd.DataFrame:
    return pd.DataFrame({c: frames[c][column] for c in coins if not frames[c].empty})


async def current_mood(settings: Settings, client: HyperliquidClient, conn: sqlite3.Connection,
                       refresh: bool = True, echo: Echo = _quiet) -> RegimeReading:
    """Judge the market now and save the reading."""
    cfg = settings.regime
    market = by_volume(await client.market())
    top = market[: cfg.breadth_top_coins]
    universe = [c.coin for c in top]
    prices = {c.coin: c.mark_price for c in market}
    if cfg.benchmark_coin not in prices:
        raise NotEnoughData(f"{cfg.benchmark_coin} isn't trading on Hyperliquid")
    if refresh:
        wanted = {coin: cfg.breadth_ma_days + 5 for coin in universe}
        wanted[cfg.benchmark_coin] = btc_history_days(settings)
        await refresh_daily(client, conn, wanted, echo)
    frames = {c: candle_frame(load_candles(conn, c, "1d")) for c in {cfg.benchmark_coin, *universe}}
    reading = detect(
        frames[cfg.benchmark_coin], prices[cfg.benchmark_coin], closes(frames, universe),
        {c: prices[c] for c in universe}, [c.funding_rate for c in top], cfg, now_ms(),
    )
    save_reading(conn, reading)
    log_event(conn, "INFO", "regime", f"{reading.regime} / {reading.volatility}: {reading.summary}")
    conn.commit()
    log.info("mood %s, volatility %s, score %+d", reading.regime, reading.volatility, reading.score)
    return reading


async def order_book(client: HyperliquidClient, coin: str, band_pct: float):
    try:
        return book_stats(await client.l2_book(coin), await client.l2_book(coin, sig_figs=3), band_pct)
    except HyperliquidError as exc:
        log.warning("order book for %s unavailable: %s", coin, exc)
        return None


async def run_scan(settings: Settings, client: HyperliquidClient, conn: sqlite3.Connection,
                   refresh: bool = True, echo: Echo = _quiet) -> tuple[list[CoinScan], int]:
    """Rank the top coins, save every result, and return (scans, timestamp)."""
    cfg = settings.scanner
    universe = select_universe(await client.market(), cfg)
    # Only spend requests (candles, order books) on coins that pass the cheap filters.
    candidates = [c.coin for c in universe if not basic_exclusions(c, cfg)]
    if refresh:
        await refresh_daily(client, conn, {coin: cfg.history_days for coin in [BENCHMARK, *candidates]}, echo)
    dailies = {coin: candle_frame(load_candles(conn, coin, "1d")) for coin in {BENCHMARK, *candidates}}
    books = {coin: await order_book(client, coin, cfg.depth_band_pct) for coin in candidates}
    ts = now_ms()
    scans = run_scanner(universe, dailies, books, cfg, ts)
    save_scan(conn, scans, ts)
    best = ", ".join(s.coin for s in shortlist(scans, cfg))
    log_event(conn, "INFO", "scan", f"shortlist: {best}", {"checked": len(scans)})
    conn.commit()
    log.info("scan of %d coins, shortlist: %s", len(scans), best)
    return scans, ts


async def coin_contexts(settings: Settings, client: HyperliquidClient, conn: sqlite3.Connection,
                        coins: Sequence[str], prices: dict[str, float], sz_decimals: dict[str, int],
                        refresh: bool = True, echo: Echo = _quiet) -> dict[str, CoinContext]:
    """Recent signal-timeframe and daily candles for each coin, with its price now."""
    cfg = settings.signals
    if refresh and coins:
        echo(f"Refreshing {cfg.timeframe} candles for {len(coins)} coins…")
        for coin in coins:
            await backfill(client, conn, coin, cfg.timeframe, cfg.candles_needed_days)
            await backfill(client, conn, coin, "1d", cfg.trend_slow_ma_days + 10)
    ts = now_ms()
    day = INTERVAL_MS["1d"]
    return {
        coin: CoinContext(
            coin,
            candle_frame(load_candles(conn, coin, cfg.timeframe, ts - cfg.candles_needed_days * day)),
            candle_frame(load_candles(conn, coin, "1d", ts - (cfg.trend_slow_ma_days + 10) * day)),
            prices[coin],
            sz_decimals.get(coin, 4),
        )
        for coin in coins
        if coin in prices
    }


@dataclass(frozen=True)
class PipelineResult:
    reading: RegimeReading
    shortlist: list[str]
    signals: list[Signal]
    market: dict[str, MarketCoin]


async def run_signals(settings: Settings, client: HyperliquidClient, conn: sqlite3.Connection,
                      account: Account, positions: Sequence[OpenPosition], refresh: bool = True,
                      prices: dict[str, float] | None = None, echo: Echo = _quiet) -> PipelineResult:
    """Mood, scan, then signals for the shortlist and open positions. Signals are saved (not executed).

    `prices` (e.g. live websocket prices) override the market table's mark prices when given.
    """
    reading = await current_mood(settings, client, conn, refresh, echo)
    scans, _ = await run_scan(settings, client, conn, refresh, echo)
    chosen = [s.coin for s in shortlist(scans, settings.scanner)]
    market = {c.coin: c for c in await client.market()}
    now_prices = {coin: c.mark_price for coin, c in market.items()} | (prices or {})
    coins = list(dict.fromkeys([*[p.coin for p in positions], *chosen]))
    contexts = await coin_contexts(settings, client, conn, coins, now_prices,
                                   {coin: c.sz_decimals for coin, c in market.items()}, refresh, echo)
    signals = generate_signals(make_strategy(settings.signals), MarketContext.from_reading(reading), contexts,
                               chosen, positions, account, settings, now_ms())
    new = save_signals(conn, signals)
    for s in signals:
        if s.action not in (Action.HOLD, Action.SKIP):
            log_event(conn, "INFO", "signal", s.reason, {"coin": s.coin, "action": s.action})
    conn.commit()
    log.info("%d signals (%d new saved)", sum(s.action is not Action.HOLD for s in signals), new)
    return PipelineResult(reading, chosen, signals, market)
