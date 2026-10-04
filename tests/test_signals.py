"""Signal rules, one test (or more) per rule, on synthetic 4h and daily candles."""

from contextlib import closing

import numpy as np
import pandas as pd
import pytest

from scout.config import SignalSettings
from scout.db import open_db
from scout.indicators import rsi
from scout.regime import Regime, Volatility
from scout.signals import (
    Account,
    Action,
    BreakoutStrategy,
    CoinContext,
    Decision,
    MarketContext,
    OpenPosition,
    Strategy,
    generate_signals,
    load_open_positions,
    save_signals,
)

CFG = SignalSettings()
SHORTS = SignalSettings(allow_shorts=True)
RISK_ON = MarketContext(Regime.RISK_ON, Volatility.NORMAL, 1.0)
NEUTRAL = MarketContext(Regime.NEUTRAL, Volatility.NORMAL, 1.0)
RISK_OFF = MarketContext(Regime.RISK_OFF, Volatility.NORMAL, 1.0)
WILD = MarketContext(Regime.RISK_ON, Volatility.WILD, 0.5)
NOW = pd.Timestamp("2026-06-01 12:00", tz="UTC")
ACCOUNT = Account(equity_usd=650.0, cash_usd=650.0, usd_to_aud=1 / 0.65)  # A$1,000


def candles(closes, *, spread=0.5, volume=None, freq="4h") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    volume = np.full(len(closes), 1e6) if volume is None else np.asarray(volume, dtype=float)
    index = pd.date_range(end=NOW, periods=len(closes), freq=freq)
    return pd.DataFrame(
        {"high": closes + spread, "low": closes - spread, "close": closes, "dollar_volume": volume}, index=index
    )


def choppy(n: int = 59, base: float = 100.0) -> list[float]:
    """Sideways: 100, 101, 100, 101, ... (recent high 101.5 with spread 0.5)."""
    return [base + (i % 2) for i in range(n)]


DAILY_UP = candles(np.linspace(70, 100, 80), freq="1D")
DAILY_DOWN = candles(np.linspace(130, 100, 80), freq="1D")


def breakout(last_close=103.0, volume_ratio=2.0, price=None, daily=DAILY_UP, spread=0.5, name="ALT") -> CoinContext:
    closes = [*choppy(), last_close]
    volume = [1e6] * 59 + [1e6 * volume_ratio]
    return CoinContext(name, candles(closes, spread=spread, volume=volume), daily,
                       last_close if price is None else price)


def breakdown(last_close=97.0, volume_ratio=2.0) -> CoinContext:
    closes = [*choppy(), last_close]
    volume = [1e6] * 59 + [1e6 * volume_ratio]
    return CoinContext("ALT", candles(closes, volume=volume), DAILY_DOWN, last_close)


def entry(coin: CoinContext, market=RISK_ON, cfg=CFG) -> Decision:
    return BreakoutStrategy(cfg).entry(coin, market)


# ------------------------------------------------------------ indicators


def test_rsi_extremes():
    assert rsi(pd.Series(np.arange(1.0, 40.0))).iloc[-1] == pytest.approx(100)
    assert rsi(pd.Series([100.0 + (i % 2) for i in range(60)])).iloc[-1] == pytest.approx(50, abs=5)


# ------------------------------------------------------------ long entry


def test_breakout_with_volume_in_uptrend_buys():
    d = entry(breakout())
    assert d.action is Action.ENTER_LONG
    assert "broke above its 3.3-day high of $101.50" in d.why
    assert "2.0x normal volume" in d.why
    assert d.stop_price == pytest.approx(d.price - 2 * d.metrics["atr"])
    assert d.stop_price < d.price


def test_neutral_mood_still_allows_longs():
    assert entry(breakout(), NEUTRAL).action is Action.ENTER_LONG


def test_risk_off_blocks_longs():
    d = entry(breakout(), RISK_OFF)
    assert d.action is Action.HOLD
    assert "RISK_OFF" in d.why


def test_no_breakout_yet():
    d = entry(breakout(last_close=101.0))
    assert d.action is Action.HOLD
    assert "no breakout yet" in d.why


def test_breakout_on_weak_volume_is_skipped():
    d = entry(breakout(volume_ratio=1.0))
    assert d.action is Action.SKIP
    assert "1.0x normal volume" in d.why


def test_overextended_rsi_is_skipped():
    closes = list(np.linspace(60, 100, 60))  # straight up: RSI near 100
    volume = [1e6] * 59 + [2e6]
    coin = CoinContext("ALT", candles(closes, volume=volume), DAILY_UP, 100.0)
    d = entry(coin)
    assert d.action is Action.SKIP
    assert "RSI" in d.why and "too stretched" in d.why


