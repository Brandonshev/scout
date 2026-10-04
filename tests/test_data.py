import asyncio
import json
from contextlib import closing

import pytest
from websockets.asyncio.server import serve

from scout.data import (
    INTERVAL_MS,
    Candle,
    HyperliquidClient,
    HyperliquidError,
    MarketCoin,
    WeightLimiter,
    backfill,
    format_price,
    load_candles,
    parse_market,
    parse_mids,
    save_candles,
    save_market_snapshot,
    stream_mids,
)
from scout.db import open_db
from tests.conftest import load_fixture
from tests.fake_hyperliquid import FakeHyperliquid

pytestmark = pytest.mark.anyio

NOW = 1_790_000_000_000  # a fixed "now" (Sep 2026) so tests are repeatable
HOUR = INTERVAL_MS["1h"]


class FakeSleep:
    def __init__(self, clock: list[float] | None = None) -> None:
        self.calls: list[float] = []
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock[0] += seconds


def make_client(fake: FakeHyperliquid, **kwargs) -> HyperliquidClient:
    kwargs.setdefault("sleep", FakeSleep())
    return HyperliquidClient(transport=fake.transport(), **kwargs)


@pytest.fixture
def conn(tmp_path):
    with closing(open_db(tmp_path / "scout.db")) as connection:
        yield connection


# ------------------------------------------------------------ parsing


def test_parse_market_fixture():
    coins = {c.coin: c for c in parse_market(load_fixture("meta_and_asset_ctxs.json"))}
    btc = coins["BTC"]
    assert btc.mark_price > 1000
    assert btc.volume_24h_usd > 0
    assert btc.open_interest > 0
    assert isinstance(btc.funding_rate, float)
    assert not btc.is_delisted
    delisted = [c for c in coins.values() if c.is_delisted]
    assert delisted, "fixture should include a delisted coin"
    assert all(c.mid_price is None for c in delisted)


def test_market_coin_derived_values():
    coin = MarketCoin(
        coin="X", is_delisted=False, max_leverage=10, mark_price=110.0, mid_price=110.0,
        prev_day_price=100.0, volume_24h_usd=5e6, open_interest=1000.0, funding_rate=0.0000125,
    )
    assert coin.change_24h_pct == pytest.approx(10.0)
    assert coin.open_interest_usd == pytest.approx(110_000)
    assert coin.funding_annual_pct == pytest.approx(10.95)


def test_parse_mids_drops_spot_markets():
    mids = parse_mids(load_fixture("all_mids.json"))
    assert "BTC" in mids and isinstance(mids["BTC"], float)
    assert not any(name.startswith("@") or "/" in name for name in mids)


def test_candle_fixture_parses():
    candles = [Candle.model_validate(raw) for raw in load_fixture("candles_btc_4h.json")]
    first = candles[0]
    assert first.coin == "BTC" and first.interval == "4h"
    assert first.close_time_ms == first.open_time_ms + INTERVAL_MS["4h"] - 1
    assert first.low <= first.open <= first.high


@pytest.mark.parametrize(
    ("price", "text"),
    [(82692.5, "82,692.50"), (1.5, "1.50"), (0.24501, "0.2450"), (0.0000123456, "0.00001235")],
)
def test_format_price(price, text):
    assert format_price(price) == text


# ------------------------------------------------------------- client


async def test_client_market_and_mids():
    fake = FakeHyperliquid(NOW)
    async with make_client(fake) as client:
        coins = await client.market()
        mids = await client.all_mids()
    assert "BTC" in {c.coin for c in coins}
    assert "BTC" in mids
    assert [r["type"] for r in fake.requests] == ["metaAndAssetCtxs", "allMids"]


async def test_candles_are_paged_sorted_and_unique():
    fake = FakeHyperliquid(NOW, page_size=100)
    start = NOW - 30 * 24 * HOUR
    async with make_client(fake) as client:
        candles = await client.candles("BTC", "1h", start, NOW)
    times = [c.open_time_ms for c in candles]
    assert len(fake.requests) > 5  # needed several pages
    assert times == sorted(set(times))
    assert all(b - a == HOUR for a, b in zip(times, times[1:]))
    assert times[0] >= start


async def test_retries_rate_limit_and_server_errors():
    fake = FakeHyperliquid(NOW)
    fake.failures = [429, 503]
    sleep = FakeSleep()
    async with make_client(fake, sleep=sleep) as client:
        await client.all_mids()
    assert len(fake.requests) == 3
    assert len(sleep.calls) == 2
    assert sleep.calls[1] > sleep.calls[0]  # waits grow


async def test_bad_request_is_not_retried():
    fake = FakeHyperliquid(NOW)
    fake.failures = [400]
    async with make_client(fake) as client:
        with pytest.raises(HyperliquidError, match="rejected"):
            await client.all_mids()
    assert len(fake.requests) == 1


async def test_gives_up_after_max_retries():
    fake = FakeHyperliquid(NOW)
    fake.failures = [500] * 10
    async with make_client(fake, max_retries=2) as client:
        with pytest.raises(HyperliquidError, match="after 3 attempts"):
            await client.all_mids()
    assert len(fake.requests) == 3


