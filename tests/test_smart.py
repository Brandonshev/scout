"""SMART:3: the ranking, the plan, the backtest and the live trader, on made-up prices (nothing real)."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from scout import smart
from scout.config import SmartSettings
from scout.data import Candle, MarketCoin
from scout.db import open_db
from scout.demo import DemoEngine
from scout.notify import Priority
from scout.service import build_plist, heartbeat_path, log_name, smart_label
from scout.smart_trader import SmartTrader, smart_settings, update_text
from tests.test_experiment import Clock, Outbox

pytestmark = pytest.mark.anyio
CFG = SmartSettings(universe=15, picks=3, keep_within=6)
DAY_MS = 86_400_000


def market(days=150, coins=20, seed=1, btc_drift=0.004, start="2026-01-01"):
    """Made-up daily candles: coin Cn drifts up when n is high and down when n is low; BTC trends up."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=days, freq="D")
    out = {}
    for n in range(coins):
        name = "BTC" if n == coins - 1 else f"C{n:02d}"
        drift = btc_drift if name == "BTC" else (n - coins / 2) * 0.002
        vol = 0.02 + 0.002 * (n % 5)
        close = 10 * np.exp(np.cumsum(drift + vol * rng.standard_normal(days)))
        out[name] = pd.DataFrame({"open": close / (1 + 0.002), "high": close * 1.02, "low": close * 0.98,
                                  "close": close, "volume": np.full(days, 1e6 * (coins - n + 50))}, index=index)
    return out


# ------------------------------------------------------------ the ranking


def test_the_universe_is_the_most_traded_coins_with_enough_history_never_btc():
    data = market()
    data["NEW"] = data["C10"].iloc[-30:] * 1  # only 30 days old
    data["NEW"]["volume"] = 1e12  # hugely traded, but too new
    panels = smart.build_panels(data, CFG)
    day = panels.close.index[-1]
    ranked = set(panels.score.loc[day].dropna().index)
    assert "BTC" not in ranked and "NEW" not in ranked and len(ranked) == 15


def test_hysteresis_keeps_held_coins_until_they_drop_far():
    scores = pd.Series({f"C{i}": float(10 - i) for i in range(10)})  # C0 best
    assert smart.choose(scores, set(), 3, 6) == ["C0", "C1", "C2"]
    assert smart.choose(scores, {"C5"}, 3, 6) == ["C5", "C0", "C1"]  # #6: kept
    assert smart.choose(scores, {"C7"}, 3, 6) == ["C0", "C1", "C2"]  # #8: dropped


def test_the_plan_buys_the_strongest_and_shorts_the_weakest():
    panels = smart.build_panels(market(), CFG)
    day = panels.close.index[-1]
    p = smart.plan(panels, day, {}, CFG)
    assert p.btc_on  # BTC trended up
    longs, shorts = {k.coin for k in p.longs}, {k.coin for k in p.shorts}
    assert len(longs) == len(shorts) == 3 and not longs & shorts
    mean = lambda coins: np.mean([int(c[1:]) for c in coins])
    assert mean(longs) > mean(shorts)  # high-numbered coins drift up
    assert sum(k.weight for k in p.longs) == pytest.approx(1)
    assert all(k.reason.startswith(f"#{k.rank} best of") for k in p.longs)
    assert all(k.reason.startswith(f"#{k.rank} worst of") for k in p.shorts)


def test_no_peeking_at_the_future():
    data = market()
    panels = smart.build_panels(data, CFG)
    day = panels.close.index[120]
    before = smart.plan(panels, day, {}, CFG)
    for frame in data.values():  # wildly change everything after `day`
        frame.loc[frame.index > day, ["close", "high", "low", "open"]] *= 7
        frame.loc[frame.index > day, "volume"] *= 0.01
    after = smart.plan(smart.build_panels(data, CFG), day, {}, CFG)
    assert [(k.coin, k.side) for k in before.picks] == [(k.coin, k.side) for k in after.picks]
    assert before.btc_on == after.btc_on


def test_bitcoin_half_goes_to_cash_in_a_downtrend():
    panels = smart.build_panels(market(btc_drift=-0.01), CFG)
    p = smart.plan(panels, panels.close.index[-1], {}, CFG)
    assert not p.btc_on and not p.wants("BTC", "long")
    assert "below its 50-day average" in smart.btc_reason(p, CFG)


def test_sizes_and_stops():
    panels = smart.build_panels(market(), CFG)
    p = smart.plan(panels, panels.close.index[-1], {}, CFG)
    assert smart.target_usd(p, None, 1000, CFG) == pytest.approx(490)  # 50% of 98%
    assert sum(smart.target_usd(p, k, 1000, CFG) for k in p.shorts) == pytest.approx(245)
    assert smart.emergency_stop(100, 4, "long", CFG) == 80 and smart.emergency_stop(100, 4, "short", CFG) == 120
    assert smart.emergency_stop(100, 4, "long", CFG.model_copy(update={"stop_atr": 0})) is None


def test_settings_never_exceed_one_x():
    with pytest.raises(ValidationError, match="can't be more than 100"):
        SmartSettings(btc_pct=60, long_pct=25, short_pct=25)
    with pytest.raises(ValidationError, match="keep_within"):
        SmartSettings(picks=6, keep_within=3)


# ------------------------------------------------------------ backtest


