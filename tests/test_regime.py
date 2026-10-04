"""Market mood tests on synthetic (made-up) prices, one scenario per mood."""

import dataclasses
import json
import sqlite3
from contextlib import closing

import numpy as np
import pandas as pd
import pytest

from scout.config import RegimeSettings
from scout.db import open_db
from scout.indicators import average_true_range, percentile_rank
from scout.regime import (
    NotEnoughData,
    Regime,
    Volatility,
    classify,
    detect,
    history,
    plot_history,
    regime_stats,
    save_reading,
    top_coins_at,
)
from scout.version import APP_VERSION

CFG = RegimeSettings()
DAYS = 420
NORMAL_FUNDING = [0.0000125] * 30  # ≈ 11% a year, Hyperliquid's baseline
CROWDED_FUNDING = [0.0001] * 30  # ≈ 88% a year


def path(start: float, daily_pct: float, n: int = DAYS + 1) -> np.ndarray:
    """Prices growing (or shrinking) by daily_pct each day. Last value = 'now'."""
    return start * (1 + daily_pct / 100) ** np.arange(n)


def btc(prices: np.ndarray, range_pct: float | np.ndarray = 2.0) -> tuple[pd.DataFrame, float]:
    """Finished daily candles for all but the last price, plus the last price as 'now'."""
    closed = prices[:-1]
    index = pd.date_range("2025-01-01", periods=len(closed), freq="D", tz="UTC")
    half = np.asarray(range_pct) / 200
    frame = pd.DataFrame({"high": closed * (1 + half), "low": closed * (1 - half), "close": closed}, index=index)
    return frame, float(prices[-1])


def coins(n_up: int, n_down: int, up_pct: float = 0.3, down_pct: float = -0.3) -> tuple[pd.DataFrame, dict]:
    series = {f"UP{i}": path(10, up_pct, 80) for i in range(n_up)}
    series |= {f"DOWN{i}": path(10, down_pct, 80) for i in range(n_down)}
    closes = pd.DataFrame({name: values[:-1] for name, values in series.items()})
    return closes, {name: float(values[-1]) for name, values in series.items()}


def run(prices, coin_data, funding=NORMAL_FUNDING, range_pct=2.0, cfg=CFG):
    frame, price_now = btc(prices, range_pct)
    closes, coin_prices = coin_data
    return detect(frame, price_now, closes, coin_prices, funding, cfg, ts_ms=0)


# ------------------------------------------------------------ the moods


def test_uptrend_with_broad_strength_is_risk_on():
    reading = run(path(50_000, 0.3), coins(30, 0))
    assert reading.regime is Regime.RISK_ON
    assert reading.points == {"trend": 4, "breadth": 2, "crowding": 0}
    assert reading.breadth_pct == 100
    assert reading.allows_new_longs
    assert "uptrend" in reading.summary
    assert "above its short- and long-term averages" in reading.summary


def test_downtrend_is_risk_off_and_blocks_longs():
    reading = run(path(100_000, -0.3), coins(0, 30))
    assert reading.regime is Regime.RISK_OFF
    assert reading.points["trend"] == -4
    assert reading.points["breadth"] == -2
    assert not reading.allows_new_longs
    assert "No new long trades" in reading.summary


def test_sideways_market_is_neutral():
    prices = np.full(DAYS + 1, 80_000.0)
    prices[-1] = 80_400  # a little above both (flat) averages
    reading = run(prices, coins(15, 15))
    assert reading.regime is Regime.NEUTRAL
    assert reading.points == {"trend": 2, "breadth": 0, "crowding": 0}
    assert "flat" in reading.summary
    assert "no clear direction" in reading.summary


def test_narrow_crowded_rally_is_only_neutral():
    # Bitcoin rising, but only 5 of 30 coins joining in, and everyone paying high funding to bet on more.
    reading = run(path(50_000, 0.3), coins(5, 25), funding=CROWDED_FUNDING)
    assert reading.points == {"trend": 4, "breadth": -2, "crowding": -1}
    assert reading.regime is Regime.NEUTRAL
    assert reading.funding_annual_pct == pytest.approx(87.6)
    assert "Funding is high" in reading.summary


def test_never_risk_on_while_btc_is_below_its_200_day_average():
    assert classify(5, btc_above_slow_ma=False, cfg=CFG) is Regime.NEUTRAL
    assert classify(5, btc_above_slow_ma=True, cfg=CFG) is Regime.RISK_ON
    relaxed = RegimeSettings(require_btc_above_slow_ma_for_risk_on=False)
    assert classify(5, btc_above_slow_ma=False, cfg=relaxed) is Regime.RISK_ON


def test_thresholds_come_from_config():
    strict = RegimeSettings(risk_on_min_score=7, risk_off_max_score=-7)
    assert run(path(50_000, 0.3), coins(30, 0), cfg=strict).regime is Regime.NEUTRAL


# ----------------------------------------------------------- volatility


def ranges(last_days_pct: float) -> np.ndarray:
    r = np.full(DAYS, 2.0)
    r[-20:] = last_days_pct
    return r


def test_wild_volatility_halves_position_size():
    reading = run(np.full(DAYS + 1, 80_000.0), coins(15, 15), range_pct=ranges(6.0))
    assert reading.volatility is Volatility.WILD
    assert reading.size_multiplier == 0.5
    assert "cut to 50%" in reading.summary


def test_calm_volatility():
    reading = run(np.full(DAYS + 1, 80_000.0), coins(15, 15), range_pct=ranges(0.5))
    assert reading.volatility is Volatility.CALM
    assert reading.size_multiplier == 1.0


