"""Demo trading engine tests: fake money, fake clock, prices fed in by hand."""

import asyncio
import re
from pathlib import Path

import pandas as pd
import pytest

from scout.db import open_db
from scout.demo import DemoEngine, DemoExecutor, DemoRunner, next_cycle_ms, single_instance
from scout.risk import BotState, Order
from scout.signals import Action, Signal, save_signals

pytestmark = pytest.mark.anyio

T0 = int(pd.Timestamp("2026-06-10 02:00", tz="UTC").timestamp() * 1000)  # 12:00 in Sydney (AEST, UTC+10)


class Clock:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms

    def advance(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def engine(settings, tmp_path, clock):
    conn = open_db(tmp_path / "scout.db")
    eng = DemoEngine(settings, conn, clock=clock)
    eng.prices.update({"ETH": 2000.0, "BTC": 80_000.0}, clock())
    yield eng
    conn.close()


def buy(coin="ETH", price=2000.0, stop=1900.0, qty=0.08) -> Signal:
    return Signal(coin, Action.ENTER_LONG, T0, T0, "4h", price, stop, "RISK_ON", f"Buying {coin}: test.", qty=qty)


def feed(engine, clock, **prices):
    engine.prices.update(prices, clock())


def events(engine, category=None) -> list[str]:
    sql = "SELECT message FROM events_log" + (" WHERE category = ?" if category else "") + " ORDER BY id"
    return [r["message"] for r in engine.conn.execute(sql, (category,) if category else ())]


# ---------------------------------------------------------------- account


def test_account_starts_with_a_thousand_aud(engine, settings):
    assert engine.account.cash == pytest.approx(settings.demo.starting_balance_usdc)
    assert engine.bot_state is BotState.RUNNING
    assert engine.account.positions() == []


async def test_buy_fills_with_slippage_and_fee(engine, settings):
    signal = buy()
    save_signals(engine.conn, [signal])
    assert await engine.open_from_signal(signal) is not None
    [position] = engine.account.positions()
    fill = 2000.0 * (1 + settings.risk.slippage_pct / 100)
    fee = 0.08 * fill * settings.risk.taker_fee_pct / 100
    assert position.entry_price == pytest.approx(fill)
    assert engine.account.cash == pytest.approx(650 - 0.08 * fill - fee)
    order = engine.conn.execute("SELECT * FROM demo_orders").fetchone()
    assert (order["status"], order["side"]) == ("filled", "buy")
    assert engine.conn.execute("SELECT acted FROM signals").fetchone()["acted"] == 1


async def test_stop_loss_closes_and_the_books_balance(engine, clock):
    await engine.open_from_signal(buy())
    feed(engine, clock, ETH=1890.0)
    await engine.check_stops()
    assert engine.account.positions() == []
    row = engine.conn.execute("SELECT * FROM demo_positions").fetchone()
    assert row["status"] == "closed"
    assert "stop loss at $1,900.00 was hit" in row["close_reason"]
    assert row["pnl_usd"] < 0
    assert engine.account.cash == pytest.approx(650 + row["pnl_usd"])  # cash change = trade result


async def test_rejections_are_recorded_with_a_reason(engine, clock):
    clock.advance(120)  # no new prices for 2 minutes: the feed has dropped
    assert await engine.open_from_signal(buy()) is None
    order = engine.conn.execute("SELECT * FROM demo_orders").fetchone()
    assert order["status"] == "rejected"
    assert "120 seconds old" in order["reason"]
    assert any("120 seconds old" in m for m in events(engine, "risk"))


async def test_exits_work_even_when_paused_and_prices_are_stale(engine, clock):
    await engine.open_from_signal(buy())
    engine.set_paused(True)
    [position] = engine.account.positions()
    clock.advance(600)
    assert await engine.close(position, "Selling ETH: test.") is not None
    assert engine.account.positions() == []


async def test_pause_blocks_entries_and_resume_allows_them(engine):
    engine.set_paused(True)
    assert await engine.open_from_signal(buy()) is None
    engine.set_paused(False)
    assert await engine.open_from_signal(buy()) is not None


async def test_move_stop_only_tightens(engine):
    await engine.open_from_signal(buy())
    lower = Signal("ETH", Action.MOVE_STOP, T0, T0, "4h", 2000.0, 1800.0, "RISK_ON", "down")
    higher = Signal("ETH", Action.MOVE_STOP, T0 + 1, T0 + 1, "4h", 2000.0, 1950.0, "RISK_ON", "up")
    await engine.execute_signals([lower])
    assert engine.account.positions()[0].stop_price == 1900.0
    await engine.execute_signals([higher])
    assert engine.account.positions()[0].stop_price == 1950.0


async def test_a_position_cant_be_closed_twice(engine):
    await engine.open_from_signal(buy())
    [position] = engine.account.positions()
    assert await engine.close(position, "first") is not None
    cash = engine.account.cash
    assert await engine.close(position, "second (e.g. from another window)") is None
    assert engine.account.cash == cash


# ------------------------------------------------------ the big two rules


async def test_kill_switch_closes_everything_and_stays_off_until_reset(engine, clock):
    await engine.open_from_signal(buy(qty=0.08))  # ~US$160 in ETH
    await engine.open_from_signal(buy("BTC", 80_000.0, 76_000.0, qty=0.002))  # ~US$160 in BTC
    await engine.housekeeping()
    feed(engine, clock, ETH=900.0, BTC=40_000.0)  # a crash: the account falls ~30%
    await engine.housekeeping()
    assert engine.bot_state is BotState.KILLED
    assert engine.account.positions() == []
    assert "fell" in engine.state.get("killed_reason")
    assert any("KILL SWITCH" in m for m in events(engine, "risk"))
    # Stays off, even much later and after prices recover
    clock.advance(3 * 24 * 3600)
    feed(engine, clock, ETH=2500.0, BTC=90_000.0)
    await engine.housekeeping()
    assert engine.bot_state is BotState.KILLED
    assert await engine.open_from_signal(buy(price=2500.0, stop=2400.0, qty=0.04)) is None
    with pytest.raises(ValueError):
        engine.set_paused(False)  # resume can't undo a kill
    engine.reset_kill()
    assert engine.bot_state is BotState.RUNNING
    assert engine.state.get_float("peak_equity_usd") == pytest.approx(engine.account.equity(engine.prices.prices))
    # US$100 is ~22% of the smaller account after the crash (a bigger order would break the 25% rule)
    assert await engine.open_from_signal(buy(price=2500.0, stop=2400.0, qty=0.04)) is not None


async def test_kill_requested_from_another_window(engine):
    await engine.open_from_signal(buy())
    with engine.conn:
        engine.state.set("kill_requested", "1")
    await engine.housekeeping()
    assert engine.bot_state is BotState.KILLED
    assert engine.account.positions() == []


async def test_daily_loss_limit_stops_new_trades_until_midnight_sydney(engine, clock):
    await engine.housekeeping()  # starts the day (12:00 Sydney)
    await engine.open_from_signal(buy(qty=0.08))
    feed(engine, clock, ETH=1700.0, BTC=80_000.0)  # ETH -15% -> account about -3.7%
    await engine.housekeeping()
    assert engine.state.get("daily_limit_hit") == "1"
    assert engine.bot_state is BotState.RUNNING  # not killed, just done for the day
    assert await engine.open_from_signal(buy("BTC", 80_000.0, 76_000.0, qty=0.002)) is None
    assert "today's loss limit was hit" in events(engine, "risk")[-1]

    clock.ms = int(pd.Timestamp("2026-06-10 13:59", tz="UTC").timestamp() * 1000)  # 23:59 Sydney
    feed(engine, clock, ETH=1700.0, BTC=80_000.0)
    await engine.housekeeping()
    assert engine.state.get("daily_limit_hit") == "1"
    clock.ms = int(pd.Timestamp("2026-06-10 14:01", tz="UTC").timestamp() * 1000)  # 00:01 Sydney, next day
    feed(engine, clock, ETH=1700.0, BTC=80_000.0)
    await engine.housekeeping()
    assert engine.state.get("daily_limit_hit") == "0"
    assert engine.state.get("day") == "2026-06-11"
    assert await engine.open_from_signal(buy("BTC", 80_000.0, 76_000.0, qty=0.0019)) is not None


async def test_funding_is_charged_while_holding(engine, clock):
    await engine.open_from_signal(buy())
    engine.funding_rates = {"ETH": 0.0001}  # 0.01% per hour
    await engine.housekeeping()
    cash = engine.account.cash
    clock.advance(3600)
    feed(engine, clock, ETH=2000.0)
    await engine.housekeeping()
    assert engine.account.cash == pytest.approx(cash - 0.08 * 2000 * 0.0001)
    assert engine.account.positions()[0].funding_usd == pytest.approx(0.016)


async def test_equity_snapshots_every_five_minutes(engine, clock):
    for _ in range(12):  # an hour... in 11 steps of 30 s, then more
        await engine.housekeeping()
        clock.advance(30)
    assert engine.conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 2  # 0:00 and 5:00
    clock.advance(300)
    await engine.housekeeping()
    assert engine.conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 3


async def test_peak_follows_new_highs(engine, clock):
    await engine.open_from_signal(buy())
    await engine.housekeeping()
    feed(engine, clock, ETH=2400.0)
    clock.advance(301)
    feed(engine, clock, ETH=2400.0)
    await engine.housekeeping()
    assert engine.state.get_float("peak_equity_usd") > 650


# ------------------------------------------------------------ executor


async def test_demo_executor_fills():
    executor = DemoExecutor(taker_fee_pct=0.045, slippage_pct=0.05, clock=lambda: 7)
    fill = await executor.execute(Order("ETH", "sell", 2.0, 100.0, reduce_only=True, reason="x"))
    assert fill.price == pytest.approx(99.95)
    assert fill.fee_usd == pytest.approx(2 * 99.95 * 0.00045)


def test_no_exchange_keys_or_signing_code_anywhere():
    source = "\n".join(p.read_text() for p in Path(__file__).parents[1].joinpath("scout").glob("*.py"))
    for forbidden in ("private_key", "eth_account", "sign_l1_action", "signTypedData", "mnemonic", "api_secret"):
        assert not re.search(forbidden, source, re.IGNORECASE), forbidden


# -------------------------------------------------------------- runner


def test_cycles_start_just_after_the_hour():
    assert next_cycle_ms(T0 + 5_000, 60) == T0 + 3_600_000 + 60_000


def test_only_one_demo_loop(tmp_path):
    with single_instance(tmp_path / "demo.lock"):
        with pytest.raises(RuntimeError, match="already running"):
            with single_instance(tmp_path / "demo.lock"):
                pass


async def test_runner_cycles_hourly_and_resyncs_after_sleep(engine, clock):
    cycles = []

    async def cycle(eng):
        cycles.append(eng.clock())

    async def stream():
        while True:
            yield {"ETH": 2000.0, "BTC": 80_000.0}
            await asyncio.sleep(0)

    state = {"jumped": False}

    async def sleep(seconds):
        clock.advance(seconds)
        if not state["jumped"] and clock() > T0 + 30 * 60_000:
            clock.advance(1000)  # the Mac slept for ~17 minutes
            state["jumped"] = True
        await asyncio.sleep(0)

    runner = DemoRunner(engine, cycle, stream, wall_clock=lambda: clock() / 1000, sleep=sleep, echo=lambda _: None)
    await runner.run(stop_after_seconds=2.5 * 3600)
    assert len(cycles) == 4  # at the start (02:00), after waking (~02:47), then 03:01 and 04:01
    assert cycles[0] == T0
    assert any("not running for 17 minutes" in m for m in events(engine, "demo"))
    after_wake = [c for c in cycles if c > T0 + 30 * 60_000]
    assert after_wake[0] < T0 + 50 * 60_000  # re-checked straight after waking, not at the next hour
    assert engine.state.get("last_tick_ms") is not None
    assert engine.state.get_float("loop_stopped_ms") >= engine.state.get_float("last_tick_ms")  # marked as stopped


# --------------------------------------------------------- notifications


class Outbox:
    """A notifier stand-in that just records what would be queued."""

    def __init__(self):
        self.messages = []
        self.enabled = True

    def notify(self, text, category, priority=None):
        self.messages.append((category, priority, text))


@pytest.fixture
def outbox(engine):
    engine.notifier = Outbox()
    return engine.notifier


async def test_trades_send_messages(engine, clock, outbox):
    signal = buy()
    signal.details["why"] = "ETH broke above its 3.3-day high on 2.1x normal volume"
    await engine.open_from_signal(signal)
    feed(engine, clock, ETH=1890.0)
    await engine.check_stops()
    (cat1, _, opened), (cat2, _, closed) = outbox.messages
    assert cat1 == cat2 == "trade"
    assert opened.startswith("🟢 BOUGHT 0.08 ETH") and "Why: ETH broke above" in opened
    assert closed.startswith("🔻 SOLD ETH: −A$") and "stop loss" in closed


async def test_kill_switch_sends_one_critical_message(engine, clock, outbox):
    from scout.notify import Priority

    await engine.open_from_signal(buy())
    await engine.open_from_signal(buy("BTC", 80_000.0, 76_000.0, qty=0.002))
    outbox.messages.clear()
    await engine.kill("Test.")
    [(category, priority, text)] = outbox.messages
    assert priority is Priority.CRITICAL
    assert text.startswith("🛑 KILL SWITCH: Test.") and "Closed 2 position(s)" in text


async def test_daily_limit_sends_a_message(engine, clock, outbox):
    await engine.housekeeping()
    await engine.open_from_signal(buy(qty=0.08))
    feed(engine, clock, ETH=1700.0, BTC=80_000.0)
    await engine.housekeeping()
    assert any(t.startswith("⚠️ Daily loss limit hit") for _, _, t in outbox.messages)


async def test_mood_change_messages(engine, outbox):
    from types import SimpleNamespace

    from scout.regime import Regime, Volatility

    def mood(regime):
        return SimpleNamespace(regime=Regime(regime), volatility=Volatility.NORMAL, btc_price=60_000.0,
                               slow_ma=70_000.0, breadth_pct=30.0, score=-4)

    engine.on_mood(mood("RISK_ON"))  # first reading: nothing to compare with
    engine.on_mood(mood("RISK_ON"))
    assert outbox.messages == []
    engine.on_mood(mood("RISK_OFF"))
    [(category, _, text)] = outbox.messages
    assert category == "mood" and text.startswith("Market mood changed to RISK_OFF (was RISK_ON)")


async def test_daily_summary_once_at_8pm_sydney(engine, clock, outbox):
    await engine.housekeeping()  # 12:00 Sydney: too early
    assert not [m for m in outbox.messages if m[0] == "summary"]
    clock.ms = int(pd.Timestamp("2026-06-10 10:00", tz="UTC").timestamp() * 1000)  # 20:00 Sydney
    feed(engine, clock, ETH=2000.0, BTC=82_000.0)
    await engine.housekeeping()
    clock.advance(600)
    feed(engine, clock, ETH=2000.0, BTC=82_000.0)
    await engine.housekeeping()
    summaries = [t for c, _, t in outbox.messages if c == "summary"]
    assert len(summaries) == 1
    assert summaries[0].startswith("📊 Update (Wed 10 Jun, 8pm)") and "Balance A$1,000.00" in summaries[0]
    assert "vs holding BTC +2.5%" in summaries[0]


async def test_feed_down_and_back_messages(engine, clock, outbox):
    runner = DemoRunner(engine, None, None, echo=lambda _: None)
    runner._watch_feed(woke=False)  # Scout is running and prices are flowing...
    clock.advance(engine.settings.notify.feed_down_seconds + 5)  # ...then they stop
    runner._watch_feed(woke=False)
    runner._watch_feed(woke=False)  # only once
    feed(engine, clock, ETH=2000.0)
    runner._watch_feed(woke=False)
    texts = [t for _, _, t in outbox.messages]
    assert len(texts) == 2
    assert texts[0].startswith("⚠️ Live prices have stopped for 2+ minutes")
    assert texts[1].startswith("✅ Live prices are back")


async def test_sleep_is_not_reported_as_a_feed_problem(engine, clock, outbox):
    runner = DemoRunner(engine, None, None, echo=lambda _: None)
    clock.advance(3600)
    runner._watch_feed(woke=True)
    assert outbox.messages == []


async def test_no_summary_on_the_evening_the_account_opens(settings, tmp_path):
    late = Clock(int(pd.Timestamp("2026-06-10 11:30", tz="UTC").timestamp() * 1000))  # 21:30 Sydney
    conn = open_db(tmp_path / "late.db")
    engine = DemoEngine(settings, conn, clock=late)
    engine.notifier = Outbox()
    engine.prices.update({"BTC": 80_000.0}, late())
    await engine.housekeeping()
    assert not [m for m in engine.notifier.messages if m[0] == "summary"]
    late.ms += 24 * 3_600_000  # the next evening
    engine.prices.update({"BTC": 80_000.0}, late())
    await engine.housekeeping()
    assert [m for m in engine.notifier.messages if m[0] == "summary"]
    conn.close()


async def test_slow_reconnect_after_sleep_is_not_a_false_alarm(engine, clock, outbox):
    runner = DemoRunner(engine, None, None, echo=lambda _: None)
    runner._watch_feed(woke=False)  # running normally
    clock.advance(90 * 60)  # the Mac slept for 90 minutes: the last price is now 90 minutes old
    runner._watch_feed(woke=True)
    clock.advance(60)  # awake for a minute, prices still reconnecting
    runner._watch_feed(woke=False)
    assert outbox.messages == []
    clock.advance(engine.settings.notify.feed_down_seconds)  # still nothing 2+ minutes after waking: real problem
    runner._watch_feed(woke=False)
    [(_, _, text)] = outbox.messages
    assert text.startswith("⚠️ Live prices have stopped for 3+ minutes")