def test_backtest_trades_both_halves_and_compares_with_bitcoin(settings):
    data = market(days=200)
    s = settings.model_copy(update={"smart": CFG})
    result = smart.backtest(data, s, data["BTC"].index[90], data["BTC"].index[-1], CFG)
    sides = {(t.coin == "BTC", t.side) for t in result.trades}
    assert {(True, "long"), (False, "long"), (False, "short")} <= sides
    assert list(result.equity.columns) == ["strategy", "btc_hold"]
    assert result.stats.fees_usd > 0 and result.stats.trades == len(result.trades)
    assert result.stats.end_equity > result.stats.start_equity  # the made-up market rewards the ranking


# ------------------------------------------------------------ live trader


def candles_of(frame, coin):
    return [Candle.model_validate({"t": int(ts.timestamp() * 1000), "T": int(ts.timestamp() * 1000) + DAY_MS - 1,
                                   "s": coin, "i": "1d", "o": r.open, "h": r.high, "l": r.low, "c": r.close,
                                   "v": r.volume, "n": 1})
            for ts, r in frame.iterrows()]


class FakeClient:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def market(self):
        return [MarketCoin(c, False, 3, float(f["close"].iat[-1]), float(f["close"].iat[-1]),
                           float(f["close"].iat[-2]), float(f["volume"].iat[-1] * f["close"].iat[-1]), 0.0, 0.0001)
                for c, f in self.data.items()]

    async def candles(self, coin, interval, start, end):
        frame = self.data[coin]
        return [c for c in candles_of(frame, coin) if start <= c.open_time_ms <= end]


@pytest.fixture
def desk(settings, tmp_path):
    data = market(start="2026-01-01")
    s = settings.model_copy(update={"smart": CFG.model_copy(update={"db_path": tmp_path / "smart.db"})})
    ss = smart_settings(s)
    clock = Clock()
    last_day = data["BTC"].index[-1]
    clock.ms = int((last_day + pd.Timedelta(days=1, minutes=20)).timestamp() * 1000)  # 00:20 UTC the next day
    conn = open_db(ss.app.db_path)
    engine = DemoEngine(ss, conn, clock=clock)
    engine.notifier = Outbox()
    engine.prices.update({c: float(f["close"].iat[-1]) for c, f in data.items()}, clock())
    client = FakeClient(data)
    trader = SmartTrader(engine, lambda _: client, echo=lambda _: None)
    yield trader, engine, client, clock, data
    conn.close()


async def test_daily_rebalance_opens_both_halves_once_a_day(desk):
    trader, engine, client, clock, data = desk
    await trader.rebalance_if_due()
    held = {(p.coin, p.side) for p in engine.account.positions()}
    assert ("BTC", "long") in held
    assert sum(side == "short" for _, side in held) == 3 and sum(side == "long" for _, side in held) == 4
    equity = engine.account.equity(engine.prices.prices)
    btc = next(p for p in engine.account.positions() if p.coin == "BTC")
    assert btc.qty * btc.entry_price == pytest.approx(equity * 0.49, rel=0.02)
    assert all(p.has_stop and p.strategy == "smart" for p in engine.account.positions())
    category, priority, text = engine.notifier.messages[-1]
    assert priority is Priority.NORMAL and text.startswith("🧠 SMART:3 daily rebalance")
    trades = len(engine.conn.execute("SELECT * FROM demo_orders").fetchall())
    clock.ms += 3_600_000
    await trader.rebalance_if_due()
    assert len(engine.conn.execute("SELECT * FROM demo_orders").fetchall()) == trades  # once a day


async def test_waits_for_yesterdays_candle(desk):
    trader, engine, client, clock, data = desk
    clock.ms += DAY_MS  # a day later, but the fake exchange has no new candle
    await trader.rebalance_if_due()
    assert engine.account.positions() == []


async def test_btc_downtrend_sells_the_bitcoin_half(desk):
    trader, engine, client, clock, data = desk
    await trader.rebalance_if_due()
    # Next day: a crash pushes BTC below its 50-day average.
    day = data["BTC"].index[-1] + pd.Timedelta(days=1)
    for coin, frame in data.items():
        last = frame.iloc[-1].copy()
        if coin == "BTC":
            last[["open", "high", "low", "close"]] *= 0.6
        frame.loc[day] = last
    clock.ms += DAY_MS
    engine.prices.update({c: float(f["close"].iat[-1]) for c, f in data.items()}, clock())
    await trader.rebalance_if_due()
    assert "BTC" not in {p.coin for p in engine.account.positions()}
    reason = engine.conn.execute("SELECT close_reason FROM demo_positions WHERE coin = 'BTC'").fetchone()[0]
    assert reason.startswith("[SMART] Selling BTC: Bitcoin closed at") and "trend is down" in reason


async def test_daily_update_at_8pm(desk, tmp_path):
    trader, engine, client, clock, data = desk
    await trader.rebalance_if_due()
    text = update_text(engine)
    assert text.startswith("🧠 SMART:3 daily update") and "Bitcoin half: holding" in text and "Short: " in text


def test_own_service_logs_and_heartbeat(settings):
    ss = smart_settings(settings)
    assert heartbeat_path(ss).name == "heartbeat-smart.json"
    label = smart_label(settings)
    assert log_name(settings, label) == "smart" and log_name(settings, None) == "service"
    plist = build_plist(settings, Path("/bin/scout"), Path("/p"), Path("/p/config.yaml"), Path("/p/.env"),
                        command=("smart", "run"), label=label)
    assert "smart" in plist["ProgramArguments"] and plist["Label"] == label
    assert plist["StandardOutPath"].endswith("smart.out.log")


def test_risk_rules_allow_the_plan(settings):
    ss = smart_settings(settings)
    assert not ss.risk.require_stop and ss.risk.max_position_pct >= 50 and ss.signals.allow_shorts
    assert ss.risk.max_open_positions == 2 * settings.smart.picks + 1 and ss.risk.max_leverage == 1
