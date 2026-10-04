"""Replay tests: the demo engine over synthetic past data, on a simulated clock."""

import asyncio
from contextlib import closing

import pandas as pd
import pytest

from scout.backtest import History, build_plans
from scout.config import Mode
from scout.db import open_db
from scout.demo import DemoEngine
from scout.notify import Notifier
from scout.replay import (
    Control,
    ReplayControl,
    ReplayRunner,
    SimClock,
    candle_path,
    parse_speed,
    price_at,
    range_between,
)
from tests.test_backtest import make_candles, to_daily

pytestmark = pytest.mark.anyio

START = pd.Timestamp("2025-01-01", tz="UTC")
END = pd.Timestamp("2025-02-15", tz="UTC")
HOUR = 3_600_000


@pytest.fixture(scope="module")
def market():
    candles = make_candles()
    return History(to_daily(candles), candles)


@pytest.fixture
def replay_settings(settings, tmp_path):
    return settings.model_copy(update={
        "mode": Mode.REPLAY,
        "app": settings.app.model_copy(update={"db_path": tmp_path / "replay.db"}),
        "scanner": settings.scanner.model_copy(update={"min_24h_volume_usd": 1e5, "universe_size": 6, "max_coins": 4}),
        "regime": settings.regime.model_copy(update={"breadth_min_coins": 3}),
    })


def make_runner(cfg, market, tmp_path, control=None, end=END, sleep=None):
    conn = open_db(cfg.app.db_path)
    sim = SimClock(int(START.timestamp() * 1000))
    engine = DemoEngine(cfg, conn, clock=sim, notifier=Notifier(conn, cfg, [], clock=sim))
    plans = build_plans(market, cfg, START, end)
    control = control or ReplayControl(tmp_path / "control.json")
    if not control.path.exists():
        control.write(Control(speed=None))
    runner = ReplayRunner(engine, sim, market, plans, START, end, control,
                          sleep=sleep or (lambda s: asyncio.sleep(0)), echo=lambda _: None)
    return runner, engine, conn


# ------------------------------------------------------------ helpers


def test_parse_speed():
    assert parse_speed("500x") == 500
    assert parse_speed("20000") == 20000
    assert parse_speed("max") is None
    with pytest.raises(ValueError):
        parse_speed("fast")


def test_control_file_round_trip(tmp_path):
    control = ReplayControl(tmp_path / "c.json")
    assert control.read() == Control()  # missing file: defaults
    control.write(Control(paused=True, speed=None, step=3))
    assert control.read() == Control(paused=True, speed=None, step=3)
    (tmp_path / "c.json").write_text("{not json")
    assert control.read() == Control()