async def test_weight_limiter_waits_when_budget_is_spent():
    clock = [0.0]
    sleep = FakeSleep(clock)
    limiter = WeightLimiter(60, clock=lambda: clock[0], sleep=sleep)  # 1 weight per second
    await limiter.acquire(60)
    assert sleep.calls == []
    await limiter.acquire(30)
    assert sum(sleep.calls) == pytest.approx(30)
    limiter.charge(10)  # extra weight learned after a response
    await limiter.acquire(1)
    assert sum(sleep.calls) == pytest.approx(41)


# ------------------------------------------------------------ storage


def test_save_candles_skips_duplicates(conn):
    candles = [Candle.model_validate(raw) for raw in load_fixture("candles_btc_4h.json")]
    assert save_candles(conn, candles) == len(candles)
    assert save_candles(conn, candles) == 0
    assert conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == len(candles)
    assert load_candles(conn, "BTC", "4h") == sorted(candles, key=lambda c: c.open_time_ms)


def test_save_market_snapshot(conn):
    coins = parse_market(load_fixture("meta_and_asset_ctxs.json"))
    assert save_market_snapshot(conn, coins, NOW) == len(coins)
    assert save_market_snapshot(conn, coins, NOW) == 0


async def test_backfill_one_year_of_4h(conn):
    fake = FakeHyperliquid(NOW)
    async with make_client(fake) as client:
        result = await backfill(client, conn, "BTC", "4h", days=365, now=NOW)
    assert not result.limited_by_history
    assert result.new == pytest.approx(365 * 6, abs=1)


async def test_backfill_reports_hyperliquid_history_limit(conn):
    fake = FakeHyperliquid(NOW)
    async with make_client(fake) as client:
        result = await backfill(client, conn, "BTC", "1h", days=365, now=NOW)
    assert result.limited_by_history  # 365 days of 1h is 8760 candles; only 5000 exist
    assert result.new == 4999  # the 5000th is still in progress, so it isn't stored


async def test_backfill_only_downloads_what_is_missing(conn):
    fake = FakeHyperliquid(NOW)
    async with make_client(fake) as client:
        await backfill(client, conn, "BTC", "1h", days=30, now=NOW)
        fake.requests.clear()
        again = await backfill(client, conn, "BTC", "1h", days=30, now=NOW)
        assert again.new == 0
        fake.now_ms = NOW + 3 * HOUR
        later = await backfill(client, conn, "BTC", "1h", days=30, now=NOW + 3 * HOUR)
        assert later.new == 3
        older = await backfill(client, conn, "BTC", "1h", days=40, now=NOW + 3 * HOUR)
        assert older.new == 10 * 24 - 3  # 40 days back from 3 hours later
    stored = [c.open_time_ms for c in load_candles(conn, "BTC", "1h")]
    assert stored == sorted(set(stored))
    assert all(b - a == HOUR for a, b in zip(stored, stored[1:]))  # no gaps
    assert all(c.is_closed(NOW + 3 * HOUR) for c in load_candles(conn, "BTC", "1h"))


# ---------------------------------------------------------- websocket


async def test_price_stream_reconnects_after_disconnect():
    message = load_fixture("ws_all_mids.json")
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        subscribe = json.loads(await ws.recv())
        assert subscribe["subscription"]["type"] == "allMids"
        await ws.send(json.dumps({"channel": "subscriptionResponse", "data": subscribe}))
        mids = dict(message["data"]["mids"], BTC=str(1000 * connections))
        await ws.send(json.dumps({"channel": "allMids", "data": {"mids": mids}}))
        # then hang up, as happens when the Mac sleeps or the network drops

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        updates = []
        async with asyncio.timeout(5):
            async for mids in stream_mids(f"ws://127.0.0.1:{port}", base_delay=0.01):
                updates.append(mids)
                if len(updates) == 2:
                    break
    assert [u["BTC"] for u in updates] == [1000.0, 2000.0]
    assert not any(name.startswith("@") for name in updates[0])


async def test_backfill_extends_history_further_back_later(conn):
    """A coin first fetched for 30 days must get older candles when 365 days are asked for later."""
    fake = FakeHyperliquid(NOW)
    async with make_client(fake) as client:
        await backfill(client, conn, "BTC", "1d", days=30, now=NOW)
        more = await backfill(client, conn, "BTC", "1d", days=365, now=NOW)
    assert more.new == 335
    assert len(load_candles(conn, "BTC", "1d")) == 365


async def test_backfill_remembers_a_coin_has_no_older_history(conn):
    fake = FakeHyperliquid(NOW)
    fake.listed_from["NEW"] = NOW - 20 * 24 * HOUR  # listed 20 days ago
    async with make_client(fake) as client:
        first = await backfill(client, conn, "NEW", "1d", days=365, now=NOW)
        assert first.new == 20
        fake.requests.clear()
        await backfill(client, conn, "NEW", "1d", days=365, now=NOW)
    # Only the newest range is checked again, not the 345 days before the listing
    assert len(fake.requests) == 1
    assert fake.requests[0]["req"]["startTime"] > NOW - 2 * 24 * HOUR
