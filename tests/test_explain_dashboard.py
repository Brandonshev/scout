"""`scout explain` and the dashboard: built on one small, hand-made trading history."""

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from scout import dashboard_data as data
from scout.db import open_db
from scout.demo import DemoEngine
from scout.explain import explain_trade, list_trades
from scout.regime import Regime, Volatility, save_reading
from scout.scanner import CoinScan, save_scan
from scout.signals import Action, Signal, save_signals

T0 = 1_780_000_000_000
HOUR = 3_600_000
USD_PER_AUD = 0.65


class Clock:
    def __init__(self):
        self.ms = T0

    def __call__(self):
        return self.ms


def build_history(settings, path):
    """A mood reading, a scan, a signal, a trade stopped out at a loss, and a winner still open."""
    clock = Clock()
    conn = open_db(path)
    engine = DemoEngine(settings, conn, clock=clock)
    reading = SimpleNamespace(
        ts_ms=T0 - HOUR, regime=Regime.RISK_ON, volatility=Volatility.NORMAL, score=5,
        summary="Bitcoin is above its short- and long-term averages: the market is in an uptrend.",
        details=lambda: {"reasons": ["Trend +4: test"], "btc_price": 80_000.0},
    )
    with conn:
        save_reading(conn, reading)
        save_scan(conn, [CoinScan("ETH", 1e9, [], {}, 4.5, {}, "ETH: volume 2.0x normal, in an uptrend.", rank=1,
                                  breakdown="trend +2×1")], T0 - HOUR)
    entry = Signal("ETH", Action.ENTER_LONG, T0, T0, "4h", 2000.0, 1900.0, "RISK_ON",
                   "Buying 0.08 ETH: ETH broke above its 3.3-day high.", qty=0.08, risk_usd=8.2)
    save_signals(conn, [entry])
    engine.prices.update({"ETH": 2000.0, "BTC": 80_000.0}, clock())
    asyncio.run(engine.housekeeping())  # records the starting BTC price and the first snapshot
    asyncio.run(engine.open_from_signal(entry))
    clock.ms += 4 * HOUR
    move = Signal("ETH", Action.MOVE_STOP, clock(), clock(), "4h", 2050.0, 1950.0, "RISK_ON", "Raising ETH's stop.")
    save_signals(conn, [move])
    engine.prices.update({"ETH": 2050.0, "BTC": 81_000.0}, clock())
    asyncio.run(engine.execute_signals([move]))
    asyncio.run(engine.housekeeping())
    clock.ms += 4 * HOUR
    engine.prices.update({"ETH": 1940.0, "BTC": 80_500.0}, clock())
    asyncio.run(engine.check_stops())  # stopped out at a small loss
    second = Signal("SOL", Action.ENTER_LONG, clock(), clock(), "4h", 100.0, 95.0, "RISK_ON", "Buying SOL: test.",
                    qty=1.0, risk_usd=5.0)
    engine.prices.update({"SOL": 100.0, "BTC": 80_500.0}, clock())
    asyncio.run(engine.open_from_signal(second))
    clock.ms += 10 * 60_000
    engine.prices.update({"SOL": 104.0, "BTC": 81_000.0}, clock())
    asyncio.run(engine.housekeeping())
    conn.close()


@pytest.fixture
def history_db(settings):
    build_history(settings, settings.app.db_path)
    return settings.app.db_path


# ---------------------------------------------------------------- explain


def test_list_trades(history_db):
    with closing(data.open_readonly(history_db)) as conn:
        rows = list_trades(conn)
    assert [(r["coin"], r["status"]) for r in rows] == [("SOL", "open"), ("ETH", "closed")]


def test_explain_walks_through_the_whole_trade(history_db, settings):
    with closing(data.open_readonly(history_db)) as conn:
        text = explain_trade(conn, 1, settings.app.tz, USD_PER_AUD)
    for heading in ("1. The market mood", "2. Why ETH was being watched", "3. The signal", "4. The order",
                    "5. While it was open", "6. How it ended", "7. In plain English"):
        assert heading in text
    assert "RISK_ON, volatility NORMAL" in text
    assert "ranked it #1" in text
    assert "broke above its 3.3-day high" in text
    assert "raised 1 time(s), from $1,900.00 to $1,950.00" in text
    assert "stop loss at $1,950.00 was hit" in text
    assert "that's the plan working" in text  # a small, controlled loss