def test_price_path_through_a_candle():
    up = pd.Series({"open": 100.0, "high": 110.0, "low": 95.0, "close": 108.0})
    path = candle_path(up, 0, 3 * HOUR)
    assert [p for _, p in path] == [100.0, 95.0, 110.0, 108.0]  # dips first, then rallies
    assert price_at(path, 0) == 100 and price_at(path, 3 * HOUR) == 108
    assert price_at(path, HOUR // 2) == pytest.approx(97.5)
    assert range_between(path, 0, HOUR) == (95.0, 100.0)
    assert range_between(path, HOUR // 2, 2 * HOUR + HOUR // 2) == (95.0, 110.0)


# ------------------------------------------------------------ full replay


async def test_replay_trades_and_records_everything(replay_settings, market, tmp_path):
    runner, engine, conn = make_runner(replay_settings, market, tmp_path)
    with closing(conn):
        await runner.run()
        count = lambda table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
        assert count("demo_positions") > 0
        assert count("equity_snapshots") > 100
        assert count("regime_history") >= 40  # one mood reading per day
        assert count("scan_results") > 0
        assert engine.state.get("mode") == "replay"
        assert engine.state.get("replay_status") == "finished"
        # Everything is stamped with simulated time, not today's date
        start_ms, end_ms = START.timestamp() * 1000, END.timestamp() * 1000
        for table, column in (("events_log", "ts_ms"), ("demo_orders", "ts_ms"), ("notifications", "created_ms")):
            low, high = conn.execute(f"SELECT MIN({column}), MAX({column}) FROM {table}").fetchone()
            assert start_ms <= low and high <= end_ms, table
        # Messages are recorded but never sent
        assert {r[0] for r in conn.execute("SELECT DISTINCT status FROM notifications")} == {"disabled"}
        # The books balance: closed results + open positions' value explain the account
        closed = conn.execute("SELECT COALESCE(SUM(pnl_usd), 0) FROM demo_positions WHERE status = 'closed'").fetchone()[0]
        open_cost = sum(p.qty * p.entry_price + p.fees_usd + p.funding_usd for p in engine.account.positions())
        assert engine.account.cash == pytest.approx(replay_settings.demo.starting_balance_usdc + closed - open_cost)


async def test_stop_hit_mid_candle_fills_at_the_stop(replay_settings, market, tmp_path):
    runner, engine, conn = make_runner(replay_settings, market, tmp_path)
    with closing(conn):
        from scout.signals import Action, Signal

        engine.prices.update({"AAA": 100.0}, engine.clock())
        signal = Signal("AAA", Action.ENTER_LONG, 0, 0, "4h", 100.0, 96.0, "RISK_ON", "Buying AAA: test.", qty=1.0)
        await engine.open_from_signal(signal)
        candle = pd.Series({"open": 100.0, "high": 101.0, "low": 90.0, "close": 99.0})
        t0 = engine.clock()
        await runner._stops({"AAA": candle_path(candle, t0, 4 * HOUR)}, t0, t0 + 4 * HOUR, t0)
        row = conn.execute("SELECT exit_price, close_reason FROM demo_positions").fetchone()
        assert row["exit_price"] == pytest.approx(96.0 * (1 - replay_settings.risk.slippage_pct / 100))
        assert "stop loss at $96.00 was hit" in row["close_reason"]


async def test_gap_below_the_stop_fills_at_the_open(replay_settings, market, tmp_path):
    runner, engine, conn = make_runner(replay_settings, market, tmp_path)
    with closing(conn):
        from scout.signals import Action, Signal

        engine.prices.update({"AAA": 100.0}, engine.clock())
        await engine.open_from_signal(Signal("AAA", Action.ENTER_LONG, 0, 0, "4h", 100.0, 96.0, "RISK_ON", "x", qty=1.0))
        candle = pd.Series({"open": 92.0, "high": 93.0, "low": 90.0, "close": 91.0})
        t0 = engine.clock()
        await runner._stops({"AAA": candle_path(candle, t0, 4 * HOUR)}, t0, t0 + HOUR, t0)
        row = conn.execute("SELECT exit_price, close_reason FROM demo_positions").fetchone()
        assert row["exit_price"] == pytest.approx(92.0 * (1 - replay_settings.risk.slippage_pct / 100))
        assert "gapped past it" in row["close_reason"]


async def test_pause_and_step_one_candle(replay_settings, market, tmp_path):
    control = ReplayControl(tmp_path / "control.json")
    control.write(Control(paused=True, speed=None, step=0))
    runner, engine, conn = make_runner(replay_settings, market, tmp_path, control,
                                       end=START + pd.Timedelta(days=2))
    first_close = int(START.timestamp() * 1000) + 4 * HOUR

    async def settle(rounds=300):
        for _ in range(rounds):
            await asyncio.sleep(0)

    with closing(conn):
        task = asyncio.create_task(runner.run())
        await settle()
        assert engine.state.get("sim_now_ms") is None  # paused: nothing happened
        assert engine.state.get("replay_paused") == "1"
        control.write(Control(paused=True, speed=None, step=1))
        await settle(2000)
        assert engine.state.get_float("sim_now_ms") == first_close  # exactly one 4h candle
        await settle(500)
        assert engine.state.get_float("sim_now_ms") == first_close  # and paused again
        control.write(Control(paused=False, speed=None, step=1))
        await asyncio.wait_for(task, timeout=30)
        assert engine.state.get("replay_status") == "finished"
