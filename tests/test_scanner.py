"""Scanner tests: recorded Hyperliquid data for the filters, synthetic prices for the scoring."""

import dataclasses
import json
from contextlib import closing

import numpy as np
import pandas as pd
import pytest

from scout.config import ScannerSettings, ScannerWeights
from scout.data import DAY_MS, Candle, L2Book, MarketCoin, parse_market
from scout.db import open_db
from scout.indicators import candle_frame
from scout.scanner import (
    BookStats,
    basic_exclusions,
    book_stats,
    evaluate_coin,
    rank_scans,
    save_scan,
    scan,
    select_universe,
    shortlist,
)
from scout.version import APP_VERSION
from tests.conftest import load_fixture

CFG = ScannerSettings()
FIXTURE_COINS = load_fixture("scanner_fixture_coins.json")
DEEP_BOOK = BookStats(spread_pct=0.01, bid_depth_usd=5e6, ask_depth_usd=5e6)
NOW = pd.Timestamp("2026-06-01", tz="UTC")
NOW_MS = int(NOW.timestamp() * 1000) + 3_600_000  # 1am UTC: yesterday's candle has closed


def market() -> dict[str, MarketCoin]:
    return {c.coin: c for c in parse_market(load_fixture("meta_and_asset_ctxs.json"))}


def recorded_daily(coin: str) -> pd.DataFrame:
    return candle_frame([Candle.model_validate(raw) for raw in load_fixture(f"candles_1d_{coin}.json")])


def recorded_book(coin: str) -> BookStats:
    exact = L2Book.model_validate(load_fixture(f"l2_book_{coin}.json"))
    grouped = L2Book.model_validate(load_fixture(f"l2_book_{coin}_sig3.json"))
    return book_stats(exact, grouped, CFG.depth_band_pct)


def fixture_now_ms() -> int:
    """'Now' at the time the fixtures were recorded: just after BTC's last daily candle."""
    last = load_fixture("candles_1d_BTC.json")[-1]
    return last["T"] + 1


def coin(name: str = "ALT", price: float = 10.0, volume: float = 50e6, oi: float = 1e8) -> MarketCoin:
    return MarketCoin(
        coin=name, is_delisted=False, max_leverage=10, mark_price=price, mid_price=price,
        prev_day_price=price, volume_24h_usd=volume, open_interest=oi, funding_rate=0.0000125,
    )


def daily(closes: np.ndarray, dollar_volume: float = 20e6, range_pct: float = 4.0) -> pd.DataFrame:
    index = pd.date_range(end=NOW - pd.Timedelta(days=1), periods=len(closes), freq="D")
    half = range_pct / 200
    return pd.DataFrame(
        {"high": closes * (1 + half), "low": closes * (1 - half), "close": closes,
         "dollar_volume": np.full(len(closes), dollar_volume)},
        index=index,
    )


def growth(daily_pct: float, days: int = 90, start: float = 10.0) -> np.ndarray:
    """Prices growing by daily_pct a day. The last value is 'now', the rest are finished days."""
    return start * (1 + daily_pct / 100) ** np.arange(days + 1)


BTC_FLAT = daily(np.full(90, 80_000.0))


def evaluate(prices: np.ndarray, *, volume_today: float = 20e6, book=DEEP_BOOK, cfg=CFG, **kwargs):
    return evaluate_coin(
        coin(price=float(prices[-1]), volume=volume_today, **kwargs), daily(prices[:-1]),
        BTC_FLAT, 80_000.0, book, cfg, NOW_MS,
    )


# ---------------------------------------------------- order book (recorded)


def test_btc_book_is_tight_and_deep():
    stats = recorded_book("BTC")
    assert stats.spread_pct < 0.01
    assert stats.depth_usd > 1_000_000


def test_thin_coin_is_excluded_for_its_order_book():
    thin = FIXTURE_COINS["thinnest"]
    stats = recorded_book(thin)
    assert stats.depth_usd < CFG.min_depth_usd
    result = evaluate_coin(
        dataclasses.replace(market()[thin], volume_24h_usd=50e6, open_interest=5e6),
        recorded_daily(thin), recorded_daily("BTC"), market()["BTC"].mark_price, stats, CFG, fixture_now_ms(),
    )
    assert not result.eligible
    assert any("of orders within ±1% of the price" in e for e in result.exclusions)


