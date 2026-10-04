"""Market data from Hyperliquid's public info API (no account needed).

Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

- REST: POST {"type": ...} to /info. Numbers arrive as strings; the models
  below turn them into floats.
- Rate limit: 1200 "weight" per minute per IP. allMids costs 2, most other
  requests 20, and candleSnapshot costs 1 more per 60 candles returned.
- Only the most recent 5000 candles per coin and interval are available.
- Websocket: subscribe to allMids for live prices. The server drops
  connections that are silent for 60s, so we ping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from scout.config import Settings
from scout.db import now_ms
from scout.version import APP_VERSION

log = logging.getLogger(__name__)

INTERVAL_MS = {
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}
MAX_CANDLES_AVAILABLE = 5000
DAY_MS = INTERVAL_MS["1d"]

WEIGHT_LIGHT = 2  # allMids, l2Book
WEIGHT_DEFAULT = 20  # metaAndAssetCtxs, candleSnapshot
CANDLES_PER_EXTRA_WEIGHT = 60

Sleep = Callable[[float], Awaitable[None]]


class HyperliquidError(RuntimeError):
    """The API refused a request or kept failing after retries."""


# ---------------------------------------------------------------- models


class _ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)


class Candle(_ApiModel):
    """One candle: what the price did during one time slot (e.g. one hour)."""

    coin: str = Field(alias="s")
    interval: str = Field(alias="i")
    open_time_ms: int = Field(alias="t")
    close_time_ms: int = Field(alias="T")
    open: float = Field(alias="o")
    high: float = Field(alias="h")
    low: float = Field(alias="l")
    close: float = Field(alias="c")
    volume: float = Field(alias="v")  # in coins, not dollars
    trades: int = Field(alias="n")

    def is_closed(self, at_ms: int) -> bool:
        return self.close_time_ms < at_ms


class AssetMeta(_ApiModel):
    name: str
    sz_decimals: int = Field(alias="szDecimals")
    max_leverage: int = Field(alias="maxLeverage")
    is_delisted: bool = Field(False, alias="isDelisted")


class AssetContext(_ApiModel):
    mark_price: float = Field(alias="markPx")
    mid_price: float | None = Field(None, alias="midPx")
    oracle_price: float = Field(alias="oraclePx")
    prev_day_price: float = Field(alias="prevDayPx")
    day_notional_volume: float = Field(alias="dayNtlVlm")
    open_interest: float = Field(alias="openInterest")
    funding: float
    premium: float | None = None


class BookLevel(_ApiModel):
    price: float = Field(alias="px")
    size: float = Field(alias="sz")  # in coins
    orders: int = Field(alias="n")


class L2Book(_ApiModel):
    """Order book snapshot: at most 20 price levels per side, best price first."""

    coin: str
    time_ms: int = Field(alias="time")
    levels: tuple[list[BookLevel], list[BookLevel]]

    @property
    def bids(self) -> list[BookLevel]:
        return self.levels[0]

    @property
    def asks(self) -> list[BookLevel]:
        return self.levels[1]


@dataclass(frozen=True)
class SpotCoin:
    """A spot pair: `pair` is the trading name ("@107" or "PURR/USDC"), `name` the token's own name."""

    pair: str
    name: str
    mark_price: float
    prev_day_price: float
    volume_24h_usd: float
    sz_decimals: int

    @property
    def change_24h_pct(self) -> float:
        return (self.mark_price / self.prev_day_price - 1) * 100 if self.prev_day_price else 0.0


def parse_spot_market(payload: list[Any]) -> list[SpotCoin]:
    meta, contexts = payload
    tokens = {t["index"]: t for t in meta["tokens"]}
    by_pair = {c["coin"]: c for c in contexts}
    coins = []
    for pair in meta["universe"]:
        ctx = by_pair.get(pair["name"])
        base = tokens.get(pair["tokens"][0], {})
        if ctx is None or ctx.get("markPx") is None:
            continue
        coins.append(SpotCoin(pair["name"], base.get("name", pair["name"]), float(ctx["markPx"]),
                              float(ctx.get("prevDayPx") or 0), float(ctx.get("dayNtlVlm") or 0),
                              int(base.get("szDecimals", 2))))
    return coins