def test_not_in_uptrend():
    d = entry(breakout(daily=DAILY_DOWN))
    assert d.action is Action.HOLD
    assert "not in an uptrend" in d.why


def test_stop_too_far_away_is_skipped():
    d = entry(breakout(spread=8.0, last_close=112.0))  # huge candles -> huge ATR
    assert d.action is Action.SKIP
    assert "stop would be" in d.why


def test_breakout_that_has_already_faded_is_skipped():
    d = entry(breakout(price=101.0))
    assert d.action is Action.SKIP
    assert "fallen back" in d.why


def test_not_enough_history():
    coin = CoinContext("ALT", candles(choppy(10)), DAILY_UP, 100.0)
    assert entry(coin).action is Action.HOLD


# ------------------------------------------------------------------ shorts


def test_shorts_are_off_by_default():
    assert entry(breakdown(), RISK_OFF).action is Action.HOLD


def test_short_mirror_rules_when_enabled():
    d = entry(breakdown(), RISK_OFF, SHORTS)
    assert d.action is Action.ENTER_SHORT
    assert d.stop_price > d.price
    assert "broke below" in d.why


def test_no_shorts_outside_risk_off():
    assert entry(breakdown(), RISK_ON, SHORTS).action is Action.HOLD
    assert entry(breakdown(), NEUTRAL, SHORTS).action is not Action.ENTER_SHORT


# ------------------------------------------------------------------- exits


def position(stop=97.0, entry_price=100.0, side="long", candles_ago=10) -> OpenPosition:
    opened = NOW - pd.Timedelta(hours=4 * candles_ago)
    return OpenPosition("ALT", side, 1.0, entry_price, stop, int(opened.timestamp() * 1000))


def manage(pos, coin, market=RISK_ON) -> Decision:
    return BreakoutStrategy(CFG).manage(pos, coin, market)


def rising(n=60, start=90.0, end=120.0) -> list[float]:
    return list(np.linspace(start, end, n))


def test_exit_when_stop_hit():
    d = manage(position(stop=97.0), breakout(price=96.5))
    assert d.action is Action.EXIT
    assert "hit the stop loss" in d.why


def test_exit_when_mood_turns_risk_off():
    d = manage(position(), breakout(), RISK_OFF)
    assert d.action is Action.EXIT
    assert "RISK_OFF" in d.why


def test_exit_when_trend_breaks():
    closes = [*rising(59), 108.0]  # sharp drop below the 20-candle average (~115)
    coin = CoinContext("ALT", candles(closes), DAILY_UP, 108.0)
    d = manage(position(stop=100.0), coin)
    assert d.action is Action.EXIT
    assert "trend broke" in d.why


def test_trailing_stop_moves_up():
    coin = CoinContext("ALT", candles(rising()), DAILY_UP, 120.0)
    d = manage(position(stop=97.0), coin)
    assert d.action is Action.MOVE_STOP
    assert 97.0 < d.stop_price < 120.0
    assert d.stop_price == pytest.approx(120.5 - 3 * d.metrics["atr"])  # highest high 120.5


def test_trailing_stop_never_moves_down():
    coin = CoinContext("ALT", candles(rising()), DAILY_UP, 120.0)
    d = manage(position(stop=119.0), coin)
    assert d.action is Action.HOLD


def test_short_exit_rules_mirror():
    coin = CoinContext("ALT", candles(list(np.linspace(120, 90, 60))), DAILY_DOWN, 90.0)
    short = position(stop=125.0, entry_price=110.0, side="short")
    assert manage(short, coin, RISK_OFF).action is Action.MOVE_STOP
    assert manage(short, coin, RISK_ON).action is Action.EXIT
    stopped = CoinContext("ALT", coin.candles, DAILY_DOWN, 126.0)
    assert manage(short, stopped, RISK_OFF).action is Action.EXIT


# ------------------------------------------------------------------ engine


def run(settings, coins, shortlist, positions=(), market=RISK_ON, strategy=None):
    strategy = strategy or BreakoutStrategy(settings.signals)
    return generate_signals(strategy, market, {c.coin: c for c in coins}, shortlist, list(positions),
                            ACCOUNT, settings, ts_ms=0)


def test_entry_signal_has_plain_english_reason_in_aud(settings):
    [signal] = run(settings, [breakout()], ["ALT"])
    assert signal.action is Action.ENTER_LONG
    assert signal.reason.startswith("Buying ")
    assert "market mood is positive (RISK_ON), ALT broke above its 3.3-day high" in signal.reason
    assert "Stop at $" in signal.reason and "% below)" in signal.reason
    assert "of the A$1,000 account" in signal.reason
    assert signal.qty > 0 and signal.risk_usd <= 6.5 + 0.01  # never more than 1% of US$650