def test_book_depth_only_counts_orders_near_the_price():
    book = {"coin": "X", "time": 0, "levels": [
        [{"px": "99.9", "sz": "10", "n": 1}, {"px": "98.0", "sz": "1000", "n": 1}],
        [{"px": "100.1", "sz": "20", "n": 1}, {"px": "103.0", "sz": "1000", "n": 1}],
    ]}
    parsed = L2Book.model_validate(book)
    stats = book_stats(parsed, parsed, band_pct=1.0)
    assert stats.spread_pct == pytest.approx(0.2)
    assert stats.bid_depth_usd == pytest.approx(999)  # 98.0 is more than 1% away
    assert stats.ask_depth_usd == pytest.approx(2002)
    assert stats.depth_usd == pytest.approx(999)


# ------------------------------------------------------ filters (recorded)


def test_new_listing_is_excluded():
    new = FIXTURE_COINS["newest_listing"]
    history = recorded_daily(new)
    assert len(history) < CFG.min_listing_days
    result = evaluate_coin(
        dataclasses.replace(market()[new], volume_24h_usd=50e6, open_interest=50e6),
        history, recorded_daily("BTC"), market()["BTC"].mark_price, DEEP_BOOK, CFG, fixture_now_ms(),
    )
    assert not result.eligible
    assert any(e.startswith("listed only") for e in result.exclusions)


def test_real_sol_data_is_measured_and_explained():
    sol = market()["SOL"]
    result = evaluate_coin(
        dataclasses.replace(sol, volume_24h_usd=max(sol.volume_24h_usd, 50e6), open_interest=max(sol.open_interest, 1e6)),
        recorded_daily("SOL"), recorded_daily("BTC"), market()["BTC"].mark_price, recorded_book("SOL"),
        CFG, fixture_now_ms(),
    )
    assert result.eligible, result.exclusions
    assert result.note.startswith("SOL: ")
    assert set(result.points) == {"rs_short", "rs_long", "volume", "trend", "volatility"}
    assert result.metrics["volume_ratio"] > 0


def test_universe_is_top_active_coins_by_volume():
    universe = select_universe(list(market().values()), ScannerSettings(universe_size=5, max_coins=5))
    assert len(universe) == 5
    assert all(not c.is_delisted for c in universe)
    volumes = [c.volume_24h_usd for c in universe]
    assert volumes == sorted(volumes, reverse=True)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"name": "USDE", "price": 1.0}, "stablecoin"),
        ({"volume": 5e6}, "24h volume"),
        ({"oi": 1_000}, "open interest"),
    ],
)
def test_basic_exclusions(kwargs, reason):
    assert any(reason in r for r in basic_exclusions(coin(**kwargs), CFG))


def test_exclude_list():
    assert basic_exclusions(coin("DOGE"), ScannerSettings(exclude_coins=["doge"])) == ["on your exclude list"]


def test_wide_spread_is_excluded():
    result = evaluate(growth(0.0), book=BookStats(spread_pct=0.4, bid_depth_usd=5e6, ask_depth_usd=5e6))
    assert any("spread 0.40%" in e for e in result.exclusions)


def test_unlisted_stablecoin_is_caught_by_behaviour():
    prices = np.full(91, 1.0)
    result = evaluate_coin(coin("NEWUSD", price=1.0), daily(prices[:-1], range_pct=0.1), BTC_FLAT, 80_000.0,
                           DEEP_BOOK, CFG, NOW_MS)
    assert any("behaves like a stablecoin" in e for e in result.exclusions)


# --------------------------------------------------- scoring (synthetic)


def test_strong_coin_note_matches_example():
    # Rising 1% a day while BTC is flat, trading twice its normal volume.
    result = evaluate(growth(1.0), volume_today=40e6)
    assert result.eligible
    assert result.points == {"rs_short": 1, "rs_long": 1, "volume": 1, "trend": 2, "volatility": 0}
    assert result.score == pytest.approx(1 + 1 + 0.5 + 2)
    assert result.note.startswith("ALT: volume 2.0x normal, outperforming BTC this week")
    assert "in an uptrend." in result.note


def test_weak_coin_scores_negative():
    result = evaluate(growth(-1.0), volume_today=5e6, cfg=ScannerSettings(min_24h_volume_usd=1e6))
    assert result.points == {"rs_short": -1, "rs_long": -1, "volume": -1, "trend": -2, "volatility": 0}
    assert result.score < 0
    assert "lagging BTC this week" in result.note
    assert "in a downtrend" in result.note
    assert "volume drying up" in result.note


def test_very_jumpy_coin_loses_a_point():
    prices = growth(0.0)
    result = evaluate_coin(coin(price=10.0), daily(prices[:-1], range_pct=12), BTC_FLAT, 80_000.0, DEEP_BOOK, CFG, NOW_MS)
    assert result.points["volatility"] == -1
    assert "very jumpy" in result.note