@dataclass(frozen=True)
class WalletPosition:
    coin: str
    size: float  # coins; positive = long, negative = short
    entry_price: float
    value_usd: float
    unrealized_usd: float = 0.0  # profit/loss on the open position so far

    @property
    def side(self) -> str:
        return "long" if self.size > 0 else "short"

    @classmethod
    def from_api(cls, p: dict) -> WalletPosition:
        return cls(p["coin"], float(p["szi"]), float(p.get("entryPx") or 0), abs(float(p.get("positionValue") or 0)),
                   float(p.get("unrealizedPnl") or 0))


LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


async def fetch_leaderboard(url: str = LEADERBOARD_URL, timeout: float = 120.0) -> list[dict]:
    """Hyperliquid's public trader leaderboard (~40 MB). Returns its rows."""
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": f"Scout/{APP_VERSION}"}) as http:
        response = await http.get(url)
        response.raise_for_status()
        return response.json()["leaderboardRows"]


@dataclass(frozen=True)
class MarketCoin:
    """A coin's current market stats (metaAndAssetCtxs, meta and context combined)."""

    coin: str
    is_delisted: bool
    max_leverage: int
    mark_price: float
    mid_price: float | None
    prev_day_price: float
    volume_24h_usd: float
    open_interest: float  # in coins
    funding_rate: float  # per hour, e.g. 0.0000125 = 0.00125% per hour
    sz_decimals: int = 4  # order sizes are rounded to this many decimals

    @property
    def change_24h_pct(self) -> float:
        if not self.prev_day_price:
            return 0.0
        return (self.mark_price / self.prev_day_price - 1) * 100

    @property
    def open_interest_usd(self) -> float:
        return self.open_interest * self.mark_price

    @property
    def funding_annual_pct(self) -> float:
        return self.funding_rate * 24 * 365 * 100


def parse_market(payload: list[Any]) -> list[MarketCoin]:
    meta, contexts = payload
    coins = []
    for raw_meta, raw_ctx in zip(meta["universe"], contexts, strict=True):
        asset = AssetMeta.model_validate(raw_meta)
        ctx = AssetContext.model_validate(raw_ctx)
        coins.append(
            MarketCoin(
                coin=asset.name,
                is_delisted=asset.is_delisted,
                max_leverage=asset.max_leverage,
                mark_price=ctx.mark_price,
                mid_price=ctx.mid_price,
                prev_day_price=ctx.prev_day_price,
                volume_24h_usd=ctx.day_notional_volume,
                open_interest=ctx.open_interest,
                funding_rate=ctx.funding,
                sz_decimals=asset.sz_decimals,
            )
        )
    return coins


def is_perp_name(name: str) -> bool:
    """allMids also returns spot markets, named like "@107" or "PURR/USDC". Scout trades perps only."""
    return not name.startswith("@") and "/" not in name


def parse_mids(mids: dict[str, str], include_spot: bool = False) -> dict[str, float]:
    return {name: float(price) for name, price in mids.items() if include_spot or is_perp_name(name)}


# ---------------------------------------------------------- rate limiting


