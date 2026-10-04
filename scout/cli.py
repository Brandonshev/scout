"""Command line: `uv run scout --help`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import re
import secrets
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from scout import copytrade, news, pipeline, service, smart, status_page
from scout.backtest import (
    BacktestResult,
    DayPlan,
    History,
    WalkForwardResult,
    build_plans,
    earliest_start,
    plot_equity,
    simulate,
    stats_table,
    verdict,
    walk_forward,
    write_trades_csv,
)
from scout.backup import backup_db
from scout.config import Mode, Settings, load_settings, summary_lines
from scout.dashboard_data import open_readonly as data_readonly
from scout.data import (
    INTERVAL_MS,
    HyperliquidClient,
    HyperliquidError,
    MarketCoin,
    backfill,
    coverage_from,
    format_price,
    history_floor,
    load_candles,
    save_market_snapshot,
    stored_range,
    stream_mids,
)
from scout.db import init_db, now_ms, open_db
from scout.demo import DemoEngine, DemoRunner, demo_account_snapshot, single_instance
from scout.doctor import run_checks
from scout.experiment import Experiment, experiment_settings, scorecard_text
from scout.explain import explain_trade, list_trades
from scout.smart_trader import SmartTrader, smart_settings
from scout.smart_trader import update_text as smart_update_text
from scout.indicators import candle_frame
from scout.logging_setup import setup_logging
from scout.notify import APPLESCRIPT, Notifier, NotifyError, backends_from_settings
from scout.regime import (
    NotEnoughData,
    Regime,
    RegimeReading,
    history,
    plot_history,
    regime_stats,
)
from scout.replay import Control, ReplayControl, ReplayRunner, SimClock, parse_speed
from scout.reports import report_message, report_text, save_report, weekly_report
from scout.risk import BotState
from scout.scanner import (
    BENCHMARK,
    CoinScan,
    on_shortlist,
    shortlist,
)
from scout.service import keep_awake, rotate_service_logs
from scout.signals import (
    Action,
    Signal,
    load_open_positions,
)
from scout.tax import (
    financial_year,
    fy_bounds,
    refresh_rates,
    tax_rows,
    write_notes,
    write_tax_csv,
)
from scout.tax import summary as tax_summary
from scout.version import APP_VERSION

app = typer.Typer(
    name="scout",
    help="Scout: an explainable, risk-first crypto trading bot (demo mode by default).",
    no_args_is_help=True,
    add_completion=False,
)
log = logging.getLogger("scout.cli")

ConfigOption = Annotated[Path, typer.Option("--config", "-c", help="Path to config.yaml.")]
EnvFileOption = Annotated[Path, typer.Option("--env-file", help="Path to the .env secrets file.")]


def _load_or_exit(config: Path, env_file: Path) -> Settings:
    try:
        return load_settings(config, env_file)
    except FileNotFoundError as exc:
        typer.secho(f"✗ {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    except ValidationError as exc:
        typer.secho(f"✗ {exc.error_count()} problem(s) in the config:", err=True, fg=typer.colors.RED)
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"]) or "(top level)"
            typer.echo(f"  - {where}: {error['msg']}", err=True)
        raise typer.Exit(1) from None


def _by_service() -> bool:
    return os.environ.get("LAUNCHED_BY_SCOUT_SERVICE") == "1"


def _start(config: Path, env_file: Path) -> Settings:
    """Load config, start logging and announce the version."""
    settings = _load_or_exit(config, env_file)
    # Under launchd, stderr goes to logs/service.err.log: keep it for crashes, not every log line.
    log_file = setup_logging(settings, console=not _by_service())
    typer.echo(f"Scout v{APP_VERSION} | {settings.mode.value.upper()} mode")
    log.info("Scout v%s starting in %s mode (log file %s)", APP_VERSION, settings.mode.value, log_file)
    return settings


@app.command()
def version() -> None:
    """Print Scout's version."""
    typer.echo(f"Scout v{APP_VERSION} (Python {platform.python_version()})")


