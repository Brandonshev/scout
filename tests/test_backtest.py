"""Backtester tests on synthetic (seeded random) markets.

The most important one is test_no_look_ahead: it scrambles every candle after a date
and checks that nothing the backtest did before that date changed.
"""

import csv
import math

import numpy as np
import pandas as pd
import pytest

from scout.backtest import (
    DayPlan,
    History,
    Portfolio,
    Trade,
    _shortlist_on,
    build_plans,
    compute_stats,
    make_windows,
    max_drawdown,
    plot_equity,
    settings_with,
    simulate,
    stop_fill,
    verdict,
    walk_forward,
    write_trades_csv,
)

START = pd.Timestamp("2023-01-01", tz="UTC")
TEST_START = pd.Timestamp("2025-01-01", tz="UTC")
TEST_END = pd.Timestamp("2025-06-01", tz="UTC")
COINS = ("BTC", "AAA", "BBB", "CCC", "DDD", "EEE")


def make_candles(seed: int = 7, days: int = 900, delist: dict | None = None) -> dict[str, pd.DataFrame]:
    """Random 4h markets with booms and busts (a slow sine wave in the drift) and volume spikes."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(START, periods=days * 6, freq="4h")
    cycle = np.sin(np.arange(len(index)) / (6 * 160) * 2 * np.pi)
    out = {}
    for k, coin in enumerate(COINS):
        returns = rng.normal(0.0006 + 0.004 * cycle, 0.010 + 0.002 * k)
        close = 100 * (k + 1) * np.exp(np.cumsum(returns))
        open_ = np.r_[close[0], close[:-1]]
        wick = np.abs(rng.normal(0, 0.004, len(index)))
        frame = pd.DataFrame(
            {
                "open": open_,
                "high": np.maximum(open_, close) * (1 + wick),
                "low": np.minimum(open_, close) * (1 - wick),
                "close": close,
                "dollar_volume": rng.lognormal(14, 0.6, len(index)),
            },
            index=index,
        )
        if delist and coin in delist:
            frame = frame[frame.index < delist[coin]]
        out[coin] = frame
    return out


def to_daily(candles: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "dollar_volume": "sum"}
    return {coin: frame.resample("1D").agg(agg).dropna() for coin, frame in candles.items()}


def history_of(candles: dict[str, pd.DataFrame]) -> History:
    return History(to_daily(candles), candles)


@pytest.fixture
def cfg(settings):
    """Settings scaled down to the synthetic market (6 coins, small volumes)."""
    return settings.model_copy(update={
        "scanner": settings.scanner.model_copy(update={"min_24h_volume_usd": 1e5, "universe_size": 6, "max_coins": 4}),
        "regime": settings.regime.model_copy(update={"breadth_min_coins": 3}),
    })


@pytest.fixture(scope="module")
def market():
    return make_candles()


# -------------------------------------------------------------- the pipeline


def run(cfg, candles, start=TEST_START, end=TEST_END, **kwargs):
    data = history_of(candles)
    plans = build_plans(data, cfg, start, end)
    return simulate(data, plans, cfg, start, end, **kwargs), plans


def test_backtest_trades_and_the_books_balance(cfg, market):
    result, _ = run(cfg, market)
    assert result.stats.trades > 0
    # Accounting identity: with everything closed, the change in the account = the sum of trade results
    total_pnl = sum(t.pnl_usd for t in result.trades)
    assert result.stats.end_equity - result.stats.start_equity == pytest.approx(total_pnl, abs=1e-6)
    assert all(t.entry_reason and t.exit_reason for t in result.trades)
    assert all(t.fees_usd > 0 for t in result.trades)
    assert 0 < result.stats.time_in_market_pct < 100


def test_no_look_ahead(cfg, market):
    """Scramble every candle from `cut` onwards. Nothing decided before `cut` may change."""
    cut = pd.Timestamp("2025-03-01", tz="UTC")
    rng = np.random.default_rng(99)
    poisoned = {}
    for coin, frame in market.items():
        frame = frame.copy()
        future = frame.index >= cut
        factor = rng.uniform(0.3, 3.0, future.sum())
        for column in ("open", "high", "low", "close"):
            frame.loc[future, column] *= factor
        frame.loc[future, "dollar_volume"] *= 50
        poisoned[coin] = frame

    honest, honest_plans = run(cfg, market, close_at_end=False)
    scrambled, scrambled_plans = run(cfg, poisoned, close_at_end=False)

    # The daily mood and shortlist up to the cut are identical
    for day in pd.date_range(TEST_START, cut, freq="D"):
        assert honest_plans[day] == scrambled_plans[day], day
    # The account value at every step up to the cut is identical
    before = honest.equity.index <= cut
    pd.testing.assert_frame_equal(honest.equity[before], scrambled.equity[scrambled.equity.index <= cut])
    # Every trade that finished before the cut is identical, and trades opened before it opened the same way
    cut_ms = cut.timestamp() * 1000
    assert [t for t in honest.trades if t.exit_ts_ms <= cut_ms] == [t for t in scrambled.trades if t.exit_ts_ms <= cut_ms]
    opened = lambda trades: [(t.coin, t.entry_ts_ms, t.entry_price, t.qty, t.entry_reason)  # noqa: E731
                             for t in trades if t.entry_ts_ms <= cut_ms]
    assert opened(honest.trades) == opened(scrambled.trades)
    assert any(t.entry_ts_ms <= cut_ms for t in honest.trades), "test needs trades before the cut to mean anything"
    # ...and the scramble really did change what happened afterwards
    assert not honest.equity.equals(scrambled.equity)


def test_benchmark_is_btc_bought_at_the_start(cfg, market):
    result, _ = run(cfg, market)
    btc = market["BTC"]["close"]
    first, last = btc[btc.index >= TEST_START].iloc[0], btc[btc.index < TEST_END].iloc[-1]
    expected = (last / first - 1) * 100
    assert result.btc_stats.total_return_pct == pytest.approx(expected, abs=0.3)  # minus fees and slippage
    assert result.btc_stats.total_return_pct < expected


def test_higher_costs_mean_lower_returns(cfg, market):
    cheap, _ = run(cfg, market)
    pricey_cfg = cfg.model_copy(update={"risk": cfg.risk.model_copy(update={"taker_fee_pct": 0.5, "slippage_pct": 0.5})})
    pricey, _ = run(pricey_cfg, market)
    assert pricey.stats.fees_usd > cheap.stats.fees_usd
    assert pricey.stats.end_equity < cheap.stats.end_equity


# -------------------------------------------------------- survivorship bias


def test_shortlist_uses_volume_ranking_of_the_time():
    days = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")

    def frame(volume, n=120, price=10.0):
        closes = price * 1.01 ** np.arange(n)
        return pd.DataFrame({"open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
                             "dollar_volume": np.full(n, volume)}, index=days[:n])

    daily = {
        "BTC": frame(5e9, price=50_000),
        "DEAD": frame(4e9, n=100),  # big back then, delisted on day 100
        "SMALL": frame(3e7),
    }
    data = History(daily)
    from scout.config import ScannerSettings

    cfg = ScannerSettings(universe_size=2, max_coins=2, min_listing_days=10, min_open_interest_usd=0)
    positions = {c: f.index for c, f in daily.items()}
    assert "DEAD" in _shortlist_on(days[90], data, positions, cfg)  # included while it was trading
    assert "SMALL" not in _shortlist_on(days[90], data, positions, cfg)  # outranked at the time
    after = _shortlist_on(days[110], data, positions, cfg)
    assert "DEAD" not in after and "SMALL" in after


def test_delisted_coin_is_sold_when_it_stops_trading(cfg):
    candles = make_candles(delist={"CCC": pd.Timestamp("2025-02-15", tz="UTC")})
    data = history_of(candles)
    plans = {day: DayPlan(day, plan.market, ("CCC",)) for day, plan in build_plans(data, cfg, TEST_START, TEST_END).items()}
    result = simulate(data, plans, cfg, TEST_START, TEST_END)
    ccc = [t for t in result.trades if t.coin == "CCC"]
    assert all(t.exit_ts_ms <= pd.Timestamp("2025-02-16", tz="UTC").timestamp() * 1000 for t in ccc)
    assert any("delisted" in t.exit_reason for t in ccc) or all("stop" in t.exit_reason or "Selling" in t.exit_reason
                                                                 for t in ccc)


# ---------------------------------------------------------------- costs


def test_portfolio_charges_fees_slippage_and_funding():
    book = Portfolio(1000.0, taker_fee_pct=0.045, slippage_pct=0.05, funding_hourly_pct=0.002)
    book.open("X", "long", 1.0, 100.0, 95.0, 0, "test", "RISK_ON")
    assert book.holdings["X"].entry_price == pytest.approx(100.05)  # paid slippage
    assert book.cash == pytest.approx(1000 - 100.05 - 100.05 * 0.00045)
    book.charge_funding({"X": 100.0}, hours=4)
    assert book.holdings["X"].funding_usd == pytest.approx(100 * 0.00002 * 4)
    trade = book.close("X", 110.0, 1, "test")
    exit_fill = 110 * (1 - 0.0005)
    fees = 100.05 * 0.00045 + exit_fill * 0.00045
    assert trade.exit_price == pytest.approx(exit_fill)
    assert trade.pnl_usd == pytest.approx(exit_fill - 100.05 - fees - 0.008)
    assert book.cash == pytest.approx(1000 + trade.pnl_usd)


def test_short_accounting_mirrors_long():
    book = Portfolio(1000.0, 0.0, 0.0, 0.002)
    book.open("X", "short", 2.0, 100.0, 105.0, 0, "test", "RISK_OFF")
    assert book.equity({"X": 90.0}) == pytest.approx(1020)
    book.charge_funding({"X": 100.0}, hours=1)  # shorts receive the assumed funding
    trade = book.close("X", 90.0, 1, "test")
    assert trade.pnl_usd == pytest.approx(20 + 2 * 100 * 0.00002)


def test_stop_fills_including_gaps():
    assert stop_fill(True, 95, open_=100, high=101, low=94) == 95  # touched during the candle
    assert stop_fill(True, 95, open_=90, high=92, low=85) == 90  # opened below the stop: worse price
    assert stop_fill(True, 95, open_=100, high=101, low=96) is None
    assert stop_fill(False, 105, open_=110, high=112, low=108) == 110


# ---------------------------------------------------------------- stats


def test_max_drawdown():
    times = pd.date_range("2025-01-01", periods=5, freq="D")
    dd, peak, bottom = max_drawdown(pd.Series([100, 120, 90, 130, 100], index=times))
    assert dd == pytest.approx(25)  # 120 -> 90
    assert (peak, bottom) == (times[1], times[2])


def test_trade_stats():
    trade = lambda pnl: Trade("X", "long", 0, 100, 1, 95, 1, 100, 0.1, 0.0, pnl, 1, "RISK_ON", "a", "b")  # noqa: E731
    equity = pd.Series([100.0, 110.0], index=pd.date_range("2025-01-01", periods=2, freq="D"))
    stats = compute_stats(equity, [trade(10), trade(5), trade(-5)], pd.Series([True, False]))
    assert stats.trades == 3
    assert stats.win_rate_pct == pytest.approx(200 / 3)
    assert stats.avg_win_usd == pytest.approx(7.5) and stats.avg_loss_usd == pytest.approx(-5)
    assert stats.profit_factor == pytest.approx(3)
    assert stats.time_in_market_pct == 50


def test_verdict_is_honest():
    equity = pd.Series([100.0, 105.0], index=pd.date_range("2025-01-01", periods=2, freq="D"))
    btc = pd.Series([100.0, 150.0], index=equity.index)
    lines = verdict(compute_stats(equity), compute_stats(btc))
    assert lines[0].startswith("DID NOT BEAT holding Bitcoin")
    assert any("too few to be confident" in line for line in lines)


# --------------------------------------------------------- walk-forward


def test_windows_never_test_on_training_data():
    windows = make_windows(pd.Timestamp("2024-07-01"), pd.Timestamp("2026-09-01"), 180, 90)
    assert len(windows) >= 6
    for w in windows:
        assert w.train_start < w.train_end == w.test_start < w.test_end
    for a, b in zip(windows, windows[1:]):
        assert b.test_start == a.test_end  # test periods follow on, with no gaps or overlaps


def test_walk_forward_picks_settings_from_training_only(cfg, market):
    wf = cfg.backtest.walk_forward.model_copy(update={"train_days": 60, "test_days": 30, "min_trades": 1,
                                                      "grid": {"breakout_periods": [10, 20]}})
    wf_cfg = cfg.model_copy(update={"backtest": cfg.backtest.model_copy(update={"walk_forward": wf})})
    data = history_of(market)
    end = pd.Timestamp("2025-05-01", tz="UTC")  # 120 days: two 60-day train + 30-day test windows
    plans = build_plans(data, wf_cfg, TEST_START, end)
    result = walk_forward(data, plans, wf_cfg, TEST_START, end)
    assert len(result.windows) == 2
    for r in result.windows:
        assert r.params["breakout_periods"] in (10, 20)
        assert r.test.start == r.window.train_end
    # Scrambling data after the first training period can't change what was picked for it
    first = result.windows[0]
    poisoned = {c: f.assign(close=np.where(f.index >= first.window.train_end, f["close"] * 3, f["close"]))
                for c, f in market.items()}
    again = walk_forward(history_of(poisoned), build_plans(history_of(poisoned), wf_cfg, TEST_START, end),
                         wf_cfg, TEST_START, end)
    assert again.windows[0].params == first.params
    assert math.isfinite(result.stats.total_return_pct)


def test_settings_with_validates():
    from scout.config import Settings

    base = Settings.model_construct(signals=__import__("scout.config", fromlist=["SignalSettings"]).SignalSettings())
    with pytest.raises(ValueError):
        settings_with(base, {"stop_atr_multiple": 5.0})  # trailing (3×) must be ≥ the first stop


# -------------------------------------------------------------- reports


def test_reports(cfg, market, tmp_path):
    result, _ = run(cfg, market)
    csv_path = write_trades_csv(result.trades, tmp_path / "trades.csv", cfg.app.tz, 1 / 0.65)
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == len(result.trades)
    assert rows[0]["entry_reason"].startswith(("Buying", "Shorting"))
    png = plot_equity(result.equity, result.stats, result.btc_stats, tmp_path / "equity.png", "test", 1 / 0.65)
    assert png.read_bytes()[:4] == b"\x89PNG"