def test_steady_volatility_is_normal():
    reading = run(np.full(DAYS + 1, 80_000.0), coins(15, 15))
    assert reading.volatility is Volatility.NORMAL
    assert reading.atr_pct == pytest.approx(2.0, rel=0.01)


def test_atr_counts_overnight_gaps():
    frame = pd.DataFrame({"high": [101, 111], "low": [99, 109], "close": [100, 110]})
    true_range = average_true_range(frame, 1)
    assert true_range.iloc[1] == 11  # from yesterday's close (100) to today's high (111)


def test_percentile_rank_counts_ties_half():
    assert percentile_rank(pd.Series([1.0, 1.0, 1.0, 1.0]), 1.0) == 50
    assert percentile_rank(pd.Series([1.0, 2.0, 3.0, 4.0]), 5.0) == 100


# -------------------------------------------------------- missing data


def test_not_enough_btc_history():
    with pytest.raises(NotEnoughData, match="need 210"):
        run(path(50_000, 0.3, 100), coins(30, 0))


def test_breadth_unknown_scores_zero():
    closes, prices = coins(30, 0)
    reading = run(path(50_000, 0.3), (closes.iloc[-10:], prices))  # only 10 days of coin history
    assert reading.breadth_pct is None
    assert reading.points["breadth"] == 0
    assert any("Breadth 0: unknown" in reason for reason in reading.reasons)


# -------------------------------------------------------------- history


def up_then_down(n: int = 600) -> np.ndarray:
    return np.concatenate([path(30_000, 0.4, n // 2), path(30_000 * 1.004 ** (n // 2), -0.4, n // 2)])


def history_inputs(prices: np.ndarray):
    index = pd.date_range("2024-01-01", periods=len(prices), freq="D", tz="UTC")
    frame = pd.DataFrame({"high": prices * 1.01, "low": prices * 0.99, "close": prices}, index=index)
    closes = pd.DataFrame({f"C{i}": prices * (1 + i / 100) for i in range(30)}, index=index)
    volume = pd.DataFrame({f"C{i}": np.full(len(prices), 1e6 * (i + 1)) for i in range(30)}, index=index)
    return frame, closes, volume


def test_history_sees_both_up_and_down_phases():
    readings = history(*history_inputs(up_then_down()), CFG, days=400)
    moods = {r.regime for r in readings}
    assert Regime.RISK_ON in moods and Regime.RISK_OFF in moods
    assert readings[0].ts_ms < readings[-1].ts_ms


def test_history_never_uses_future_data():
    frame, closes, volume = history_inputs(up_then_down())
    full = history(frame, closes, volume, CFG, days=400)
    cut = 450
    partial = history(frame.iloc[:cut], closes.iloc[:cut], volume.iloc[:cut], CFG, days=400)
    by_time = {r.ts_ms: r for r in full}
    for reading in partial:
        same_day = by_time[reading.ts_ms]
        assert (reading.regime, reading.score, reading.breadth_pct) == (
            same_day.regime, same_day.score, same_day.breadth_pct,
        )


def test_breadth_universe_uses_volume_at_the_time():
    index = pd.date_range("2025-01-01", periods=60, freq="D", tz="UTC")
    volume = pd.DataFrame({"OLDSTAR": [1e9] * 30 + [1.0] * 30, "NEWSTAR": [1.0] * 30 + [1e9] * 30}, index=index)
    assert top_coins_at(volume, index[30], 1) == ["OLDSTAR"]
    assert top_coins_at(volume, index[-1], 1) == ["NEWSTAR"]


def test_regime_stats():
    base = run(path(50_000, 0.3), coins(30, 0))
    readings = [
        dataclasses.replace(base, regime=Regime.RISK_ON, btc_price=100.0),
        dataclasses.replace(base, regime=Regime.RISK_ON, btc_price=110.0),
        dataclasses.replace(base, regime=Regime.RISK_OFF, btc_price=99.0),
        dataclasses.replace(base, regime=Regime.RISK_OFF, btc_price=99.0),
    ]
    stats = regime_stats(readings)
    assert stats[Regime.RISK_ON].days == 2
    assert stats[Regime.RISK_ON].avg_next_day_pct == pytest.approx((10 + -10) / 2)
    assert stats[Regime.RISK_OFF].avg_next_day_pct == pytest.approx(0)
    assert stats[Regime.NEUTRAL].days == 0 and stats[Regime.NEUTRAL].avg_next_day_pct is None


# ------------------------------------------------------ storage + chart


def test_save_reading(tmp_path):
    reading = run(path(50_000, 0.3), coins(30, 0))
    with closing(open_db(tmp_path / "scout.db")) as conn:
        save_reading(conn, reading)
        conn.commit()
        row = conn.execute("SELECT * FROM regime_history").fetchone()
    assert row["regime"] == "RISK_ON"
    assert row["risk_level"] == "NORMAL"
    assert row["reason"] == reading.summary
    assert row["app_version"] == APP_VERSION
    assert json.loads(row["details_json"])["points"]["trend"] == 4


def test_old_database_gets_new_column(tmp_path):
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:  # regime_history as created by v0.1.0/v0.2.0
        conn.execute(
            "CREATE TABLE regime_history (id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, regime TEXT NOT NULL, "
            "risk_level TEXT, score REAL, reason TEXT NOT NULL, app_version TEXT NOT NULL)"
        )
    with closing(open_db(db_path)) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(regime_history)")}
    assert "details_json" in columns


def test_plot_history_saves_png(tmp_path):
    readings = history(*history_inputs(up_then_down()), CFG, days=300)
    out = plot_history(readings, CFG, tmp_path / "reports" / "mood.png", note="test")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