def test_explain_an_open_trade(history_db, settings):
    with closing(data.open_readonly(history_db)) as conn:
        text = explain_trade(conn, 2, settings.app.tz, USD_PER_AUD)
    assert "still open" in text and "Last price $104.00" in text


def test_explain_unknown_trade(history_db, settings):
    with closing(data.open_readonly(history_db)) as conn, pytest.raises(KeyError):
        explain_trade(conn, 99, settings.app.tz, USD_PER_AUD)


# ---------------------------------------------------------- dashboard data


def test_read_only_connection_refuses_writes(history_db):
    with closing(data.open_readonly(history_db)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM demo_positions")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE bot_state SET value = 'KILLED' WHERE key = 'state'")


def test_dashboard_numbers(history_db, settings):
    tz = str(settings.app.tz)
    with closing(data.open_readonly(history_db)) as conn:
        o = data.overview(conn, USD_PER_AUD)
        assert o["mode"] == "DEMO" and o["state"] == "RUNNING"
        assert o["btc_pct"] == pytest.approx((81_000 / 80_000 - 1) * 100)
        positions = data.open_positions(conn, USD_PER_AUD, tz)
        assert list(positions["coin"]) == ["SOL"]
        assert positions["now"].iloc[0] == 104.0  # the latest price recorded by the engine
        trades = data.trade_history(conn, USD_PER_AUD, tz)
        assert list(trades["coin"]) == ["ETH"] and trades["pnl_aud"].iloc[0] < 0
        curve = data.equity_curve(conn, USD_PER_AUD, tz)
        assert {"Scout", "Just holding BTC"} <= set(curve.columns)
        ts, short = data.shortlist(conn, 10)
        assert list(short["coin"]) == ["ETH"] and short["shortlisted"].iloc[0]
        assert data.latest_mood(conn)["regime"] == "RISK_ON"


def test_glossary_covers_every_term_on_the_page():
    terms = " ".join(term for term, _ in data.GLOSSARY)
    for shown in ("RUNNING", "Balance", "vs holding BTC", "Market mood", "Volatility", "Shortlist", "Score",
                  "Stop loss", "Trailing stop", "Kill switch", "Daily loss limit", "Equity curve", "Replay speed",
                  "Breakout", "RSI", "ATR", "Drawdown", "Messages"):
        assert shown in terms, shown


# ------------------------------------------------------- the page itself


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dashboard_page_renders_and_writes_nothing(history_db, write_config, no_env_file, monkeypatch, settings):
    from streamlit.testing.v1 import AppTest

    config = settings.app.db_path.parent.parent / "config.yaml"  # the test config written by the fixture
    monkeypatch.setenv("SCOUT_CONFIG", str(config))
    monkeypatch.setenv("SCOUT_ENV_FILE", str(no_env_file))
    before = _digest(history_db)
    page = AppTest.from_file("scout/dashboard.py", default_timeout=60).run()
    assert not page.exception
    text = " ".join(m.value for m in page.markdown)
    assert "RUNNING" in text and "DEMO" in text
    assert [t.label for t in page.tabs] == ["📈 Now", "📜 Trade history", "❓ What does this mean?"]
    assert any("Kill switch" in m.value for m in page.markdown)
    assert _digest(history_db) == before  # the database is untouched


def test_replay_controls_only_touch_the_control_file(history_db, no_env_file, monkeypatch, settings, tmp_path):
    from streamlit.testing.v1 import AppTest

    from scout.replay import Control, ReplayControl

    replay_db = settings.app.db_path.parent / "replay.db"
    build_history(settings.model_copy(update={"app": settings.app.model_copy(update={"db_path": replay_db})}),
                  replay_db)
    control = ReplayControl(settings.app.db_path.parent / "replay_control.json")
    control.write(Control(paused=False, speed=20000.0))
    monkeypatch.setenv("SCOUT_CONFIG", str(settings.app.db_path.parent.parent / "config.yaml"))
    monkeypatch.setenv("SCOUT_ENV_FILE", str(no_env_file))
    monkeypatch.setenv("SCOUT_DASHBOARD_SOURCE", "Replay")
    before = _digest(replay_db)
    page = AppTest.from_file("scout/dashboard.py", default_timeout=60).run()
    assert not page.exception
    assert control.read() == Control(paused=False, speed=20000.0)  # loading the page changes nothing
    pause = next(b for b in page.sidebar.button if "Pause" in b.label)
    pause.click().run()
    assert control.read().paused is True
    step = next(b for b in page.sidebar.button if "Step" in b.label)
    step.click().run()
    assert control.read().step == 1
    assert _digest(replay_db) == before