class WeightLimiter:
    """Token bucket for Hyperliquid's per-minute request weight.

    We budget below the real 1200/min limit so other tools (or a browser tab on
    the Hyperliquid website) from the same IP don't push us over.
    """

    def __init__(
        self,
        weight_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.capacity = float(weight_per_minute)
        self.rate = weight_per_minute / 60.0
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self, weight: int) -> None:
        """Wait until `weight` can be spent, then spend it."""
        async with self._lock:
            self._refill()
            while self.tokens < weight:
                await self._sleep((weight - self.tokens) / self.rate)
                self._refill()
            self.tokens -= weight

    def charge(self, weight: int) -> None:
        """Spend weight we only learn about after a response (may go into debt)."""
        self._refill()
        self.tokens -= weight


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with jitter: ~1s, 2s, 4s, ... up to `cap`."""
    delay = min(cap, base * 2**attempt)
    return delay * random.uniform(0.8, 1.2)


# ----------------------------------------------------------------- client


class HyperliquidClient:
    """Async client for the public info endpoint. Use as `async with HyperliquidClient(...) as hl:`."""

    def __init__(
        self,
        api_url: str = "https://api.hyperliquid.xyz/info",
        *,
        timeout: float = 10.0,
        weight_per_minute: int = 800,
        max_retries: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.api_url = api_url
        self.max_retries = max_retries
        self.limiter = WeightLimiter(weight_per_minute, sleep=sleep)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": f"Scout/{APP_VERSION}"},
        )

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> HyperliquidClient:
        return cls(
            settings.data.api_url,
            timeout=settings.data.request_timeout_seconds,
            weight_per_minute=settings.data.weight_per_minute,
            max_retries=settings.data.max_retries,
            **kwargs,
        )

    async def __aenter__(self) -> HyperliquidClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, body: dict[str, Any], weight: int) -> Any:
        """POST to /info, retrying with backoff on rate limits, server errors and network trouble."""
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire(weight)
            try:
                response = await self._http.post(self.api_url, json=body)
            except httpx.TransportError as exc:
                problem = f"network error: {exc!r}"
                retry_after = None
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code != 429 and response.status_code < 500:
                    raise HyperliquidError(
                        f"{body['type']} rejected ({response.status_code}): {response.text[:200]}"
                    )
                problem = f"HTTP {response.status_code}"
                retry_after = _retry_after_seconds(response)

            if attempt == self.max_retries:
                raise HyperliquidError(f"{body['type']} failed after {attempt + 1} attempts ({problem})")
            delay = retry_after if retry_after is not None else backoff_delay(attempt)
            log.warning("%s: %s, retrying in %.1fs (attempt %d)", body["type"], problem, delay, attempt + 1)
            await self._sleep(delay)
        raise AssertionError("unreachable")

    async def all_mids(self, include_spot: bool = False) -> dict[str, float]:
        """Current mid price of every perp coin (halfway between best buy and sell offers); spot pairs too
        if include_spot."""
        return parse_mids(await self._post({"type": "allMids"}, WEIGHT_LIGHT), include_spot)

    async def l2_book(self, coin: str, sig_figs: int | None = None) -> L2Book:
        """Order book. sig_figs=None is full precision (exact spread); 2-5 groups prices into
        wider buckets so the 20 levels reach further from the price (depth)."""
        body: dict[str, Any] = {"type": "l2Book", "coin": coin}
        if sig_figs is not None:
            body["nSigFigs"] = sig_figs
        return L2Book.model_validate(await self._post(body, WEIGHT_LIGHT))

    async def market(self) -> list[MarketCoin]:
        """Every perp coin with price, 24h volume, open interest and funding rate."""
        return parse_market(await self._post({"type": "metaAndAssetCtxs"}, WEIGHT_DEFAULT))

    async def spot_market(self) -> list[SpotCoin]:
        """Every spot pair (e.g. "@107") with its token name, price and 24h volume."""
        return parse_spot_market(await self._post({"type": "spotMetaAndAssetCtxs"}, WEIGHT_DEFAULT))

    async def positions_of(self, wallet: str) -> list[WalletPosition]:
        """A wallet's open perp positions on the main market (public data)."""
        state = await self._post({"type": "clearinghouseState", "user": wallet}, WEIGHT_LIGHT)
        return [WalletPosition.from_api(p["position"]) for p in state.get("assetPositions", [])]

    async def fills_of(self, wallet: str, start_ms: int) -> list[dict]:
        """A wallet's trades since start_ms (public data, up to 2000 per request)."""
        fills = await self._post({"type": "userFillsByTime", "user": wallet, "startTime": start_ms}, WEIGHT_DEFAULT)
        self.limiter.charge(len(fills) // 20)  # this request costs 1 extra weight per 20 fills returned
        return fills

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        """Candles whose open time is in [start_ms, end_ms], oldest first. Pages through large ranges."""
        step = INTERVAL_MS[interval]
        found: dict[int, Candle] = {}
        cursor = start_ms
        while cursor <= end_ms:
            body = {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": end_ms},
            }
            page = [Candle.model_validate(raw) for raw in await self._post(body, WEIGHT_DEFAULT)]
            self.limiter.charge(len(page) // CANDLES_PER_EXTRA_WEIGHT)
            if not page:
                break
            for candle in page:
                if start_ms <= candle.open_time_ms <= end_ms:
                    found[candle.open_time_ms] = candle
            next_cursor = page[-1].open_time_ms + step
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return [found[t] for t in sorted(found)]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------- storage


def save_candles(conn: sqlite3.Connection, candles: Iterable[Candle]) -> int:
    """Store candles, skipping ones already saved. Returns how many were new. The caller commits."""
    before = conn.total_changes
    conn.executemany(
        """INSERT INTO candles
               (coin, interval, open_time_ms, close_time_ms, open, high, low, close, volume, trades)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (coin, interval, open_time_ms) DO NOTHING""",
        [
            (c.coin, c.interval, c.open_time_ms, c.close_time_ms, c.open, c.high, c.low, c.close, c.volume, c.trades)
            for c in candles
        ],
    )
    return conn.total_changes - before


def stored_range(conn: sqlite3.Connection, coin: str, interval: str) -> tuple[int, int] | None:
    """(oldest, newest) open time stored for this coin and interval, or None if nothing is stored."""
    row = conn.execute(
        "SELECT MIN(open_time_ms), MAX(open_time_ms) FROM candles WHERE coin = ? AND interval = ?",
        (coin, interval),
    ).fetchone()
    return None if row[0] is None else (row[0], row[1])


def coverage_from(conn: sqlite3.Connection, coin: str, interval: str) -> int | None:
    """The earliest time from which this coin's history has been fully downloaded, if known."""
    row = conn.execute("SELECT from_ms FROM candle_coverage WHERE coin = ? AND interval = ?", (coin, interval)).fetchone()
    return None if row is None else row[0]


def history_floor(interval: str, now: int) -> int:
    """The oldest candle Hyperliquid still has for this interval (it keeps the latest 5000)."""
    step = INTERVAL_MS[interval]
    return (now // step - (MAX_CANDLES_AVAILABLE - 1)) * step


def load_candles(conn: sqlite3.Connection, coin: str, interval: str, since_ms: int = 0) -> list[Candle]:
    # Column names match Candle's field names (SQLite names are case-insensitive,
    # so the API's "t"/"T" aliases can't be used here).
    rows = conn.execute(
        """SELECT coin, interval, open_time_ms, close_time_ms, open, high, low, close, volume, trades
           FROM candles WHERE coin = ? AND interval = ? AND open_time_ms >= ?
           ORDER BY open_time_ms""",
        (coin, interval, since_ms),
    ).fetchall()
    return [Candle(**dict(row)) for row in rows]


def save_market_snapshot(conn: sqlite3.Connection, coins: Iterable[MarketCoin], ts_ms: int) -> int:
    """Store a point-in-time copy of the market table. The caller commits."""
    before = conn.total_changes
    conn.executemany(
        """INSERT INTO market_snapshots
               (ts_ms, coin, mark_price, mid_price, volume_24h_usd, open_interest, funding_rate)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (ts_ms, coin) DO NOTHING""",
        [
            (ts_ms, c.coin, c.mark_price, c.mid_price, c.volume_24h_usd, c.open_interest, c.funding_rate)
            for c in coins
        ],
    )
    return conn.total_changes - before


@dataclass(frozen=True)
class BackfillResult:
    coin: str
    interval: str
    requested_from_ms: int
    available_from_ms: int  # the oldest candle Hyperliquid still has for this interval
    fetched: int
    new: int

    @property
    def limited_by_history(self) -> bool:
        """True if we asked for older data than Hyperliquid keeps."""
        return self.available_from_ms > self.requested_from_ms


async def backfill(
    client: HyperliquidClient,
    conn: sqlite3.Connection,
    coin: str,
    interval: str,
    days: int,
    now: int | None = None,
) -> BackfillResult:
    """Download and store the last `days` of closed candles, fetching only what isn't stored yet.

    Only finished (closed) candles are stored: a candle still in progress would change,
    and a strategy that trusted it would be peeking at an unfinished picture.
    """
    now = now_ms() if now is None else now
    step = INTERVAL_MS[interval]
    requested_from = (now - days * DAY_MS) // step * step
    available_from = history_floor(interval, now)
    start = max(requested_from, available_from)

    # Fetch only the gaps: older than what we have (unless already checked), and newer.
    ranges = [(start, now)]
    existing = stored_range(conn, coin, interval)
    covered = coverage_from(conn, coin, interval)
    if existing is not None:
        oldest, newest = existing
        ranges = []
        if start < oldest and (covered is None or start < covered):
            ranges.append((start, oldest - step))
        ranges.append((max(start, newest + step), now))

    fetched = new = 0
    for range_start, range_end in ranges:
        if range_start > range_end:
            continue
        candles = [c for c in await client.candles(coin, interval, range_start, range_end) if c.is_closed(now)]
        fetched += len(candles)
        new += save_candles(conn, candles)
    checked_from = start if covered is None or existing is None else min(covered, start)
    conn.execute(
        "INSERT INTO candle_coverage (coin, interval, from_ms) VALUES (?, ?, ?) "
        "ON CONFLICT (coin, interval) DO UPDATE SET from_ms = MIN(from_ms, excluded.from_ms)",
        (coin, interval, checked_from),
    )
    conn.commit()
    return BackfillResult(coin, interval, requested_from, available_from, fetched, new)


# -------------------------------------------------------------- websocket


async def stream_mids(
    ws_url: str = "wss://api.hyperliquid.xyz/ws",
    *,
    ping_every: float = 30.0,
    silence_timeout: float = 60.0,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    include_spot: bool = False,
) -> AsyncIterator[dict[str, float]]:
    """Yield live mid prices for every perp coin, forever, reconnecting automatically.

    Each update contains every coin, so after a reconnect (e.g. the Mac woke up)
    the very next update is a complete, fresh picture.
    """
    attempt = 0
    subscribe = json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
    while True:
        try:
            async with ws_connect(ws_url) as ws:
                await ws.send(subscribe)
                log.info("price stream connected")
                keepalive = asyncio.create_task(_ping_forever(ws, ping_every))
                try:
                    while True:
                        async with asyncio.timeout(silence_timeout):
                            raw = await ws.recv()
                        message = json.loads(raw)
                        if message.get("channel") == "allMids":
                            attempt = 0
                            yield parse_mids(message["data"]["mids"], include_spot)
                finally:
                    keepalive.cancel()
        except (OSError, TimeoutError, WebSocketException) as exc:
            delay = backoff_delay(attempt, base_delay, max_delay)
            attempt += 1
            log.warning("price stream dropped (%r), reconnecting in %.1fs", exc, delay)
            await asyncio.sleep(delay)


async def _ping_forever(ws: Any, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        await ws.send(json.dumps({"method": "ping"}))


def format_price(price: float) -> str:
    """Readable price: 2 decimals for normal prices, more for tiny ones (≈4 significant digits)."""
    if price >= 1 or price <= 0:
        return f"{price:,.2f}"
    decimals = 3 - math.floor(math.log10(price))
    return f"{price:.{decimals}f}"