def test_wild_volatility_halves_position(settings):
    [normal] = run(settings, [breakout()], ["ALT"])
    [wild] = run(settings, [breakout()], ["ALT"], market=WILD)
    assert wild.qty == pytest.approx(normal.qty / 2, rel=0.01)
    assert "because volatility is wild" in wild.reason


def test_only_shortlisted_coins_can_be_entered(settings):
    assert run(settings, [breakout()], []) == []


def test_max_open_positions(settings):
    held = [OpenPosition(f"H{i}", "long", 1, 100, 90, 0) for i in range(3)]
    signals = run(settings, [breakout()], ["ALT"], positions=held)
    alt = next(s for s in signals if s.coin == "ALT")
    assert alt.action is Action.SKIP
    assert "already holding 3 positions" in alt.reason


def test_exit_frees_a_slot(settings):
    held = [OpenPosition(f"H{i}", "long", 1, 100, 90, 0) for i in range(2)] + [position(stop=99.0)]
    held[2] = OpenPosition("OLD", "long", 1.0, 100.0, 99.0, held[2].opened_ts_ms)
    old = CoinContext("OLD", breakout(price=98.0).candles, DAILY_UP, 98.0)  # below its stop -> exit
    signals = run(settings, [old, breakout()], ["ALT"], positions=held)
    actions = {s.coin: s.action for s in signals}
    assert actions["OLD"] is Action.EXIT
    assert actions["ALT"] is Action.ENTER_LONG


def test_held_coin_is_managed_not_rebought(settings):
    signals = run(settings, [breakout()], ["ALT"], positions=[position()])
    assert [s.action for s in signals] != [Action.ENTER_LONG]
    assert all(s.action is not Action.ENTER_LONG for s in signals)


def test_exit_reason(settings):
    [signal] = run(settings, [breakout(price=96.5)], [], positions=[position(stop=97.0)])
    assert signal.reason.startswith("Selling ALT: price $96.50 hit the stop loss at $97.00")
    assert "(-3.5%)" in signal.reason


def test_move_stop_reason(settings):
    coin = CoinContext("ALT", candles(rising()), DAILY_UP, 120.0)
    [signal] = run(settings, [coin], [], positions=[position(stop=97.0)])
    assert signal.action is Action.MOVE_STOP
    assert signal.reason.startswith("Raising ALT's stop to $")
    assert "locking in part of the gain" in signal.reason


def test_strategies_are_pluggable(settings):
    class AlwaysBuy(Strategy):
        name = "always"

        def entry(self, coin, market):
            return Decision(coin.coin, Action.ENTER_LONG, "testing", coin.price, 1, coin.price * 0.95)

        def manage(self, position, coin, market):
            return Decision(coin.coin, Action.HOLD, "testing", coin.price)

    [signal] = run(settings, [breakout()], ["ALT"], strategy=AlwaysBuy(settings.signals))
    assert signal.action is Action.ENTER_LONG
    assert signal.details["strategy"] == "always"
    assert signal.risk_usd > 0  # the engine still sizes it


# ----------------------------------------------------------------- storage


def test_save_signals_once_per_candle_and_skips_holds(settings, tmp_path):
    signals = run(settings, [breakout(), breakout(last_close=101.0, name="FLAT")], ["ALT", "FLAT"])
    assert {s.action for s in signals} == {Action.ENTER_LONG, Action.HOLD}
    with closing(open_db(tmp_path / "scout.db")) as conn:
        assert save_signals(conn, signals) == 1
        assert save_signals(conn, signals) == 0  # same candle: not stored twice
        conn.commit()
        row = conn.execute("SELECT * FROM signals").fetchone()
    assert row["action"] == "ENTER_LONG"
    assert row["reason"].startswith("Buying ")
    assert row["qty"] > 0 and row["risk_usd"] > 0


def test_load_open_positions(tmp_path):
    with closing(open_db(tmp_path / "scout.db")) as conn:
        conn.execute(
            "INSERT INTO demo_positions (coin, side, status, opened_ts_ms, qty, entry_price, stop_price, open_reason, "
            "app_version) VALUES ('ETH', 'long', 'open', 5, 0.1, 2600, 2500, 'test', 'x')"
        )
        [pos] = load_open_positions(conn)
    assert (pos.coin, pos.side, pos.stop_price) == ("ETH", "long", 2500)