def test_pullback_within_uptrend_wording():
    prices = growth(0.5)
    prices[-1] = prices[-18]  # a dip below the 20-day average, still above the 50-day
    result = evaluate(prices)
    assert result.points["trend"] == 0
    assert "pulling back within an uptrend" in result.note


def test_ranking_and_shortlist():
    scans = [
        evaluate(growth(1.0), volume_today=40e6),
        evaluate(growth(-1.0)),
        evaluate(growth(0.3)),
        dataclasses.replace(evaluate(growth(2.0)), exclusions=["on your exclude list"]),
    ]
    ranked = rank_scans(scans)
    assert [s.rank for s in ranked] == [1, 2, 3, None]
    assert ranked[0].score > ranked[1].score > ranked[2].score
    assert ranked[0].reason.startswith("#1, score +4.5 (rs_short +1×1")
    assert len(shortlist(ranked, ScannerSettings(max_coins=2, universe_size=25))) == 2


def test_weights_come_from_config():
    only_volume = ScannerSettings(weights=ScannerWeights(rs_short=0, rs_long=0, volume=1, trend=0, volatility=0))
    quiet_riser = evaluate(growth(1.0), volume_today=20e6, cfg=only_volume)
    busy_faller = evaluate(growth(-1.0), volume_today=40e6, cfg=only_volume)
    assert busy_faller.score > quiet_riser.score


def test_scan_ranks_whole_universe():
    universe = [coin("BTC", price=80_000.0, oi=1e3), coin("UP", price=float(growth(1.0)[-1]), volume=40e6),
                coin("DOWN", price=float(growth(-1.0)[-1]))]
    dailies = {"BTC": BTC_FLAT, "UP": daily(growth(1.0)[:-1]), "DOWN": daily(growth(-1.0)[:-1])}
    books = {"UP": DEEP_BOOK, "DOWN": DEEP_BOOK}  # BTC has no book here, so it must be excluded
    results = scan(universe, dailies, books, CFG, NOW_MS)
    assert [s.coin for s in results if s.eligible] == ["UP", "DOWN"]
    btc = next(s for s in results if s.coin == "BTC")
    assert "order book unavailable" in btc.exclusions


# ------------------------------------------------------------- storage


def test_save_scan_stores_reasons(tmp_path):
    scans = rank_scans([evaluate(growth(1.0), volume_today=40e6), evaluate(growth(0.0), volume_today=5e6, oi=1)])
    with closing(open_db(tmp_path / "scout.db")) as conn:
        save_scan(conn, scans, NOW_MS)
        conn.commit()
        rows = conn.execute("SELECT * FROM scan_results ORDER BY id").fetchall()
    assert [r["passed"] for r in rows] == [1, 0]
    assert rows[0]["rank"] == 1 and rows[1]["rank"] is None
    assert "outperforming BTC" in rows[0]["reason"]
    assert rows[1]["reason"].startswith("Excluded: ")
    assert rows[1]["score"] is None
    assert json.loads(rows[0]["details_json"])["points"]["trend"] == 2
    assert {r["app_version"] for r in rows} == {APP_VERSION}
    assert {r["ts_ms"] for r in rows} == {NOW_MS}


def test_history_days_covers_every_measurement():
    assert CFG.history_days >= CFG.min_listing_days
    assert NOW_MS - CFG.history_days * DAY_MS < NOW_MS


def test_cheaply_excluded_coin_only_shows_the_real_reason():
    # Low-volume coins are skipped before candles and order books are fetched.
    empty = daily(np.array([]))
    result = evaluate_coin(coin(volume=1e6), empty, BTC_FLAT, 80_000.0, None, CFG, NOW_MS)
    assert len(result.exclusions) == 1
    assert "24h volume" in result.exclusions[0]


def test_weak_coins_stay_off_the_shortlist_even_on_quiet_days():
    # Only three coins pass the filters, so all fit in the top 10: the negative one must still be left out.
    scans = rank_scans([evaluate(growth(1.0), volume_today=40e6), evaluate(growth(0.3)),
                        evaluate(growth(-1.0), cfg=ScannerSettings(min_24h_volume_usd=1e6))])
    assert [s.rank for s in scans] == [1, 2, 3]
    assert scans[2].score < 0
    assert len(shortlist(scans, CFG)) == 2
    assert len(shortlist(scans, ScannerSettings(min_score=-100))) == 3  # the old behaviour, if configured