@app.command("config-check")
def config_check(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Load and validate config.yaml + .env, then summarise the settings in plain English."""
    settings = _start(config, env_file)
    for line in summary_lines(settings):
        typer.echo(f"  {line}")
    typer.secho("✓ Config OK", fg=typer.colors.GREEN)
    log.info("config check passed")


@app.command("init-db")
def init_db_command(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Create the SQLite database and any missing tables. Safe to run again: existing data is kept."""
    settings = _start(config, env_file)
    tables = init_db(settings.app.db_path)
    log.info("database ready at %s (%d tables)", settings.app.db_path, len(tables))
    typer.secho(f"✓ Database ready: {settings.app.db_path}", fg=typer.colors.GREEN)
    typer.echo(f"  Tables: {', '.join(tables)}")


# ------------------------------------------------------------ market data


def make_client(settings: Settings) -> HyperliquidClient:
    """Build the API client (tests replace this to run offline)."""
    return HyperliquidClient.from_settings(settings)


def _date(ms: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%d %b %Y %H:%M")


def _money(value: float) -> str:
    """US$ amounts in millions/billions, e.g. 1.95B, 215.9M."""
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            return f"{value / size:,.2f}{suffix}"
    return f"{value:,.0f}"


def _by_volume(coins: list[MarketCoin]) -> list[MarketCoin]:
    return pipeline.by_volume(coins)


@app.command()
def fetch(
    coin: Annotated[
        list[str] | None, typer.Option("--coin", help="Coin to download, e.g. BTC. Repeat for more coins.")
    ] = None,
    top: Annotated[
        int | None, typer.Option("--top", min=1, help="Download the N coins with the highest 24h volume.")
    ] = None,
    interval: Annotated[
        list[str] | None, typer.Option("--interval", "-i", help="Candle size: 15m, 1h, 4h or 1d. Repeatable.")
    ] = None,
    days: Annotated[int | None, typer.Option("--days", min=1, help="How many days of history.")] = None,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Download past candles into the database. Only missing candles are fetched."""
    settings = _start(config, env_file)
    intervals = interval or settings.data.backfill_intervals
    unknown = [i for i in intervals if i not in INTERVAL_MS]
    if unknown:
        typer.secho(f"✗ unsupported interval(s): {', '.join(unknown)} (use {', '.join(INTERVAL_MS)})", fg="red", err=True)
        raise typer.Exit(1)
    days = days or settings.data.backfill_days
    try:
        asyncio.run(_fetch(settings, coin or [], top, intervals, days))
    except HyperliquidError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None


async def _fetch(settings: Settings, coins: list[str], top: int | None, intervals: list[str], days: int) -> None:
    tz = settings.app.tz
    async with make_client(settings) as client:
        market = _by_volume(await client.market())
        names = {c.coin.lower(): c.coin for c in market}
        if coins:
            missing = [c for c in coins if c.lower() not in names]
            if missing:
                typer.secho(f"✗ unknown or delisted coin(s) on Hyperliquid: {', '.join(missing)}", fg="red", err=True)
                raise typer.Exit(1)
            chosen = [names[c.lower()] for c in coins]
        else:
            chosen = [c.coin for c in market[: top or settings.data.backfill_top_coins]]

        typer.echo(f"Downloading {days} days of {', '.join(intervals)} candles for {', '.join(chosen)}")
        with closing(open_db(settings.app.db_path)) as conn:
            for name in chosen:
                for iv in intervals:
                    result = await backfill(client, conn, name, iv, days)
                    start = max(result.requested_from_ms, result.available_from_ms)
                    typer.echo(
                        f"  {name:>6} {iv:>3}: {result.new:>5,} new, {result.fetched:>5,} downloaded "
                        f"(from {_date(start, tz)})"
                    )
                    if result.limited_by_history:
                        held = (now_ms() - result.available_from_ms) / INTERVAL_MS["1d"]
                        typer.secho(
                            f"         ⚠ Hyperliquid only keeps the latest 5000 {iv} candles "
                            f"(≈{held:.0f} days), not the {days} days asked for.",
                            fg="yellow",
                        )
                    log.info("backfill %s %s: %d new of %d", name, iv, result.new, result.fetched)


@app.command()
def market(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="How many coins to show.")] = 25,
    save: Annotated[bool, typer.Option("--save/--no-save", help="Store a snapshot in the database.")] = True,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Show coins sorted by 24h trading volume, with price, 24h change, funding and open interest."""
    settings = _start(config, env_file)
    try:
        coins = asyncio.run(_market(settings))
    except HyperliquidError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None

    ts = now_ms()
    if save:
        with closing(open_db(settings.app.db_path)) as conn:
            saved = save_market_snapshot(conn, coins, ts)
            conn.commit()
        log.info("saved market snapshot of %d coins", saved)

    active = _by_volume(coins)
    table = Table(
        title=f"Hyperliquid perps by 24h volume (US$) — {_date(ts, settings.app.tz)} Sydney",
        caption="Funding ≈ yearly = hourly rate × 24 × 365, if it stayed the same all year.",
    )
    for header in ("#", "Coin", "Price", "24h", "Volume 24h", "Funding/hr", "≈ yearly", "Open int."):
        table.add_column(header, justify="left" if header == "Coin" else "right", no_wrap=True)
    for rank, c in enumerate(active[:limit], start=1):
        change = f"{c.change_24h_pct:+.2f}%"
        table.add_row(
            str(rank),
            c.coin,
            format_price(c.mark_price),
            f"[green]{change}[/]" if c.change_24h_pct >= 0 else f"[red]{change}[/]",
            _money(c.volume_24h_usd),
            f"{c.funding_rate * 100:+.4f}%",
            f"{c.funding_annual_pct:+.1f}%",
            _money(c.open_interest_usd),
        )
    Console().print(table)
    typer.echo(f"{len(active)} active coins ({len(coins) - len(active)} delisted hidden).")


async def _market(settings: Settings) -> list[MarketCoin]:
    async with make_client(settings) as client:
        return await client.market()


@app.command()
def watch(
    coin: Annotated[list[str] | None, typer.Option("--coin", help="Coin to watch. Repeatable.")] = None,
    seconds: Annotated[float, typer.Option("--seconds", min=1, help="Stop after this many seconds.")] = 30,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Show live prices from the websocket stream (reconnects automatically)."""
    settings = _start(config, env_file)
    coins = coin or ["BTC", "ETH"]
    try:
        asyncio.run(_watch(settings, coins, seconds))
    except TimeoutError:
        pass
    typer.echo("Stopped.")


async def _watch(settings: Settings, coins: list[str], seconds: float) -> None:
    last_print = 0.0
    async with asyncio.timeout(seconds):
        async for mids in stream_mids(settings.data.ws_url):
            if time.monotonic() - last_print < 1:
                continue
            last_print = time.monotonic()
            now = datetime.now(settings.app.tz).strftime("%H:%M:%S")
            prices = "  ".join(f"{c} {format_price(mids[c])}" if c in mids else f"{c} ?" for c in coins)
            typer.echo(f"{now}  {prices}")


# ------------------------------------------------------------ market mood

MOOD_COLOURS = {Regime.RISK_ON: typer.colors.GREEN, Regime.NEUTRAL: typer.colors.YELLOW, Regime.RISK_OFF: typer.colors.RED}


def _btc_history_days(settings: Settings) -> int:
    return pipeline.btc_history_days(settings)


async def _current_mood(settings: Settings, refresh: bool = True) -> RegimeReading:
    async with make_client(settings) as client:
        with closing(open_db(settings.app.db_path)) as conn:
            return await pipeline.current_mood(settings, client, conn, refresh, typer.echo)


def _print_reading(reading: RegimeReading, tz: ZoneInfo) -> None:
    typer.echo()
    typer.secho(
        f"Market mood: {reading.regime}   Volatility: {reading.volatility}   Score: {reading.score:+d}",
        fg=MOOD_COLOURS[reading.regime],
        bold=True,
    )
    typer.echo(f"as of {_date(reading.ts_ms, tz)} Sydney\n")
    typer.echo(textwrap.fill(reading.summary, 100))
    typer.echo("\nWhy:")
    for reason in reading.reasons:
        typer.echo(textwrap.fill(reason, 100, initial_indent="  • ", subsequent_indent="    "))
    longs = "ALLOWED" if reading.allows_new_longs else "BLOCKED"
    typer.echo(f"\nRules for trading: new long trades {longs}; position size ×{reading.size_multiplier:g}")


@app.command()
def mood(
    watch: Annotated[bool, typer.Option("--watch", help="Re-check every regime.update_minutes until Ctrl+C.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh", help="Download missing candles first.")] = True,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Show the current market mood (RISK_ON / NEUTRAL / RISK_OFF) and why. Saved to regime_history."""
    settings = _start(config, env_file)
    try:
        if watch:
            asyncio.run(
                _every(settings, settings.regime.update_minutes, "mood check",
                       lambda: _show_current_mood(settings))
            )
        else:
            _print_reading(asyncio.run(_current_mood(settings, refresh)), settings.app.tz)
    except (HyperliquidError, NotEnoughData) as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        typer.echo("Stopped.")


async def _show_current_mood(settings: Settings) -> None:
    _print_reading(await _current_mood(settings), settings.app.tz)


async def _every(
    settings: Settings, minutes: int, label: str, job: Callable[[], Awaitable[None]]
) -> None:
    """Run `job` every `minutes` until Ctrl+C. Each run refreshes its own data first."""
    while True:
        started = time.time()
        try:
            await job()
        except HyperliquidError as exc:
            log.error("%s failed: %s (will retry next round)", label, exc)
        next_at = started + minutes * 60
        typer.echo(f"\nNext check at {_date(int(next_at * 1000), settings.app.tz)} (Ctrl+C to stop)")
        # Sleep in short steps against the wall clock: if the Mac sleeps, we notice as soon as it wakes.
        while (remaining := next_at - time.time()) > 0:
            await asyncio.sleep(min(remaining, 30))
        overdue = time.time() - next_at
        if overdue > settings.data.stale_after_seconds:
            log.warning("%s was %.0f min overdue (Mac asleep?): refreshing data before deciding", label, overdue / 60)


@app.command("mood-history")
def mood_history(
    days: Annotated[int, typer.Option("--days", min=30, help="How many past days to show.")] = 365,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Where to save the PNG chart.")] = None,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh", help="Download missing candles first.")] = True,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Work out the mood for each past day and save a chart of it against the BTC price."""
    settings = _start(config, env_file)
    try:
        readings = asyncio.run(_mood_history(settings, days, refresh))
    except (HyperliquidError, NotEnoughData) as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    if not readings:
        typer.secho("✗ not enough history to judge any past days", fg="red", err=True)
        raise typer.Exit(1)

    cfg = settings.regime
    path = output or settings.app.reports_dir / f"mood_history_{datetime.now(settings.app.tz):%Y%m%d_%H%M}.png"
    note = (
        f"Breadth: top {cfg.breadth_top_coins} coins by volume at the time, from today's top "
        f"{cfg.history_candidate_coins} (delisted coins missing, so slightly optimistic). "
        "Crowding (funding) isn't included in history."
    )
    plot_history(readings, cfg, path, note)

    tz = settings.app.tz
    typer.echo(f"\nMood for {len(readings)} days, {_date(readings[0].ts_ms, tz)} → {_date(readings[-1].ts_ms, tz)}:")
    for regime, stats in regime_stats(readings).items():
        move = "n/a" if stats.avg_next_day_pct is None else f"{stats.avg_next_day_pct:+.2f}%"
        typer.secho(
            f"  {regime:<9} {stats.days:>4} days ({stats.share_pct:4.0f}%)   BTC's average next-day move: {move}",
            fg=MOOD_COLOURS[regime],
        )
    wild = sum(r.volatility == "WILD" for r in readings)
    typer.echo(f"  Wild volatility on {wild} days ({100 * wild / len(readings):.0f}%)")
    typer.echo("  (Next-day moves are a first hint only. Proper testing with fees comes in v0.6.0.)")
    typer.echo(f"  Note: {note}")
    typer.secho(f"✓ Chart saved: {path}", fg="green")
    log.info("mood history chart for %d days saved to %s", len(readings), path)


async def _mood_history(settings: Settings, days: int, refresh: bool) -> list[RegimeReading]:
    cfg = settings.regime
    async with make_client(settings) as client:
        pool = [c.coin for c in _by_volume(await client.market())[: cfg.history_candidate_coins]]
        with closing(open_db(settings.app.db_path)) as conn:
            if refresh:
                wanted = {coin: days + cfg.breadth_ma_days + 35 for coin in pool}
                wanted[cfg.benchmark_coin] = days + _btc_history_days(settings)
                await pipeline.refresh_daily(client, conn, wanted, typer.echo)
            frames = {c: candle_frame(load_candles(conn, c, "1d")) for c in {cfg.benchmark_coin, *pool}}
    btc = frames[cfg.benchmark_coin]
    return history(btc, pipeline.closes(frames, pool), pipeline.closes(frames, pool, "dollar_volume"), cfg, days)


# ------------------------------------------------------------ coin scanner


async def _run_scan(settings: Settings, refresh: bool = True) -> tuple[list[CoinScan], int]:
    async with make_client(settings) as client:
        with closing(open_db(settings.app.db_path)) as conn:
            return await pipeline.run_scan(settings, client, conn, refresh, typer.echo)


def _print_scan(scans: list[CoinScan], ts: int, settings: Settings, show_all: bool) -> None:
    cfg = settings.scanner
    chosen = shortlist(scans, cfg)
    eligible = [s for s in scans if s.eligible]
    excluded = [s for s in scans if not s.eligible]
    shown = eligible if show_all else chosen
    table = Table(
        title=f"Coin scan — {_date(ts, settings.app.tz)} Sydney: {len(scans)} checked, "
        f"{len(eligible)} passed filters, top {len(chosen)} shortlisted",
    )
    for header in ("#", "Coin", "Score", "7d vs BTC", "30d vs BTC", "Volume", "Trend", "Moves/day", "Spread", "Depth ±1%"):
        table.add_column(header, justify="left" if header == "Coin" else "right", no_wrap=True)
    trend_words = {2: "[green]up[/]", -2: "[red]down[/]"}
    for s in shown:
        m = s.metrics
        table.add_row(
            str(s.rank),
            s.coin if on_shortlist(s, cfg) else f"[dim]{s.coin}[/]",
            f"{s.score:+.1f}",
            "—" if s.coin == BENCHMARK else f"{m['rs_short_pct']:+.1f}",
            "—" if s.coin == BENCHMARK else f"{m['rs_long_pct']:+.1f}",
            f"{m['volume_ratio']:.1f}x",
            trend_words.get(s.points["trend"], "mixed"),
            f"{m['atr_pct']:.1f}%",
            f"{m['spread_pct']:.3f}%",
            _money(m["depth_usd"]),
        )
    Console().print(table)
    typer.echo("\nWhy each shortlisted coin is there:")
    for s in chosen:
        typer.echo(textwrap.fill(f"{s.rank:>2}. {s.note}", 100, subsequent_indent="    "))
    if excluded:
        typer.echo(f"\nExcluded ({len(excluded)}):")
        for s in excluded:
            typer.echo(textwrap.fill(f"  {s.coin}: {'; '.join(s.exclusions)}", 100, subsequent_indent="    "))
    typer.echo("\n(\"7d/30d vs BTC\" = the coin's % change minus Bitcoin's, in percentage points.)")


@app.command()
def scan(
    watch: Annotated[bool, typer.Option("--watch", help="Re-scan every scanner.update_minutes until Ctrl+C.")] = False,
    show_all: Annotated[bool, typer.Option("--all", help="Show every coin that passed, not just the shortlist.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh", help="Download missing candles first.")] = True,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Rank the top coins by volume and shortlist the best ones to trade, with a reason for each."""
    settings = _start(config, env_file)

    async def once() -> None:
        scans, ts = await _run_scan(settings, refresh)
        _print_scan(scans, ts, settings, show_all)

    try:
        if watch:
            asyncio.run(_every(settings, settings.scanner.update_minutes, "scan", once))
        else:
            asyncio.run(once())
    except HyperliquidError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        typer.echo("Stopped.")


# ------------------------------------------------------------ trade signals


async def _run_signals(settings: Settings, refresh: bool = True) -> tuple[RegimeReading, list[str], list[Signal]]:
    async with make_client(settings) as client:
        with closing(open_db(settings.app.db_path)) as conn:
            positions = load_open_positions(conn)
            account = demo_account_snapshot(settings, conn)
            result = await pipeline.run_signals(settings, client, conn, account, positions, refresh, echo=typer.echo)
    return result.reading, result.shortlist, result.signals


SIGNAL_STYLE = {
    Action.ENTER_LONG: ("BUY", typer.colors.GREEN),
    Action.ENTER_SHORT: ("SHORT", typer.colors.MAGENTA),
    Action.EXIT: ("SELL", typer.colors.RED),
    Action.MOVE_STOP: ("STOP↑", typer.colors.CYAN),
}


def _print_signals(reading: RegimeReading, chosen: list[str], signals: list[Signal], settings: Settings) -> None:
    longs = "allowed" if reading.allows_new_longs else "blocked"
    shorts = "allowed" if settings.signals.allow_shorts and reading.regime is Regime.RISK_OFF else "off"
    typer.secho(
        f"\nMarket mood: {reading.regime} (volatility {reading.volatility}) — new longs {longs}, shorts {shorts}",
        fg=MOOD_COLOURS[reading.regime],
    )
    typer.echo(f"Shortlist: {', '.join(chosen) or '(empty)'}")
    typer.echo(f"Strategy: {settings.signals.strategy} on {settings.signals.timeframe} candles\n")

    def show(items: list[Signal], title: str) -> None:
        if not items:
            return
        typer.secho(title, bold=True)
        for s in items:
            label, colour = SIGNAL_STYLE.get(s.action, ("", None))
            prefix = f"  {label:<6}" if label else "  "
            typer.secho(textwrap.fill(f"{prefix}{s.reason}", 100, subsequent_indent="         "), fg=colour)
        typer.echo()

    actions = [s for s in signals if s.action in SIGNAL_STYLE]
    show(actions, "Signals:")
    if not actions:
        typer.echo("No trade signals right now.\n")
    show([s for s in signals if s.action is Action.SKIP], "Setups skipped (a rule said no):")
    show([s for s in signals if s.action is Action.HOLD], "Watching:")
    typer.echo("Demo mode: nothing is traded yet. Demo trading arrives in v0.7.0.")


@app.command()
def signals(
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh", help="Download missing candles first.")] = True,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Check the market mood, scan for coins, and show current trade signals with reasons."""
    settings = _start(config, env_file)
    try:
        reading, chosen, found = asyncio.run(_run_signals(settings, refresh))
    except (HyperliquidError, NotEnoughData) as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    _print_signals(reading, chosen, found, settings)


# ---------------------------------------------------------------- backtest


async def _load_history(
    settings: Settings, start: pd.Timestamp, end: pd.Timestamp, refresh: bool
) -> tuple[History, dict[pd.Timestamp, DayPlan]]:
    """Daily candles for every coin Hyperliquid has listed (delisted ones too), then signal candles
    for the coins that ever made the shortlist. Also returns the daily mood/shortlist plans."""
    tf = settings.signals.timeframe
    now = now_ms()
    days_back = int((now - start.timestamp() * 1000) / INTERVAL_MS["1d"]) + 1
    btc_days = days_back + _btc_history_days(settings)
    pool_days = days_back + settings.scanner.history_days + 35
    since = int(start.timestamp() * 1000) - (pool_days - days_back) * INTERVAL_MS["1d"]
    async with make_client(settings) as client:
        listing = await client.market()
        delisted = {c.coin for c in listing if c.is_delisted}
        with closing(open_db(settings.app.db_path)) as conn:
            if refresh:
                todo = [c.coin for c in listing if _needs_refresh(
                    conn, c.coin, "1d", c.coin in delisted, settings.scanner.min_24h_volume_usd, now,
                    now - (btc_days if c.coin == BENCHMARK else pool_days) * INTERVAL_MS["1d"])]
                typer.echo(f"Updating daily candles for {len(todo)} of {len(listing)} coins "
                           "(delisted coins included; the first run takes a while)…")
                for n, coin in enumerate(todo, start=1):
                    await backfill(client, conn, coin, "1d", btc_days if coin == BENCHMARK else pool_days)
                    if n % 25 == 0:
                        typer.echo(f"  {n}/{len(todo)}")
            daily = {c.coin: candle_frame(load_candles(conn, c.coin, "1d", since if c.coin != BENCHMARK else 0))
                     for c in listing}
            daily = {coin: frame for coin, frame in daily.items() if not frame.empty}

            typer.echo("Working out the mood and shortlist for each day…")
            data = History(daily)
            plans = build_plans(data, settings, start, end, progress=typer.echo)
            tradeable = sorted({BENCHMARK, *(coin for plan in plans.values() for coin in plan.shortlist)})
            if refresh:
                first_needed = int((start - pd.Timedelta(days=settings.backtest.warmup_days + 5)).timestamp() * 1000)
                todo = [c for c in tradeable if _needs_refresh(conn, c, tf, c in delisted, 0, now, first_needed)]
                typer.echo(f"Updating {tf} candles for {len(todo)} of the {len(tradeable)} coins that were ever shortlisted…")
                for n, coin in enumerate(todo, start=1):
                    await backfill(client, conn, coin, tf, days_back + settings.backtest.warmup_days + 5)
                    if n % 10 == 0:
                        typer.echo(f"  {n}/{len(todo)}")
            first = int((start - pd.Timedelta(days=settings.backtest.warmup_days + 5)).timestamp() * 1000)
            candles = {coin: candle_frame(load_candles(conn, coin, tf, first)) for coin in tradeable}
    data.candles = {coin: frame for coin, frame in candles.items() if not frame.empty}
    return data, plans


def _needs_refresh(conn, coin: str, interval: str, delisted: bool, min_volume: float, now: int,
                   since_ms: int) -> bool:
    """Download if the history hasn't been checked back far enough, or the newest candles are missing.
    Skip coins whose data can't have changed (delisted) or can't matter (always tiny: refreshed weekly)."""
    stored = stored_range(conn, coin, interval)
    covered = coverage_from(conn, coin, interval)
    required = max(since_ms, history_floor(interval, now))
    if stored is None or covered is None or covered > required + INTERVAL_MS[interval]:
        return True
    if delisted:
        return False
    if now - stored[1] < 2 * INTERVAL_MS["1d"] + INTERVAL_MS[interval]:
        return False
    if min_volume and now - stored[1] < 7 * INTERVAL_MS["1d"]:
        biggest = conn.execute(
            "SELECT MAX(volume * close) FROM candles WHERE coin = ? AND interval = ?", (coin, interval)
        ).fetchone()[0]
        return (biggest or 0) >= min_volume / 2
    return True


def _print_stats(stats, btc, usd_to_aud: float, title: str) -> list[str]:
    table = Table(title=title)
    for header in ("", "Scout", "Just holding BTC"):
        table.add_column(header, justify="left" if not header else "right")
    rows = stats_table(stats, btc, usd_to_aud)
    for row in rows:
        table.add_row(*row)
    Console().print(table)
    return [f"{name:<22} {ours:>28} {theirs:>28}" for name, ours, theirs in rows]


def _assumptions(settings: Settings) -> str:
    r, b = settings.risk, settings.backtest
    return (
        f"Costs: taker fee {r.taker_fee_pct}% per trade, slippage {r.slippage_pct}% each way, funding "
        f"{b.funding_rate_hourly_pct}%/hour on open positions (≈{b.funding_rate_hourly_pct * 24 * 365:.1f}% a year). "
        "The mood and shortlist are updated daily (live: hourly). Order-book and open-interest filters and "
        "crowding can't be tested (no history). Delisted coins are included."
    )


def _period(settings: Settings, from_: str | None, to: str | None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The test/replay period, moved forward if Hyperliquid doesn't keep candles that old."""
    step = pd.Timedelta(milliseconds=INTERVAL_MS[settings.signals.timeframe])
    requested_start = pd.Timestamp(from_, tz="UTC") if from_ else pd.Timestamp("2000-01-01", tz="UTC")
    end = pd.Timestamp(to, tz="UTC") if to else pd.Timestamp.now(tz="UTC").floor(step) - step
    # Signal candles only go back 5000 candles; find out where that is before downloading everything.
    candle_floor = pd.Timestamp(now_ms() - 5000 * step.total_seconds() * 1000, unit="ms", tz="UTC").ceil(step)
    start = max(requested_start, (candle_floor + pd.Timedelta(days=settings.backtest.warmup_days)).ceil("D"))
    if from_ and start > requested_start:
        typer.secho(
            f"⚠ Hyperliquid only keeps 5000 {settings.signals.timeframe} candles (back to {candle_floor:%d %b %Y}). "
            f"With {settings.backtest.warmup_days} days of warm-up, the period starts {start:%d %b %Y}, "
            f"not {requested_start:%d %b %Y}.", fg="yellow")
    if end <= start:
        typer.secho("✗ the end date must be after the start date", fg="red", err=True)
        raise typer.Exit(1)
    return start, end


def _history_or_exit(settings: Settings, start: pd.Timestamp, end: pd.Timestamp, refresh: bool):
    try:
        data, plans = asyncio.run(_load_history(settings, start, end, refresh))
    except HyperliquidError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    return data, plans, max(start, earliest_start(data, settings))


@app.command("backtest")
def backtest_command(
    from_: Annotated[str | None, typer.Option("--from", help="Start date, e.g. 2024-07-01 (default: earliest possible).")] = None,
    to: Annotated[str | None, typer.Option("--to", help="End date (default: now).")] = None,
    walk: Annotated[bool, typer.Option("--walk-forward", help="Tune on one period, test on the next unseen one.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh", help="Download missing candles first.")] = True,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Replay the strategy over past candles with fees, slippage and funding, and compare with holding BTC."""
    settings = _start(config, env_file)
    tz = settings.app.tz
    usd_to_aud = 1 / settings.demo.aud_to_usdc_rate
    start, end = _period(settings, from_, to)
    data, plans, start = _history_or_exit(settings, start, end, refresh)

    stamp = datetime.now(tz).strftime("%Y%m%d_%H%M")
    kind = "walkforward" if walk else "backtest"
    out = settings.app.reports_dir / f"{kind}_{start:%Y%m%d}_{end:%Y%m%d}_{stamp}"
    lines = [f"Scout v{APP_VERSION} {kind}, {start:%d %b %Y} → {end:%d %b %Y} (UTC)", _assumptions(settings), ""]

    if walk:
        typer.echo("Walk-forward: tuning on each training period, then testing on the next…")
        result: BacktestResult | WalkForwardResult = walk_forward(data, plans, settings, start, end, progress=typer.echo)
        lines += ["Windows (settings picked on training data only, then tested on unseen data):"]
        for r in result.windows:
            train = "n/a" if r.train is None else f"{r.train.stats.total_return_pct:+.1f}%"
            lines.append(
                f"  train {r.window.train_start:%Y-%m-%d}→{r.window.train_end:%Y-%m-%d} {train:>7} | "
                f"test {r.window.test_start:%Y-%m-%d}→{r.window.test_end:%Y-%m-%d} "
                f"{r.test.stats.total_return_pct:+6.1f}% vs BTC {r.test.btc_stats.total_return_pct:+6.1f}% | "
                f"{r.test.stats.trades} trades | {r.params}{' | ' + r.note if r.note else ''}"
            )
        shade = [(r.window.test_start, r.window.test_end) for r in result.windows]
        title = "walk-forward (out-of-sample test periods only)"
    else:
        typer.echo("Running the backtest…")
        result = simulate(data, plans, settings, start, end)
        shade = ()
        title = "backtest"

    typer.echo()
    for line in lines[3:]:
        typer.echo(line)
    lines += ["", *_print_stats(result.stats, result.btc_stats, usd_to_aud, f"Scout vs just holding BTC — {title}")]
    typer.echo("\nVerdict:")
    for line in verdict(result.stats, result.btc_stats):
        typer.secho(f"  • {line}", bold=True)
    lines += ["", "Verdict:", *(f"  • {v}" for v in verdict(result.stats, result.btc_stats))]
    typer.echo(f"\n{textwrap.fill(_assumptions(settings), 100)}")

    plot_equity(result.equity, result.stats, result.btc_stats, out / "equity.png", title, usd_to_aud, shade)
    write_trades_csv(result.trades, out / "trades.csv", tz, usd_to_aud)
    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    typer.secho(f"\n✓ Saved equity chart, trade list and summary to {out}", fg="green")
    log.info("%s %s→%s: %+.1f%% vs BTC %+.1f%%, %d trades", kind, start.date(), end.date(),
             result.stats.total_return_pct, result.btc_stats.total_return_pct, result.stats.trades)


# ------------------------------------------------------------ demo trading


def price_stream(settings: Settings):
    """Live prices for the demo loop (tests replace this to run offline)."""
    return stream_mids(settings.data.ws_url)


async def _live_prices(settings: Settings) -> dict[str, float]:
    async with make_client(settings) as client:
        return await client.all_mids()


def _aud(usd: float, settings: Settings, signed: bool = False) -> str:
    aud = usd / settings.demo.aud_to_usdc_rate
    sign = ("+" if aud >= 0 else "-") if signed else ("-" if aud < 0 else "")
    return f"{sign}A${abs(aud):,.2f}"


def _demo_cycle(settings: Settings):
    async def cycle(engine: DemoEngine) -> None:
        async with make_client(settings) as client:
            result = await pipeline.run_signals(
                settings, client, engine.conn, engine.account_for_signals(), engine.positions_for_signals(),
                refresh=True, prices=dict(engine.prices.prices),
            )
        engine.funding_rates = {coin: m.funding_rate for coin, m in result.market.items()}
        engine.on_mood(result.reading)
        stamp = datetime.now(settings.app.tz).strftime("%H:%M")
        acting = [s for s in result.signals if s.action in (Action.ENTER_LONG, Action.ENTER_SHORT, Action.EXIT,
                                                            Action.MOVE_STOP)]
        typer.secho(f"\n[{stamp}] Mood {result.reading.regime} (volatility {result.reading.volatility}); shortlist: "
                    f"{', '.join(result.shortlist) or '(empty)'}; {len(acting)} signal(s).",
                    fg=MOOD_COLOURS[result.reading.regime])
        for s in acting:
            typer.echo(textwrap.fill(f"  • {s.reason}", 100, subsequent_indent="    "))
        await engine.execute_signals(result.signals)
        _print_account_line(engine)

    return cycle


def _print_account_line(engine: DemoEngine) -> None:
    settings = engine.settings
    equity = engine.account.equity(engine.prices.prices)
    start = engine.state.get_float("initial_equity_usd", equity)
    typer.echo(f"  Account {_aud(equity, settings)} ({(equity / start - 1) * 100:+.2f}% since the start), "
               f"{len(engine.account.positions())} open position(s), state {engine.bot_state}.")


@app.command()
def demo(
    minutes: Annotated[float | None, typer.Option("--minutes", min=0, help="Stop after this many minutes.")] = None,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Start demo trading: fake money, real live prices. Runs until Ctrl+C (positions stay in the account)."""
    settings = _start(config, env_file)
    if settings.mode is not Mode.DEMO:
        typer.secho(f"✗ `scout demo` needs mode: demo (config says {settings.mode.value})", fg="red", err=True)
        raise typer.Exit(1)
    lock = settings.app.db_path.parent / "demo.lock"
    rotate_service_logs(settings.app.log_dir)
    try:
        with single_instance(lock), closing(open_db(settings.app.db_path)) as conn:
            engine = DemoEngine(settings, conn, notifier=make_notifier(settings, conn))
            typer.echo(engine.on_start(by_service=_by_service()))
            if engine.recent_starts() >= settings.service.crash_loop_restarts:
                pause = settings.service.crash_loop_pause_minutes
                message = (f"⚠️ Scout has restarted {settings.service.crash_loop_restarts}+ times in an hour, so "
                           f"something is wrong. Waiting {pause} minutes before trying again; run `scout doctor`.")
                engine.event("ERROR", "demo", message)
                engine.notify(message, "service")
                asyncio.run(engine.notifier.deliver())
                time.sleep(pause * 60)
            engine.event("INFO", "demo", f"Demo trading started (Scout v{APP_VERSION}).")
            conn.commit()
            typer.echo(f"Demo trading with fake money. State: {engine.bot_state}. Ctrl+C to stop.")
            if not engine.notifier.enabled:
                typer.echo("Phone alerts are off (see `scout ntfy-setup`).")
            if engine.bot_state is BotState.KILLED:
                typer.secho("⚠ The kill switch is on: nothing new will be traded until `scout reset-kill`.", fg="red")
            awake = keep_awake(os.getpid()) if settings.service.keep_awake and sys.platform == "darwin" else None
            runner = DemoRunner(engine, _demo_cycle(settings), lambda: price_stream(settings), echo=typer.echo)
            try:
                asyncio.run(_run_until_stopped(runner, minutes))
            except Exception as exc:
                engine.record_crash(f"{type(exc).__name__}: {exc}")
                raise  # exit with an error, so launchd restarts Scout (and the restart alert says why)
            finally:
                if awake is not None:
                    awake.terminate()
            engine.event("INFO", "demo", "Demo trading stopped.")
            conn.commit()
    except RuntimeError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        typer.echo("\nStopped. Open positions stay in the demo account; run `scout demo` to keep managing them.")


async def _run_until_stopped(runner: DemoRunner, minutes: float | None) -> None:
    """Run the demo loop; SIGTERM (launchd stopping the service, or a shutdown) ends it cleanly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    await runner.run(minutes * 60 if minutes is not None else None, stop=stop)


def _print_status(engine: DemoEngine) -> None:
    settings, conn, state = engine.settings, engine.conn, engine.state
    tz = settings.app.tz
    now = engine.clock()
    last_tick = int(state.get_float("last_tick_ms", 0))
    stopped = int(state.get_float("loop_stopped_ms", 0))
    running = last_tick and stopped < last_tick and now - last_tick < (settings.demo.tick_seconds * 3 + 30) * 1000
    ctx = engine.risk_context()
    initial = state.get_float("initial_equity_usd", ctx.equity_usd)
    colour = {BotState.RUNNING: "green", BotState.PAUSED: "yellow", BotState.KILLED: "red"}[ctx.state]
    typer.secho(f"State: {ctx.state}", fg=colour, bold=True, nl=False)
    loop = "demo loop running" if running else (
        f"demo loop NOT running (last active {_date(last_tick, tz)})" if last_tick else "demo loop never started")
    typer.echo(f"  ({loop})")
    if ctx.state is BotState.KILLED:
        typer.secho(f"  Kill switch: {state.get('killed_reason', '')}", fg="red")
    exposure = sum(p.notional_usd for p in ctx.positions)
    typer.echo(f"Account:     {_aud(ctx.equity_usd, settings)} (US${ctx.equity_usd:,.2f}), "
               f"{(ctx.equity_usd / initial - 1) * 100:+.2f}% since the start ({_aud(initial, settings)})")
    typer.echo(f"Cash:        {_aud(engine.account.cash, settings)}; in positions {_aud(exposure, settings)} "
               f"({exposure / ctx.equity_usd * 100:.0f}% of the account, max {settings.risk.max_total_exposure_pct:g}%)")
    today = (ctx.equity_usd / ctx.day_start_equity_usd - 1) * 100
    limit = " — LIMIT HIT, no new trades today" if ctx.daily_limit_hit else ""
    typer.echo(f"Today:       {today:+.2f}% since midnight Sydney (daily loss limit -{settings.risk.daily_loss_limit_pct:g}%){limit}")
    typer.echo(f"From peak:   -{ctx.drawdown_pct:.2f}% (kill switch at -{settings.risk.kill_switch_drawdown_pct:g}%)")
    typer.echo(f"Positions:   {len(ctx.positions)} open (max {settings.risk.max_open_positions})"
               + (f": {', '.join(p.coin for p in ctx.positions)}" if ctx.positions else ""))
    waiting = conn.execute("SELECT COUNT(*) FROM notifications WHERE status = 'pending'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM notifications WHERE status = 'failed'").fetchone()[0]
    if engine.notifier and engine.notifier.enabled:
        quiet = " (quiet hours)" if engine.notifier.quiet(now) else ""
        channels = " + ".join(b.name for b in engine.notifier.backends)
        typer.echo(f"Alerts:      {channels}; {waiting} waiting{quiet}, {failed} failed")
    else:
        typer.echo("Alerts:      off (see `scout ntfy-setup`)")
    mood = conn.execute("SELECT ts_ms, regime, risk_level FROM regime_history ORDER BY ts_ms DESC LIMIT 1").fetchone()
    if mood:
        typer.echo(f"Latest mood: {mood['regime']} / {mood['risk_level']} at {_date(mood['ts_ms'], tz)}")
    events = conn.execute(
        "SELECT ts_ms, level, message FROM events_log WHERE category IN ('risk', 'trade', 'control') "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if events:
        typer.echo("Recent:")
        for e in events:
            typer.echo(textwrap.fill(f"  {_date(e['ts_ms'], tz)}  {e['message']}", 100, subsequent_indent="      "))


def _engine_with_prices(settings: Settings, conn, fetch: bool = True) -> DemoEngine:
    engine = DemoEngine(settings, conn, notifier=make_notifier(settings, conn))
    if fetch and engine.account.positions():
        try:
            engine.prices.update(asyncio.run(_live_prices(settings)), now_ms())
        except HyperliquidError as exc:
            typer.secho(f"⚠ couldn't fetch live prices ({exc}); values use entry prices", fg="yellow")
    return engine


@app.command()
def status(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Show the demo account: state, value, today's result, drawdown, positions and recent events."""
    settings = _start(config, env_file)
    with closing(open_db(settings.app.db_path)) as conn:
        _print_status(_engine_with_prices(settings, conn))


@app.command()
def positions(
    closed: Annotated[int, typer.Option("--closed", min=0, help="Also show the last N closed trades.")] = 0,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """List open demo positions with live profit/loss (and optionally recent closed trades)."""
    settings = _start(config, env_file)
    tz = settings.app.tz
    with closing(open_db(settings.app.db_path)) as conn:
        engine = _engine_with_prices(settings, conn)
        held = engine.account.positions()
        table = Table(title=f"Open demo positions ({len(held)})")
        for header in ("Coin", "Side", "Qty", "Entry", "Now", "Stop", "Profit/loss", "Opened"):
            table.add_column(header, justify="left" if header in ("Coin", "Side") else "right", no_wrap=True)
        for p in held:
            price = engine.prices.prices.get(p.coin, p.entry_price)
            pnl = p.unrealised_usd(price) - p.fees_usd - p.funding_usd
            pct = pnl / (p.qty * p.entry_price) * 100
            table.add_row(p.coin, p.side, f"{p.qty:g}", f"${format_price(p.entry_price)}", f"${format_price(price)}",
                          f"${format_price(p.stop_price)}",
                          f"[{'green' if pnl >= 0 else 'red'}]{_aud(pnl, settings, True)} ({pct:+.1f}%)[/]",
                          _date(p.opened_ts_ms, tz))
        Console().print(table)
        for p in held:
            typer.echo(textwrap.fill(f"  {p.coin}: {p.open_reason}", 100, subsequent_indent="    "))
        if closed:
            rows = conn.execute(
                "SELECT coin, side, entry_price, exit_price, pnl_usd, closed_ts_ms, close_reason FROM demo_positions "
                "WHERE status = 'closed' ORDER BY closed_ts_ms DESC LIMIT ?", (closed,)
            ).fetchall()
            typer.echo(f"\nLast {len(rows)} closed trade(s):")
            for r in rows:
                typer.echo(textwrap.fill(
                    f"  {_date(r['closed_ts_ms'], tz)} {r['coin']} {r['side']}: {_aud(r['pnl_usd'], settings, True)} "
                    f"— {r['close_reason']}", 100, subsequent_indent="    "))


@app.command()
def pause(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Stop opening new trades. Open positions and their stops are still managed."""
    _set_paused(True, config, env_file)


@app.command()
def resume(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Allow new trades again after `scout pause`."""
    _set_paused(False, config, env_file)


def _set_paused(paused: bool, config: Path, env_file: Path) -> None:
    settings = _start(config, env_file)
    with closing(open_db(settings.app.db_path)) as conn:
        try:
            DemoEngine(settings, conn).set_paused(paused)
        except ValueError as exc:
            typer.secho(f"✗ {exc}", fg="red", err=True)
            raise typer.Exit(1) from None
    typer.secho("Paused: no new trades; open positions are still managed." if paused
                else "Resumed: new trades allowed.", fg="yellow" if paused else "green")


YesOption = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")]


@app.command()
def kill(yes: YesOption = False, config: ConfigOption = Path("config.yaml"),
         env_file: EnvFileOption = Path(".env")) -> None:
    """KILL SWITCH: close every demo position now and stop trading until `scout reset-kill`."""
    settings = _start(config, env_file)
    if not yes and not typer.confirm("Close every demo position now and stop all trading until reset?"):
        raise typer.Exit(1)
    with closing(open_db(settings.app.db_path)) as conn:
        engine = _engine_with_prices(settings, conn)
        held = len(engine.account.positions())
        asyncio.run(engine.kill("You pressed the kill switch."))
        left = engine.account.positions()
        asyncio.run(engine.notifier.deliver())  # the kill switch message goes out now, even in quiet hours
    typer.secho(f"KILLED: closed {held - len(left)} position(s). Nothing new is traded until `scout reset-kill`.",
                fg="red", bold=True)
    if left:
        typer.secho(f"⚠ {len(left)} position(s) had no price; the demo loop will close them when prices arrive.",
                    fg="yellow")


@app.command("reset-kill")
def reset_kill(yes: YesOption = False, config: ConfigOption = Path("config.yaml"),
               env_file: EnvFileOption = Path(".env")) -> None:
    """Turn the kill switch off. The current account value becomes the new peak."""
    settings = _start(config, env_file)
    with closing(open_db(settings.app.db_path)) as conn:
        engine = _engine_with_prices(settings, conn)
        if engine.bot_state is not BotState.KILLED:
            typer.echo(f"The kill switch isn't on (state: {engine.bot_state}).")
            return
        typer.echo(f"The kill switch went off because: {engine.state.get('killed_reason', '(unknown)')}")
        if not yes and not typer.confirm("Have you worked out why, and do you want to start trading again?"):
            raise typer.Exit(1)
        engine.reset_kill()
    typer.secho("Kill switch reset: trading can resume.", fg="green")


# ------------------------------------------------------------ notifications


def make_notifier(settings: Settings, conn) -> Notifier:
    """The iMessage notifier (tests replace this to avoid real messages)."""
    return Notifier(conn, settings, backends_from_settings(settings))


@app.command("notify-test")
def notify_test(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be sent, without sending.")] = False,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Send one test alert through each channel that's set up (ntfy, iMessage), even if not switched on yet."""
    settings = _start(config, env_file)
    text = ("Test message: if you can read this on your iPhone, Scout's alerts work. "
            f"Sent {datetime.now(settings.app.tz):%a %d %b %H:%M} (Sydney).")
    with closing(open_db(settings.app.db_path)) as conn:
        header = Notifier(conn, settings, []).header()
        backends = make_backends(settings, include_disabled=True)
    if not backends:
        typer.secho("✗ No alert channel is set up. Run `uv run scout ntfy-setup` (recommended), or put your number "
                    'in .env for iMessage:\n    SCOUT_NOTIFY__IMESSAGE_RECIPIENT="+614XXXXXXXX"', fg="red", err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.echo(f"Would send through: {', '.join(b.name for b in backends)}\n\n{header}\n{text}\n")
        if any(b.name == "imessage" for b in backends):
            typer.echo("iMessage uses this AppleScript (recipient and text are passed as arguments):\n")
            typer.echo(APPLESCRIPT)
        return
    failures = 0
    for backend in backends:
        try:
            asyncio.run(backend.send(f"{header}\n{text}"))
        except NotifyError as exc:
            failures += 1
            typer.secho(f"✗ {backend.name}: not sent: {exc}", fg="red", err=True)
            typer.echo(_notify_help(backend.name), err=True)
            log.error("notify-test via %s failed: %s", backend.name, exc)
            continue
        typer.secho(f"✓ {backend.name}: sent. Check your iPhone.", fg="green")
        log.info("notify-test sent via %s", backend.name)
    off = [b.name for b in backends if not getattr(settings.notify, f"{b.name}_enabled")]
    if off and failures < len(backends):
        typer.echo(f"Switched off in config.yaml (so `scout demo` won't use it yet): {', '.join(off)}.")
    if failures:
        raise typer.Exit(1)


def _notify_help(channel: str) -> str:
    if channel == "ntfy":
        return ("  Check: the internet connection, and that the ntfy app on your iPhone is subscribed to exactly the "
                "topic in .env (run `uv run scout ntfy-setup` to see it).")
    return ("  Things to check:\n"
            "  1. The Messages app on this Mac is signed in to iMessage (Messages → Settings → iMessage).\n"
            "  2. System Settings → Privacy & Security → Automation → allow Terminal to control Messages.\n"
            "  3. The number is in international format (+614…) and uses iMessage.")


def make_backends(settings: Settings, include_disabled: bool = False):
    """The alert channels (tests replace this to avoid sending anything real)."""
    return backends_from_settings(settings, include_disabled=include_disabled)


@app.command("ntfy-setup")
def ntfy_setup(
    new: Annotated[bool, typer.Option("--new", help="Make a new topic, replacing the old one.")] = False,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Set up push alerts through the free ntfy iPhone app: makes a secret topic and switches alerts on."""
    settings = _start(config, env_file)
    existing = settings.notify.ntfy_topic
    if existing is not None and not new:
        topic = existing.get_secret_value()
        typer.echo("Your ntfy topic is already set up (use --new to replace it).")
    else:
        topic = new_ntfy_topic()
        set_env_value(env_file, "SCOUT_NOTIFY__NTFY_TOPIC", topic)
        typer.secho(f"✓ Made a new secret topic and saved it in {env_file.name}.", fg="green")
    if set_config_flag(config, "ntfy_enabled", True):
        typer.secho("✓ Switched ntfy alerts on in config.yaml.", fg="green")
    server = settings.notify.ntfy_server
    typer.echo(f"""
On your iPhone:
  1. Install the official "ntfy" app: https://apps.apple.com/us/app/ntfy/id1625396347
  2. Open it and allow notifications.
  3. Tap + (Subscribe to topic) and type this topic exactly:

        {topic}

     Leave the server as {server.removeprefix("https://")} (the default). Tap Subscribe.
  4. Back here:   uv run scout notify-test
  5. Then restart the background demo so it uses the new alerts:
                  uv run scout service stop && uv run scout service start

Keep the topic private: anyone who knows it can read your alerts.""")


def new_ntfy_topic() -> str:
    """scout-xxxx-xxxx-xxxx-xxxx: 16 random lowercase letters/digits (~82 bits), easy to type on a phone."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"  # no l/1/o/0 lookalikes
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return "scout-" + "-".join(groups)


def set_env_value(path: Path, key: str, value: str) -> None:
    """Add or replace KEY="value" in a .env file, keeping it readable only by you."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entry = f'{key}="{value}"'
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(key)}\s*=", line):
            lines[i] = entry
            break
    else:
        lines.append(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def set_config_flag(path: Path, key: str, value: bool) -> bool:
    """Set `  key: true/false` in config.yaml, keeping comments. Returns True if it changed."""
    text = path.read_text(encoding="utf-8")
    word = "true" if value else "false"
    updated, count = re.subn(rf"^(\s+{re.escape(key)}:\s*)(true|false)", rf"\g<1>{word}", text, count=1, flags=re.M)
    if count == 0:
        raise typer.BadParameter(f"couldn't find `{key}:` in {path.name}")
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _mask(recipient: str) -> str:
    """+61412345678 -> +614•••••678; name@icloud.com -> na•••@icloud.com (never print it in full)."""
    if "@" in recipient:
        name, domain = recipient.split("@", 1)
        return f"{name[:2]}•••@{domain}"
    return f"{recipient[:4]}{'•' * max(0, len(recipient) - 7)}{recipient[-3:]}"


# ------------------------------------------------------ replay, dashboard, explain


def _replay_paths(settings: Settings) -> tuple[Path, Path]:
    folder = settings.app.db_path.parent
    return folder / "replay.db", folder / "replay_control.json"


@app.command()
def replay(
    from_: Annotated[str, typer.Option("--from", help="Start date, e.g. 2025-01-01.")],
    to: Annotated[str, typer.Option("--to", help="End date, e.g. 2025-03-31.")],
    speed: Annotated[str, typer.Option("--speed", help="e.g. 500x, 20000x or max.")] = "500x",
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh", help="Download missing candles first.")] = True,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Run the demo trader over past prices at high speed. Watch it with `scout dashboard` (choose Replay)."""
    settings = _start(config, env_file)
    try:
        pace = parse_speed(speed)
    except ValueError:
        typer.secho(f"✗ speed must look like 500x, 20000x or max (got {speed!r})", fg="red", err=True)
        raise typer.Exit(1) from None
    start, end = _period(settings, from_, to)
    data, plans, start = _history_or_exit(settings, start, end, refresh)

    db_path, control_path = _replay_paths(settings)
    lock = db_path.parent / "replay.lock"
    try:
        with single_instance(lock):
            for leftover in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
                leftover.unlink(missing_ok=True)  # every replay starts from a fresh A$1,000
            replay_settings = settings.model_copy(update={
                "mode": Mode.REPLAY,
                "app": settings.app.model_copy(update={"db_path": db_path}),
            })
            control = ReplayControl(control_path)
            control.write(Control(paused=False, speed=pace, step=0))
            sim = SimClock(int(start.timestamp() * 1000))
            with closing(open_db(db_path)) as conn:
                engine = DemoEngine(replay_settings, conn, clock=sim, notifier=Notifier(conn, replay_settings, [], clock=sim))
                hours = (end - start).total_seconds() / 3600
                duration = "as fast as possible" if pace is None else f"about {hours * 3600 / pace / 60:,.0f} minutes"
                typer.echo(f"Replaying {start:%d %b %Y} → {end:%d %b %Y} at {speed} ({duration}) on a fresh "
                           f"A${settings.demo.starting_balance_aud:,.0f} account in {db_path.name}.")
                typer.echo("Watch it: `uv run scout dashboard` in another window, then choose Replay. "
                           "Pause/step/speed are in the dashboard's sidebar.")
                runner = ReplayRunner(engine, sim, data, plans, start, end, control, echo=typer.echo)
                asyncio.run(runner.run())
                _replay_summary(engine, settings)
    except RuntimeError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        typer.echo("\nReplay stopped. Its results so far stay in the dashboard until the next replay.")


def _replay_summary(engine: DemoEngine, settings: Settings) -> None:
    conn = engine.conn
    equity = engine.account.equity(engine.prices.prices)
    start = engine.state.get_float("initial_equity_usd", equity)
    btc_start = engine.state.get_float("btc_start_price", 0)
    btc_now = engine.prices.prices.get("BTC")
    trades = conn.execute("SELECT COUNT(*), SUM(pnl_usd > 0) FROM demo_positions WHERE status = 'closed'").fetchone()
    typer.secho(f"\nReplay finished: {_aud(equity, settings)} ({(equity / start - 1) * 100:+.1f}%)"
                + (f" vs holding BTC {(btc_now / btc_start - 1) * 100:+.1f}%" if btc_start and btc_now else "")
                + f" · {trades[0]} closed trades, {trades[1] or 0} winners · "
                  f"{len(engine.account.positions())} still open", bold=True)
    typer.echo("Explain any trade: `uv run scout explain --replay` (lists them), then `scout explain <id> --replay`.")


@app.command()
def dashboard(
    port: Annotated[int, typer.Option("--port", help="Local port for the dashboard.")] = 8501,
    replay_view: Annotated[bool, typer.Option("--replay", help="Open on the Replay view.")] = False,
    headless: Annotated[bool, typer.Option("--headless", help="Don't open a browser window automatically.")] = False,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Open the read-only dashboard in your browser (only reachable from this Mac)."""
    _start(config, env_file)
    env = os.environ | {
        "SCOUT_CONFIG": str(config.resolve()),
        "SCOUT_ENV_FILE": str(env_file.resolve()),
        "SCOUT_DASHBOARD_SOURCE": "Replay" if replay_view else "Demo",
    }
    page = Path(__file__).with_name("dashboard.py")
    typer.echo(f"Dashboard: http://localhost:{port}  (Ctrl+C to stop)")
    command = [sys.executable, "-m", "streamlit", "run", str(page), "--server.address", "127.0.0.1",
               "--server.port", str(port), "--browser.gatherUsageStats", "false", "--theme.base", "light",
               "--server.headless", "true" if headless else "false"]
    try:
        subprocess.run(command, env=env, check=False)
    except KeyboardInterrupt:
        pass


@app.command()
def explain(
    trade_id: Annotated[int | None, typer.Argument(help="The trade number (leave out to list recent trades).")] = None,
    replay_db: Annotated[bool, typer.Option("--replay", help="Look in the latest replay instead of the demo.")] = False,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Explain, step by step and in plain English, why a trade happened and how it ended."""
    settings = _start(config, env_file)
    path = _replay_paths(settings)[0] if replay_db else settings.app.db_path
    if not path.is_file():
        typer.secho(f"✗ nothing recorded yet in {path.name}", fg="red", err=True)
        raise typer.Exit(1)
    tz = settings.app.tz
    with closing(data_readonly(path)) as conn:
        if trade_id is None:
            rows = list_trades(conn)
            if not rows:
                typer.echo("No trades yet.")
                return
            typer.echo(f"Recent trades (explain one with `scout explain <id>{' --replay' if replay_db else ''}`):")
            for r in rows:
                result = "open" if r["status"] == "open" else _aud(r["pnl_usd"], settings, signed=True)
                typer.echo(f"  #{r['id']:<4} {_date(r['opened_ts_ms'], tz)}  {r['coin']:<8} {r['side']:<5} {result}")
            return
        try:
            typer.echo(explain_trade(conn, trade_id, tz, settings.demo.aud_to_usdc_rate))
        except KeyError as exc:
            typer.secho(f"✗ {exc.args[0]}", fg="red", err=True)
            raise typer.Exit(1) from None


# ------------------------------------------------------------ running unattended

service_app = typer.Typer(help="Run `scout demo` in the background: start at login, restart after a crash.",
                          no_args_is_help=True)
app.add_typer(service_app, name="service")


def _project_paths(config: Path, env_file: Path) -> tuple[Path, Path, Path, Path]:
    """(the scout program, the project folder, config.yaml, .env) as absolute paths for launchd."""
    scout_bin = Path(sys.executable).parent / "scout"
    config = config.resolve()
    return scout_bin, config.parent, config, env_file.resolve()


@service_app.command("install")
def service_install(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Install and start the background service (it also starts at every login)."""
    settings = _start(config, env_file)
    scout_bin, project, config_path, env_path = _project_paths(config, env_file)
    if not scout_bin.exists():
        typer.secho(f"✗ can't find the scout program at {scout_bin}; run `uv sync` first", fg="red", err=True)
        raise typer.Exit(1)
    try:
        path = service.install(settings, scout_bin, project, config_path, env_path)
    except RuntimeError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    typer.secho(f"✓ Installed {path.name} and started Scout in the background.", fg="green")
    typer.echo("It starts at every login and restarts after a crash. Check it with `uv run scout service status`.\n"
               "If iMessage is on, macOS may ask whether 'python' can control Messages: allow it.")


@service_app.command("uninstall")
def service_uninstall(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Stop the background service and remove it (Scout won't start at login any more)."""
    settings = _start(config, env_file)
    removed = service.uninstall(settings)
    typer.echo("✓ Service stopped and removed." if removed else "The service wasn't installed.")


@service_app.command("stop")
def service_stop(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Stop Scout until the next login (or `scout service start`). Open positions stay in the account."""
    settings = _start(config, env_file)
    typer.echo("✓ Stopped." if service.stop(settings) else "It wasn't running.")


@service_app.command("start")
def service_start(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Start the installed service again."""
    settings = _start(config, env_file)
    if not service.plist_path(settings).exists():
        typer.secho("✗ not installed: run `uv run scout service install`", fg="red", err=True)
        raise typer.Exit(1)
    started, why = service.start(settings)
    if started:
        typer.secho("✓ Started.", fg="green")
    elif why == "it's already running":
        typer.echo("It's already running.")
    else:
        typer.secho(f"✗ Couldn't start it: {why}", fg="red", err=True)
        raise typer.Exit(1)


@service_app.command("status")
def service_status_command(config: ConfigOption = Path("config.yaml"),
                           env_file: EnvFileOption = Path(".env")) -> None:
    """Is the background service installed and running? When did it last check in?"""
    settings = _start(config, env_file)
    s = service.status(settings)
    beat = service.read_heartbeat(service.heartbeat_path(settings))
    typer.echo(f"Installed: {'yes' if s.installed else 'no'} ({s.plist})")
    typer.echo(f"Running:   {'yes, pid ' + str(s.pid) if s.pid else 'no'}"
               + (f" (last exit code {s.last_exit})" if s.last_exit not in (None, "0") else ""))
    if beat:
        age = (now_ms() - beat["ts_ms"]) / 1000
        colour = "green" if age < 120 else "red"
        typer.secho(f"Heartbeat: {age:.0f}s ago · v{beat['version']} · {beat['state']} · "
                    f"{beat['open_positions']} open position(s)", fg=colour)
    else:
        typer.echo("Heartbeat: none yet")


@app.command()
def backup(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Copy the database to data/backups now (the demo also does this once a day)."""
    settings = _start(config, env_file)
    if not settings.app.db_path.exists():
        typer.secho("✗ no database yet", fg="red", err=True)
        raise typer.Exit(1)
    path = backup_db(settings.app.db_path, settings.app.backup_dir, now_ms(), settings.app.tz,
                     settings.app.backup_keep_days)
    typer.secho(f"✓ Backed up to {path}", fg="green")


@app.command()
def report(
    kind: Annotated[str, typer.Argument(help="Which report: weekly.")] = "weekly",
    days: Annotated[int, typer.Option("--days", min=1, help="How many days to cover.")] = 7,
    send: Annotated[bool, typer.Option("--send", help="Also queue it as an iMessage.")] = False,
    replay_db: Annotated[bool, typer.Option("--replay", help="Report on the latest replay instead.")] = False,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Performance vs BTC, best/worst trades, market moods and an honest verdict (sent every Sunday evening)."""
    if kind != "weekly":
        typer.secho("✗ the only report so far is `weekly`", fg="red", err=True)
        raise typer.Exit(1)
    settings = _start(config, env_file)
    path = _replay_paths(settings)[0] if replay_db else settings.app.db_path
    if not path.exists():
        typer.secho(f"✗ nothing recorded yet in {path.name}", fg="red", err=True)
        raise typer.Exit(1)
    usd_per_aud, tz = settings.demo.aud_to_usdc_rate, settings.app.tz
    with closing(open_db(path)) as conn:
        end = int(conn.execute("SELECT MAX(ts_ms) FROM equity_snapshots").fetchone()[0] or now_ms()) if replay_db \
            else now_ms()
        weekly = weekly_report(conn, end, days)
        typer.echo(report_text(weekly, usd_per_aud, tz))
        saved = save_report(weekly, settings.app.reports_dir / ("replay" if replay_db else "weekly"), usd_per_aud, tz)
        typer.secho(f"✓ Saved to {saved}", fg="green")
        if send:
            make_notifier(settings, conn).notify(report_message(weekly, usd_per_aud, tz), "report")
            typer.echo("Queued as an iMessage (sent by the demo loop, respecting quiet hours).")


@app.command("tax-export")
def tax_export(
    fy: Annotated[int | None, typer.Option("--fy", help="Financial year by its end, e.g. 2026 = Jul 2025–Jun 2026.")] = None,
    replay_db: Annotated[bool, typer.Option("--replay", help="Export the latest replay (for testing).")] = False,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh", help="Download the latest RBA rates.")] = True,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """CSV of every trade (Sydney time, USD and AUD at the RBA daily rate) for your accountant."""
    settings = _start(config, env_file)
    tz = settings.app.tz
    path = _replay_paths(settings)[0] if replay_db else settings.app.db_path
    if not path.exists():
        typer.secho(f"✗ nothing recorded yet in {path.name}", fg="red", err=True)
        raise typer.Exit(1)
    fy = fy or financial_year(datetime.now(tz).date())
    start_ms, end_ms = fy_bounds(fy, tz)
    with closing(open_db(path)) as conn:
        if refresh:
            try:
                count = asyncio.run(refresh_rates(conn, settings.tax.fx_url))
                typer.echo(f"Updated {count} daily AUD/USD rates from the RBA.")
            except (httpx.HTTPError, ValueError) as exc:
                typer.secho(f"⚠ couldn't download RBA rates ({exc}); using the rates already saved", fg="yellow")
        try:
            rows = tax_rows(conn, tz, start_ms, end_ms)
        except LookupError as exc:
            typer.secho(f"✗ {exc}", fg="red", err=True)
            raise typer.Exit(1) from None
        account = (conn.execute("SELECT value FROM bot_state WHERE key = 'mode'").fetchone() or ["demo"])[0]
    folder = settings.app.reports_dir / "tax"
    out = write_tax_csv(rows, folder / f"scout_trades_FY{fy}_{account}.csv")
    write_notes(folder / "README_for_accountant.txt")
    totals = tax_summary(rows)
    typer.echo(f"FY{fy} (1 Jul {fy - 1} – 30 Jun {fy}): {totals['opens']} opened, {totals['closes']} closed · "
               f"realised {totals['realised_aud']:+,.2f} AUD ({totals['realised_usd']:+,.2f} USD) · "
               f"fees {totals['fees_aud']:,.2f} AUD")
    typer.secho(f"✓ Saved {out} (+ README_for_accountant.txt)", fg="green")
    if account.lower() != "live":
        typer.secho(f"Note: these are {account.upper()} (fake-money) trades: not real, not taxable. The same "
                    "export works for real trades later.", fg="yellow")


@app.command()
def doctor(
    offline: Annotated[bool, typer.Option("--offline", help="Skip the network checks.")] = False,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Check config, secrets, database, disk, logs, backups, the service, sleep, network, rates and iMessage."""
    settings = _start(config, env_file)
    checks = asyncio.run(run_checks(settings, env_file, network=not offline))
    marks = {"ok": ("✓", "green"), "warn": ("⚠", "yellow"), "fail": ("✗", "red")}
    for c in checks:
        mark, colour = marks[c.status]
        typer.secho(f"{mark} {c.name}: ", fg=colour, nl=False, bold=True)
        typer.echo(c.detail)
        if c.fix:
            typer.echo(f"    fix: {c.fix}")
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    typer.echo(f"\n{len(checks)} checks: {len(checks) - failed - warned} OK, {warned} to look at, {failed} failing.")
    if failed:
        raise typer.Exit(1)


# ------------------------------------------------------------ the experiment

@app.command("news")
def news_command(
    coin: Annotated[str | None, typer.Option("--coin", help="Only headlines about this coin, e.g. SOL.")] = None,
    hours: Annotated[int, typer.Option("--hours", min=1, help="How far back to look.")] = 24,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Latest crypto news headlines, and which ones the experiment would act on."""
    settings = _start(config, env_file)
    headlines, errors = asyncio.run(news.fetch_headlines(settings.news))
    for name, error in errors.items():
        typer.secho(f"⚠ {name}: {error}", fg="yellow")
    since = now_ms() - hours * 3_600_000
    shown = sorted((h for h in headlines if h.published_ms >= since and (coin is None or h.mentions(coin))),
                   key=lambda h: -h.published_ms)
    tz = settings.app.tz
    for h in shown:
        flag = "⚠ " if h.serious else "  "
        typer.echo(f"{flag}{datetime.fromtimestamp(h.published_ms / 1000, tz):%a %H:%M}  {h.source:<16} {h.title}")
    typer.echo(f"\n{len(shown)} headlines in the last {hours}h"
               + (f" mentioning {coin}" if coin else "") + ". ⚠ = serious words (hack, rug pull, delisting...).")
    if coin:
        verdict = news.judge(coin, None, shown)
        typer.echo(f"Verdict for {coin}: " + ("BLOCK buys / SELL longs — " if verdict.danger else "no action")
                   + (f" ({verdict.why})" if verdict.why else ""))
    typer.echo(f"{settings.experiment.name} only acts on serious headlines about smaller coins; "
               f"{settings.demo.name} ignores news.")


experiment_app = typer.Typer(help="A separate fake account testing copy trading (70%) and high-risk coins (30%).",
                             no_args_is_help=True)
app.add_typer(experiment_app, name="experiment")


def experiment_stream(settings: Settings):
    """Live prices including spot coins (tests replace this to run offline)."""
    return stream_mids(settings.data.ws_url, include_spot=True)


def _experiment_engine(settings: Settings, conn, fetch_prices: bool = False) -> DemoEngine:
    xs = experiment_settings(settings)
    engine = DemoEngine(xs, conn, notifier=Notifier(conn, xs, make_backends(settings), label=settings.experiment.name))
    if fetch_prices and engine.account.positions():
        async def mids():
            async with make_client(settings) as client:
                return await client.all_mids(include_spot=True)
        try:
            engine.prices.update(asyncio.run(mids()), now_ms())
        except HyperliquidError as exc:
            typer.secho(f"⚠ couldn't fetch live prices ({exc})", fg="yellow")
    return engine


@experiment_app.command("run")
def experiment_run(
    minutes: Annotated[float | None, typer.Option("--minutes", min=0, help="Stop after this many minutes.")] = None,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Run the experiment (copy trading + high-risk coins) on live prices with fake money."""
    settings = _start(config, env_file)
    xs = experiment_settings(settings)
    lock = xs.app.db_path.parent / "experiment.lock"
    try:
        with single_instance(lock), closing(open_db(xs.app.db_path)) as conn:
            engine = _experiment_engine(settings, conn)
            typer.echo(engine.on_start(by_service=_by_service()))
            experiment = Experiment(engine, make_client, echo=typer.echo)
            engine.event("INFO", "experiment", f"{settings.experiment.name} started (Scout v{APP_VERSION}).")
            conn.commit()
            typer.echo(f"{settings.experiment.name}: copy trading {settings.experiment.copy_pct:g}% + high-risk coins "
                       f"{settings.experiment.high_risk_pct:g}%, fake money, no stop losses. Ctrl+C to stop.")

            async def nothing(_: DemoEngine) -> None:
                return None

            awake = keep_awake(os.getpid()) if settings.service.keep_awake and sys.platform == "darwin" else None
            runner = DemoRunner(engine, nothing, lambda: experiment_stream(settings), echo=typer.echo,
                                jobs=experiment.jobs())
            try:
                asyncio.run(_run_until_stopped(runner, minutes))
            except Exception as exc:
                engine.record_crash(f"{type(exc).__name__}: {exc}")
                raise
            finally:
                if awake is not None:
                    awake.terminate()
            engine.event("INFO", "experiment", f"{settings.experiment.name} stopped.")
            conn.commit()
    except RuntimeError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        typer.echo("\nStopped. Open positions stay in the experiment account.")


@experiment_app.command("status")
def experiment_status(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """The experiment's scorecard, open positions per strategy and the wallets it follows."""
    settings = _start(config, env_file)
    xs = experiment_settings(settings)
    if not xs.app.db_path.exists():
        typer.echo("The experiment hasn't started yet: `uv run scout experiment run` (or install-service).")
        return
    with closing(open_db(xs.app.db_path)) as conn:
        engine = _experiment_engine(settings, conn, fetch_prices=True)
        followed = copytrade.wallets_from_json(engine.state.get("followed_wallets"))
        typer.echo(scorecard_text(engine, followed, settings.app.db_path))
        beat = service.read_heartbeat(service.heartbeat_path(xs))
        if beat:
            typer.echo(f"Loop: heartbeat {(now_ms() - beat['ts_ms']) / 1000:.0f}s ago, v{beat['version']}")
        for p in engine.account.positions():
            price = engine.prices.prices.get(p.coin, p.entry_price)
            pnl = p.unrealised_usd(price) - p.fees_usd - p.funding_usd
            typer.echo(f"  [{p.strategy}] {p.source if p.strategy == 'high_risk' else p.coin} {p.side} "
                       f"{_aud(pnl, xs, signed=True)} ({pnl / (p.qty * p.entry_price) * 100:+.1f}%)")


@experiment_app.command("wallets")
def experiment_wallets(
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-rank wallets now (takes several minutes).")] = False,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """The wallets the experiment copies, and why they were picked."""
    settings = _start(config, env_file)
    xs = experiment_settings(settings)
    with closing(open_db(xs.app.db_path)) as conn:
        engine = _experiment_engine(settings, conn)
        if refresh:
            typer.echo("Checking the leaderboard and the best candidates' trades (this takes a few minutes)…")
            asyncio.run(Experiment(engine, make_client, echo=typer.echo).refresh_wallets())
        followed = copytrade.wallets_from_json(engine.state.get("followed_wallets"))
    if not followed:
        typer.echo("No wallets picked yet (the experiment picks them when it starts, or use --refresh).")
        return
    for n, s in enumerate(followed, start=1):
        typer.echo(f"{n:>2}. {s.wallet}  30d {s.return_pct:+.1f}% (US${s.pnl_usd:,.0f} on US${s.account_usd:,.0f}), "
                   f"{s.closing_trades} trades, {s.win_rate_pct:.0f}% won, profit factor {s.profit_factor:.2f}, "
                   f"profitable {s.good_windows}/3 stretches, open positions {s.open_pnl_usd:+,.0f} US$")


@experiment_app.command("install-service")
def experiment_install_service(config: ConfigOption = Path("config.yaml"),
                               env_file: EnvFileOption = Path(".env")) -> None:
    """Run the experiment in the background too: starts at login, restarts after a crash."""
    settings = _start(config, env_file)
    scout_bin, project, config_path, env_path = _project_paths(config, env_file)
    path = service.install(settings, scout_bin, project, config_path, env_path, command=("experiment", "run"),
                           label=service.experiment_label(settings))
    typer.secho(f"✓ Installed {path.name}: the experiment now runs in the background.", fg="green")


@experiment_app.command("uninstall-service")
def experiment_uninstall_service(config: ConfigOption = Path("config.yaml"),
                                 env_file: EnvFileOption = Path(".env")) -> None:
    """Stop the background experiment and remove its service (its account is kept)."""
    settings = _start(config, env_file)
    removed = service.uninstall(settings, label=service.experiment_label(settings))
    typer.echo("✓ Experiment service removed." if removed else "It wasn't installed.")


# ------------------------------------------------------------ SMART:3

smart_app = typer.Typer(help="SMART:3: Bitcoin trend + a long/short ranking of the most-traded coins (own fake account).",
                        no_args_is_help=True)
app.add_typer(smart_app, name="smart")


def _smart_engine(settings: Settings, conn, fetch_prices: bool = False) -> DemoEngine:
    ss = smart_settings(settings)
    engine = DemoEngine(ss, conn, notifier=Notifier(conn, ss, make_backends(settings), label=settings.smart.name))
    if fetch_prices and engine.account.positions():
        async def mids():
            async with make_client(settings) as client:
                return await client.all_mids()
        try:
            engine.prices.update(asyncio.run(mids()), now_ms())
        except HyperliquidError as exc:
            typer.secho(f"⚠ couldn't fetch live prices ({exc})", fg="yellow")
    return engine


def _other_accounts(settings: Settings) -> dict[str, Path]:
    return {settings.demo.name: settings.app.db_path, settings.experiment.name: settings.experiment.db_path}


@smart_app.command("run")
def smart_run(
    minutes: Annotated[float | None, typer.Option("--minutes", min=0, help="Stop after this many minutes.")] = None,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Run SMART:3 on live prices with fake money."""
    settings = _start(config, env_file)
    ss = smart_settings(settings)
    lock = ss.app.db_path.parent / "smart.lock"
    try:
        with single_instance(lock), closing(open_db(ss.app.db_path)) as conn:
            engine = _smart_engine(settings, conn)
            typer.echo(engine.on_start(by_service=_by_service()))
            trader = SmartTrader(engine, make_client, echo=typer.echo, others=_other_accounts(settings))
            engine.event("INFO", "smart", f"{settings.smart.name} started (Scout v{APP_VERSION}).")
            conn.commit()
            s = settings.smart
            typer.echo(f"{s.name}: {s.btc_pct:g}% Bitcoin trend + {s.long_pct:g}% long / {s.short_pct:g}% short "
                       f"ranking, rebalanced daily after {s.rebalance_after_utc:%H:%M} UTC. Ctrl+C to stop.")

            async def nothing(_: DemoEngine) -> None:
                return None

            awake = keep_awake(os.getpid()) if settings.service.keep_awake and sys.platform == "darwin" else None
            runner = DemoRunner(engine, nothing, lambda: stream_mids(settings.data.ws_url), echo=typer.echo,
                                jobs=trader.jobs())
            try:
                asyncio.run(_run_until_stopped(runner, minutes))
            except Exception as exc:
                engine.record_crash(f"{type(exc).__name__}: {exc}")
                raise
            finally:
                if awake is not None:
                    awake.terminate()
            engine.event("INFO", "smart", f"{settings.smart.name} stopped.")
            conn.commit()
    except RuntimeError as exc:
        typer.secho(f"✗ {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        typer.echo("\nStopped. Open positions stay in the SMART:3 account.")


@smart_app.command("status")
def smart_status(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """SMART:3's balance, positions and today's plan."""
    settings = _start(config, env_file)
    ss = smart_settings(settings)
    if not ss.app.db_path.exists():
        typer.echo(f"{settings.smart.name} hasn't started yet: `uv run scout smart run` (or install-service).")
        return
    with closing(open_db(ss.app.db_path)) as conn:
        engine = _smart_engine(settings, conn, fetch_prices=True)
        typer.echo(smart_update_text(engine, _other_accounts(settings)))
        plan = engine.state.get("smart_last_plan")
        if plan:
            typer.echo(f"Last plan ({engine.state.get('smart_rebalanced_day')}): {plan}")
        beat = service.read_heartbeat(service.heartbeat_path(ss))
        if beat:
            typer.echo(f"Loop: heartbeat {(now_ms() - beat['ts_ms']) / 1000:.0f}s ago, v{beat['version']}")


@smart_app.command("backtest")
def smart_backtest(
    start: Annotated[str, typer.Option("--start", help="First day (YYYY-MM-DD).")] = "2023-09-01",
    end: Annotated[str | None, typer.Option("--end", help="Last day (default: the latest data).")] = None,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Test SMART:3 on past daily candles (already downloaded by the main demo), against holding Bitcoin."""
    settings = _start(config, env_file)
    with closing(open_db(settings.app.db_path)) as conn:
        coins = [r[0] for r in conn.execute("SELECT DISTINCT coin FROM candles WHERE interval = '1d'")]
        daily = {c: smart.frames_from_candles(load_candles(conn, c, "1d")) for c in coins}
    daily = {c: f for c, f in daily.items() if not f.empty}
    if smart.BTC not in daily:
        typer.secho("✗ No daily candles yet: run `uv run scout backtest` once to download them.", fg="red")
        raise typer.Exit(1)
    last = max(f.index[-1] for f in daily.values())
    first, final = pd.Timestamp(start), pd.Timestamp(end) if end else last
    typer.echo(f"Testing {settings.smart.name} from {first:%d %b %Y} to {final:%d %b %Y} on {len(daily)} coins…")
    result = smart.backtest(daily, settings, first, final, progress=typer.echo)
    rate = 1 / settings.demo.aud_to_usdc_rate
    table = Table(title=f"{settings.smart.name} vs holding Bitcoin")
    for column in ("", settings.smart.name, "Hold BTC"):
        table.add_column(column)
    for row in stats_table(result.stats, result.btc_stats, rate):
        table.add_row(*row)
    table.add_row("Sharpe (return per unit of risk)", f"{smart.sharpe(result.equity['strategy']):.2f}",
                  f"{smart.sharpe(result.equity['btc_hold']):.2f}")
    Console().print(table)
    for line in verdict(result.stats, result.btc_stats):
        typer.echo(f"• {line}")
    stamp = datetime.now(settings.app.tz).strftime("%Y%m%d_%H%M")
    out = settings.app.reports_dir / f"smart_{first:%Y%m%d}_{final:%Y%m%d}_{stamp}"
    plot_equity(result.equity, result.stats, result.btc_stats, out.with_suffix(".png"),
                f"{settings.smart.name} backtest", rate)
    write_trades_csv(result.trades, out.with_suffix(".csv"), settings.app.tz, rate)
    typer.echo(f"Chart: {out.with_suffix('.png')}\nTrades: {out.with_suffix('.csv')}")


@smart_app.command("install-service")
def smart_install_service(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Run SMART:3 in the background: starts at login, restarts after a crash."""
    settings = _start(config, env_file)
    scout_bin, project, config_path, env_path = _project_paths(config, env_file)
    path = service.install(settings, scout_bin, project, config_path, env_path, command=("smart", "run"),
                           label=service.smart_label(settings))
    typer.secho(f"✓ Installed {path.name}: {settings.smart.name} now runs in the background.", fg="green")


@smart_app.command("uninstall-service")
def smart_uninstall_service(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Stop the background SMART:3 and remove its service (its account is kept)."""
    settings = _start(config, env_file)
    removed = service.uninstall(settings, label=service.smart_label(settings))
    typer.echo(f"✓ {settings.smart.name} service removed." if removed else "It wasn't installed.")


# ------------------------------------------------------------ public status page

status_page_app = typer.Typer(help="STATUS.md: all three accounts on one page, for helpers following on GitHub.",
                              no_args_is_help=True)
app.add_typer(status_page_app, name="status-page")


def _status_accounts(settings: Settings) -> list[status_page.AccountInfo]:
    s, x = settings, settings.experiment
    return [
        status_page.AccountInfo(s.demo.name, "Buys coins breaking out of their recent range when the market mood is "
                                "healthy; stop loss on every trade; long only.", s),
        status_page.AccountInfo(x.name, f"{x.copy_pct:g}% copies new positions of top Hyperliquid wallets; "
                                f"{x.high_risk_pct:g}% buys small coins with sudden price and volume jumps; "
                                "news headlines as a safety check.", experiment_settings(s)),
        status_page.AccountInfo(s.smart.name, f"{s.smart.btc_pct:g}% Bitcoin while above its {s.smart.btc_ma_days}-day "
                                f"average; {s.smart.long_pct:g}% long the {s.smart.picks} best-ranked coins and "
                                f"{s.smart.short_pct:g}% short the {s.smart.picks} worst, rebalanced daily.",
                                smart_settings(s)),
    ]


@status_page_app.command("make")
def status_page_make(
    publish: Annotated[bool, typer.Option("--publish", help="Also push it to GitHub (the `status` branch).")] = False,
    config: ConfigOption = Path("config.yaml"),
    env_file: EnvFileOption = Path(".env"),
) -> None:
    """Write reports/STATUS.md (and with --publish, put it on GitHub)."""
    settings = _start(config, env_file)
    project = config.resolve().parent

    async def mids() -> dict[str, float]:
        async with make_client(settings) as client:
            return await client.all_mids(include_spot=True)

    try:
        prices = asyncio.run(mids())
    except HyperliquidError as exc:
        typer.secho(f"✗ couldn't fetch live prices ({exc}); not updating the page.", fg="red", err=True)
        raise typer.Exit(1) from None
    text = status_page.build_page(_status_accounts(settings), prices, now_ms(), settings,
                                  status_page.current_commit(project))
    path = settings.app.reports_dir / "STATUS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    typer.echo(f"✓ Wrote {path}")
    if publish:
        try:
            commit = status_page.publish(project, text)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            typer.secho(f"✗ couldn't publish: {exc}", fg="red", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"✓ Published to the `{status_page.STATUS_BRANCH}` branch ({commit[:7]}).")


@status_page_app.command("install-service")
def status_page_install(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Refresh and publish STATUS.md every hour in the background."""
    settings = _start(config, env_file)
    scout_bin, project, config_path, env_path = _project_paths(config, env_file)
    path = service.install(settings, scout_bin, project, config_path, env_path,
                           command=("status-page", "make", "--publish"), label=service.status_label(settings),
                           every_seconds=3600)
    typer.secho(f"✓ Installed {path.name}: STATUS.md is published every hour.", fg="green")


@status_page_app.command("uninstall-service")
def status_page_uninstall(config: ConfigOption = Path("config.yaml"), env_file: EnvFileOption = Path(".env")) -> None:
    """Stop publishing STATUS.md."""
    settings = _start(config, env_file)
    removed = service.uninstall(settings, label=service.status_label(settings))
    typer.echo("✓ Status page service removed." if removed else "It wasn't installed.")
